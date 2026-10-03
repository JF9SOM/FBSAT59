"""FT4 tab: Tune button (continuous test tone for TX level / ALC adjustment)."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

pytest.importorskip("scipy")

from comms.ft4.codec import TX_SAMPLE_RATE  # noqa: E402 -- must follow importorskip above
from data.database import SCHEMA_SQL  # noqa: E402
from ui.ft4_tab import _TUNE_MAX_S, Ft4Tab  # noqa: E402


def _make_tab(qtbot: QtBot) -> Ft4Tab:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    tab = Ft4Tab(conn, MagicMock())
    qtbot.addWidget(tab)
    tab._out_device = 0
    tab._tx_rig = lambda: None  # type: ignore[method-assign]
    return tab


def test_tune_on_starts_tone_worker_at_audio_freq_and_off_aborts(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot)
    tab._audio_freq_edit.setText("1200")
    with patch("ui.ft4_tab._TxWorker") as worker_cls:
        tab._tune_btn.setChecked(True)

        assert tab._tuning and tab._tx_in_progress
        args, kwargs = worker_cls.call_args
        tone = args[0]
        assert len(tone) == int(_TUNE_MAX_S * TX_SAMPLE_RATE)
        spectrum = np.abs(np.fft.rfft(tone[TX_SAMPLE_RATE : 2 * TX_SAMPLE_RATE]))
        assert np.argmax(spectrum) == 1200  # 1 s window -> 1 Hz bins
        assert kwargs["timeout_is_normal"] is True
        assert kwargs["watchdog_s"] == _TUNE_MAX_S

        tab._tune_btn.setChecked(False)
        worker_cls.return_value.abort.assert_called_once()


def test_tune_finished_resets_state_and_button(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot)
    with patch("ui.ft4_tab._TxWorker"):
        tab._tune_btn.setChecked(True)
    tab._on_tune_finished()

    assert not tab._tuning
    assert not tab._tx_in_progress
    assert tab._tx_worker is None
    assert not tab._tune_btn.isChecked()


def test_tune_refused_while_transmitting_or_without_sound_card(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot)
    tab._tx_in_progress = True
    with patch("ui.ft4_tab._TxWorker") as worker_cls:
        tab._tune_btn.setChecked(True)
        worker_cls.assert_not_called()
    assert not tab._tune_btn.isChecked()

    tab._tx_in_progress = False
    tab._out_device = None
    with patch("ui.ft4_tab._TxWorker") as worker_cls:
        tab._tune_btn.setChecked(True)
        worker_cls.assert_not_called()
    assert not tab._tune_btn.isChecked()


def test_transmit_now_refused_while_tuning(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot)
    tab._codec = MagicMock(is_available=True)
    tab._tuning = True
    tab._transmit_now()
    tab._codec.encode_audio.assert_not_called()
