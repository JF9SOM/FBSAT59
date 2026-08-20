"""SatDump built-in FFT/waterfall HTTP polling for the METEOR tab.

SatDump's own ``--fft_enable --http_server`` flags (see satdump.py's
SatDumpProcess) make it serve a small local HTTP API with periodic FFT
spectrum snapshots, independent of the SDR device it already holds
exclusively while running ``live``. Polling this lets the METEOR tab show a
live "is RF actually arriving" waterfall during reception, without needing
a second connection to the SDR (which SatDump would refuse to share -- see
MeteorTab's module docstring).

Plain threading.Thread + callables, not QThread/Signal, following the same
pattern as comms.ft4.rx_capture.Ft4RxCaptureWorker and
core.doppler_worker.DopplerWorker -- this keeps it testable without a Qt
event loop. The Qt-owning caller (MeteorTab) bridges the callbacks into its
own Signals; see MeteorTab._fft_frame_received / _fft_unavailable.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Callable

_POLL_INTERVAL_S = 0.4
# ~6s of continuous failure before giving up and reporting -- SatDump's HTTP
# server can take a moment to come up after the process starts, so a few
# early failures are expected and retried silently rather than reported.
_MAX_CONSECUTIVE_FAILURES = 15


def find_free_port() -> int:
    """Return an available localhost TCP port for SatDump's --http_server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class SatDumpFftPoller:
    """Polls SatDump's own FFT HTTP API (127.0.0.1:port/api) in a background thread.

    Callbacks fire from this class's own worker thread, not the caller's
    thread -- if the caller is a QObject, it must bridge them into its own
    Signals rather than touching widgets directly (see MeteorTab).
    """

    def __init__(
        self,
        port: int,
        on_frame: Callable[[list[float]], None],
        on_unavailable: Callable[[str], None],
        poll_interval_s: float = _POLL_INTERVAL_S,
    ) -> None:
        self._url = f"http://127.0.0.1:{port}/api"
        self._on_frame = on_frame
        self._on_unavailable = on_unavailable
        self._poll_interval_s = poll_interval_s
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the poll loop to stop and wait (briefly) for it to exit."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ------------------------------------------------------------------

    def _run(self) -> None:
        consecutive_failures = 0
        reported_unavailable = False
        while not self._stop_event.is_set():
            try:
                req = urllib.request.Request(self._url)
                # Short timeout: this is a loopback request to a process we
                # just launched, and stop() joins this thread from the GUI
                # thread with a bounded wait -- a slow/hung request here
                # would otherwise stall the UI for that same duration.
                with urllib.request.urlopen(req, timeout=0.5) as resp:
                    payload = json.loads(resp.read())
                values = payload.get("fft_values") if isinstance(payload, dict) else None
                if isinstance(values, list) and values:
                    self._on_frame([float(v) for v in values])
                    consecutive_failures = 0
                    reported_unavailable = False
            except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                consecutive_failures += 1
                if consecutive_failures == _MAX_CONSECUTIVE_FAILURES and not reported_unavailable:
                    reported_unavailable = True
                    self._on_unavailable(
                        "Could not reach SatDump's waterfall API "
                        "(--fft_enable / --http_server). This SatDump "
                        "build may not support it, or it hasn't started yet."
                    )
            self._stop_event.wait(self._poll_interval_s)
