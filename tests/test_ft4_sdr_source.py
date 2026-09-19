"""FT4 tab: receiving from an SDR.

FT4's decoder needs 12 kHz audio in which every tone sits at its true audio
frequency. The tab used to connect the SDR pipeline's audio_ready directly,
which is neither 12 kHz nor frequency-true, so FT4 could not decode from an
SDR at all. It now taps the I/Q through sdr.usb_audio (see that module).
"""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

pytest.importorskip("scipy")

from data.database import SCHEMA_SQL  # noqa: E402 -- must follow importorskip above
from sdr.usb_audio import SdrUsbAudioTap  # noqa: E402
from ui.ft4_tab import Ft4Tab  # noqa: E402


class _FakePipeline:
    """The two things Ft4Tab uses of an SDRPipeline: subscribe/unsubscribe and the device rate."""

    def __init__(self, sample_rate: float = 250_000.0) -> None:
        self._device = MagicMock()
        self._device.sample_rate = sample_rate
        self.subscribers: list[Any] = []

    def subscribe(self, callback: Any) -> None:
        self.subscribers.append(callback)

    def unsubscribe(self, callback: Any) -> None:
        self.subscribers.remove(callback)


def _make_tab(qtbot: QtBot, pipeline: _FakePipeline | None) -> Ft4Tab:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    radio_control = MagicMock()
    radio_control._sdr_control._pipeline = pipeline
    tab = Ft4Tab(conn, radio_control)
    qtbot.addWidget(tab)
    return tab


def test_connecting_subscribes_a_usb_audio_tap_to_the_iq_stream(qtbot: QtBot) -> None:
    pipeline = _FakePipeline()
    tab = _make_tab(qtbot, pipeline)
    tab._disconnect_sdr_audio()  # start from a known state whatever __init__ did
    pipeline.subscribers.clear()

    tab._connect_sdr_audio()

    assert isinstance(tab._sdr_tap, SdrUsbAudioTap)
    assert len(pipeline.subscribers) == 1
    assert tab._sdr_connected
    tab._disconnect_sdr_audio()


def test_disconnecting_unsubscribes_and_stops_the_tap(qtbot: QtBot) -> None:
    pipeline = _FakePipeline()
    tab = _make_tab(qtbot, pipeline)
    tab._disconnect_sdr_audio()
    pipeline.subscribers.clear()
    tab._connect_sdr_audio()
    tap = tab._sdr_tap
    assert tap is not None

    tab._disconnect_sdr_audio()

    assert pipeline.subscribers == []
    assert tab._sdr_tap is None
    assert not tab._sdr_connected
    assert tap._thread is None


def test_iq_pushed_by_the_pipeline_reaches_the_rx_capture_as_12khz_audio(qtbot: QtBot) -> None:
    pipeline = _FakePipeline()
    tab = _make_tab(qtbot, pipeline)
    tab._disconnect_sdr_audio()
    pipeline.subscribers.clear()
    tab._rx_capture.stop()
    # A recording stand-in: the real capture worker swaps its buffer out at
    # every UTC period boundary, which would race with this test.
    received: list[int] = []
    tab._rx_capture = MagicMock()
    tab._rx_capture.push_audio.side_effect = lambda chunk: received.append(len(chunk))
    tab._rx_source = "sdr"
    tab._connect_sdr_audio()
    (push,) = pipeline.subscribers

    # 1 s of a 1000 Hz USB tone, delivered as 16384-sample pipeline blocks
    n = 16_384 * 16
    iq = np.exp(2j * np.pi * 1_000.0 * np.arange(n) / 250_000.0).astype(np.complex64)
    for i in range(0, n, 16_384):
        push(iq[i : i + 16_384])
    expected = n / 250_000.0 * 12_000  # 12 kHz, not 48 kHz

    # The tap's worker thread is still converting when the last block is pushed.
    qtbot.waitUntil(lambda: sum(received) > 0.98 * expected, timeout=5_000)
    tab._disconnect_sdr_audio()

    assert sum(received) == pytest.approx(expected, rel=0.02)


def test_no_pipeline_means_nothing_to_connect(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot, None)
    tab._connect_sdr_audio()
    assert tab._sdr_tap is None
    assert not tab._sdr_connected
