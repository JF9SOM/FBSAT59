"""Terrestrial (non-satellite) APRS presets: pseudo-satellite -2, no Doppler, DATA-FM."""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import MagicMock

import pytest

from comms.aprs.engine import detect_modem_for_transmitter
from comms.mode_detection import is_aprs_transmitter
from core.engine import TERRESTRIAL_ID, TERRESTRIAL_IDS, DopplerCalculator, SatelliteEngine
from data.database import SCHEMA_SQL
from data.transmitter_manager import TransmitterManager
from rig.controller import _FT991_MODE_MAP, _SATNOGS_TO_RIGCTLD_MODE, MODE_MAP, HamlibNetController


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def _terrestrial(db: sqlite3.Connection) -> list[dict]:
    TransmitterManager(db).load_community_transmitters()
    rows = db.execute(
        "SELECT * FROM transmitters WHERE norad_cat_id < 0 ORDER BY downlink_low",
    ).fetchall()
    return [dict(r) for r in rows]


def test_both_frequencies_are_loaded_as_simplex_data_fm(db: sqlite3.Connection) -> None:
    rows = _terrestrial(db)
    assert [(r["downlink_low"], r["uplink_low"], r["baud"]) for r in rows] == [
        (144_640_000, 144_640_000, 9600),
        (144_660_000, 144_660_000, 1200),
    ]
    assert {r["mode"] for r in rows} == {"FM-D"}
    assert all(r["source"] == "community" and r["alive"] == 1 for r in rows)
    # One pseudo-satellite per frequency, so the Quick Panel lists both.
    assert [r["norad_cat_id"] for r in rows] == [-3, -2]
    sats = db.execute("SELECT is_hidden FROM satellites WHERE norad_cat_id < 0").fetchall()
    assert [r["is_hidden"] for r in sats] == [0, 0]


def test_an_old_row_moves_to_the_new_pseudo_satellite(db: sqlite3.Connection) -> None:
    """First release put both under -2; the 9600 row must follow its new ID."""
    db.execute("INSERT INTO satellites (norad_cat_id, name) VALUES (-2, 'Terrestrial (old)')")
    db.execute(
        "INSERT INTO transmitters (uuid, norad_cat_id, description, source)"
        " VALUES ('community-terrestrial-aprs-9600', -2, 'old', 'community')"
    )
    db.commit()
    TransmitterManager(db).load_community_transmitters()
    row = db.execute(
        "SELECT norad_cat_id FROM transmitters WHERE uuid = 'community-terrestrial-aprs-9600'"
    ).fetchone()
    assert row["norad_cat_id"] == -3
    name = db.execute("SELECT name FROM satellites WHERE norad_cat_id = -2").fetchone()["name"]
    assert name == "Terrestrial 1200 (地上系)"


def test_aprs_tab_matches_them_and_picks_the_right_baud(db: sqlite3.Connection) -> None:
    for row in _terrestrial(db):
        assert is_aprs_transmitter(row)
        assert detect_modem_for_transmitter(row) == str(row["baud"])


def test_observation_has_zero_range_rate_so_doppler_is_nil() -> None:
    engine = SatelliteEngine(MagicMock(), 35.0, 139.0, 0.0)
    assert TERRESTRIAL_ID in TERRESTRIAL_IDS
    obs = engine.observe(-3)
    assert obs is not None
    assert obs.range_rate_km_s == 0.0
    corrected, shift = DopplerCalculator.correct_downlink(144_660_000.0, obs.range_rate_km_s)
    assert corrected == 144_660_000.0
    assert shift == 0.0


def test_fm_d_maps_to_data_fm_everywhere() -> None:
    assert _FT991_MODE_MAP["FM-D"] == "A"
    assert _SATNOGS_TO_RIGCTLD_MODE["FM-D"] == "PKTFM"
    assert MODE_MAP["FM-D"] == 4096  # RIG_MODE_PKTFM


def test_quick_panel_treats_only_the_others_sentinel_as_no_selection() -> None:
    """-2/-3 are negative but real entries; only -1 ("Others") must be ignored."""
    from unittest.mock import MagicMock

    from ui.main_window import MainWindow, SatDetailPanel

    for norad in (-2, -3):
        fake = MagicMock()
        fake._filter_combo.currentText.return_value = "All Satellites"
        fake._radio_control._transmitters = []
        MainWindow._on_comms_satellite_requested(fake, "aprs", norad)
        fake._select_satellite_by_norad.assert_called_once_with(norad)
        fake._refresh_radio_control.assert_called_once_with(norad)

    fake = MagicMock()
    MainWindow._on_comms_satellite_requested(fake, "aprs", SatDetailPanel.INPUT_SOURCE_OTHERS)
    fake._select_satellite_by_norad.assert_not_called()


# -- NET controller: simplex (split off) for terrestrial transmitters -------------------


def _net(simplex: bool = True) -> HamlibNetController:
    ctrl = HamlibNetController(ctcss_method="ft991")
    ctrl.set_simplex(simplex)
    return ctrl


def test_simplex_connect_turns_split_off_instead_of_on() -> None:
    ctrl = _net()
    sent: list[str] = []
    ctrl._cmd = lambda c: sent.append(c) or "RPRT 0"  # type: ignore[method-assign]
    ctrl._init_vfo()
    assert "S 0 VFOA" in sent
    assert not any(c.startswith("S 1") for c in sent)


def test_satellite_connect_still_enables_split() -> None:
    ctrl = _net(simplex=False)
    sent: list[str] = []
    ctrl._cmd = lambda c: sent.append(c) or "RPRT 0"  # type: ignore[method-assign]
    ctrl._init_vfo()
    assert "S 1 Main" in sent


def _socket_sent(run: Any) -> list[str]:
    from unittest.mock import patch

    sent: list[str] = []
    sock = MagicMock()
    sock.recv.return_value = b"RPRT 0\n"
    sock.sendall.side_effect = lambda b: sent.append(b.decode().strip())
    with patch("rig.controller.socket.socket", return_value=sock):
        run()
    return sent


def test_simplex_pre_connect_init_and_preset_never_enable_split_or_write_tx() -> None:
    ctrl = _net()
    ctrl.set_transponder_freqs(144_660_000.0, 144_660_000.0)
    sent = _socket_sent(
        lambda: (ctrl._send_split_init_independent(), ctrl._send_freq_preset_independent())
    )
    assert "S 0 VFOA" in sent
    assert not any(c.startswith("S 1") for c in sent)
    assert "F 144660000" in sent
    assert not any(c.startswith("I ") for c in sent)


def test_simplex_mode_is_set_once_on_the_single_vfo() -> None:
    ctrl = HamlibNetController(ctcss_method="hamlib")
    ctrl.set_simplex(True)
    sent = _socket_sent(lambda: ctrl.send_mode_only("FM-D", "FM-D"))
    assert sent == ["M PKTFM 0"]


def test_ft991_simplex_mode_sets_data_fm_and_leaves_the_vfos_unswapped() -> None:
    ctrl = _net()
    sent = _socket_sent(lambda: ctrl.send_mode_only("FM-D", "FM-D"))
    assert sent.count("w MD0A;") >= 1  # DATA-FM
    assert sent.count("w SV;") % 2 == 0  # every VFO swap is undone


def test_simplex_doppler_cycle_writes_only_the_receive_frequency() -> None:
    from rig.controller import RigState

    ctrl = _net()
    ctrl._state = RigState.CONNECTED
    ctrl._sock = MagicMock()
    sent: list[str] = []
    ctrl._cmd_raw = lambda c: sent.append(c) or "RPRT 0"  # type: ignore[method-assign]
    ctrl.set_vfo_frequencies(144_660_000.0, 144_660_000.0)
    assert sent == ["F 144660000"]
