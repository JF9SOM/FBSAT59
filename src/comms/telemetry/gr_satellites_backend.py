"""gr-satellites subprocess backend for the Telemetry tab.

Launches gr_satellites as a subprocess, forwards IQ samples from the SDR
pipeline via UDP, and parses decoded telemetry text from stdout.

Environment note:
  A *system* gr-satellites install (e.g. apt) requires NumPy 1.x, while the
  FBSAT59 venv has NumPy 2.x, so a PYTHONPATH pointing at the system
  site-packages is added when launching that variant. The *bundled*
  conda-pack environment (see gr_satellites_install.py) is fully
  self-contained and needs no such workaround — see start() below.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Signal

from comms.aprs.direwolf import _kiss_decode_frames
from comms.meteor.fft_waterfall import find_free_port
from comms.telemetry.gr_satellites_install import (
    bundled_satyaml_dir,
    find_gr_satellites_executable,
    resolve_gr_satellites_command,
)

# Path that makes a *system* (apt) gr_satellites find system gnuradio + NumPy 1.x
_GR_PYTHONPATH = "/usr/lib/python3/dist-packages"
_SYSTEM_SATYAML_DIR = Path(_GR_PYTHONPATH) / "satellites" / "satyaml"

# UDP port used to send IQ from the SDR pipeline to gr_satellites
_UDP_PORT = 7356

# How long to wait for gr_satellites' --kiss_server to accept a connection
# after Popen returns (the subprocess needs a moment to bind it).
_KISS_CONNECT_RETRY_INTERVAL_S = 0.2
_KISS_CONNECT_RETRIES = 15  # ~3s total

# Cache of "does this argv_prefix's gr_satellites support --kiss_server?",
# keyed by the resolved command (bundled python+script, or system
# executable) so the ~0.3s --help probe runs at most once per process.
_kiss_server_supported_cache: dict[tuple[str, ...], bool] = {}


def _supports_kiss_server(argv_prefix: list[str], env: dict[str, str]) -> bool:
    """Probe whether this gr_satellites build understands --kiss_server.

    --kiss_server exists since gr-satellites 3.x and --kiss_server_address
    since 4.x+; a system (e.g. apt) install could still be older. --help
    itself exits 1 and prints usage to stderr on this argparse setup, so
    only the presence of the flag text is checked, not the exit code.
    """
    key = tuple(argv_prefix)
    cached = _kiss_server_supported_cache.get(key)
    if cached is not None:
        return cached
    supported = False
    try:
        result = subprocess.run(  # noqa: S603
            [*argv_prefix, "--help"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        supported = "--kiss_server" in (result.stdout + result.stderr)
    except (OSError, subprocess.TimeoutExpired):
        supported = False
    _kiss_server_supported_cache[key] = supported
    return supported


def detect_gr_satellites() -> bool:
    """Return True if gr_satellites (bundled or system) is available."""
    return find_gr_satellites_executable() is not None


def _satyaml_dir() -> Path | None:
    """Return the satyaml definitions directory for whichever install is active."""
    bundled = bundled_satyaml_dir()
    if bundled is not None:
        return bundled
    return _SYSTEM_SATYAML_DIR if _SYSTEM_SATYAML_DIR.exists() else None


def list_gr_satellites_norads() -> set[int]:
    """Return the set of NORAD IDs supported by the installed gr-satellites."""
    satyaml_dir = _satyaml_dir()
    if satyaml_dir is None:
        return set()
    try:
        import yaml
    except ImportError:
        return set()
    norads: set[int] = set()
    for yml in satyaml_dir.glob("*.yml"):
        try:
            with open(yml) as fh:
                data = yaml.safe_load(fh)
            if isinstance(data, dict) and isinstance(data.get("norad"), int):
                norads.add(int(data["norad"]))
        except Exception:
            pass
    return norads


def list_gr_satellites_with_names() -> list[tuple[int, str]]:
    """Return sorted list of (norad, name) for all gr-satellites supported satellites."""
    satyaml_dir = _satyaml_dir()
    if satyaml_dir is None:
        return []
    try:
        import yaml
    except ImportError:
        return []
    result: list[tuple[int, str]] = []
    for yml in satyaml_dir.glob("*.yml"):
        try:
            with open(yml) as fh:
                data = yaml.safe_load(fh)
            if isinstance(data, dict) and isinstance(data.get("norad"), int):
                norad = int(data["norad"])
                name = str(data.get("name", str(norad)))
                result.append((norad, name))
        except Exception:
            pass
    result.sort(key=lambda t: t[1].upper())
    return result


def get_satellite_info(norad: int) -> dict[str, object] | None:
    """Return {'name': str, 'transmitters': list, 'frequencies': list[int]} from the YAML.

    'frequencies' is a sorted list of unique downlink frequencies (Hz) found in the YAML.
    Returns None if the satellite is not found.
    """
    satyaml_dir = _satyaml_dir()
    if satyaml_dir is None:
        return None
    try:
        import yaml
    except ImportError:
        return None
    for yml in satyaml_dir.glob("*.yml"):
        try:
            with open(yml) as fh:
                data = yaml.safe_load(fh)
            if isinstance(data, dict) and data.get("norad") == norad:
                txs = data.get("transmitters", {})
                tx_names = list(txs.keys())
                freqs: list[int] = sorted(
                    {
                        int(v["frequency"])
                        for v in txs.values()
                        if isinstance(v, dict) and v.get("frequency")
                    }
                )
                return {
                    "name": str(data.get("name", "")),
                    "transmitters": tx_names,
                    "frequencies": freqs,
                }
        except Exception:
            pass
    return None


class _UdpIqForwarder:
    """Sends IQ chunks from the SDR pipeline to gr_satellites via UDP."""

    def __init__(self, port: int = _UDP_PORT) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._port = port
        self._active = False

    def start(self) -> None:
        self._active = True

    def stop(self) -> None:
        self._active = False

    def push_samples(self, samples: np.ndarray) -> None:
        if not self._active:
            return
        data = samples.view(np.float32).tobytes()
        chunk = 32768
        for i in range(0, len(data), chunk):
            with contextlib.suppress(OSError):
                self._sock.sendto(data[i : i + chunk], ("127.0.0.1", self._port))

    def close(self) -> None:
        self._active = False
        with contextlib.suppress(OSError):
            self._sock.close()


class _KissFrameReader(threading.Thread):
    """Reads gr_satellites' ``--kiss_server`` TCP stream and emits raw frames.

    A plain ``threading.Thread`` (not QThread) to match this backend's
    existing ``_read_stdout`` reader. Connects with retries since the
    subprocess needs a moment to bind the KISS server after Popen returns;
    any frames gr_satellites decodes before this thread manages to connect
    are lost (the KISS server buffers nothing for late clients) — accepted,
    same "Start-only" scope as the AFSK/Direwolf path (Phase 1).

    Reuses ``comms.aprs.direwolf._kiss_decode_frames()`` for deframing: it
    already extracts only data frames (KISS command nibble 0) and unescapes
    them, which is exactly gr_satellites' payload here too.
    """

    def __init__(
        self,
        port: int,
        on_frame: Callable[[bytes], None],
        address: str = "127.0.0.1",
    ) -> None:
        super().__init__(daemon=True, name="gr-sat-kiss-reader")
        self._port = port
        self._address = address
        self._on_frame = on_frame
        self._sock: socket.socket | None = None
        self._stop_event = threading.Event()

    def run(self) -> None:
        sock: socket.socket | None = None
        for _ in range(_KISS_CONNECT_RETRIES):
            if self._stop_event.is_set():
                return
            try:
                sock = socket.create_connection((self._address, self._port), timeout=1.0)
                break
            except OSError:
                time.sleep(_KISS_CONNECT_RETRY_INTERVAL_S)
        if sock is None:
            return
        self._sock = sock
        buf = bytearray()
        while not self._stop_event.is_set():
            try:
                chunk = sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf.extend(chunk)
            for frame in _kiss_decode_frames(buf):
                self._on_frame(frame)
        with contextlib.suppress(OSError):
            sock.close()

    def close(self) -> None:
        """Signal the thread to stop and unblock a pending recv()."""
        self._stop_event.set()
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                self._sock.close()


class GrSatellitesBackend(QObject):
    """Manages a gr_satellites subprocess and emits decoded telemetry."""

    # Emitted with a formatted multi-line text block per received frame
    telemetry_received = Signal(str)
    status_changed = Signal(str)
    # Emitted with the raw deframed bytes of one KISS data frame, received
    # via --kiss_server (SatNOGS DB upload, Phase 2). Not emitted at all
    # when the resolved gr_satellites build doesn't support --kiss_server —
    # see kiss_supported.
    raw_frame_received = Signal(bytes)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._forwarder: _UdpIqForwarder | None = None
        self._pipeline: object | None = None
        self._kiss_reader: _KissFrameReader | None = None
        self._started_norad: int | None = None
        self._kiss_supported = False

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def started_norad(self) -> int | None:
        """NORAD id of the currently (or most recently) started subprocess.

        The subprocess targets exactly one satellite per run, so a raw
        frame's NORAD attribution is this value rather than something
        resolved per-frame.
        """
        return self._started_norad

    @property
    def kiss_supported(self) -> bool:
        """True if the last start() resolved a gr_satellites build that
        understands --kiss_server (raw_frame_received will actually fire)."""
        return self._kiss_supported

    def start(
        self,
        norad: int,
        samp_rate: int,
        pipeline: object,
    ) -> tuple[bool, str]:
        """Start gr_satellites for *norad* and attach to *pipeline*.

        Returns (ok, error_message).
        """
        if self.is_running:
            self.stop()

        resolved = resolve_gr_satellites_command()
        if resolved is None:
            return False, "gr_satellites not found — install via Help > gr-satellites…"
        argv_prefix, is_bundled = resolved

        env = os.environ.copy()
        if not is_bundled:
            # System (e.g. apt) install: needs the NumPy 1.x PYTHONPATH hack.
            # The bundled conda-pack environment is self-contained and needs
            # no such workaround.
            env["PYTHONPATH"] = _GR_PYTHONPATH + os.pathsep + env.get("PYTHONPATH", "")

        self._kiss_supported = _supports_kiss_server(argv_prefix, env)
        kiss_port: int | None = None
        if self._kiss_supported:
            kiss_port = find_free_port()

        cmd = [
            *argv_prefix,
            str(norad),
            "--udp",
            "--udp_port",
            str(_UDP_PORT),
            "--iq",
            "--samp_rate",
            str(samp_rate),
        ]
        if kiss_port is not None:
            cmd += ["--kiss_server", str(kiss_port), "--kiss_server_address", "127.0.0.1"]

        # On Windows this ultimately runs python.exe (a console-subsystem
        # executable), which launched from this windowed/console-less
        # PyInstaller build would otherwise pop up a visible console window
        # (same issue fixed for satdump.exe in comms/meteor/satdump.py).
        try:
            if sys.platform == "win32":
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    env=env,
                    creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
                )
            else:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    env=env,
                )
        except OSError as exc:
            return False, str(exc)

        self._reader = threading.Thread(target=self._read_stdout, daemon=True, name="gr-sat-reader")
        self._reader.start()

        self._started_norad = norad
        if kiss_port is not None:
            self._kiss_reader = _KissFrameReader(kiss_port, self.raw_frame_received.emit)
            self._kiss_reader.start()

        self._forwarder = _UdpIqForwarder(_UDP_PORT)
        self._forwarder.start()
        self._pipeline = pipeline
        with contextlib.suppress(AttributeError):
            pipeline.subscribe(self._forwarder.push_samples)  # type: ignore[attr-defined]

        status = f"gr-satellites running (NORAD {norad})"
        if not self._kiss_supported:
            status += " — SatNOGS upload unavailable (gr_satellites too old for --kiss_server)"
        self.status_changed.emit(status)
        return True, ""

    def stop(self) -> None:
        """Stop the subprocess and detach from the SDR pipeline.

        Order matters: the KISS socket is closed *before* the subprocess is
        terminated (unblocks _KissFrameReader's recv() promptly rather than
        waiting on the OS to notice the peer died), then the process is
        terminated/waited, and only then are both reader threads joined —
        the same "stop the producer before joining" lesson learned from
        AudioBridge/Direwolf (see docs/communications.md).
        """
        if self._forwarder is not None and self._pipeline is not None:
            with contextlib.suppress(AttributeError):
                self._pipeline.unsubscribe(self._forwarder.push_samples)  # type: ignore[attr-defined]
            self._forwarder.stop()
            self._forwarder.close()
            self._forwarder = None
            self._pipeline = None

        if self._kiss_reader is not None:
            self._kiss_reader.close()

        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

        if self._reader is not None:
            self._reader.join(timeout=3)
            self._reader = None
        if self._kiss_reader is not None:
            self._kiss_reader.join(timeout=3)
            self._kiss_reader = None
        self._started_norad = None

        self.status_changed.emit("gr-satellites stopped")

    # ------------------------------------------------------------------
    # stdout parser
    # ------------------------------------------------------------------

    def _read_stdout(self) -> None:
        """Read gr_satellites stdout and emit one signal per frame block."""
        if self._proc is None or self._proc.stdout is None:
            return
        buf: list[str] = []
        for raw_line in self._proc.stdout:
            line = raw_line.rstrip()
            if not line:
                if buf:
                    self.telemetry_received.emit("\n".join(buf))
                    buf = []
            else:
                buf.append(line)
        if buf:
            self.telemetry_received.emit("\n".join(buf))
