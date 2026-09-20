"""Qt-widget regression test for CwTab's gap-marker anchor stability.

Kept separate from test_cw_decoder.py (which is intentionally Qt/ONNX-free)
since this specifically exercises CwTab._reconcile_decode() end-to-end,
including its confirmed/pending state — qtbot + qtbot.addWidget() per this
project's QWidget testing convention.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import MagicMock

import numpy as np
from PySide6.QtCore import QObject, Signal

from comms.cw.codec import DecodeResult
from ui.cw_tab import CwTab

_EMPTY_ENERGY = np.zeros(0, dtype=np.float32)


def _make_tab(qtbot: Any) -> CwTab:
    conn = sqlite3.connect(":memory:")
    tab = CwTab(conn)
    qtbot.addWidget(tab)
    return tab


class TestGapMarkerAnchorStability:
    """self._last_char_abs_time (the gap-detection anchor) must only ever
    advance to a character that has just been permanently *confirmed*,
    never to one still sitting in the tentative pending tail.

    Regression coverage for a real bug: greedy CTC's per-character timing
    is not perfectly reproducible across independent decode passes of
    overlapping windows, so if the anchor were allowed to track
    still-pending content, a later cycle's fresh (and slightly different)
    timing for that same audio could make an already-shown gap marker
    silently vanish — observed live as two unrelated transmissions
    running together ("...SK" directly followed by "CQ...") with no
    separator at all, even though a newline had correctly appeared for
    that same gap the cycle before.
    """

    def test_anchor_is_untouched_when_nothing_newly_confirms(self, qtbot: Any) -> None:
        tab = _make_tab(qtbot)
        tab._last_char_abs_time = 3.0  # pretend something confirmed earlier

        # 20s window, 5s pending margin -> cutoff_rel=15. Both characters
        # are past it (t > 15), so nothing should newly confirm this cycle.
        # "A"/"B" (not "S"/"K") deliberately avoids apply_prosign_conventions'
        # unrelated SK -> VA rule, which is not what this test is about.
        result = DecodeResult(
            offsets=[("A", 16.0), ("B", 16.5)],
            window_duration=20.0,
            frame_energy=_EMPTY_ENERGY,
        )
        tab._reconcile_decode(result)

        assert tab._confirmed_text == ""
        assert "AB" in tab._pending_text
        assert tab._last_char_abs_time == 3.0  # untouched — not 16.5

    def test_anchor_advances_to_the_last_confirmed_character_only(self, qtbot: Any) -> None:
        tab = _make_tab(qtbot)

        # "DE " confirms (t <= 15); "AB" stays pending (t > 15). "A"/"B"
        # (not "S"/"K") deliberately avoids the unrelated SK -> VA rule.
        result = DecodeResult(
            offsets=[("D", 8.0), ("E", 8.3), (" ", 8.4), ("A", 16.0), ("B", 16.5)],
            window_duration=20.0,
            frame_energy=_EMPTY_ENERGY,
        )
        tab._reconcile_decode(result)

        assert "DE" in tab._confirmed_text
        assert "AB" in tab._pending_text
        assert tab._last_char_abs_time == 8.4  # last *confirmed* char, not 16.5

    def test_fully_silent_window_leaves_anchor_untouched(self, qtbot: Any) -> None:
        tab = _make_tab(qtbot)
        tab._last_char_abs_time = 12.0
        tab._pending_text = "SK"

        # No characters decoded at all (offsets empty) -> the pending tail
        # is folded into confirmed, but the anchor itself must not move.
        result = DecodeResult(offsets=[], window_duration=20.0, frame_energy=_EMPTY_ENERGY)
        tab._reconcile_decode(result)

        assert tab._confirmed_text == "SK"
        assert tab._pending_text == ""
        assert tab._last_char_abs_time == 12.0


class TestDeferredTrailingS:
    """Regression coverage for the "SK" -> "VA" prosign display convention
    when "S" and "K" mature into confirmed_text in *separate* decode
    cycles (as opposed to landing in the same delta, already covered by
    test_cw_decoder.py's apply_prosign_conventions tests)."""

    def test_trailing_s_is_held_back_instead_of_confirmed_bare(self, qtbot: Any) -> None:
        tab = _make_tab(qtbot)

        # "73 S" all sit within the confirmable region (t <= 15 for a 20s
        # window), but the trailing standalone "S" must be deferred.
        result = DecodeResult(
            offsets=[("7", 8.0), ("3", 8.3), (" ", 8.5), ("S", 9.0)],
            window_duration=20.0,
            frame_energy=_EMPTY_ENERGY,
        )
        tab._reconcile_decode(result)

        assert tab._confirmed_text == "73 "
        assert tab._pending_text == "S"

    def test_deferred_s_resolves_to_va_once_k_arrives_next_cycle(self, qtbot: Any) -> None:
        tab = _make_tab(qtbot)
        tab._reconcile_decode(
            DecodeResult(
                offsets=[("7", 8.0), ("3", 8.3), (" ", 8.5), ("S", 9.0)],
                window_duration=20.0,
                frame_energy=_EMPTY_ENERGY,
            )
        )
        tab._samples_dropped_total = int(5.0 * tab._rx_sample_rate)  # window advances 5s

        # Fresh decode of the (now shifted) window re-recognises "S" and
        # finally "K", both within the confirmable region.
        tab._reconcile_decode(
            DecodeResult(
                offsets=[("S", 3.6), ("K", 4.1)],
                window_duration=20.0,
                frame_energy=_EMPTY_ENERGY,
            )
        )

        assert tab._confirmed_text == "73 VA"
        assert "S" not in tab._pending_text


class _FakeSdrDemod(QObject):
    """Stand-in for CwSdrDemod so these tests need neither scipy nor a thread."""

    audio_ready = Signal(object)

    instances: list[_FakeSdrDemod] = []

    def __init__(self, sample_rate: int) -> None:
        super().__init__()
        self.sample_rate = sample_rate
        self.started = False
        self.stopped = False
        _FakeSdrDemod.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def push_samples(self, iq: Any) -> None:  # pragma: no cover - never called here
        pass


def _make_sdr_tab(qtbot: Any, monkeypatch: Any, rate: int = 250_000) -> tuple[CwTab, MagicMock]:
    """A CwTab whose radio control exposes a fake SDR pipeline."""
    _FakeSdrDemod.instances.clear()
    monkeypatch.setattr("ui.cw_tab.CwSdrDemod", _FakeSdrDemod)
    pipeline = MagicMock()
    pipeline._device.sample_rate = float(rate)
    radio_control = MagicMock()
    radio_control._sdr_control._pipeline = pipeline
    tab = CwTab(sqlite3.connect(":memory:"), radio_control=radio_control)
    qtbot.addWidget(tab)
    return tab, pipeline


class TestSdrInputUsesPrivateCwDemodulator:
    """The SDR input must demodulate the raw I/Q itself (independent of SDR
    Control's demodulation mode, which is USB by default and rejects a CW
    carrier below the tuned frequency) and treat the result as 48 kHz PCM
    (it used to be labelled 3200 Hz, the model's own rate)."""

    def test_subscribes_to_raw_iq_and_does_not_use_pipeline_audio(
        self, qtbot: Any, monkeypatch: Any
    ) -> None:
        tab, pipeline = _make_sdr_tab(qtbot, monkeypatch, rate=250_000)

        tab._connect_sdr_audio()

        demod = _FakeSdrDemod.instances[0]
        assert demod.sample_rate == 250_000
        assert demod.started
        pipeline.subscribe.assert_called_once_with(demod.push_samples)
        # The pipeline's mode-dependent audio path must not be involved.
        pipeline.request_audio.assert_not_called()
        pipeline.audio_ready.connect.assert_not_called()
        assert tab._sdr_connected

    def test_decoder_is_told_the_audio_is_48khz(self, qtbot: Any, monkeypatch: Any) -> None:
        tab, _pipeline = _make_sdr_tab(qtbot, monkeypatch)

        tab._connect_sdr_audio()

        assert tab._rx_sample_rate == 48_000

    def test_rolling_buffer_holds_20_real_seconds_at_48khz(
        self, qtbot: Any, monkeypatch: Any
    ) -> None:
        tab, _pipeline = _make_sdr_tab(qtbot, monkeypatch)
        tab._connect_sdr_audio()
        tab._running = True

        demod = _FakeSdrDemod.instances[0]
        chunk = np.zeros(4_800, dtype=np.float32)  # 0.1 s at 48 kHz
        for _ in range(250):  # 25 s of audio
            demod.audio_ready.emit(chunk)

        buffered_s = sum(len(c) for c in tab._rx_buffer) / 48_000
        assert 19.0 <= buffered_s <= 20.0

    def test_disconnect_unsubscribes_and_stops_the_demodulator(
        self, qtbot: Any, monkeypatch: Any
    ) -> None:
        tab, pipeline = _make_sdr_tab(qtbot, monkeypatch)
        tab._connect_sdr_audio()
        demod = _FakeSdrDemod.instances[0]

        tab._disconnect_sdr_audio()

        pipeline.unsubscribe.assert_called_once_with(demod.push_samples)
        assert demod.stopped
        assert tab._sdr_demod is None
        assert tab._sdr_pipeline is None
        assert not tab._sdr_connected

    def test_reconnect_after_pipeline_change_uses_a_fresh_demodulator(
        self, qtbot: Any, monkeypatch: Any
    ) -> None:
        tab, _pipeline = _make_sdr_tab(qtbot, monkeypatch, rate=250_000)
        tab._connect_sdr_audio()
        tab._running = True

        # MainWindow builds a brand-new SDRPipeline (possibly at another
        # sample rate) on every SDR reconnect and then notifies the tab.
        new_pipeline = MagicMock()
        new_pipeline._device.sample_rate = 960_000.0
        tab._radio_control._sdr_control._pipeline = new_pipeline  # type: ignore[union-attr]
        tab.refresh_sdr_pipeline(new_pipeline)

        old, fresh = _FakeSdrDemod.instances
        assert old.stopped
        assert fresh.sample_rate == 960_000
        assert not fresh.stopped
        new_pipeline.subscribe.assert_called_once_with(fresh.push_samples)
