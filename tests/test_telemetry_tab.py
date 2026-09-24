"""Unit tests for ui/telemetry_tab.py — satellite combo DB lookups.

Verifies both mode combos list exactly the satellites this app's own DB
has actual data for. The AFSK/Direwolf combo lists AX.25-capable
satellites (mode_detection.is_ax25_telemetry_transmitter(), joined against
satellites.is_hidden) — it no longer merges in the static
telemetry_formats/*.json catalog unconditionally (2026-09-05: that let
satellites with no satellites/transmitters DB row at all, e.g. GOLF-TEE
AO-109, appear as selectable-but-broken "ghost" entries). The
gr-satellites combo got the same DB-presence filter the same day (its
400+ satellite YAML bundle has the identical class of ghost entries — see
_norads_with_live_transmitter()). Runs without needing a real Qt display
(conftest.py forces QT_QPA_PLATFORM offscreen) or a direwolf/gr-satellites
installation.
"""

from __future__ import annotations

import sqlite3
import types
from typing import Any

import pytest
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionViewItem, QWidget
from pytestqt.qtbot import QtBot

import ui.telemetry_tab as telemetry_tab_mod
from comms.telemetry.satnogs_uploader import (
    load_satnogs_upload_settings,
    save_satnogs_upload_settings,
)
from ui.telemetry_tab import TelemetryTab


class _FakeRadioControl(QWidget):
    """A QWidget (not just QObject) so it satisfies TelemetryTab's
    radio_control: QWidget parameter type."""

    rig_connected = Signal()
    rig_disconnected = Signal()
    rig2_connected = Signal()
    rig2_disconnected = Signal()
    transmitter_changed = Signal(object)

    def current_transmitter(self) -> None:
        return None


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    c.execute(
        "CREATE TABLE satellites (norad_cat_id INTEGER PRIMARY KEY, name TEXT, is_hidden INTEGER)"
    )
    c.execute(
        """CREATE TABLE transmitters (
            uuid TEXT PRIMARY KEY, norad_cat_id INTEGER, description TEXT,
            mode TEXT, baud INTEGER, alive INTEGER
        )"""
    )
    return c


@pytest.fixture
def app() -> QApplication:
    existing = QApplication.instance()
    return existing if isinstance(existing, QApplication) else QApplication([])


def _combo_items(tab: TelemetryTab) -> list[tuple[int, str]]:
    combo = tab._combo_afsk_sat
    return [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())]


def test_combo_empty_without_any_matching_transmitter(
    app: QApplication, conn: sqlite3.Connection
) -> None:
    """With no satellites/transmitters rows at all, the combo is empty.

    It now draws solely from this app's own DB, not the static
    telemetry_formats/*.json catalog — fail-closed, not fail-open, since a
    JSON-only "ghost" entry with nothing behind it (no satellite, no
    transmitter) was worse than an empty dropdown before any SATNOGS sync."""
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        assert _combo_items(tab) == []
    finally:
        tab.close()


def test_combo_excludes_hidden_satellite_with_matching_transmitter(
    app: QApplication, conn: sqlite3.Connection
) -> None:
    """A satellite carrying an AX.25-capable transmitter that has since
    decayed and been auto-hidden must drop out of the combo, even though
    its transmitter still matches is_ax25_telemetry_transmitter()."""
    conn.execute(
        "INSERT INTO satellites (norad_cat_id, name, is_hidden) VALUES (68796, 'ARICA-2', 2)"
    )
    conn.execute(
        "INSERT INTO transmitters (uuid, norad_cat_id, description, mode, baud, alive) "
        "VALUES ('u1', 68796, 'Mode U - GMSK4k8 - AX.25', 'GMSK', 4800, 1)"
    )
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        norads = {norad for norad, _name in _combo_items(tab)}
        assert 68796 not in norads
    finally:
        tab.close()


def test_combo_includes_ax25_satellite_from_db(app: QApplication, conn: sqlite3.Connection) -> None:
    """ARICA-2 (68796) has no telemetry_formats/*.json definition, but is a
    4800 baud GMSK satellite explicitly marked AX.25 in its SATNOGS
    description — surfaces in the combo from the DB alone."""
    conn.execute(
        "INSERT INTO satellites (norad_cat_id, name, is_hidden) VALUES (68796, 'ARICA-2', 0)"
    )
    conn.execute(
        "INSERT INTO transmitters (uuid, norad_cat_id, description, mode, baud, alive) "
        "VALUES ('u1', 68796, 'Mode U - GMSK4k8 - AX.25', 'GMSK', 4800, 1)"
    )
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        items = dict(_combo_items(tab))
        assert items.get(68796) == "ARICA-2  (68796)"
    finally:
        tab.close()


def test_combo_excludes_non_ax25_transmitter(app: QApplication, conn: sqlite3.Connection) -> None:
    """A satellite with a transmitter that doesn't match
    is_ax25_telemetry_transmitter() (e.g. a linear SSB transponder) must
    not be pulled into the combo just because it exists in the DB."""
    conn.execute("INSERT INTO satellites (norad_cat_id, name, is_hidden) VALUES (99999, 'X', 0)")
    conn.execute(
        "INSERT INTO transmitters (uuid, norad_cat_id, description, mode, baud, alive) "
        "VALUES ('u1', 99999, 'Linear transponder', 'USB', NULL, 1)"
    )
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        norads = {norad for norad, _name in _combo_items(tab)}
        assert 99999 not in norads
    finally:
        tab.close()


def _gr_combo_items(tab: TelemetryTab) -> list[tuple[int, str]]:
    combo = tab._combo_gr_sat
    return [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())]


def test_gr_combo_excludes_hidden_satellite(app: QApplication, conn: sqlite3.Connection) -> None:
    """gr-satellites' own YAML catalog is independent of our satellites
    table, so a NORAD id it lists (e.g. a decayed CubeSat we've since
    auto-hidden) must still be dropped from the combo — the app's own
    tracking is the authority on whether a satellite is still up there,
    not gr-satellites' static list. Both satellites carry a live
    transmitter here so is_hidden is isolated as the only variable."""
    conn.execute(
        "INSERT INTO satellites (norad_cat_id, name, is_hidden) VALUES (47311, 'Maya-2', 2)"
    )
    conn.execute("INSERT INTO satellites (norad_cat_id, name, is_hidden) VALUES (25544, 'ISS', 0)")
    for norad in (47311, 25544):
        conn.execute(
            "INSERT INTO transmitters (uuid, norad_cat_id, description, mode, baud, alive) "
            f"VALUES ('u{norad}', {norad}, 'TLM', 'AFSK', 1200, 1)"
        )
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        tab._gr_sat_list = [(47311, "Maya-2"), (25544, "ISS")]
        tab._populate_gr_combo()
        norads = {norad for norad, _name in _gr_combo_items(tab)}
        assert 47311 not in norads
        assert 25544 in norads
    finally:
        tab.close()


def test_gr_combo_excludes_satellite_absent_from_db(
    app: QApplication, conn: sqlite3.Connection
) -> None:
    """A satellite gr-satellites knows about but our own DB has never
    seen (no satellites/transmitters row at all — never synced from
    SATNOGS) must be excluded: picking it would be a silent no-op, since
    _on_telemetry_satellite_requested()'s gr-mode branch finds no
    transmitters and returns before updating anything (2026-09-05 —
    previously this case was deliberately left in, "absence isn't
    evidence it decayed", but that reasoning missed that absence also
    means there's nothing to actually receive)."""
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        tab._gr_sat_list = [(99000, "Some Unsynced Sat")]
        tab._populate_gr_combo()
        norads = {norad for norad, _name in _gr_combo_items(tab)}
        assert 99000 not in norads
    finally:
        tab.close()


def test_gr_combo_excludes_satellite_with_no_alive_transmitter(
    app: QApplication, conn: sqlite3.Connection
) -> None:
    """A satellite with a satellites row but zero alive transmitters (all
    dead/decayed, or never actually registered on SATNOGS) must also be
    excluded — same silent no-op as the fully-absent case above, just
    with the satellite list highlight working while the transponder list
    stays empty."""
    conn.execute(
        "INSERT INTO satellites (norad_cat_id, name, is_hidden) VALUES (39090, 'STRAND-1', 0)"
    )
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        tab._gr_sat_list = [(39090, "STRAND-1")]
        tab._populate_gr_combo()
        norads = {norad for norad, _name in _gr_combo_items(tab)}
        assert 39090 not in norads
    finally:
        tab.close()


def test_gr_combo_includes_satellite_with_live_transmitter(
    app: QApplication, conn: sqlite3.Connection
) -> None:
    """A satellite gr-satellites knows about that also has a live
    transmitter in our own DB must be offered — the normal case."""
    conn.execute("INSERT INTO satellites (norad_cat_id, name, is_hidden) VALUES (25544, 'ISS', 0)")
    conn.execute(
        "INSERT INTO transmitters (uuid, norad_cat_id, description, mode, baud, alive) "
        "VALUES ('u1', 25544, 'Mode V APRS', 'AFSK', 1200, 1)"
    )
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        tab._gr_sat_list = [(25544, "ISS")]
        tab._populate_gr_combo()
        norads = {norad for norad, _name in _gr_combo_items(tab)}
        assert 25544 in norads
    finally:
        tab.close()


# ---------------------------------------------------------------------------
# SatNOGS DB upload footer controls
# ---------------------------------------------------------------------------


class _RecordingUploader:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def submit(self, conn, raw, norad, received_at, force=False, on_result=None) -> bool:  # noqa: ANN001
        self.calls.append((conn, raw, norad, received_at))
        return True


def test_satnogs_toggle_persists_to_app_settings(
    app: QApplication, conn: sqlite3.Connection
) -> None:
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        assert tab._btn_satnogs_toggle.text() == "SatNOGS Upload: OFF"
        tab._btn_satnogs_toggle.setChecked(True)
        assert load_satnogs_upload_settings(conn)["enabled"] is True
        assert tab._btn_satnogs_toggle.text() == "SatNOGS Upload: ON"
        tab._btn_satnogs_toggle.setChecked(False)
        assert load_satnogs_upload_settings(conn)["enabled"] is False
    finally:
        tab.close()


def test_satnogs_toggle_reflects_saved_state_on_open(
    app: QApplication, conn: sqlite3.Connection
) -> None:
    save_satnogs_upload_settings(conn, {"enabled": True, "api_key": "k"})
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        assert tab._btn_satnogs_toggle.isChecked() is True
        assert tab._btn_satnogs_toggle.text() == "SatNOGS Upload: ON"
    finally:
        tab.close()


def test_satnogs_controls_stay_visible_in_gr_satellites_mode(
    app: QApplication, conn: sqlite3.Connection
) -> None:
    """Phase 2 added the gr-satellites --kiss_server raw-frame route (see
    _on_gr_raw_frame()), so the SatNOGS-upload cluster covers both paths now
    and must stay visible when Mode is switched to gr-satellites (Phase 1
    used to hide it here; that behaviour was reverted)."""
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        widgets = (
            tab._btn_satnogs_toggle,
            tab._btn_satnogs_api,
            tab._btn_satnogs_link,
        )
        assert all(not w.isHidden() for w in widgets)  # AFSK mode by default

        tab._combo_mode.setCurrentText("gr-satellites")
        assert all(not w.isHidden() for w in widgets)

        tab._combo_mode.setCurrentText("Direwolf (AX.25)")
        assert all(not w.isHidden() for w in widgets)
    finally:
        tab.close()


def test_satnogs_link_disabled_without_any_target(
    app: QApplication, conn: sqlite3.Connection
) -> None:
    """With both mode combos empty and nothing selected in the main list,
    the link has nothing to point at and is disabled."""
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        tab._combo_afsk_sat.clear()
        tab._combo_gr_sat.clear()
        tab._selected_norad = None
        tab._update_satnogs_link_enabled()
        assert tab._btn_satnogs_link.isEnabled() is False
        tab.set_satellite(25544, "ISS")
        assert tab._btn_satnogs_link.isEnabled() is True
    finally:
        tab.close()


def test_satnogs_link_emits_open_request_for_active_satellite(
    app: QApplication, conn: sqlite3.Connection
) -> None:
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        got: list[tuple[int, str]] = []
        tab.open_satnogs_requested.connect(lambda n, name: got.append((n, name)))
        tab.set_satellite(43803, "JO-97")  # any norad — link only needs a selected satellite
        assert tab._btn_satnogs_link.isEnabled() is True
        tab._btn_satnogs_link.click()
        assert got == [(43803, "JO-97")]
    finally:
        tab.close()


def test_ax25_frame_forwards_raw_to_satnogs_uploader(
    app: QApplication, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _RecordingUploader()
    monkeypatch.setattr(telemetry_tab_mod, "get_satnogs_uploader", lambda: rec)
    monkeypatch.setattr(
        telemetry_tab_mod,
        "decode_ax25",
        lambda raw: types.SimpleNamespace(src="JY1SAT", payload=b"\x00\x11\x22"),
    )
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        monkeypatch.setattr(tab, "_callsign_to_norad", lambda src: 43803)
        raw = bytes.fromhex("9c86aa8ea662e0a08a82a49886e103f000112233")
        tab._on_ax25_frame(raw)
        assert len(rec.calls) == 1
        c_conn, c_raw, c_norad, _c_ts = rec.calls[0]
        assert c_conn is conn
        assert c_raw == raw  # full AX.25 frame, not just the payload
        assert c_norad == 43803
    finally:
        tab.close()


def test_gr_raw_frame_forwards_to_satnogs_uploader_with_started_norad(
    app: QApplication, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_on_gr_raw_frame() (Phase 2, --kiss_server route) must attribute the
    frame to the subprocess's started_norad — gr_satellites targets exactly
    one satellite per run, so there is no per-frame NORAD to resolve."""
    rec = _RecordingUploader()
    monkeypatch.setattr(telemetry_tab_mod, "get_satnogs_uploader", lambda: rec)
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        monkeypatch.setattr(type(tab._gr_backend), "started_norad", property(lambda self: 25544))
        raw = b"\x9c\x86\xaa\x8e\xa6\x62\xe0\xa0\x8a\x82\xa4\x98\x86\xe1\x03\xf0\x00\x11\x22\x33"
        tab._on_gr_raw_frame(raw)
        assert len(rec.calls) == 1
        c_conn, c_raw, c_norad, _c_ts = rec.calls[0]
        assert c_conn is conn
        assert c_raw == raw
        assert c_norad == 25544
    finally:
        tab.close()


def test_gr_raw_frame_noop_without_started_norad(
    app: QApplication, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before Start (or if the subprocess never actually launched),
    started_norad is None — must not call the uploader at all."""
    rec = _RecordingUploader()
    monkeypatch.setattr(telemetry_tab_mod, "get_satnogs_uploader", lambda: rec)
    tab = TelemetryTab(conn, _FakeRadioControl())
    try:
        assert tab._gr_backend.started_norad is None
        tab._on_gr_raw_frame(b"\x00\x11")
        assert rec.calls == []
    finally:
        tab.close()


def test_gr_combo_lists_provisional_catalog_entry_under_real_id(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    """gr-satellites still files Foresail-1p under provisional 98467 while
    the DB tracks it as 66778. It must be offered under 66778 (so the combo,
    satellite list and Radio Control agree) and remember 98467 as the id to
    launch gr_satellites with."""
    conn.execute(
        "INSERT INTO satellites (norad_cat_id, name, is_hidden) VALUES (66778, 'Foresail-1p', 0)"
    )
    conn.execute(
        "INSERT INTO transmitters (uuid, norad_cat_id, description, mode, baud, alive) "
        "VALUES ('u1', 66778, 'MODE U - GMSK 9k6 TLM Skylink', 'GMSK', 9600, 1)"
    )
    tab = TelemetryTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)
    tab._gr_sat_list = [(98467, "FORESAIL-1P")]
    tab._populate_gr_combo()
    assert _gr_combo_items(tab) == [(66778, "FORESAIL-1P  (66778)")]
    assert tab._gr_catalog_ids == {66778: 98467}


def test_gr_combo_does_not_remap_real_id_entry_with_shared_name(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    """Only provisional catalog ids are matched by name: an unrelated
    spacecraft that merely shares a name must not be wired to this catalog
    entry."""
    conn.execute("INSERT INTO satellites (norad_cat_id, name, is_hidden) VALUES (39197, 'IRIS', 0)")
    conn.execute(
        "INSERT INTO transmitters (uuid, norad_cat_id, description, mode, baud, alive) "
        "VALUES ('u1', 39197, 'TLM', 'FM', 0, 1)"
    )
    tab = TelemetryTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)
    tab._gr_sat_list = [(57315, "IRIS")]
    tab._populate_gr_combo()
    assert _gr_combo_items(tab) == []
    assert tab._gr_catalog_ids == {}


def test_gr_combo_provisional_match_skips_hidden_satellite(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    conn.execute(
        "INSERT INTO satellites (norad_cat_id, name, is_hidden) VALUES (66778, 'Foresail-1p', 2)"
    )
    conn.execute(
        "INSERT INTO transmitters (uuid, norad_cat_id, description, mode, baud, alive) "
        "VALUES ('u1', 66778, 'TLM', 'GMSK', 9600, 1)"
    )
    tab = TelemetryTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)
    tab._gr_sat_list = [(98467, "FORESAIL-1P")]
    tab._populate_gr_combo()
    assert _gr_combo_items(tab) == []


def test_start_gr_satellites_launches_with_catalog_id(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    """Selecting the remapped entry must start gr_satellites with the catalog
    id (98467) while frames stay attributed to the real id (66778)."""
    conn.execute(
        "INSERT INTO satellites (norad_cat_id, name, is_hidden) VALUES (66778, 'Foresail-1p', 0)"
    )
    conn.execute(
        "INSERT INTO transmitters (uuid, norad_cat_id, description, mode, baud, alive) "
        "VALUES ('u1', 66778, 'TLM', 'GMSK', 9600, 1)"
    )
    tab = TelemetryTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)
    tab._gr_sat_list = [(98467, "FORESAIL-1P")]
    tab._populate_gr_combo()
    tab._sdr_pipeline = types.SimpleNamespace(_device=types.SimpleNamespace(sample_rate=250000))
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_start(*args: object, **kwargs: object) -> tuple[bool, str]:
        calls.append((args, kwargs))
        return True, ""

    tab._gr_backend.start = fake_start  # type: ignore[method-assign]
    tab._start_gr_satellites()
    assert len(calls) == 1
    assert calls[0][0][0] == 66778
    assert calls[0][1] == {"catalog_norad": 98467}


def test_gr_combo_provisional_match_survives_hidden_old_provisional_row(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    """After a provisional->real migration the DB keeps the old provisional
    row hidden (is_hidden=2) with no live transmitters. That must not hide the
    live row under the real id (HCT-SAT2: catalog 98470, DB 98470 hidden +
    66671 live)."""
    conn.execute(
        "INSERT INTO satellites (norad_cat_id, name, is_hidden) VALUES (98470, 'HCT-SAT2', 2)"
    )
    conn.execute(
        "INSERT INTO satellites (norad_cat_id, name, is_hidden) VALUES (66671, 'HCT-SAT2', 0)"
    )
    conn.execute(
        "INSERT INTO transmitters (uuid, norad_cat_id, description, mode, baud, alive) "
        "VALUES ('u1', 66671, 'TLM', 'GMSK', 9600, 1)"
    )
    tab = TelemetryTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)
    tab._gr_sat_list = [(98470, "HCT-SAT2")]
    tab._populate_gr_combo()
    assert _gr_combo_items(tab) == [(66671, "HCT-SAT2  (66671)")]
    assert tab._gr_catalog_ids == {66671: 98470}


# ---------------------------------------------------------------------------
# Received Frames table: rejected (dim) rows must stay readable
# ---------------------------------------------------------------------------


def _luminance(c: QColor) -> float:
    def lin(v: int) -> float:
        x = v / 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(c.red()) + 0.7152 * lin(c.green()) + 0.0722 * lin(c.blue())


def _contrast(a: QColor, b: QColor) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _option(tab: TelemetryTab, row: int, *, selected: bool, text: str, base: str) -> Any:
    """The style option the table delegate paints *row* with, on a given theme."""
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Text, QColor(text))
    palette.setColor(QPalette.ColorRole.Base, QColor(base))
    option = QStyleOptionViewItem()
    option.palette = palette
    if selected:
        option.state |= QStyle.StateFlag.State_Selected
    tab._table.itemDelegate().initStyleOption(option, tab._table.model().index(row, 3))
    return option


def test_dim_row_uses_no_fixed_foreground_colour(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    """A hard-coded grey foreground is what made rejected rows unreadable."""
    tab = TelemetryTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)
    tab._append_row(callsign="", sat_name="X", data="[?] ...", norad=None, dim=True)
    item = tab._table.item(0, 3)
    assert item.data(Qt.ItemDataRole.ForegroundRole) is None
    assert item.data(Qt.ItemDataRole.UserRole + 1) is True


@pytest.mark.parametrize(
    ("text", "base"),
    [
        ("#dddddd", "#1e1e1e"),
        ("#dddddd", "#2b2b2b"),
        ("#111111", "#ffffff"),
        ("#222222", "#ececec"),
    ],
)
def test_dim_row_stays_readable_on_dark_and_light_themes(
    qtbot: QtBot, conn: sqlite3.Connection, text: str, base: str
) -> None:
    tab = TelemetryTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)
    tab._append_row(callsign="", sat_name="X", data="[?] ...", norad=None, dim=True)
    opt = _option(tab, 0, selected=False, text=text, base=base)
    muted = opt.palette.color(QPalette.ColorRole.Text)
    assert muted != QColor(text)  # actually muted
    assert _contrast(muted, QColor(base)) >= 3.0
    assert opt.font.italic()


def test_dim_row_keeps_theme_selection_colours_when_selected(
    qtbot: QtBot, conn: sqlite3.Connection
) -> None:
    """Selected: the theme's own highlight text colour is used (grey on the grey
    selection highlight was the reported problem) -- only italics mark it muted."""
    tab = TelemetryTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)
    tab._append_row(callsign="", sat_name="X", data="[?] ...", norad=None, dim=True)
    tab._append_row(callsign="J", sat_name="X", data="[TLM] ...", norad=1)
    dim_sel = _option(tab, 0, selected=True, text="#dddddd", base="#1e1e1e")
    normal_sel = _option(tab, 1, selected=True, text="#dddddd", base="#1e1e1e")
    assert dim_sel.palette.color(QPalette.ColorRole.Text) == normal_sel.palette.color(
        QPalette.ColorRole.Text
    )
    assert dim_sel.font.italic() and not normal_sel.font.italic()


def test_normal_row_is_not_restyled(qtbot: QtBot, conn: sqlite3.Connection) -> None:
    tab = TelemetryTab(conn, _FakeRadioControl())
    qtbot.addWidget(tab)
    tab._append_row(callsign="J", sat_name="X", data="[TLM] ...", norad=1)
    opt = _option(tab, 0, selected=False, text="#dddddd", base="#1e1e1e")
    assert opt.palette.color(QPalette.ColorRole.Text) == QColor("#dddddd")
    assert not opt.font.italic()
