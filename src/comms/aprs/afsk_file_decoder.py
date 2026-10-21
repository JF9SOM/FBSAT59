"""Decode a whole 1200 baud AFSK I/Q recording much faster than real time (Telemetry tab "Fast").

The 4800 / 9600 baud Fast decode runs the coherent MSK detector on the file
(``coherent_msk.CoherentMskFileDecoder``). 1200 baud Bell 202 AFSK has no such detector in this
app; its proven decoder is Direwolf, which the live SDR path already feeds with the audio of
``AfskAudioDiscriminator`` (the satellite front end: narrow IF, clamped, no de-emphasis). This
worker does the same with a recording:

  1. the file's I/Q goes through the discriminator block by block, giving 48 kHz audio;
  2. the audio is written to the stdin of a Direwolf process of its own (``ADEVICE stdin
     stdout`` reads a raw pipe as fast as it is fed, with no real-time pacing), with its own KISS
     port so it cannot clash with the live session;
  3. the KISS frames Direwolf reports are read back and emitted with their time in the recording.

A frame's time is the amount of audio fed when it came out of Direwolf: a pipe buffer's worth of
audio (under a second) after it was received, so it is good to about a second. The recording's
own start time (from its file name) still makes that a usable reception time.

The signals are those of ``CoherentMskFileDecoder``, so the Telemetry tab handles both alike.
"""

from __future__ import annotations

import contextlib
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QThread, Signal

from comms.aprs.afsk_audio_demod import AfskAudioDiscriminator
from comms.aprs.direwolf import _kiss_decode_frames, find_direwolf

_AUDIO_RATE = 48_000
# Silence after the last audio so a frame that ends the recording is completed.
_TAIL_S = 2.0
# How long to wait for Direwolf's KISS port to come up, and for its last frames after the audio.
_CONNECT_TIMEOUT_S = 8.0
_DRAIN_IDLE_S = 1.5
_DRAIN_MAX_S = 20.0
_PROGRESS_INTERVAL_S = 0.25


# Direwolf accepts a KISSPORT only up to 49151 (an operating-system assigned ephemeral port is
# usually above that and is refused: Direwolf then falls back to its default 8001).
_PORT_RANGE = (20_000, 40_000)


def _free_port() -> int:
    """A TCP port Direwolf can use (within its accepted range) that is free on 127.0.0.1 now."""
    ports = list(range(_PORT_RANGE[0], _PORT_RANGE[1]))
    random.shuffle(ports)
    for port in ports[:200]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise OSError("no free TCP port for Direwolf's KISS interface")


def _write_config(port: int) -> str:
    """A minimal direwolf.conf for 1200 baud on stdin with its own KISS port."""
    conf = (
        "MYCALL N0CALL\n"
        "ADEVICE stdin stdout\n"
        f"ARATE {_AUDIO_RATE}\n"
        "ACHANNELS 1\n"
        "CHANNEL 0\n"
        "MODEM 1200\n"
        "PTT NONE\n"
        f"KISSPORT {port}\n"
    )
    fd, path = tempfile.mkstemp(prefix="direwolf_fast_", suffix=".conf")
    with os.fdopen(fd, "w") as handle:
        handle.write(conf)
    return path


def _to_pcm(audio: np.ndarray) -> bytes:
    """48 kHz float audio as the 16 bit PCM the live path writes to Direwolf."""
    clipped = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    return bytes((clipped * 32767).astype("<i2").tobytes())


class Afsk1200FileDecoder(QThread):
    """Decode a 1200 baud AFSK I/Q recording through Direwolf, faster than real time.

    Usage
    -----
    job = Afsk1200FileDecoder(path)
    job.frame_found.connect(consumer)   # (bytes: AX.25 frame without FCS, float: seconds in)
    job.progress.connect(show)          # (float 0..1, int frames so far)
    job.decoded.connect(done)           # (float: recording seconds, float: elapsed, int frames)
    job.failed.connect(error)           # (str)
    job.start()
    ...
    job.cancel()                        # stops early; decoded is then not emitted
    """

    frame_found: Signal = Signal(bytes, float)
    progress: Signal = Signal(float, int)
    decoded: Signal = Signal(float, float, int)
    failed: Signal = Signal(str)

    def __init__(self, path: Any, parent: Any = None, satellite: bool = True) -> None:
        super().__init__(parent)
        self._path = Path(path)
        self._satellite = satellite
        self._cancel = threading.Event()
        self._proc: subprocess.Popen[bytes] | None = None

    def cancel(self) -> None:
        """Ask the worker to stop at the next block and wait for it."""
        self._cancel.set()
        proc = self._proc
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.terminate()
        self.wait(5000)

    # -- Direwolf -------------------------------------------------------------------- #

    def _command(self, binary: Path, conf_path: str) -> list[str]:
        """The Direwolf command line (a method so a test can substitute a stand-in)."""
        return [str(binary), "-c", conf_path, "-t", "0"]

    def _start_direwolf(self, binary: Path, conf_path: str) -> subprocess.Popen[bytes]:
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        return subprocess.Popen(
            self._command(binary, conf_path),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )

    @staticmethod
    def _drain_stdout(proc: subprocess.Popen[bytes]) -> None:
        """Read (and drop) Direwolf's console output so it never blocks on a full pipe."""
        stdout = proc.stdout
        with contextlib.suppress(Exception):
            while stdout is not None and stdout.read(4096):
                pass

    @staticmethod
    def _connect_kiss(port: int, proc: subprocess.Popen[bytes]) -> socket.socket | None:
        deadline = time.monotonic() + _CONNECT_TIMEOUT_S
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=1.0)
            except OSError:
                time.sleep(0.1)
                continue
            sock.settimeout(0.01)
            return sock
        return None

    @staticmethod
    def _read_kiss(sock: socket.socket, buffer: bytearray) -> list[bytes]:
        """Frames that have arrived on the KISS socket (non-blocking)."""
        try:
            chunk = sock.recv(65536)
        except (TimeoutError, BlockingIOError):
            return []
        except OSError:
            return []
        if chunk:
            buffer.extend(chunk)
        return _kiss_decode_frames(buffer)

    # -- the job --------------------------------------------------------------------- #

    def run(self) -> None:
        from comms.telemetry.iq_wav import WavError, iter_iq_blocks, read_wav_info

        try:
            info = read_wav_info(self._path)
        except (WavError, ValueError) as exc:
            self.failed.emit(str(exc))
            return
        binary = find_direwolf()
        if binary is None:
            self.failed.emit("Direwolf not found. Use Help > Direwolf… to install.")
            return
        try:
            discriminator = AfskAudioDiscriminator(info.sample_rate, satellite=self._satellite)
        except (ImportError, ValueError) as exc:
            self.failed.emit(str(exc))
            return

        try:
            port = _free_port()
        except OSError as exc:
            self.failed.emit(str(exc))
            return
        conf_path = _write_config(port)
        started = last_report = time.monotonic()
        frames = 0
        try:
            try:
                self._proc = self._start_direwolf(binary, conf_path)
            except OSError as exc:
                self.failed.emit(f"Failed to start Direwolf: {exc}")
                return
            proc = self._proc
            threading.Thread(target=self._drain_stdout, args=(proc,), daemon=True).start()
            sock = self._connect_kiss(port, proc)
            if sock is None:
                self.failed.emit("Direwolf did not open its KISS port")
                return
            buffer = bytearray()
            fed = 0  # audio samples written so far
            done = 0  # I/Q samples read so far
            stdin = proc.stdin
            assert stdin is not None

            def emit_frames() -> None:
                nonlocal frames
                for frame in self._read_kiss(sock, buffer):
                    frames += 1
                    self.frame_found.emit(frame, fed / _AUDIO_RATE)

            try:
                for block in iter_iq_blocks(self._path, info):
                    if self._cancel.is_set():
                        return
                    audio = discriminator.process(block)
                    if len(audio):
                        stdin.write(_to_pcm(audio))
                        stdin.flush()
                        fed += len(audio)
                    done += len(block)
                    emit_frames()
                    now = time.monotonic()
                    if now - last_report >= _PROGRESS_INTERVAL_S:
                        last_report = now
                        self.progress.emit(done / max(info.frames, 1), frames)
                stdin.write(_to_pcm(np.zeros(int(_TAIL_S * _AUDIO_RATE), dtype=np.float32)))
                stdin.flush()
                fed += int(_TAIL_S * _AUDIO_RATE)
            except (BrokenPipeError, OSError) as exc:
                if not self._cancel.is_set():
                    self.failed.emit(f"Direwolf stopped: {exc}")
                    return
            with contextlib.suppress(OSError):
                stdin.close()
            # Direwolf finishes the audio it was given and reports the last frames.
            drain_start = last_frame = time.monotonic()
            while not self._cancel.is_set():
                before = frames
                emit_frames()
                now = time.monotonic()
                if frames != before:
                    last_frame = now
                if proc.poll() is not None and now - last_frame > 0.3:
                    break
                if now - last_frame > _DRAIN_IDLE_S or now - drain_start > _DRAIN_MAX_S:
                    break
                time.sleep(0.05)
            with contextlib.suppress(OSError):
                sock.close()
            if self._cancel.is_set():
                return
            self.progress.emit(1.0, frames)
            self.decoded.emit(info.duration_s, time.monotonic() - started, frames)
        finally:
            leftover, self._proc = self._proc, None
            if leftover is not None:
                with contextlib.suppress(Exception):
                    leftover.terminate()
                with contextlib.suppress(Exception):
                    leftover.wait(timeout=3)
                if leftover.poll() is None:
                    with contextlib.suppress(Exception):
                        leftover.kill()
            with contextlib.suppress(OSError):
                os.unlink(conf_path)
