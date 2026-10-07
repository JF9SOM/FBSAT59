"""FT4 tab: TX-time residual Doppler correction via the audio tone.

Some rigs (FT-991, both NET and Direct mode) ignore CAT frequency writes
while transmitting, so RigController.set_vfo_frequencies() deliberately
leaves last_ul_hz frozen at the pre-TX value for the whole ~5.2s burst.
Ft4Tab._build_tx_doppler_offset_fn() compensates by shifting the TX audio
tone continuously through the burst instead -- see docs/hamlib.md
"FT4 送信中ドップラー残差補正".
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

pytest.importorskip("scipy")

from comms.ft4.codec import FT4_TX_DURATION  # noqa: E402
from data.database import SCHEMA_SQL  # noqa: E402 -- must follow importorskip above
from ui.ft4_tab import Ft4Tab  # noqa: E402


def _make_tab(
    qtbot: QtBot, tx_doppler_offsets_fn: object = None, tx_audio_sign_fn: object = None
) -> Ft4Tab:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    tab = Ft4Tab(
        conn,
        MagicMock(),
        tx_doppler_offsets_fn=tx_doppler_offsets_fn,  # type: ignore[arg-type]
        tx_audio_sign_fn=tx_audio_sign_fn,  # type: ignore[arg-type]
    )
    qtbot.addWidget(tab)
    return tab


def test_no_rig_returns_no_correction(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot, tx_doppler_offsets_fn=MagicMock())
    fn, residual = tab._build_tx_doppler_offset_fn(None)
    assert fn is None
    assert residual is None


def test_no_callback_wired_up_returns_no_correction(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot, tx_doppler_offsets_fn=None)
    rig = MagicMock(last_ul_hz=145_900_000.0)
    fn, residual = tab._build_tx_doppler_offset_fn(rig)
    assert fn is None
    assert residual is None


def test_rig_never_written_returns_no_correction(qtbot: QtBot) -> None:
    """last_ul_hz is None until the first write after connect -- no baseline yet."""
    tab = _make_tab(qtbot, tx_doppler_offsets_fn=MagicMock(return_value=[145_900_010.0] * 6))
    rig = MagicMock(last_ul_hz=None)
    fn, residual = tab._build_tx_doppler_offset_fn(rig)
    assert fn is None
    assert residual is None


def test_no_satellite_selected_returns_no_correction(qtbot: QtBot) -> None:
    """MainWindow's callback returns None when nothing is selected -- fall back cleanly."""
    tab = _make_tab(qtbot, tx_doppler_offsets_fn=MagicMock(return_value=None))
    rig = MagicMock(last_ul_hz=145_900_000.0)
    fn, residual = tab._build_tx_doppler_offset_fn(rig)
    assert fn is None
    assert residual is None


def test_frozen_rig_yields_growing_residual(qtbot: QtBot) -> None:
    """The core case: FT-991 froze last_ul_hz before TX started, but the true
    Doppler-corrected target keeps drifting -- the residual must track that
    drift continuously across the burst, not just apply a single value."""
    last_ul = 145_900_000.0
    # Simulate a steady drift of 40 Hz over the burst (matches the ~6-8 Hz/s
    # measured on RS-44 -- see project history).
    targets = [last_ul + 40.0 * (i / 5.0) for i in range(6)]
    offsets_fn = MagicMock(return_value=targets)
    tab = _make_tab(qtbot, tx_doppler_offsets_fn=offsets_fn)
    rig = MagicMock(last_ul_hz=last_ul)

    fn, residual_at_start = tab._build_tx_doppler_offset_fn(rig)

    offsets_fn.assert_called_once()
    args = offsets_fn.call_args[0]
    assert args[0] == pytest.approx(FT4_TX_DURATION)

    assert fn is not None
    assert residual_at_start == pytest.approx(0.0)
    assert fn(0.0) == pytest.approx(0.0)
    assert fn(FT4_TX_DURATION) == pytest.approx(40.0)
    assert fn(FT4_TX_DURATION / 2) == pytest.approx(20.0, abs=1.0)


def test_well_tracked_rig_yields_near_zero_residual(qtbot: QtBot) -> None:
    """Icom/FTX-1F: last_ul_hz is itself kept live by the ordinary Doppler
    cycle, so the computed residual should be small -- a harmless no-op."""
    last_ul = 435_600_000.0
    targets = [last_ul + 0.3 * i for i in range(6)]  # sub-Hz drift between cycles
    tab = _make_tab(qtbot, tx_doppler_offsets_fn=MagicMock(return_value=targets))
    rig = MagicMock(last_ul_hz=last_ul)

    fn, residual_at_start = tab._build_tx_doppler_offset_fn(rig)

    assert fn is not None
    assert abs(residual_at_start) < 1.0
    assert abs(fn(FT4_TX_DURATION)) < 2.0


def test_lsb_uplink_flips_audio_correction_sign(qtbot: QtBot) -> None:
    """On an LSB uplink RF = dial - audio, so a UL target that has drifted UP
    must pull the audio tone DOWN (the old +residual doubled the drift)."""
    last_ul = 145_900_000.0
    targets = [last_ul + 40.0 * (i / 5.0) for i in range(6)]
    tab = _make_tab(
        qtbot,
        tx_doppler_offsets_fn=MagicMock(return_value=targets),
        tx_audio_sign_fn=lambda: -1.0,
    )
    fn, _ = tab._build_tx_doppler_offset_fn(MagicMock(last_ul_hz=last_ul))
    assert fn is not None
    assert fn(FT4_TX_DURATION) == pytest.approx(-40.0)


def test_usb_uplink_keeps_audio_correction_sign(qtbot: QtBot) -> None:
    last_ul = 145_900_000.0
    targets = [last_ul + 40.0 * (i / 5.0) for i in range(6)]
    tab = _make_tab(
        qtbot,
        tx_doppler_offsets_fn=MagicMock(return_value=targets),
        tx_audio_sign_fn=lambda: 1.0,
    )
    fn, _ = tab._build_tx_doppler_offset_fn(MagicMock(last_ul_hz=last_ul))
    assert fn is not None
    assert fn(FT4_TX_DURATION) == pytest.approx(40.0)


def test_switched_off_sends_fixed_tone(qtbot: QtBot) -> None:
    """The TX Doppler checkbox off -> no correction function (WSJT-X-style fixed tone)."""
    last_ul = 145_900_000.0
    targets = [last_ul + 40.0 * (i / 5.0) for i in range(6)]
    offsets_fn = MagicMock(return_value=targets)
    tab = _make_tab(qtbot, tx_doppler_offsets_fn=offsets_fn, tx_audio_sign_fn=lambda: -1.0)
    assert tab._adc_tx_check.isChecked()  # on by default
    tab._adc_tx_check.setChecked(False)
    fn, residual = tab._build_tx_doppler_offset_fn(MagicMock(last_ul_hz=last_ul))
    assert fn is None
    assert residual is None
    offsets_fn.assert_not_called()
    tab._adc_tx_check.setChecked(True)
    fn, _ = tab._build_tx_doppler_offset_fn(MagicMock(last_ul_hz=last_ul))
    assert fn is not None


def test_switch_state_is_saved(qtbot: QtBot) -> None:
    tab = _make_tab(qtbot)
    tab._adc_tx_check.setChecked(False)
    row = tab._conn.execute("SELECT value FROM app_settings WHERE key='ft4_settings'").fetchone()
    assert '"tx_doppler_audio": false' in row[0]


def test_adc_rx_off_by_default_and_audio_untouched(qtbot: QtBot) -> None:
    """ADC RX is opt-in; with it off the decoder gets the audio as received."""
    tab = _make_tab(qtbot)
    assert not tab._adc_rx_check.isChecked()
    audio = np.zeros(12_000 * 7, dtype=np.float32)
    assert tab._adc_rx_audio(audio) is audio


def test_adc_tx_holds_the_uplink_only_while_on(qtbot: QtBot) -> None:
    """ADC TX holds the rig's uplink for a transmission and releases it afterwards."""
    tab = _make_tab(qtbot)
    rig = MagicMock()
    tab._ul_hold_rig = rig
    tab._release_ul_hold()
    rig.set_hold_ul.assert_called_once_with(False)
    assert tab._ul_hold_rig is None
    tab._release_ul_hold()  # idempotent
    rig.set_hold_ul.assert_called_once_with(False)


def test_adc_rx_restored_ticked_starts_once_a_rig_connects(qtbot: QtBot) -> None:
    """Saved ADC RX = on: the first tick with a connected FT-991 turns slot sync on."""
    tab = _make_tab(qtbot)
    rig = MagicMock()
    rig.supports_slot_sync = True
    rig.slot_sync = False
    rig.is_connected = True
    rig.cat_blocked = True  # stop the tick right after switching the mode on
    tab._adc_rx = True  # as loaded from ft4_settings, without the checkbox toggled
    tab._slot_sync_rig = lambda: rig  # type: ignore[method-assign]
    assert tab._slot_sync_timer.isActive()
    tab._slot_sync_tick()
    rig.set_slot_sync.assert_called_once_with(True)
