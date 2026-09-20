"""Q65 tab: receiving from an SDR.

Q65's decoder (libq65) needs 12 kHz audio in which every tone sits at its true
audio frequency. The tab used to buffer the SDR pipeline's audio_ready
directly -- 48 kHz audio in whatever mode SDR Control was set to -- and cut it
up as if it were 12 kHz, so an SDR could not decode. It now taps the I/Q
through sdr.usb_audio, like the FT4 tab.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

from data.database import SCHEMA_SQL
from i18n import _
from ui.q65_tab import Q65Tab


class _FakePipeline:
    """The things Q65Tab uses of an SDRPipeline: subscribe/unsubscribe and the device rate."""

    def __init__(self, sample_rate: float = 250_000.0) -> None:
        self._device = MagicMock()
        self._device.sample_rate = sample_rate
        self.subscribers: list[Any] = []
        # Anything the old audio_ready-based path would have touched.
        self.audio_ready = MagicMock()
        self.request_audio = MagicMock()

    def subscribe(self, callback: Any) -> None:
        self.subscribers.append(callback)

    def unsubscribe(self, callback: Any) -> None:
        self.subscribers.remove(callback)


class _FakeTap:
    """Stand-in for SdrUsbAudioTap so the wiring tests need no scipy."""

    instances: list[_FakeTap] = []

    def __init__(self, sample_rate: float, on_audio: Any) -> None:
        self.sample_rate = sample_rate
        self.on_audio = on_audio
        self.started = False
        self.stopped = False
        _FakeTap.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def push_samples(self, iq: Any) -> None:  # pragma: no cover - never called here
        pass


def _make_tab(qtbot: QtBot, pipeline: _FakePipeline | None) -> Q65Tab:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    radio_control = MagicMock()
    radio_control._sdr_control._pipeline = pipeline
    tab = Q65Tab(conn, radio_control)
    qtbot.addWidget(tab)
    return tab


def _fresh(tab: Q65Tab, pipeline: _FakePipeline | None) -> None:
    """Start from a known state whatever __init__ already attached."""
    tab._disconnect_sdr_audio()
    if pipeline is not None:
        pipeline.subscribers.clear()


def test_connecting_subscribes_a_tap_to_the_iq_stream_not_to_audio_ready(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sdr.usb_audio.SdrUsbAudioTap", _FakeTap)
    _FakeTap.instances.clear()
    pipeline = _FakePipeline(sample_rate=250_000.0)
    tab = _make_tab(qtbot, pipeline)
    _fresh(tab, pipeline)
    _FakeTap.instances.clear()

    tab._connect_sdr_audio()

    (tap,) = _FakeTap.instances
    assert tap.sample_rate == 250_000.0
    assert tap.started
    assert pipeline.subscribers == [tap.push_samples]
    assert tab._sdr_connected
    # The mode-dependent 48 kHz audio path must not be involved.
    pipeline.audio_ready.connect.assert_not_called()
    pipeline.request_audio.assert_not_called()


def test_disconnecting_unsubscribes_and_stops_the_tap(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sdr.usb_audio.SdrUsbAudioTap", _FakeTap)
    pipeline = _FakePipeline()
    tab = _make_tab(qtbot, pipeline)
    _fresh(tab, pipeline)
    _FakeTap.instances.clear()
    tab._connect_sdr_audio()
    (tap,) = _FakeTap.instances

    tab._disconnect_sdr_audio()

    assert pipeline.subscribers == []
    assert tap.stopped
    assert tab._sdr_tap is None
    assert tab._sdr_pipeline is None
    assert not tab._sdr_connected


def test_audio_from_the_tap_thread_reaches_the_decode_buffer(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sdr.usb_audio.SdrUsbAudioTap", _FakeTap)
    pipeline = _FakePipeline()
    tab = _make_tab(qtbot, pipeline)
    _fresh(tab, pipeline)
    _FakeTap.instances.clear()
    tab._input_combo.setCurrentText(_("SDR"))
    tab._connect_sdr_audio()
    (tap,) = _FakeTap.instances

    tap.on_audio(np.zeros(12_000, dtype=np.float32))  # what the tap's thread calls

    qtbot.waitUntil(lambda: sum(len(c) for c in tab._audio_buffer) == 12_000, timeout=2_000)


def test_audio_is_ignored_unless_the_sdr_input_is_selected(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sdr.usb_audio.SdrUsbAudioTap", _FakeTap)
    pipeline = _FakePipeline()
    tab = _make_tab(qtbot, pipeline)
    _fresh(tab, pipeline)
    _FakeTap.instances.clear()
    tab._input_combo.setCurrentText(_("Rig Soundcard"))
    tab._connect_sdr_audio()
    (tap,) = _FakeTap.instances

    tap.on_audio(np.zeros(12_000, dtype=np.float32))
    qtbot.wait(100)

    assert tab._audio_buffer == []


def test_no_pipeline_means_nothing_to_connect(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot, None)
    tab._connect_sdr_audio()
    assert tab._sdr_tap is None
    assert not tab._sdr_connected


def test_iq_pushed_by_the_pipeline_arrives_as_12khz_audio(qtbot: QtBot) -> None:
    """End to end with the real tap: 1 s of I/Q becomes ~1 s of 12 kHz audio
    (it used to be 48 kHz audio that the tab mistook for 12 kHz)."""
    pytest.importorskip("scipy")
    pipeline = _FakePipeline()
    tab = _make_tab(qtbot, pipeline)
    _fresh(tab, pipeline)
    tab._input_combo.setCurrentText(_("SDR"))
    tab._connect_sdr_audio()
    (push,) = pipeline.subscribers

    n = 16_384 * 16
    iq = np.exp(2j * np.pi * 1_000.0 * np.arange(n) / 250_000.0).astype(np.complex64)
    for i in range(0, n, 16_384):
        push(iq[i : i + 16_384])
    expected = n / 250_000.0 * 12_000  # 12 kHz, not 48 kHz

    qtbot.waitUntil(lambda: sum(len(c) for c in tab._audio_buffer) > 0.98 * expected, timeout=5_000)
    tab._disconnect_sdr_audio()

    assert sum(len(c) for c in tab._audio_buffer) == pytest.approx(expected, rel=0.02)
