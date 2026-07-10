"""Direwolf subprocess manager, KISS TCP client, and audio bridge.

Architecture
------------
DirewolfManager
  - Finds the direwolf binary (user-installed → system PATH → bundled)
  - Writes a temporary direwolf.conf (ADEVICE stdin stdout, PTT NONE,
    KISSPORT 8001, no audio library dependency)
  - Launches direwolf as a subprocess
  - Owns the AudioBridge and KissClient instances

AudioBridge (QThread)
  - Subscribes to the shared 48 kHz RX stream via AudioDeviceManager (so other
    Communications tabs can read the same soundcard input at the same time)
    and writes the PCM to direwolf's stdin
  - Reads direwolf's stdout and plays TX audio via sounddevice OutputStream.
    DirewolfManager.start() claims the output device exclusively through
    AudioDeviceManager for the lifetime of the bridge, since two tabs
    transmitting to the same output device at once would just garble the
    audio on air.

KissClient (QThread)
  - TCP connection to 127.0.0.1:8001
  - Emits ``frame_received(bytes)`` Signal for each decoded KISS frame
  - Provides ``send_frame(bytes)`` to wrap and transmit an AX.25 frame
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal

from comms.audio_device_manager import get_audio_device_manager

_AUDIO_OWNER = "APRS/Direwolf"

# ---------------------------------------------------------------------------
# KISS protocol constants
# ---------------------------------------------------------------------------

_FEND: int = 0xC0
_FESC: int = 0xDB
_TFEND: int = 0xDC
_TFESC: int = 0xDD

_KISS_DATA_FRAME: int = 0x00  # port 0, data frame command


def _kiss_encode(ax25_data: bytes) -> bytes:
    """Wrap raw AX.25 bytes in a KISS data frame."""
    escaped = bytearray()
    for byte in ax25_data:
        if byte == _FEND:
            escaped += bytes([_FESC, _TFEND])
        elif byte == _FESC:
            escaped += bytes([_FESC, _TFESC])
        else:
            escaped.append(byte)
    return bytes([_FEND, _KISS_DATA_FRAME]) + bytes(escaped) + bytes([_FEND])


def _kiss_decode_frames(buf: bytearray) -> list[bytes]:
    """Extract complete KISS frames from *buf* (modified in place)."""
    frames: list[bytes] = []
    while True:
        try:
            start = buf.index(_FEND)
        except ValueError:
            break
        try:
            end = buf.index(_FEND, start + 1)
        except ValueError:
            break
        raw = buf[start + 1 : end]
        del buf[: end + 1]
        if not raw:
            continue
        cmd = raw[0]
        if (cmd & 0x0F) != 0:
            continue  # not a data frame
        # Unescape
        payload = bytearray()
        i = 1
        while i < len(raw):
            if raw[i] == _FESC and i + 1 < len(raw):
                nxt = raw[i + 1]
                payload.append(_FEND if nxt == _TFEND else _FESC)
                i += 2
            else:
                payload.append(raw[i])
                i += 1
        frames.append(bytes(payload))
    return frames


# ---------------------------------------------------------------------------
# Direwolf binary detection
# ---------------------------------------------------------------------------

_APP_NAME = "fbsat59"


def _user_direwolf_dir() -> Path:
    """Return the user-installed direwolf directory for this platform."""
    try:
        from platformdirs import user_data_dir

        return Path(user_data_dir(_APP_NAME)) / "direwolf"
    except ImportError:
        if sys.platform == "win32":
            base = os.environ.get("APPDATA", Path.home())
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path.home() / ".local" / "share"
        return Path(base) / _APP_NAME / "direwolf"


def _bundled_direwolf() -> Path | None:
    """Return the bundled direwolf binary path when running from PyInstaller."""
    if not getattr(sys, "frozen", False):
        return None
    exe_name = "direwolf.exe" if sys.platform == "win32" else "direwolf"
    base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    candidate = base / exe_name
    return candidate if candidate.exists() else None


def find_direwolf() -> Path | None:
    """Locate a direwolf binary using priority: user → system → bundled.

    Returns None when direwolf cannot be found.
    """
    exe = "direwolf.exe" if sys.platform == "win32" else "direwolf"

    # 1. User-installed
    user_path = _user_direwolf_dir() / exe
    if user_path.exists():
        return user_path

    # 2. System PATH
    system = shutil.which("direwolf")
    if system:
        return Path(system)

    # 3. Bundled
    return _bundled_direwolf()


# ---------------------------------------------------------------------------
# KISS TCP client (QThread)
# ---------------------------------------------------------------------------


class KissClient(QThread):
    """TCP KISS client that connects to Direwolf on localhost:8001.

    Signals
    -------
    frame_received(bytes)
        Emitted for each decoded AX.25 frame received from Direwolf.
    connection_lost()
        Emitted when the TCP connection drops unexpectedly.
    """

    frame_received: Signal = Signal(bytes)
    connection_lost: Signal = Signal()

    _PORT = 8001
    _CONNECT_TIMEOUT = 5.0  # seconds to wait for Direwolf to start
    _RECV_TIMEOUT = 1.0

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._stop_event = threading.Event()
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()

    def run(self) -> None:
        """Connect and read KISS frames until stop() is called."""
        deadline = time.monotonic() + self._CONNECT_TIMEOUT
        while not self._stop_event.is_set():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                sock.connect(("127.0.0.1", self._PORT))
                sock.settimeout(self._RECV_TIMEOUT)
                self._sock = sock
                break
            except OSError:
                if time.monotonic() > deadline:
                    return
                time.sleep(0.2)

        buf = bytearray()
        while not self._stop_event.is_set():
            try:
                chunk = self._sock.recv(4096)  # type: ignore[union-attr]
                if not chunk:
                    break
                buf.extend(chunk)
                for frame in _kiss_decode_frames(buf):
                    self.frame_received.emit(frame)
            except TimeoutError:
                continue
            except OSError:
                break

        if self._sock:
            self._sock.close()
            self._sock = None
        if not self._stop_event.is_set():
            self.connection_lost.emit()

    def send_frame(self, ax25_data: bytes) -> None:
        """Send an AX.25 frame wrapped in KISS encoding."""
        with self._send_lock:
            if self._sock is None:
                return
            with contextlib.suppress(OSError):
                self._sock.sendall(_kiss_encode(ax25_data))

    def stop(self) -> None:
        """Signal the thread to stop and wait for it to finish."""
        self._stop_event.set()
        if self._sock:
            with contextlib.suppress(OSError):
                self._sock.shutdown(socket.SHUT_RDWR)
        self.wait(3000)


# ---------------------------------------------------------------------------
# Audio bridge (QThread)
# ---------------------------------------------------------------------------


class AudioBridge(QThread):
    """Bridges an RX audio source ↔ Direwolf stdin/stdout.

    RX source is either:
      - the configured soundcard input device (shared with other
        Communications tabs via AudioDeviceManager), or
      - *sdr_pipeline*, if given: a G3ruhSdrDemod is run against the SDR's
        raw I/Q (used for the SDR-fed 9600 baud G3RUH receive-only path —
        see AprsEngine.start_sdr_direwolf()). Mutually exclusive with the
        soundcard input; *in_device* is ignored when *sdr_pipeline* is set.

    Either way, reads from *proc.stdout* and plays the TX audio through the
    configured output device (None for the SDR-fed receive-only path).
    """

    def __init__(
        self,
        proc: subprocess.Popen[bytes],
        in_device: int | None,
        out_device: int | None,
        sdr_pipeline: Any = None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._proc = proc
        self._in_device = in_device
        self._out_device = out_device
        self._sdr_pipeline = sdr_pipeline
        self._stop_event = threading.Event()

    _SAMPLE_RATE = 48000
    _BLOCK_SIZE = 2048  # samples per read
    _BYTES_PER_SAMPLE = 2  # int16

    def run(self) -> None:
        """Start audio capture and stdin writer."""
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError:
            return

        # RX: audio source → Direwolf stdin
        def _rx_callback(chunk: Any) -> None:
            if self._stop_event.is_set():
                return
            try:
                pcm = (chunk * 32767).astype("int16").tobytes()
                self._proc.stdin.write(pcm)  # type: ignore[union-attr]
                self._proc.stdin.flush()  # type: ignore[union-attr]
            except (OSError, BrokenPipeError):
                self._stop_event.set()

        mgr = get_audio_device_manager()
        sdr_demod: Any = None
        if self._sdr_pipeline is not None:
            from comms.aprs.g3ruh_demod import G3ruhSdrDemod

            try:
                sr = int(self._sdr_pipeline._device.sample_rate)
            except AttributeError:
                sr = 0
            if sr > 0:
                sdr_demod = G3ruhSdrDemod(sample_rate=sr, parent=self)
                sdr_demod.audio_ready.connect(_rx_callback)
                sdr_demod.start()
                self._sdr_pipeline.subscribe(sdr_demod.push_samples)
        else:
            mgr.acquire_input(_AUDIO_OWNER, self._in_device, self._SAMPLE_RATE, _rx_callback)

        try:
            while not self._stop_event.is_set():
                # TX: Direwolf stdout → soundcard output (blocking read)
                chunk = self._proc.stdout.read(  # type: ignore[union-attr]
                    self._BLOCK_SIZE * self._BYTES_PER_SAMPLE
                )
                if not chunk:
                    break
                if self._out_device is not None:
                    pcm = np.frombuffer(chunk, dtype="int16").astype("float32") / 32768.0
                    sd.play(
                        pcm,
                        samplerate=self._SAMPLE_RATE,
                        device=self._out_device,
                        blocking=False,
                    )
                    # Only does real work once per bridge session — pops the
                    # snapshot taken in acquire_output(), so later chunks are
                    # a cheap no-op (see AudioDeviceManager.pin_active_output).
                    mgr.pin_active_output(_AUDIO_OWNER)
        except Exception:  # noqa: BLE001
            pass
        finally:
            if sdr_demod is not None:
                self._sdr_pipeline.unsubscribe(sdr_demod.push_samples)
                sdr_demod.stop()
            else:
                mgr.release_input(_AUDIO_OWNER, self._in_device)

    def stop(self) -> None:
        self._stop_event.set()
        self.wait(3000)


# ---------------------------------------------------------------------------
# Direwolf manager
# ---------------------------------------------------------------------------


class DirewolfManager:
    """Manages the Direwolf subprocess lifecycle.

    Usage
    -----
    mgr = DirewolfManager()
    ok, err = mgr.start(callsign="JF9SOM", ssid=9,
                        in_device=0, out_device=1)
    kiss = mgr.kiss_client   # KissClient (QThread, already started)
    ...
    mgr.stop()
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._conf_path: str | None = None
        self._kiss: KissClient | None = None
        self._audio: AudioBridge | None = None
        self._out_device: int | None = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    @property
    def is_running(self) -> bool:
        """True when the Direwolf process is alive."""
        return self._proc is not None and self._proc.poll() is None

    @property
    def kiss_client(self) -> KissClient | None:
        """The active KissClient, or None when not running."""
        return self._kiss

    def start(
        self,
        callsign: str,
        ssid: int,
        via: str = "ARISS",
        in_device: int | None = None,
        out_device: int | None = None,
        modem: str = "1200",
        sdr_pipeline: Any = None,
    ) -> tuple[bool, str]:
        """Start Direwolf and the audio / KISS threads.

        *modem* is the AX.25 baud rate Direwolf should decode/encode at —
        "1200" (Bell 202 AFSK) or "9600" (G3RUH). Falls back to "1200" for
        any other value.

        *sdr_pipeline*, if given, feeds Direwolf's stdin from a
        G3ruhSdrDemod running against this SDR pipeline's raw I/Q instead
        of the soundcard input device — *in_device* is ignored in that
        case. See AudioBridge.

        Returns (True, "") on success, (False, reason) on failure.
        """
        if self.is_running:
            return True, ""

        binary = find_direwolf()
        if binary is None:
            return False, "Direwolf not found. Use Help > Direwolf… to install."

        mgr = get_audio_device_manager()
        if out_device is not None and not mgr.acquire_output(_AUDIO_OWNER, out_device):
            other = mgr.output_owner(out_device) or "another tab"
            return False, f"Sound card output is in use by {other}"

        conf_path = self._write_config(callsign, ssid, modem)
        try:
            self._proc = subprocess.Popen(
                [str(binary), "-c", conf_path, "-t", "0"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            if out_device is not None:
                mgr.release_output(_AUDIO_OWNER, out_device)
            return False, f"Failed to start Direwolf: {exc}"

        self._out_device = out_device
        self._conf_path = conf_path

        # Audio bridge: soundcard (or SDR) ↔ Direwolf stdin/stdout
        self._audio = AudioBridge(self._proc, in_device, out_device, sdr_pipeline=sdr_pipeline)
        self._audio.start()

        # KISS client: connect after a brief delay for Direwolf to init
        time.sleep(0.3)
        self._kiss = KissClient()
        self._kiss.start()

        return True, ""

    def stop(self) -> None:
        """Gracefully stop all threads and terminate Direwolf."""
        if self._kiss:
            self._kiss.stop()
            self._kiss = None

        if self._audio:
            self._audio.stop()
            self._audio = None

        if self._out_device is not None:
            get_audio_device_manager().release_output(_AUDIO_OWNER, self._out_device)
            self._out_device = None

        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                with contextlib.suppress(Exception):
                    self._proc.kill()
            self._proc = None

        if self._conf_path and os.path.exists(self._conf_path):
            with contextlib.suppress(OSError):
                os.unlink(self._conf_path)
            self._conf_path = None

    # ------------------------------------------------------------------ #
    # Config file generation
    # ------------------------------------------------------------------ #

    def _write_config(self, callsign: str, ssid: int, modem: str = "1200") -> str:
        """Write a minimal direwolf.conf to a temp file and return its path."""
        station = f"{callsign}-{ssid}" if ssid else callsign
        modem_baud = modem if modem in ("1200", "9600") else "1200"
        conf = (
            f"MYCALL {station}\n"
            "ADEVICE stdin stdout\n"
            "ACHANNELS 1\n"
            "CHANNEL 0\n"
            f"MODEM {modem_baud}\n"
            "PTT NONE\n"
            f"KISSPORT 8001\n"
        )
        fd, path = tempfile.mkstemp(prefix="direwolf_", suffix=".conf")
        with os.fdopen(fd, "w") as f:
            f.write(conf)
        return path
