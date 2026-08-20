"""Unit tests for comms/meteor/fft_waterfall.py — the Qt-independent,
thread-based poller for SatDump's --fft_enable/--http_server API.

No Qt event loop, real SatDump process, or real SDR is involved: a plain
stdlib HTTP server stands in for SatDump's own --http_server endpoint, and
on_frame/on_unavailable callbacks are exercised directly against a real
background thread (same style as test_ft4_rx_capture.py).
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
import time

import pytest

import comms.meteor.fft_waterfall as fft_waterfall
from comms.meteor.fft_waterfall import SatDumpFftPoller, find_free_port


class _FakeSatDumpHandler(http.server.BaseHTTPRequestHandler):
    """Serves a canned fft_values payload at /api, like SatDump's own server."""

    fft_values: list[float] = [1.0, 2.0, 3.0, 4.0]

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps({"fft_values": self.fft_values}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: N802
        pass  # keep test output quiet


class _FakeSatDumpServer:
    """Minimal context-managed HTTPServer stand-in for SatDump's --http_server."""

    def __init__(self, port: int) -> None:
        self._server = http.server.HTTPServer(("127.0.0.1", port), _FakeSatDumpHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _FakeSatDumpServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._thread.join(timeout=2.0)
        self._server.server_close()


class TestFindFreePort:
    def test_returns_a_bindable_port(self) -> None:
        port = find_free_port()
        # The port should be free for someone else to bind immediately after.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))


class TestSatDumpFftPoller:
    def test_receives_frames_from_server(self) -> None:
        port = find_free_port()
        received: list[list[float]] = []
        got_frame = threading.Event()

        def on_frame(values: list[float]) -> None:
            received.append(values)
            got_frame.set()

        def on_unavailable(_msg: str) -> None:
            pytest.fail("on_unavailable should not fire while the server is up")

        with _FakeSatDumpServer(port):
            poller = SatDumpFftPoller(port, on_frame, on_unavailable, poll_interval_s=0.05)
            poller.start()
            try:
                assert got_frame.wait(timeout=3.0), "on_frame never fired"
            finally:
                poller.stop()

        assert received[0] == _FakeSatDumpHandler.fft_values

    def test_reports_unavailable_when_nothing_listening(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fft_waterfall, "_MAX_CONSECUTIVE_FAILURES", 3)
        port = find_free_port()  # nothing listens on this port
        got_unavailable = threading.Event()
        messages: list[str] = []

        def on_frame(_values: list[float]) -> None:
            pytest.fail("on_frame should not fire when nothing is listening")

        def on_unavailable(msg: str) -> None:
            messages.append(msg)
            got_unavailable.set()

        poller = SatDumpFftPoller(port, on_frame, on_unavailable, poll_interval_s=0.02)
        poller.start()
        try:
            assert got_unavailable.wait(timeout=3.0), "on_unavailable never fired"
        finally:
            poller.stop()

        assert len(messages) == 1  # reported once, not on every failed poll after that

    def test_stop_is_prompt_and_idempotent(self) -> None:
        port = find_free_port()
        poller = SatDumpFftPoller(port, lambda _v: None, lambda _m: None, poll_interval_s=0.05)
        poller.start()
        time.sleep(0.1)
        start = time.monotonic()
        poller.stop()
        elapsed = time.monotonic() - start
        assert elapsed < 1.0
        poller.stop()  # calling again with no thread running must not raise
