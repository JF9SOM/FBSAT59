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
import threading
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Signal

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


class GrSatellitesBackend(QObject):
    """Manages a gr_satellites subprocess and emits decoded telemetry."""

    # Emitted with a formatted multi-line text block per received frame
    telemetry_received = Signal(str)
    status_changed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._forwarder: _UdpIqForwarder | None = None
        self._pipeline: object | None = None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

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

        try:
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

        self._forwarder = _UdpIqForwarder(_UDP_PORT)
        self._forwarder.start()
        self._pipeline = pipeline
        with contextlib.suppress(AttributeError):
            pipeline.subscribe(self._forwarder.push_samples)  # type: ignore[attr-defined]

        self.status_changed.emit(f"gr-satellites running (NORAD {norad})")
        return True, ""

    def stop(self) -> None:
        """Stop the subprocess and detach from the SDR pipeline."""
        if self._forwarder is not None and self._pipeline is not None:
            with contextlib.suppress(AttributeError):
                self._pipeline.unsubscribe(self._forwarder.push_samples)  # type: ignore[attr-defined]
            self._forwarder.stop()
            self._forwarder.close()
            self._forwarder = None
            self._pipeline = None

        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
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
