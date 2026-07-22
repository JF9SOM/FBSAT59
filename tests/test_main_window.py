"""
メインウィンドウ関連コンポーネントのテスト

WorldMapView   — 世界地図ウィジェット
SatDetailPanel — 衛星詳細パネル
PassPanel      — パス予測パネル（タブ構成）
MainWindow     — メインウィンドウ
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from core.engine import Observation, PassInfo
from data.database import SCHEMA_SQL
from data.tle_manager import TLEManager
from ui.main_window import _MODE_INVERT, MainWindow, SatDetailPanel
from ui.pass_panel import PassPanel  # noqa: F401  (used in isinstance checks)
from ui.radio_control_widget import RadioControlWidget
from ui.world_map import WorldMapView

# ---------------------------------------------------------------------------
# フィクスチャ
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> sqlite3.Connection:
    """テスト用インメモリ SQLite DB（スキーマ初期化済み）。"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


@pytest.fixture()
def tle_manager(db: sqlite3.Connection) -> TLEManager:
    return TLEManager(db)


@pytest.fixture()
def populated_db(db: sqlite3.Connection) -> sqlite3.Connection:
    """衛星レコードを 2 件持つ DB。"""
    db.execute(
        "INSERT INTO satellites (norad_cat_id, name) VALUES (?, ?)",
        (25544, "ISS (ZARYA)"),
    )
    db.execute(
        "INSERT INTO satellites (norad_cat_id, name) VALUES (?, ?)",
        (43017, "AO-91"),
    )
    db.commit()
    return db


def _make_observation(
    norad: int = 25544,
    el: float = 45.0,
    az: float = 180.0,
    visible: bool = True,
) -> Observation:
    return Observation(
        norad_cat_id=norad,
        timestamp=datetime.now(UTC),
        elevation_deg=el,
        azimuth_deg=az,
        range_km=412.5,
        range_rate_km_s=-2.134,
        is_above_horizon=visible,
    )


def _make_pass_info(
    norad: int = 25544,
    max_el: float = 45.0,
    duration: float = 600.0,
) -> PassInfo:
    now = datetime.now(UTC)
    return PassInfo(
        norad_cat_id=norad,
        aos=now,
        tca=now + timedelta(minutes=5),
        los=now + timedelta(seconds=duration),
        max_elevation_deg=max_el,
        aos_azimuth_deg=90.0,
        los_azimuth_deg=270.0,
        duration_s=duration,
    )


# ---------------------------------------------------------------------------
# SatDetailPanel
# ---------------------------------------------------------------------------


class TestSatDetailPanel:
    def test_create(self, qtbot) -> None:
        w = SatDetailPanel()
        qtbot.addWidget(w)
        assert w is not None

    def test_initial_state_is_dashes(self, qtbot) -> None:
        w = SatDetailPanel()
        qtbot.addWidget(w)
        assert w._name_label.text() == "—"
        assert w._el_label.text() == "—"

    def test_set_satellite(self, qtbot) -> None:
        w = SatDetailPanel()
        qtbot.addWidget(w)
        w.set_satellite(25544, "ISS (ZARYA)")
        assert w._name_label.text() == "ISS (ZARYA)"
        assert w._norad_label.text() == "25544"

    def test_update_observation_visible(self, qtbot) -> None:
        w = SatDetailPanel()
        qtbot.addWidget(w)
        obs = _make_observation(el=45.12, az=180.34, visible=True)
        w.update_observation(obs)
        assert "45.12" in w._el_label.text()
        assert "180.34" in w._az_label.text()
        assert "412.5" in w._range_label.text()
        assert "-2.134" in w._rate_label.text()

    def test_update_observation_below_horizon(self, qtbot) -> None:
        w = SatDetailPanel()
        qtbot.addWidget(w)
        obs = _make_observation(el=-5.0, visible=False)
        w.update_observation(obs)
        assert "horizon" in w._vis_label.text().lower() or "below" in w._vis_label.text().lower()

    def test_update_observation_none_resets_fields(self, qtbot) -> None:
        w = SatDetailPanel()
        qtbot.addWidget(w)
        obs = _make_observation()
        w.update_observation(obs)
        w.update_observation(None)
        assert w._el_label.text() == "—"
        assert w._az_label.text() == "—"

    def test_clear_resets_all(self, qtbot) -> None:
        w = SatDetailPanel()
        qtbot.addWidget(w)
        w.set_satellite(25544, "ISS")
        w.update_observation(_make_observation())
        w.clear()
        assert w._name_label.text() == "—"
        assert w._norad_label.text() == "—"
        assert w._el_label.text() == "—"


# ---------------------------------------------------------------------------
# PassPanel
# ---------------------------------------------------------------------------


class TestPassListPanel:
    def test_create(self, qtbot) -> None:
        w = PassPanel()
        qtbot.addWidget(w)
        assert w is not None

    def test_initial_state_empty(self, qtbot) -> None:
        w = PassPanel()
        qtbot.addWidget(w)
        assert w._target_table.rowCount() == 0
        assert w._passes == []

    def test_set_passes_empty(self, qtbot) -> None:
        w = PassPanel()
        qtbot.addWidget(w)
        w.set_passes([])
        assert w._target_table.rowCount() == 0

    def test_set_passes_single(self, qtbot) -> None:
        w = PassPanel()
        qtbot.addWidget(w)
        w.set_passes([_make_pass_info()])
        assert w._target_table.rowCount() == 1

    def test_set_passes_multiple(self, qtbot) -> None:
        w = PassPanel()
        qtbot.addWidget(w)
        passes = [_make_pass_info(max_el=float(e)) for e in [15, 35, 65]]
        w.set_passes(passes)
        assert w._target_table.rowCount() == 3
        assert len(w._passes) == 3

    def test_set_passes_columns_populated(self, qtbot) -> None:
        w = PassPanel()
        qtbot.addWidget(w)
        w.set_passes([_make_pass_info(max_el=60.0, duration=600.0)])
        assert w._target_table.item(0, 1) is not None  # Max El
        assert "60.0" in w._target_table.item(0, 1).text()

    def test_clear(self, qtbot) -> None:
        w = PassPanel()
        qtbot.addWidget(w)
        w.set_passes([_make_pass_info()])
        w.clear()
        assert w._target_table.rowCount() == 0
        assert w._passes == []

    def test_pass_selected_signal_exists(self, qtbot) -> None:
        w = PassPanel()
        qtbot.addWidget(w)
        assert hasattr(w, "pass_selected")

    def test_column_count(self, qtbot) -> None:
        w = PassPanel()
        qtbot.addWidget(w)
        assert w._target_table.columnCount() == len(PassPanel._TARGET_COLS)

    def test_has_two_tabs(self, qtbot) -> None:
        w = PassPanel()
        qtbot.addWidget(w)
        assert w._tabs.count() == 2

    def test_group_tab_has_search_button(self, qtbot) -> None:
        w = PassPanel()
        qtbot.addWidget(w)
        assert w._group_search_btn is not None

    def test_group_tab_has_cancel_button(self, qtbot) -> None:
        w = PassPanel()
        qtbot.addWidget(w)
        assert not w._group_cancel_btn.isEnabled()

    def test_set_satellites_updates_list(self, qtbot) -> None:
        w = PassPanel()
        qtbot.addWidget(w)
        w.set_satellites([(25544, "ISS"), (43017, "AO-91")])
        assert w._sat_list == [(25544, "ISS"), (43017, "AO-91")]

    def test_set_satellites_invalidates_cache(self, qtbot) -> None:
        w = PassPanel()
        qtbot.addWidget(w)
        w._cache_key = ("dummy",)  # type: ignore[assignment]
        w.set_satellites([(25544, "ISS")])
        assert w._cache_key is None

    def test_highlight_satellite_signal_exists(self, qtbot) -> None:
        w = PassPanel()
        qtbot.addWidget(w)
        assert hasattr(w, "highlight_satellite")


# ---------------------------------------------------------------------------
# RadioControlWidget
# ---------------------------------------------------------------------------


class TestRadioControlWidget:
    def test_create(self, qtbot) -> None:
        w = RadioControlWidget()
        qtbot.addWidget(w)
        assert w is not None

    def test_initial_satellite_is_dash(self, qtbot) -> None:
        w = RadioControlWidget()
        qtbot.addWidget(w)
        assert w._sat_name_label.text() == "—"
        assert w._norad_label.text() == "—"

    def test_set_satellite(self, qtbot) -> None:
        w = RadioControlWidget()
        qtbot.addWidget(w)
        w.set_satellite(25544, "ISS (ZARYA)")
        assert w._sat_name_label.text() == "ISS (ZARYA)"
        assert w._norad_label.text() == "25544"

    def test_clear_satellite(self, qtbot) -> None:
        w = RadioControlWidget()
        qtbot.addWidget(w)
        w.set_satellite(25544, "ISS")
        w.clear_satellite()
        assert w._sat_name_label.text() == "—"
        assert w._norad_label.text() == "—"

    def test_update_doppler_downlink(self, qtbot) -> None:
        w = RadioControlWidget()
        qtbot.addWidget(w)
        w.update_doppler(
            downlink_nominal_hz=437_550_000.0,
            downlink_corrected_hz=437_548_000.0,
            downlink_shift_hz=-2000.0,
            uplink_nominal_hz=None,
            uplink_corrected_hz=None,
            uplink_shift_hz=None,
            mode="FM",
            ctcss_hz=67.0,
        )
        assert "437" in w._downlink_label.text()
        assert "-2000" in w._downlink_doppler_label.text()
        assert w._mode_label.text() == "FM"
        assert "67.0" in w._ctcss_label.text()

    def test_update_doppler_none_clears(self, qtbot) -> None:
        w = RadioControlWidget()
        qtbot.addWidget(w)
        w.update_doppler(None, None, None, None, None, None)
        assert w._downlink_label.text() == "—"
        assert w._uplink_label.text() == "—"

    def test_update_rotator(self, qtbot) -> None:
        from rig.controller import RotatorState

        w = RadioControlWidget()
        qtbot.addWidget(w)
        w.update_rotator(RotatorState(azimuth_deg=180.5, elevation_deg=45.0))
        assert "180.5" in w._rot_az_label.text()
        assert "45.0" in w._rot_el_label.text()

    def test_update_rotator_none(self, qtbot) -> None:
        w = RadioControlWidget()
        qtbot.addWidget(w)
        w.update_rotator(None)
        assert w._rot_az_label.text() == "—"
        assert w._rot_el_label.text() == "—"

    def test_no_rig_buttons_disabled(self, qtbot) -> None:
        w = RadioControlWidget()
        qtbot.addWidget(w)
        assert not w._connect_rig1_btn.isEnabled()
        assert not w._connect_rig2_btn.isEnabled()
        assert not w._connect_rot_btn.isEnabled()

    def test_rig_status_not_configured(self, qtbot) -> None:
        w = RadioControlWidget()
        qtbot.addWidget(w)
        assert "configured" in w._rig1_status_label.text().lower()
        assert "configured" in w._rig2_status_label.text().lower()

    # -- Transponder combo --

    def test_initial_combo_empty_and_disabled(self, qtbot) -> None:
        w = RadioControlWidget()
        qtbot.addWidget(w)
        assert w._xpdr_combo.count() == 0
        assert not w._xpdr_combo.isEnabled()

    def test_set_transmitters_populates_combo(self, qtbot) -> None:
        w = RadioControlWidget()
        qtbot.addWidget(w)
        xpdrs = [
            {
                "description": "FM Voice",
                "type": "Transponder",
                "downlink_low": 145_800_000,
                "uplink_low": 145_200_000,
                "mode": "FM",
                "ctcss_tone": None,
                "invert": 0,
            },
            {
                "description": "APRS",
                "type": "Transceiver",
                "downlink_low": 437_825_000,
                "uplink_low": 437_825_000,
                "mode": "AFSK",
                "ctcss_tone": None,
                "invert": 0,
            },
        ]
        w.set_transmitters(xpdrs)
        assert w._xpdr_combo.count() == 2
        assert w._xpdr_combo.isEnabled()
        assert "FM Voice" in w._xpdr_combo.itemText(0)
        assert "145.800" in w._xpdr_combo.itemText(0)

    def test_set_transmitters_emits_signal(self, qtbot) -> None:
        w = RadioControlWidget()
        qtbot.addWidget(w)
        xpdr = {
            "description": "FM",
            "type": "Transponder",
            "downlink_low": 145_800_000,
            "uplink_low": None,
            "mode": "FM",
            "ctcss_tone": 67.0,
            "invert": 0,
        }
        received: list[object] = []
        w.transmitter_changed.connect(lambda x: received.append(x))
        w.set_transmitters([xpdr])
        assert len(received) == 1
        assert received[0] == xpdr

    def test_set_transmitters_empty_disables_combo(self, qtbot) -> None:
        w = RadioControlWidget()
        qtbot.addWidget(w)
        w.set_transmitters([])
        assert w._xpdr_combo.count() == 0
        assert not w._xpdr_combo.isEnabled()

    def test_set_transmitters_empty_emits_none(self, qtbot) -> None:
        w = RadioControlWidget()
        qtbot.addWidget(w)
        received: list[object] = []
        w.transmitter_changed.connect(lambda x: received.append(x))
        w.set_transmitters([])
        assert received == [None]

    def test_clear_satellite_clears_combo(self, qtbot) -> None:
        w = RadioControlWidget()
        qtbot.addWidget(w)
        xpdr = {
            "description": "FM",
            "type": "Transponder",
            "downlink_low": 145_800_000,
            "uplink_low": None,
            "mode": "FM",
            "ctcss_tone": None,
            "invert": 0,
        }
        w.set_transmitters([xpdr])
        w.clear_satellite()
        assert w._xpdr_combo.count() == 0
        assert not w._xpdr_combo.isEnabled()

    def test_combo_change_emits_correct_xpdr(self, qtbot) -> None:
        w = RadioControlWidget()
        qtbot.addWidget(w)
        xpdrs = [
            {
                "description": "FM",
                "type": "Transponder",
                "downlink_low": 145_800_000,
                "uplink_low": None,
                "mode": "FM",
                "ctcss_tone": None,
                "invert": 0,
            },
            {
                "description": "APRS",
                "type": "Transceiver",
                "downlink_low": 437_825_000,
                "uplink_low": 437_825_000,
                "mode": "AFSK",
                "ctcss_tone": None,
                "invert": 0,
            },
        ]
        w.set_transmitters(xpdrs)
        received: list[object] = []
        w.transmitter_changed.connect(lambda x: received.append(x))
        w._xpdr_combo.setCurrentIndex(1)
        assert len(received) == 1
        assert received[0] == xpdrs[1]

    def test_xpdr_label_format(self, qtbot) -> None:
        from ui.radio_control_widget import RadioControlWidget

        xpdr = {"description": "ISS Voice", "type": "Transponder", "downlink_low": 145_800_000}
        label = RadioControlWidget._xpdr_label(xpdr)
        assert "ISS Voice" in label
        assert "145.800" in label
        assert "Transponder" in label


# ---------------------------------------------------------------------------
# TransmitterDialog
# ---------------------------------------------------------------------------


class TestTransmitterDialog:
    def _mgr(self, db: sqlite3.Connection) -> object:
        from data.transmitter_manager import TransmitterManager

        return TransmitterManager(db)

    def test_type_list_includes_transceiver(self) -> None:
        from ui.transmitter_dialog import _TYPES

        assert "Transceiver" in _TYPES

    def test_type_list_order(self) -> None:
        from ui.transmitter_dialog import _TYPES

        assert _TYPES == ["Transmitter", "Transponder", "Transceiver", "Beacon"]

    def test_satnogs_norad_spin_exists_and_default_zero(self, qtbot, db) -> None:
        from ui.transmitter_dialog import TransmitterDialog

        w = TransmitterDialog(self._mgr(db), norad_cat_id=25544)
        qtbot.addWidget(w)
        assert hasattr(w, "_satnogs_norad_spin")
        assert w._satnogs_norad_spin.value() == 0

    def test_satnogs_norad_spin_range(self, qtbot, db) -> None:
        from ui.transmitter_dialog import TransmitterDialog

        w = TransmitterDialog(self._mgr(db))
        qtbot.addWidget(w)
        assert w._satnogs_norad_spin.minimum() == 0
        assert w._satnogs_norad_spin.maximum() == 999999

    def test_edit_mode_prefills_fields(self, qtbot, db) -> None:
        from ui.transmitter_dialog import TransmitterDialog

        existing = {
            "uuid": "manual-test",
            "norad_cat_id": 25544,
            "description": "ISS FM",
            "type": "Transceiver",
            "downlink_low": 145_800_000,
            "downlink_high": None,
            "uplink_low": 144_490_000,
            "uplink_high": None,
            "mode": "FM",
            "invert": False,
            "ctcss_tone": 67.0,
            "ctcss_tone_type": "CTCSS",
            "notes": "test note",
        }
        w = TransmitterDialog(self._mgr(db), existing=existing)
        qtbot.addWidget(w)
        assert w._desc_edit.text() == "ISS FM"
        assert abs(w._dl_spin.value() - 145.800) < 0.001
        assert abs(w._ul_spin.value() - 144.490) < 0.001
        assert w._type_combo.currentText() == "Transceiver"
        assert w._mode_combo.currentText() == "FM"
        assert abs(w._ctcss_spin.value() - 67.0) < 0.1
        assert w._notes_edit.text() == "test note"

    def test_edit_mode_title(self, qtbot, db) -> None:
        from ui.transmitter_dialog import TransmitterDialog

        existing = {
            "uuid": "manual-test",
            "norad_cat_id": 25544,
            "description": "ISS FM",
            "type": "Transceiver",
            "downlink_low": 145_800_000,
            "downlink_high": None,
            "uplink_low": None,
            "uplink_high": None,
            "mode": "FM",
            "invert": False,
            "ctcss_tone": None,
            "ctcss_tone_type": None,
            "notes": "",
        }
        w = TransmitterDialog(self._mgr(db), existing=existing)
        qtbot.addWidget(w)
        assert "Edit" in w.windowTitle()

    def test_edit_mode_norad_spin_disabled(self, qtbot, db) -> None:
        from ui.transmitter_dialog import TransmitterDialog

        existing = {
            "uuid": "manual-test",
            "norad_cat_id": 25544,
            "description": "ISS FM",
            "type": "Transponder",
            "downlink_low": 145_800_000,
            "downlink_high": None,
            "uplink_low": None,
            "uplink_high": None,
            "mode": "FM",
            "invert": False,
            "ctcss_tone": None,
            "ctcss_tone_type": None,
            "notes": "",
        }
        w = TransmitterDialog(self._mgr(db), existing=existing)
        qtbot.addWidget(w)
        assert not w._norad_spin.isEnabled()


class TestUpdateTransmitterTypeField:
    """update_transmitter が type フィールドを更新できることを確認する。"""

    def test_update_type_allowed(self, db: sqlite3.Connection) -> None:
        from data.transmitter_manager import TransmitterManager

        db.execute("INSERT OR IGNORE INTO satellites (norad_cat_id, name) VALUES (25544, 'ISS')")
        db.commit()
        mgr = TransmitterManager(db)
        uid = mgr.add_manual_transmitter(
            norad_cat_id=25544,
            description="Test FM",
            downlink_low=145_800_000,
            mode="FM",
            xpdr_type="Transponder",
        )
        mgr.update_transmitter(uid, type="Transceiver")
        row = db.execute("SELECT type FROM transmitters WHERE uuid = ?", (uid,)).fetchone()
        assert row["type"] == "Transceiver"


class TestSyncFromSatnogsTargetNorad:
    """sync_from_satnogs の target_norad_cat_id オーバーライドテスト。"""

    def test_target_norad_remaps_storage(self, db: sqlite3.Connection) -> None:
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from data.transmitter_manager import TransmitterManager

        db.execute(
            "INSERT OR IGNORE INTO satellites (norad_cat_id, name) VALUES (68795, 'ORIGAMISAT-2')"
        )
        db.commit()
        mgr = TransmitterManager(db)

        mock_data = [
            {
                "uuid": "test-uuid-remap",
                "norad_cat_id": 98325,
                "description": "Mode U CW",
                "type": "Transmitter",
                "uplink_low": None,
                "uplink_high": None,
                "downlink_low": 437_505_000,
                "downlink_high": None,
                "mode": "CW",
                "invert": False,
                "baud": 24,
                "ctcss_tone": None,
                "alive": True,
            }
        ]

        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_data
        mock_resp.raise_for_status.return_value = None

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("data.transmitter_manager.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            result = asyncio.run(
                mgr.sync_from_satnogs(norad_cat_id=98325, target_norad_cat_id=68795)
            )

        assert result["inserted"] == 1
        row = db.execute(
            "SELECT norad_cat_id FROM transmitters WHERE uuid='test-uuid-remap'"
        ).fetchone()
        assert row["norad_cat_id"] == 68795

    def test_no_target_uses_api_norad(self, db: sqlite3.Connection) -> None:
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from data.transmitter_manager import TransmitterManager

        db.execute("INSERT OR IGNORE INTO satellites (norad_cat_id, name) VALUES (98325, 'Test')")
        db.commit()
        mgr = TransmitterManager(db)

        mock_data = [
            {
                "uuid": "test-uuid-noop",
                "norad_cat_id": 98325,
                "description": "Test",
                "type": "Transmitter",
                "uplink_low": None,
                "uplink_high": None,
                "downlink_low": 437_000_000,
                "downlink_high": None,
                "mode": "FM",
                "invert": False,
                "baud": None,
                "ctcss_tone": None,
                "alive": True,
            }
        ]

        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_data
        mock_resp.raise_for_status.return_value = None
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("data.transmitter_manager.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            asyncio.run(mgr.sync_from_satnogs(norad_cat_id=98325))

        row = db.execute(
            "SELECT norad_cat_id FROM transmitters WHERE uuid='test-uuid-noop'"
        ).fetchone()
        assert row["norad_cat_id"] == 98325

    def test_update_path_no_binding_error(self, db: sqlite3.Connection) -> None:
        """sync_from_satnogs UPDATE ブランチで SQL バインドエラーが発生しないことを確認する。"""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from data.transmitter_manager import TransmitterManager

        db.execute("INSERT OR IGNORE INTO satellites (norad_cat_id, name) VALUES (25544, 'ISS')")
        db.execute(
            """
            INSERT INTO transmitters
            (uuid, norad_cat_id, description, type,
             downlink_low, mode, alive, source, manual_override, updated_at)
            VALUES ('existing-uuid', 25544, 'ISS FM', 'Transponder',
                    145800000, 'FM', 1, 'satnogs', 0, '2024-01-01')
            """
        )
        db.commit()
        mgr = TransmitterManager(db)

        mock_data = [
            {
                "uuid": "existing-uuid",
                "norad_cat_id": 25544,
                "description": "ISS FM updated",
                "type": "Transceiver",
                "uplink_low": 144_490_000,
                "uplink_high": None,
                "downlink_low": 145_800_000,
                "downlink_high": None,
                "mode": "FM",
                "invert": False,
                "baud": None,
                "ctcss_tone": 67.0,
                "alive": True,
            }
        ]

        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_data
        mock_resp.raise_for_status.return_value = None
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("data.transmitter_manager.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            result = asyncio.run(mgr.sync_from_satnogs(norad_cat_id=25544))

        assert result["updated"] == 1
        row = db.execute(
            "SELECT description FROM transmitters WHERE uuid='existing-uuid'"
        ).fetchone()
        assert row["description"] == "ISS FM updated"


# ---------------------------------------------------------------------------
# TestSyncSatelliteNamesStatus
# ---------------------------------------------------------------------------


class TestSyncSatelliteNamesStatus:
    """sync_satellite_names が status フィールドを正しく同期するテスト。"""

    def _run(self, coro):  # type: ignore[no-untyped-def]
        import asyncio

        return asyncio.run(coro)

    def test_alive_status_saved(self, db: sqlite3.Connection) -> None:
        """SatNOGS status='alive' は DB に 'alive' として保存される。"""
        from unittest.mock import AsyncMock, MagicMock, patch

        from data.transmitter_manager import TransmitterManager

        db.execute("INSERT INTO satellites (norad_cat_id, name) VALUES (25544, 'ISS')")
        db.commit()
        mgr = TransmitterManager(db)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = [
            {"norad_cat_id": 25544, "name": "ISS (ZARYA)", "status": "alive"}
        ]

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("data.transmitter_manager.httpx.AsyncClient", return_value=mock_client):
            self._run(mgr.sync_satellite_names())

        row = db.execute("SELECT status FROM satellites WHERE norad_cat_id = 25544").fetchone()
        assert row["status"] == "alive"

    def test_reentred_maps_to_dead(self, db: sqlite3.Connection) -> None:
        """SatNOGS status='re-entered' は DB に 'dead' として保存される。"""
        from unittest.mock import AsyncMock, MagicMock, patch

        from data.transmitter_manager import TransmitterManager

        db.execute("INSERT INTO satellites (norad_cat_id, name) VALUES (99001, 'SAT-RE')")
        db.commit()
        mgr = TransmitterManager(db)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = [
            {"norad_cat_id": 99001, "name": "SAT-RE", "status": "re-entered"}
        ]

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("data.transmitter_manager.httpx.AsyncClient", return_value=mock_client):
            self._run(mgr.sync_satellite_names())

        row = db.execute("SELECT status FROM satellites WHERE norad_cat_id = 99001").fetchone()
        assert row["status"] == "dead"

    def test_future_maps_to_unknown(self, db: sqlite3.Connection) -> None:
        """SatNOGS status='future' は DB に 'unknown' として保存される。"""
        from unittest.mock import AsyncMock, MagicMock, patch

        from data.transmitter_manager import TransmitterManager

        db.execute("INSERT INTO satellites (norad_cat_id, name) VALUES (99002, 'SAT-FUTURE')")
        db.commit()
        mgr = TransmitterManager(db)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = [
            {"norad_cat_id": 99002, "name": "SAT-FUTURE", "status": "future"}
        ]

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("data.transmitter_manager.httpx.AsyncClient", return_value=mock_client):
            self._run(mgr.sync_satellite_names())

        row = db.execute("SELECT status FROM satellites WHERE norad_cat_id = 99002").fetchone()
        assert row["status"] == "unknown"


# ---------------------------------------------------------------------------
# MainWindow
# ---------------------------------------------------------------------------


class TestMainWindow:
    """MainWindow の UI 構造・動作テスト。"""

    def _make_window(self, qtbot, db: sqlite3.Connection, tle_manager: TLEManager) -> MainWindow:
        w = MainWindow(conn=db, tle_manager=tle_manager)
        qtbot.addWidget(w)
        return w

    def test_create(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        assert w is not None

    def test_window_title_contains_app_name(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        assert "FBSAT59" in w.windowTitle()

    def test_has_tab_widget_with_four_tabs(self, qtbot, db, tle_manager) -> None:
        # Tab count is 7: Dashboard, World Map, Radar, Pass Chart, Group Pass Chart,
        # Radio Control, SDR Control (hidden by default but still counted)
        w = self._make_window(qtbot, db, tle_manager)
        assert w._tab_widget is not None
        assert w._tab_widget.count() == 7

    def test_tab_has_world_map(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        assert isinstance(w._world_map, WorldMapView)

    def test_tab_has_radar_view(self, qtbot, db, tle_manager) -> None:
        from ui.radar_view import RadarView

        w = self._make_window(qtbot, db, tle_manager)
        assert isinstance(w._radar_view, RadarView)

    def test_tab_has_pass_chart(self, qtbot, db, tle_manager) -> None:
        from ui.pass_chart import PassChartView

        w = self._make_window(qtbot, db, tle_manager)
        assert isinstance(w._pass_chart, PassChartView)

    def test_tab_has_radio_control(self, qtbot, db, tle_manager) -> None:
        from ui.radio_control_widget import RadioControlWidget

        w = self._make_window(qtbot, db, tle_manager)
        assert isinstance(w._radio_control, RadioControlWidget)

    def test_has_satellite_list(self, qtbot, db, tle_manager) -> None:
        from PySide6.QtWidgets import QListWidget

        w = self._make_window(qtbot, db, tle_manager)
        assert isinstance(w._sat_list, QListWidget)

    def test_has_detail_panel(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        assert isinstance(w._detail_panel, SatDetailPanel)

    def test_has_pass_list(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        assert isinstance(w._pass_list, PassPanel)

    def test_timer_is_active(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        assert w._timer.isActive()

    def test_timer_interval_is_1000ms(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        assert w._timer.interval() == 1000

    def test_statusbar_qth_label_exists(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        assert w._qth_label is not None

    def test_statusbar_tle_label_exists(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        assert w._tle_label is not None

    def test_statusbar_url_label_exists(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        assert w._url_label is not None

    def test_statusbar_qr_button_exists(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        assert w._qr_button is not None

    def test_statusbar_rig_label_exists(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        assert w._rig_label is not None

    def test_menubar_has_file_menu(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        mb = w.menuBar()
        assert mb is not None
        titles = [mb.actions()[i].text() for i in range(len(mb.actions()))]
        assert any("File" in t for t in titles)

    def test_menubar_has_satellite_menu(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        mb = w.menuBar()
        assert mb is not None
        titles = [mb.actions()[i].text() for i in range(len(mb.actions()))]
        assert any("Satellite" in t for t in titles)

    def test_menubar_has_radio_menu(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        mb = w.menuBar()
        assert mb is not None
        titles = [mb.actions()[i].text() for i in range(len(mb.actions()))]
        assert any("Radio" in t for t in titles)

    def test_menubar_has_view_menu(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        mb = w.menuBar()
        assert mb is not None
        titles = [mb.actions()[i].text() for i in range(len(mb.actions()))]
        assert any("View" in t for t in titles)

    def test_menubar_has_help_menu(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        mb = w.menuBar()
        assert mb is not None
        titles = [mb.actions()[i].text() for i in range(len(mb.actions()))]
        assert any("Help" in t for t in titles)

    def test_satellite_list_populates_from_db(self, qtbot, populated_db) -> None:
        tm = TLEManager(populated_db)
        w = MainWindow(conn=populated_db, tle_manager=tm)
        qtbot.addWidget(w)
        idx = w._filter_combo.findText("All Satellites")
        w._filter_combo.setCurrentIndex(idx)
        assert w._sat_list.count() == 6  # 2 from populated_db + 3 community satellites + Moon

    def test_empty_db_gives_empty_satellite_list(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        idx = w._filter_combo.findText("All Satellites")
        w._filter_combo.setCurrentIndex(idx)
        assert w._sat_list.count() == 4  # 3 community satellites + Moon always loaded at startup

    def test_no_crash_with_none_engine(self, qtbot, db, tle_manager) -> None:
        w = MainWindow(conn=db, tle_manager=tle_manager, engine=None)
        qtbot.addWidget(w)
        w._on_tick()  # should not raise

    def test_no_crash_with_none_pass_predictor(self, qtbot, db, tle_manager) -> None:
        w = MainWindow(conn=db, tle_manager=tle_manager, pass_predictor=None)
        qtbot.addWidget(w)
        w._selected_norad = 25544
        w._refresh_passes()  # should not raise

    def test_web_url_empty_without_fastapi_app(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        assert w._web_server_url == ""
        assert w._url_label.text() == ""

    def test_web_server_is_none_without_fastapi_app(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        assert w._web_server is None

    def test_close_stops_timer(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        assert w._timer.isActive()
        w.close()
        assert not w._timer.isActive()

    def test_on_sat_selected_negative_row_clears(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        w._selected_norad = 25544
        w._on_sat_selected(-1)
        assert w._selected_norad is None

    def test_all_norads_populated(self, qtbot, populated_db) -> None:
        tm = TLEManager(populated_db)
        w = MainWindow(conn=populated_db, tle_manager=tm)
        qtbot.addWidget(w)
        assert len(w._all_norads) == 5  # 2 from populated_db + 3 community satellites

    def test_on_tick_no_crash_empty_db(self, qtbot, db, tle_manager) -> None:
        w = self._make_window(qtbot, db, tle_manager)
        w._on_tick()  # should not raise with empty DB

    def test_edit_transmitter_warns_when_no_list_selection(self, qtbot, db, tle_manager) -> None:
        """Edit Transmitter: sat_list に選択がなければ警告を表示する。"""
        from unittest.mock import patch

        w = self._make_window(qtbot, db, tle_manager)
        w._selected_norad = 25544  # stale value — should be ignored
        w._sat_list.clearSelection()
        with patch("ui.main_window.QMessageBox.warning") as mock_warn:
            w._on_edit_transmitter()
        mock_warn.assert_called_once()

    def test_delete_transmitter_warns_when_no_list_selection(self, qtbot, db, tle_manager) -> None:
        """Delete Transmitter: sat_list に選択がなければ警告を表示する。"""
        from unittest.mock import patch

        w = self._make_window(qtbot, db, tle_manager)
        w._selected_norad = 25544  # stale value — should be ignored
        w._sat_list.clearSelection()
        with patch("ui.main_window.QMessageBox.warning") as mock_warn:
            w._on_delete_transmitter()
        mock_warn.assert_called_once()


class TestHideSatellite:
    """Hide Satellite 機能のテスト。"""

    def _make_window(self, qtbot, db: sqlite3.Connection, tle_manager: TLEManager) -> MainWindow:
        w = MainWindow(conn=db, tle_manager=tle_manager)
        qtbot.addWidget(w)
        return w

    def test_set_hidden_updates_db(self, qtbot, populated_db) -> None:
        """_set_hidden(True) で is_hidden=1 がDBに保存される。"""
        tle_manager = TLEManager(populated_db)
        w = self._make_window(qtbot, populated_db, tle_manager)
        w._set_hidden(25544, True)
        row = populated_db.execute(
            "SELECT is_hidden FROM satellites WHERE norad_cat_id = ?", (25544,)
        ).fetchone()
        assert row["is_hidden"] == 1

    def test_set_hidden_false_unhides(self, qtbot, populated_db) -> None:
        """_set_hidden(False) で is_hidden=0 に戻る。"""
        tle_manager = TLEManager(populated_db)
        populated_db.execute("UPDATE satellites SET is_hidden = 1 WHERE norad_cat_id = ?", (25544,))
        populated_db.commit()
        w = self._make_window(qtbot, populated_db, tle_manager)
        w._set_hidden(25544, False)
        row = populated_db.execute(
            "SELECT is_hidden FROM satellites WHERE norad_cat_id = ?", (25544,)
        ).fetchone()
        assert row["is_hidden"] == 0

    def test_hidden_satellite_not_in_all_filter(self, qtbot, populated_db) -> None:
        """is_hidden=1 の衛星は 'All Satellites' フィルターに表示されない。"""
        tle_manager = TLEManager(populated_db)
        populated_db.execute("UPDATE satellites SET is_hidden = 1 WHERE norad_cat_id = ?", (25544,))
        populated_db.commit()
        w = self._make_window(qtbot, populated_db, tle_manager)
        from PySide6.QtCore import Qt

        w._filter_combo.setCurrentText("All Satellites")
        role = Qt.ItemDataRole.UserRole
        norads = [w._sat_list.item(i).data(role) for i in range(w._sat_list.count())]
        assert 25544 not in norads

    def test_hidden_filter_shows_only_hidden(self, qtbot, populated_db) -> None:
        """'Hidden' フィルターでは is_hidden=1 の衛星だけが表示される。"""
        from PySide6.QtCore import Qt

        tle_manager = TLEManager(populated_db)
        populated_db.execute("UPDATE satellites SET is_hidden = 1 WHERE norad_cat_id = ?", (25544,))
        populated_db.commit()
        w = self._make_window(qtbot, populated_db, tle_manager)
        w._filter_combo.setCurrentText("Hidden")
        assert w._sat_list.count() == 1
        assert w._sat_list.item(0).data(Qt.ItemDataRole.UserRole) == 25544

    def test_search_surfaces_system_hidden_satellite(self, qtbot, populated_db) -> None:
        """is_hidden=2 の衛星も、検索クエリがあれば "(Hidden)" 付きで見つかる。"""
        from PySide6.QtCore import Qt

        tle_manager = TLEManager(populated_db)
        populated_db.execute("UPDATE satellites SET is_hidden = 2 WHERE norad_cat_id = ?", (25544,))
        populated_db.commit()
        w = self._make_window(qtbot, populated_db, tle_manager)
        w._filter_combo.setCurrentText("All Satellites")
        w._search_box.setText("zarya")
        w._apply_filter()
        role = Qt.ItemDataRole.UserRole
        norads = [w._sat_list.item(i).data(role) for i in range(w._sat_list.count())]
        assert 25544 in norads
        idx = norads.index(25544)
        assert "(Hidden)" in w._sat_list.item(idx).text()

    def test_search_surfaces_user_hidden_satellite(self, qtbot, populated_db) -> None:
        """is_hidden=1 の衛星も、検索クエリがあれば見つかる。"""
        from PySide6.QtCore import Qt

        tle_manager = TLEManager(populated_db)
        populated_db.execute("UPDATE satellites SET is_hidden = 1 WHERE norad_cat_id = ?", (25544,))
        populated_db.commit()
        w = self._make_window(qtbot, populated_db, tle_manager)
        w._filter_combo.setCurrentText("All Satellites")
        w._search_box.setText("zarya")
        w._apply_filter()
        role = Qt.ItemDataRole.UserRole
        norads = [w._sat_list.item(i).data(role) for i in range(w._sat_list.count())]
        assert 25544 in norads

    def test_no_search_query_still_hides_system_hidden(self, qtbot, populated_db) -> None:
        """検索クエリが空なら is_hidden=2 の衛星は従来通り表示されない。"""
        from PySide6.QtCore import Qt

        tle_manager = TLEManager(populated_db)
        populated_db.execute("UPDATE satellites SET is_hidden = 2 WHERE norad_cat_id = ?", (25544,))
        populated_db.commit()
        w = self._make_window(qtbot, populated_db, tle_manager)
        w._filter_combo.setCurrentText("All Satellites")
        w._search_box.setText("")
        w._apply_filter()
        role = Qt.ItemDataRole.UserRole
        norads = [w._sat_list.item(i).data(role) for i in range(w._sat_list.count())]
        assert 25544 not in norads

    def test_hide_satellite_warns_when_no_selection(self, qtbot, db, tle_manager) -> None:
        """Hide Satellite: sat_list に選択がなければ警告を表示する。"""
        from unittest.mock import patch

        w = self._make_window(qtbot, db, tle_manager)
        w._sat_list.clearSelection()
        with patch("ui.main_window.QMessageBox.warning") as mock_warn:
            w._on_hide_satellite()
        mock_warn.assert_called_once()

    def test_system_hidden_not_in_all_filter(self, qtbot, populated_db) -> None:
        """is_hidden=2 の衛星は 'All Satellites' フィルターに表示されない。"""
        from PySide6.QtCore import Qt

        tle_manager = TLEManager(populated_db)
        populated_db.execute("UPDATE satellites SET is_hidden = 2 WHERE norad_cat_id = ?", (25544,))
        populated_db.commit()
        w = self._make_window(qtbot, populated_db, tle_manager)
        w._filter_combo.setCurrentText("All Satellites")
        role = Qt.ItemDataRole.UserRole
        norads = [w._sat_list.item(i).data(role) for i in range(w._sat_list.count())]
        assert 25544 not in norads

    def test_system_hidden_not_in_hidden_filter(self, qtbot, populated_db) -> None:
        """is_hidden=2 の衛星は 'Hidden' フィルターにも表示されない。"""
        from PySide6.QtCore import Qt

        tle_manager = TLEManager(populated_db)
        populated_db.execute("UPDATE satellites SET is_hidden = 2 WHERE norad_cat_id = ?", (25544,))
        populated_db.commit()
        w = self._make_window(qtbot, populated_db, tle_manager)
        w._filter_combo.setCurrentText("Hidden")
        role = Qt.ItemDataRole.UserRole
        norads = [w._sat_list.item(i).data(role) for i in range(w._sat_list.count())]
        assert 25544 not in norads


class TestAutoHideFollowedSatellites:
    """norad_follow_id による衛星自動非表示のテスト。"""

    def _run(self, coro):  # type: ignore[no-untyped-def]
        import asyncio

        return asyncio.run(coro)

    def _make_mock_client(self, payload):  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, MagicMock

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = payload
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        return mock_client

    def test_norad_follow_id_sets_is_hidden_2(self, db: sqlite3.Connection) -> None:
        """norad_follow_id が異なる衛星は is_hidden=2 に自動設定される。"""
        from unittest.mock import patch

        from data.transmitter_manager import TransmitterManager

        db.execute("INSERT INTO satellites (norad_cat_id, name) VALUES (98325, 'TMP-SAT')")
        db.commit()
        mgr = TransmitterManager(db)
        payload = [
            {
                "norad_cat_id": 98325,
                "name": "TMP-SAT",
                "status": "alive",
                "norad_follow_id": 68795,
            }
        ]
        with patch(
            "data.transmitter_manager.httpx.AsyncClient",
            return_value=self._make_mock_client(payload),
        ):
            self._run(mgr.sync_satellite_names())

        row = db.execute("SELECT is_hidden FROM satellites WHERE norad_cat_id = 98325").fetchone()
        assert row["is_hidden"] == 2

    def test_no_norad_follow_id_stays_visible(self, db: sqlite3.Connection) -> None:
        """norad_follow_id が無い衛星は is_hidden が変わらない。"""
        from unittest.mock import patch

        from data.transmitter_manager import TransmitterManager

        db.execute("INSERT INTO satellites (norad_cat_id, name) VALUES (25544, 'ISS')")
        db.commit()
        mgr = TransmitterManager(db)
        payload = [{"norad_cat_id": 25544, "name": "ISS (ZARYA)", "status": "alive"}]
        with patch(
            "data.transmitter_manager.httpx.AsyncClient",
            return_value=self._make_mock_client(payload),
        ):
            self._run(mgr.sync_satellite_names())

        row = db.execute("SELECT is_hidden FROM satellites WHERE norad_cat_id = 25544").fetchone()
        assert row["is_hidden"] == 0

    def test_norad_follow_id_same_as_norad_not_hidden(self, db: sqlite3.Connection) -> None:
        """norad_follow_id が norad_cat_id と同じ場合は自動非表示にならない。"""
        from unittest.mock import patch

        from data.transmitter_manager import TransmitterManager

        db.execute("INSERT INTO satellites (norad_cat_id, name) VALUES (25544, 'ISS')")
        db.commit()
        mgr = TransmitterManager(db)
        payload = [
            {
                "norad_cat_id": 25544,
                "name": "ISS (ZARYA)",
                "status": "alive",
                "norad_follow_id": 25544,
            }
        ]
        with patch(
            "data.transmitter_manager.httpx.AsyncClient",
            return_value=self._make_mock_client(payload),
        ):
            self._run(mgr.sync_satellite_names())

        row = db.execute("SELECT is_hidden FROM satellites WHERE norad_cat_id = 25544").fetchone()
        assert row["is_hidden"] == 0


class TestOrphanSatelliteAutoHide:
    """孤立衛星（transmitter=0件・status=unknown）の自動非表示テスト。"""

    def _run(self, coro):  # type: ignore[no-untyped-def]
        import asyncio

        return asyncio.run(coro)

    def _make_mock_client(self, payload):  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, MagicMock

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = payload
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        return mock_client

    def test_orphan_unknown_gets_hidden(self, db: sqlite3.Connection) -> None:
        """transmitter=0件・status=unknownの孤立衛星は is_hidden=2 になる。"""
        from unittest.mock import patch

        from data.transmitter_manager import TransmitterManager

        # 孤立衛星: transmitter なし・status='unknown'（デフォルト）
        db.execute("INSERT INTO satellites (norad_cat_id, name) VALUES (99999, 'Mode U - Orphan')")
        db.commit()
        mgr = TransmitterManager(db)

        with patch(
            "data.transmitter_manager.httpx.AsyncClient",
            return_value=self._make_mock_client([]),
        ):
            self._run(mgr.sync_from_satnogs())

        row = db.execute("SELECT is_hidden FROM satellites WHERE norad_cat_id = 99999").fetchone()
        assert row["is_hidden"] == 2

    def test_satellite_with_transmitter_not_hidden(self, db: sqlite3.Connection) -> None:
        """transmitter が存在する衛星は孤立判定されない。"""
        from unittest.mock import patch

        from data.transmitter_manager import TransmitterManager

        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status) VALUES (25544, 'ISS', 'alive')"
        )
        db.commit()
        mgr = TransmitterManager(db)

        # transmitter を1件追加
        payload = [
            {
                "uuid": "uuid-iss-fm",
                "norad_cat_id": 25544,
                "description": "ISS FM",
                "downlink_low": 145800000,
                "mode": "FM",
                "alive": True,
            }
        ]
        with patch(
            "data.transmitter_manager.httpx.AsyncClient",
            return_value=self._make_mock_client(payload),
        ):
            self._run(mgr.sync_from_satnogs())

        row = db.execute("SELECT is_hidden FROM satellites WHERE norad_cat_id = 25544").fetchone()
        assert row["is_hidden"] == 0

    def test_alive_satellite_without_transmitter_not_hidden(self, db: sqlite3.Connection) -> None:
        """status='alive' の衛星は transmitter がなくても自動非表示にならない。"""
        from unittest.mock import patch

        from data.transmitter_manager import TransmitterManager

        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status)"
            " VALUES (55555, 'ALIVE-SAT', 'alive')"
        )
        db.commit()
        mgr = TransmitterManager(db)

        with patch(
            "data.transmitter_manager.httpx.AsyncClient",
            return_value=self._make_mock_client([]),
        ):
            self._run(mgr.sync_from_satnogs())

        row = db.execute("SELECT is_hidden FROM satellites WHERE norad_cat_id = 55555").fetchone()
        assert row["is_hidden"] == 0


class TestStatusInheritanceFromNoradFollowId:
    """norad_follow_id による status 引き継ぎテスト。"""

    def _run(self, coro):  # type: ignore[no-untyped-def]
        import asyncio

        return asyncio.run(coro)

    def _make_mock_client(self, payload):  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, MagicMock

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = payload
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        return mock_client

    def test_alive_status_propagated_to_follow_target(self, db: sqlite3.Connection) -> None:
        """remnant(alive) の status が follow 先(unknown) に伝播する。"""
        from unittest.mock import patch

        from data.transmitter_manager import TransmitterManager

        # 仮NORAD(98325)とfollow先の正式NORAD(68795)を登録
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status)"
            " VALUES (98325, 'OrigamiSat-2-temp', 'unknown')"
        )
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status)"
            " VALUES (68795, 'ORIGAMISAT-2', 'unknown')"
        )
        db.commit()
        mgr = TransmitterManager(db)

        payload = [
            {
                "norad_cat_id": 98325,
                "name": "OrigamiSat-2",
                "status": "alive",
                "norad_follow_id": 68795,
            }
        ]
        with patch(
            "data.transmitter_manager.httpx.AsyncClient",
            return_value=self._make_mock_client(payload),
        ):
            self._run(mgr.sync_satellite_names())

        row = db.execute("SELECT status FROM satellites WHERE norad_cat_id = 68795").fetchone()
        assert row["status"] == "alive"

    def test_dead_status_propagated_to_follow_target(self, db: sqlite3.Connection) -> None:
        """remnant(dead) の status が follow 先(unknown) に伝播する。"""
        from unittest.mock import patch

        from data.transmitter_manager import TransmitterManager

        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status)"
            " VALUES (11111, 'OLD-SAT-temp', 'unknown')"
        )
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status)"
            " VALUES (22222, 'OLD-SAT', 'unknown')"
        )
        db.commit()
        mgr = TransmitterManager(db)

        payload = [
            {
                "norad_cat_id": 11111,
                "name": "Old Sat",
                "status": "re-entered",
                "norad_follow_id": 22222,
            }
        ]
        with patch(
            "data.transmitter_manager.httpx.AsyncClient",
            return_value=self._make_mock_client(payload),
        ):
            self._run(mgr.sync_satellite_names())

        row = db.execute("SELECT status FROM satellites WHERE norad_cat_id = 22222").fetchone()
        assert row["status"] == "dead"

    def test_follow_target_alive_not_overwritten_by_unknown(self, db: sqlite3.Connection) -> None:
        """follow 先がすでに alive なら unknown の remnant で上書きされない。"""
        from unittest.mock import patch

        from data.transmitter_manager import TransmitterManager

        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status)"
            " VALUES (33333, 'SAT-temp', 'unknown')"
        )
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name, status)"
            " VALUES (44444, 'SAT-official', 'alive')"
        )
        db.commit()
        mgr = TransmitterManager(db)

        payload = [
            {
                "norad_cat_id": 33333,
                "name": "SAT future",
                "status": "future",  # → 'unknown'
                "norad_follow_id": 44444,
            }
        ]
        with patch(
            "data.transmitter_manager.httpx.AsyncClient",
            return_value=self._make_mock_client(payload),
        ):
            self._run(mgr.sync_satellite_names())

        row = db.execute("SELECT status FROM satellites WHERE norad_cat_id = 44444").fetchone()
        assert row["status"] == "alive"


class TestOverwriteProtection:
    """manual_override / Overwrite Protection の動作テスト。"""

    def test_add_manual_transmitter_default_override_true(self, db: sqlite3.Connection) -> None:
        """add_manual_transmitter はデフォルトで manual_override=1 を保存する。"""
        from data.transmitter_manager import TransmitterManager

        db.execute("INSERT OR IGNORE INTO satellites (norad_cat_id, name) VALUES (25544, 'ISS')")
        db.commit()
        mgr = TransmitterManager(db)
        uid = mgr.add_manual_transmitter(25544, "ISS FM", 145_800_000, "FM")
        row = db.execute(
            "SELECT manual_override FROM transmitters WHERE uuid = ?", (uid,)
        ).fetchone()
        assert row["manual_override"] == 1

    def test_add_manual_transmitter_override_false(self, db: sqlite3.Connection) -> None:
        """manual_override=False を指定すると 0 で保存される。"""
        from data.transmitter_manager import TransmitterManager

        db.execute("INSERT OR IGNORE INTO satellites (norad_cat_id, name) VALUES (25544, 'ISS')")
        db.commit()
        mgr = TransmitterManager(db)
        uid = mgr.add_manual_transmitter(25544, "ISS FM", 145_800_000, "FM", manual_override=False)
        row = db.execute(
            "SELECT manual_override FROM transmitters WHERE uuid = ?", (uid,)
        ).fetchone()
        assert row["manual_override"] == 0

    def test_update_transmitter_can_set_override_false(self, db: sqlite3.Connection) -> None:
        """update_transmitter で manual_override=0 に変更できる。"""
        from data.transmitter_manager import TransmitterManager

        db.execute("INSERT OR IGNORE INTO satellites (norad_cat_id, name) VALUES (25544, 'ISS')")
        db.commit()
        mgr = TransmitterManager(db)
        uid = mgr.add_manual_transmitter(25544, "ISS FM", 145_800_000, "FM")
        mgr.update_transmitter(uid, notes="updated", manual_override=0)
        row = db.execute(
            "SELECT manual_override FROM transmitters WHERE uuid = ?", (uid,)
        ).fetchone()
        assert row["manual_override"] == 0

    def test_update_transmitter_without_override_keeps_existing(
        self, db: sqlite3.Connection
    ) -> None:
        """update_transmitter に manual_override を渡さない場合、既存値が維持される。"""
        from data.transmitter_manager import TransmitterManager

        db.execute("INSERT OR IGNORE INTO satellites (norad_cat_id, name) VALUES (25544, 'ISS')")
        db.commit()
        mgr = TransmitterManager(db)
        uid = mgr.add_manual_transmitter(25544, "ISS FM", 145_800_000, "FM")
        mgr.update_transmitter(uid, notes="changed")
        row = db.execute(
            "SELECT manual_override FROM transmitters WHERE uuid = ?", (uid,)
        ).fetchone()
        assert row["manual_override"] == 1

    def test_transmitter_dialog_overwrite_check_default_true(self, qtbot, db) -> None:
        """TransmitterDialog の Overwrite protection チェックボックスはデフォルト ON。"""
        from data.transmitter_manager import TransmitterManager
        from ui.transmitter_dialog import TransmitterDialog

        mgr = TransmitterManager(db)
        w = TransmitterDialog(mgr, norad_cat_id=25544)
        qtbot.addWidget(w)
        assert hasattr(w, "_overwrite_check")
        assert w._overwrite_check.isChecked()

    def test_transmitter_dialog_prefills_override_false(self, qtbot, db) -> None:
        """manual_override=0 のレコードを編集するとチェックボックスが OFF になる。"""
        from data.transmitter_manager import TransmitterManager
        from ui.transmitter_dialog import TransmitterDialog

        mgr = TransmitterManager(db)
        existing = {
            "uuid": "manual-test",
            "norad_cat_id": 25544,
            "description": "ISS FM",
            "type": "Transponder",
            "downlink_low": 145_800_000,
            "downlink_high": None,
            "uplink_low": None,
            "uplink_high": None,
            "mode": "FM",
            "invert": False,
            "ctcss_tone": None,
            "ctcss_tone_type": None,
            "notes": "",
            "manual_override": 0,
        }
        w = TransmitterDialog(mgr, existing=existing)
        qtbot.addWidget(w)
        assert not w._overwrite_check.isChecked()


class TestCycleSetting:
    """Cycle スピンボックスと QTimer 連携テスト。"""

    def test_cycle_spin_exists_and_default(self, qtbot, db) -> None:
        """RadioControlWidget に _cycle_spin が存在しデフォルト 1000ms。"""
        from ui.radio_control_widget import RadioControlWidget

        w = RadioControlWidget()
        qtbot.addWidget(w)
        assert hasattr(w, "_cycle_spin")
        assert w._cycle_spin.value() == 1000

    def test_cycle_spin_range(self, qtbot, db) -> None:
        """Cycle スピンボックスの範囲が 10〜10000。"""
        from ui.radio_control_widget import RadioControlWidget

        w = RadioControlWidget()
        qtbot.addWidget(w)
        assert w._cycle_spin.minimum() == 10
        assert w._cycle_spin.maximum() == 10000
        assert w._cycle_spin.singleStep() == 10

    def test_set_cycle_updates_spin(self, qtbot, db) -> None:
        """set_cycle() がスピンボックスを更新する（シグナル発火なし）。"""
        from ui.radio_control_widget import RadioControlWidget

        w = RadioControlWidget()
        qtbot.addWidget(w)
        received = []
        w.cycle_changed.connect(received.append)
        w.set_cycle(500)
        assert w._cycle_spin.value() == 500
        assert received == []  # blockSignals により発火しない

    def test_cycle_changed_signal_emitted(self, qtbot, db) -> None:
        """スピンボックス変更時に cycle_changed シグナルが emit される。"""
        from ui.radio_control_widget import RadioControlWidget

        w = RadioControlWidget()
        qtbot.addWidget(w)
        received = []
        w.cycle_changed.connect(received.append)
        w._cycle_spin.setValue(2000)
        assert 2000 in received

    def test_cycle_saved_to_db(self, qtbot, db) -> None:
        """cycle 変更が DB に rig_cycle_ms キーで保存される。"""
        from data.tle_manager import TLEManager
        from ui.main_window import MainWindow

        tle_manager = TLEManager(db)
        w = MainWindow(conn=db, tle_manager=tle_manager)
        qtbot.addWidget(w)
        w._on_cycle_changed(2000)
        row = db.execute("SELECT value FROM app_settings WHERE key = 'rig_cycle_ms'").fetchone()
        assert row is not None
        assert int(row["value"]) == 2000

    def test_cycle_loaded_from_db(self, qtbot, db) -> None:
        """DB に rig_cycle_ms がある場合、起動時に DopplerWorker と UI に反映される。

        rig_cycle_ms は self._timer（表示更新用、常に1秒固定 — see
        test_timer_interval_is_1000ms）ではなく self._doppler_worker の
        間隔を制御する（2026-07-10、Doppler独立スレッド化）。
        """
        from data.tle_manager import TLEManager
        from ui.main_window import MainWindow

        db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('rig_cycle_ms', '500')"
        )
        db.commit()
        tle_manager = TLEManager(db)
        w = MainWindow(conn=db, tle_manager=tle_manager)
        qtbot.addWidget(w)
        assert w._doppler_worker._interval_s == 0.5
        assert w._timer.interval() == 1000
        assert w._radio_control._cycle_spin.value() == 500


class TestTuneLockButtons:
    """T（Tune）/ L（Lock）ボタンテスト。"""

    def test_tune_btn_exists(self, qtbot) -> None:
        from ui.radio_control_widget import RadioControlWidget

        w = RadioControlWidget()
        qtbot.addWidget(w)
        assert hasattr(w, "_tune_btn")

    def test_lock_btn_exists_and_checkable(self, qtbot) -> None:
        from ui.radio_control_widget import RadioControlWidget

        w = RadioControlWidget()
        qtbot.addWidget(w)
        assert hasattr(w, "_lock_btn")
        assert w._lock_btn.isCheckable()

    def test_tune_btn_emits_signal(self, qtbot) -> None:
        from ui.radio_control_widget import RadioControlWidget

        w = RadioControlWidget()
        qtbot.addWidget(w)
        received = []
        w.tune_requested.connect(lambda: received.append(True))
        w._tune_btn.click()
        assert received == [True]

    def test_lock_btn_emits_signal(self, qtbot) -> None:
        from ui.radio_control_widget import RadioControlWidget

        w = RadioControlWidget()
        qtbot.addWidget(w)
        received = []
        w.lock_changed.connect(received.append)
        w._lock_btn.setChecked(True)
        assert True in received

    def test_tune_resets_override(self, qtbot, db) -> None:
        """Tune 押下で _tune_dl_override / _tune_ul_override がセットされる。"""
        from data.tle_manager import TLEManager
        from ui.main_window import MainWindow

        tle_manager = TLEManager(db)
        w = MainWindow(conn=db, tle_manager=tle_manager)
        qtbot.addWidget(w)
        w._current_transmitter = {
            "downlink_low": 145_800_000,
            "downlink_high": 145_950_000,
            "uplink_low": 435_000_000,
            "uplink_high": 435_150_000,
            "invert": False,
        }
        w._on_tune_requested()
        assert w._tune_dl_override == (145_800_000 + 145_950_000) / 2
        assert w._tune_ul_override == (435_000_000 + 435_150_000) / 2

    def test_lock_flag_updated(self, qtbot, db) -> None:
        """Lock ボタントグルで _trsp_lock フラグが更新される。"""
        from data.tle_manager import TLEManager
        from ui.main_window import MainWindow

        tle_manager = TLEManager(db)
        w = MainWindow(conn=db, tle_manager=tle_manager)
        qtbot.addWidget(w)
        assert w._trsp_lock is False
        w._on_lock_changed(True)
        assert w._trsp_lock is True
        w._on_lock_changed(False)
        assert w._trsp_lock is False

    def test_toggle_lock_flips_checked_state_and_emits(self, qtbot) -> None:
        """RadioControlWidget.toggle_lock() (used by MainWindow's Ctrl+L
        hotkey, GitHub Issue #14) flips _lock_btn the same way a real click
        would, including emitting lock_changed."""
        from ui.radio_control_widget import RadioControlWidget

        w = RadioControlWidget()
        qtbot.addWidget(w)
        received = []
        w.lock_changed.connect(received.append)

        assert w._lock_btn.isChecked() is False
        w.toggle_lock()
        assert w._lock_btn.isChecked() is True
        w.toggle_lock()
        assert w._lock_btn.isChecked() is False
        assert received == [True, False]

    def test_ctrl_l_shortcut_toggles_lock(self, qtbot, db) -> None:
        """MainWindow wires Ctrl+L to RadioControlWidget.toggle_lock()
        (GitHub Issue #14: operator's hands are on the rig's VFO knob, not
        the mouse, while manually retuning)."""
        from data.tle_manager import TLEManager
        from ui.main_window import MainWindow

        tle_manager = TLEManager(db)
        w = MainWindow(conn=db, tle_manager=tle_manager)
        qtbot.addWidget(w)

        assert w._trsp_lock is False
        w._lock_shortcut.activated.emit()
        assert w._trsp_lock is True
        w._lock_shortcut.activated.emit()
        assert w._trsp_lock is False


class TestLockDialFeedback:
    """Lock (L button) dial feedback: _lock_watch_cycle() (pre-Connect) and
    the _rig_send()/_doppler_cycle() integration for the connected case.

    Design (2026-07-16, confirmed live against an FTX-1F, RS-44 and JO-97
    passes): while Lock is on, DL is under the operator's SOLE manual
    control -- the software only reads it (never writes it) each cycle,
    and derives UL directly from that reading. An earlier design kept
    actively writing DL (with a detected delta folded in) even while Lock
    was on; that periodically reasserted an absolute DL frequency computed
    from a read taken one CAT round trip (~150-300ms) earlier, which
    visibly fought the operator's real-time VFO motion -- any further
    turning between that read and the following write got overwritten
    mid-gesture, reported live as "the frequency reverts even while I'm
    still turning the dial, I can't even tell how far I turned it". The
    only way to stop competing with the operator for control of DL is to
    stop writing it at all while Lock is on.

    The manual-retune offset is recomputed directly from each fresh
    reading (offset = live_dl - dl_baseline) rather than accumulated
    incrementally against a "last written" tracker -- simpler and safer
    now that DL is never written, since there is no "did our own write
    actually land" ambiguity to get out of sync over (that ambiguity was
    the root cause of an earlier bug where ordinary sub-1Hz-per-cycle
    Doppler drift got misdetected as manual retuning, because
    set_vfo_frequencies()'s own write throttle and MainWindow's separate
    tracker of "what we last wrote" could silently diverge).

    The offset persists across Lock ON/OFF toggles (confirmed explicitly
    with the user): turning Lock off must resume Doppler correction from
    wherever the operator left DL, not snap back to the pure nominal-based
    trajectory. It resets only on selecting a different transponder or
    pressing the T (Tune) button.

    Read mechanism and safety findings (V command corrupts the rig's
    actual Main/Sub role assignment; bare "f"/"i" is the only verified-safe
    read path; scoped to Yaesu-CAT NET-mode rigs only) were established via
    live hardware testing against an FTX-1F on 2026-07-15 and remain
    unchanged by this redesign.
    """

    def _make_window(self, qtbot, db):
        from data.tle_manager import TLEManager
        from ui.main_window import MainWindow

        tle_manager = TLEManager(db)
        w = MainWindow(conn=db, tle_manager=tle_manager)
        qtbot.addWidget(w)
        return w

    def _make_yaesu_rig(self, connected: bool):
        from rig.controller import HamlibNetController, RigState

        ctrl = HamlibNetController(host="localhost", port=4532, ctcss_method="ftx1")
        if connected:
            ctrl._sock = MagicMock()
            with ctrl._lock:
                ctrl._state = RigState.CONNECTED
        return ctrl

    def _make_ftx1_direct_rig(self, connected: bool):
        from rig.controller import HamlibDirectController, RigState

        ctrl = HamlibDirectController(model_id=1051, port="/dev/null")
        if connected:
            ctrl._rig = MagicMock()
            with ctrl._lock:
                ctrl._state = RigState.CONNECTED
        return ctrl

    def _make_ft991_direct_rig(self, connected: bool):
        from rig.controller import HamlibDirectController, RigState

        ctrl = HamlibDirectController(model_id=1035, port="/dev/null")  # FT-991
        if connected:
            ctrl._rig = MagicMock()
            with ctrl._lock:
                ctrl._state = RigState.CONNECTED
        return ctrl

    def _make_ic705_direct_rig(self, connected: bool):
        from rig.controller import HamlibDirectController, RigState

        ctrl = HamlibDirectController(model_id=3085, port="/dev/null")  # IC-705
        if connected:
            ctrl._rig = MagicMock()
            with ctrl._lock:
                ctrl._state = RigState.CONNECTED
        return ctrl

    def _make_satmode_direct_rig(self, connected: bool):
        from rig.controller import HamlibDirectController, RigState

        ctrl = HamlibDirectController(model_id=3081, port="/dev/null")  # IC-9700
        if connected:
            ctrl._rig = MagicMock()
            with ctrl._lock:
                ctrl._state = RigState.CONNECTED
        return ctrl

    def _fake_engine(self, rr: float):
        class _FakeEngine:
            def observe(self, norad: int) -> Observation:
                return Observation(
                    norad_cat_id=norad,
                    timestamp=datetime.now(UTC),
                    elevation_deg=45.0,
                    azimuth_deg=180.0,
                    range_km=1000.0,
                    range_rate_km_s=rr,
                    is_above_horizon=True,
                )

        return _FakeEngine()

    _TRANSMITTER = {
        "downlink_low": 145_800_000,
        "downlink_high": 145_950_000,
        "uplink_low": 435_000_000,
        "uplink_high": 435_150_000,
        "invert": False,
        "mode": "USB",
    }
    _TRANSMITTER_INVERTED = {**_TRANSMITTER, "invert": True}

    # -- _lock_watch_cycle(): pre-Connect, direct-computation polling --

    def test_lock_watch_noop_when_not_locked(self, qtbot, db) -> None:
        w = self._make_window(qtbot, db)
        rig = self._make_yaesu_rig(connected=False)
        rig.read_dl_ul_independent = MagicMock(return_value=(145_800_080.0, 435_000_000.0))
        w._rig_controller = rig
        w._trsp_lock = False
        w._lock_watch_cycle()
        rig.read_dl_ul_independent.assert_not_called()

    def test_lock_watch_noop_for_non_net_controller(self, qtbot, db) -> None:
        w = self._make_window(qtbot, db)
        w._rig_controller = MagicMock()  # not a HamlibNetController instance
        w._trsp_lock = True
        w._lock_watch_cycle()  # must not raise

    def test_lock_watch_noop_when_no_rig(self, qtbot, db) -> None:
        w = self._make_window(qtbot, db)
        w._rig_controller = None
        w._trsp_lock = True
        w._lock_watch_cycle()  # must not raise

    def test_lock_watch_noop_for_non_yaesu_cat(self, qtbot, db) -> None:
        """Scoped to ctcss_method 'ftx1'/'ft991' -- the only configuration
        verified live to support V-free frequency readback. Other NET rigs
        (satmode Icom, generic/IC-705) keep the existing Doppler-driven-
        only Lock behaviour and must not be touched by this poller."""
        from rig.controller import HamlibNetController

        w = self._make_window(qtbot, db)
        rig = HamlibNetController(host="localhost", port=4532, ctcss_method="icom_civ")
        rig.read_dl_ul_independent = MagicMock(return_value=(145_800_080.0, 435_000_000.0))
        w._rig_controller = rig
        w._trsp_lock = True
        w._lock_watch_cycle()
        rig.read_dl_ul_independent.assert_not_called()

    def test_lock_watch_noop_for_generic_net_hamlib(self, qtbot, db) -> None:
        """The generic NET "hamlib" bucket (2026-07-20) is connected-case
        only -- read_dl_ul_independent() itself still only supports
        "ftx1"/"ft991", so this pre-Connect poller must not even attempt
        it for a generic NET rig (avoids a misleading "read failed" log
        every cycle for a rig that was never eligible for this path)."""
        from rig.controller import HamlibNetController

        w = self._make_window(qtbot, db)
        rig = HamlibNetController(host="localhost", port=4532, ctcss_method="hamlib")
        rig.read_dl_ul_independent = MagicMock(return_value=(145_800_080.0, 435_000_000.0))
        w._rig_controller = rig
        w._trsp_lock = True
        w._lock_watch_cycle()
        rig.read_dl_ul_independent.assert_not_called()

    def test_lock_watch_noop_for_satmode_net(self, qtbot, db) -> None:
        """Satmode NET (2026-07-20) is connected-case only too, for the
        same reason as the generic "hamlib" bucket above."""
        from rig.controller import HamlibNetController

        w = self._make_window(qtbot, db)
        rig = HamlibNetController(
            host="localhost", port=4532, ctcss_method="hamlib", is_satmode_rig=True
        )
        rig.read_dl_ul_independent = MagicMock(return_value=(145_800_080.0, 435_000_000.0))
        w._rig_controller = rig
        w._trsp_lock = True
        w._lock_watch_cycle()
        rig.read_dl_ul_independent.assert_not_called()

    def test_lock_watch_noop_once_connected(self, qtbot, db) -> None:
        """The connected case is handled by _rig_send() on the Doppler
        cycle's own cadence -- this method must do nothing once connected,
        not even a read."""
        w = self._make_window(qtbot, db)
        rig = self._make_yaesu_rig(connected=True)
        rig.read_dl_ul_independent = MagicMock(return_value=(145_800_080.0, 435_000_000.0))
        w._rig_controller = rig
        w._trsp_lock = True
        w._lock_watch_cycle()
        rig.read_dl_ul_independent.assert_not_called()

    def test_lock_watch_noop_without_selected_satellite(self, qtbot, db) -> None:
        w = self._make_window(qtbot, db)
        rig = self._make_yaesu_rig(connected=False)
        rig.read_dl_ul_independent = MagicMock(return_value=(145_800_080.0, 435_000_000.0))
        w._rig_controller = rig
        w._trsp_lock = True
        w._selected_norad = None
        w._lock_watch_cycle()
        rig.read_dl_ul_independent.assert_not_called()

    def test_lock_watch_computes_offset_directly_no_write(self, qtbot, db) -> None:
        """Confirmed explicitly with the user (2026-07-16): Lock ON stops
        ALL writes to the rig, not just DL -- this method only ever reads
        and updates self._dial_feedback_offset_hz for later use once Lock
        is released."""
        w = self._make_window(qtbot, db)
        rig = self._make_yaesu_rig(connected=False)
        # rr=0.0 -> dl_baseline == downlink_low exactly.
        rig.read_dl_ul_independent = MagicMock(return_value=(145_800_080.0, 435_000_000.0))
        w._rig_controller = rig
        w._trsp_lock = True
        w._engine = self._fake_engine(rr=0.0)
        w._selected_norad = 99999
        w._current_transmitter = dict(self._TRANSMITTER)
        w._dial_feedback_offset_hz = 0.0

        w._lock_watch_cycle()

        assert w._dial_feedback_offset_hz == 80.0

    def test_lock_watch_discards_reading_close_to_ul_crosscheck(self, qtbot, db) -> None:
        """Confirmed live (2026-07-15, FTX-1F): a stale/broken DL reading
        can return the UL value instead -- this must be discarded, not
        misread as a huge manual retune."""
        w = self._make_window(qtbot, db)
        rig = self._make_yaesu_rig(connected=False)
        rig.read_dl_ul_independent = MagicMock(return_value=(435_000_000.0, 435_000_000.0))
        w._rig_controller = rig
        w._trsp_lock = True
        w._engine = self._fake_engine(rr=0.0)
        w._selected_norad = 99999
        w._current_transmitter = dict(self._TRANSMITTER)
        w._dial_feedback_offset_hz = 12.3

        w._lock_watch_cycle()

        assert w._dial_feedback_offset_hz == 12.3  # unchanged

    def test_lock_watch_ignores_implausible_jump(self, qtbot, db) -> None:
        w = self._make_window(qtbot, db)
        rig = self._make_yaesu_rig(connected=False)
        # +300kHz -- comfortably past _DIAL_FEEDBACK_SANITY_HZ (200kHz, raised
        # 2026-07-22 to cover a full-passband sweep on wide transponders).
        rig.read_dl_ul_independent = MagicMock(return_value=(146_100_000.0, 435_000_000.0))
        w._rig_controller = rig
        w._trsp_lock = True
        w._engine = self._fake_engine(rr=0.0)
        w._selected_norad = 99999
        w._current_transmitter = dict(self._TRANSMITTER)
        w._dial_feedback_offset_hz = 0.0

        w._lock_watch_cycle()

        assert w._dial_feedback_offset_hz == 0.0

    def test_lock_watch_none_reading_is_noop(self, qtbot, db) -> None:
        w = self._make_window(qtbot, db)
        rig = self._make_yaesu_rig(connected=False)
        rig.read_dl_ul_independent = MagicMock(return_value=None)
        w._rig_controller = rig
        w._trsp_lock = True
        w._engine = self._fake_engine(rr=0.0)
        w._selected_norad = 99999
        w._current_transmitter = dict(self._TRANSMITTER)
        w._dial_feedback_offset_hz = 0.0

        w._lock_watch_cycle()

        assert w._dial_feedback_offset_hz == 0.0

    # -- _rig_send() (inside _doppler_cycle()): connected-case integration --

    class _SyncThread:
        """Runs the target synchronously instead of spawning a real
        thread, so _rig_send()'s side effects can be asserted on
        deterministically without waiting on a background thread."""

        def __init__(self, target=None, daemon=None, **kwargs) -> None:
            self._target = target

        def start(self) -> None:
            if self._target is not None:
                self._target()

    def _run_doppler_cycle(self, w, rig, rr: float, transmitter: dict, trsp_lock: bool) -> list:
        from unittest.mock import patch

        w._rig_controller = rig
        w._rig2_controller = None
        w._trsp_lock = trsp_lock
        w._engine = self._fake_engine(rr)
        w._selected_norad = 99999
        w._current_transmitter = transmitter

        received: list = []
        w._doppler_computed.connect(received.append)
        with patch("ui.main_window.threading.Thread", self._SyncThread):
            w._doppler_cycle()
        return received

    def test_rig_send_never_writes_dl_but_keeps_writing_ul_while_locked(self, qtbot, db) -> None:
        """Confirmed explicitly with the user (2026-07-16): Lock ON stops
        DL writes only. UL has no competing physical control (the
        operator only touches the DL VFO), so it keeps being written
        every cycle -- using the same value already computed for the
        display, one cycle behind this read (see _rig_send()'s comment)."""
        w = self._make_window(qtbot, db)
        rig = self._make_yaesu_rig(connected=True)
        rig.get_frequency = MagicMock(return_value=145_800_080.0)  # +80Hz manual retune
        rig.get_split_frequency = MagicMock(return_value=435_000_000.0)
        rig.set_vfo_frequencies = MagicMock(return_value=True)
        w._dial_feedback_offset_hz = 0.0

        self._run_doppler_cycle(w, rig, rr=0.0, transmitter=dict(self._TRANSMITTER), trsp_lock=True)

        # offset was still 0.0 when _doppler_cycle() computed this cycle's
        # ul -- the +80Hz detected by this same cycle's read is not folded
        # in until the *next* cycle (matches what the display shows).
        rig.set_vfo_frequencies.assert_called_once_with(None, 435_000_000.0)
        assert w._dial_feedback_offset_hz == 80.0

    def test_rig_send_no_retune_keeps_offset_unchanged(self, qtbot, db) -> None:
        w = self._make_window(qtbot, db)
        rig = self._make_yaesu_rig(connected=True)
        rig.get_frequency = MagicMock(return_value=145_800_000.0)  # matches baseline exactly
        rig.get_split_frequency = MagicMock(return_value=435_000_000.0)
        rig.set_vfo_frequencies = MagicMock(return_value=True)
        w._dial_feedback_offset_hz = 0.0

        self._run_doppler_cycle(w, rig, rr=0.0, transmitter=dict(self._TRANSMITTER), trsp_lock=True)

        rig.set_vfo_frequencies.assert_called_once_with(None, 435_000_000.0)
        assert w._dial_feedback_offset_hz == 0.0

    def test_rig_send_discards_reading_close_to_ul_crosscheck(self, qtbot, db) -> None:
        w = self._make_window(qtbot, db)
        rig = self._make_yaesu_rig(connected=True)
        rig.get_frequency = MagicMock(return_value=435_000_000.0)  # == UL, broken read
        rig.get_split_frequency = MagicMock(return_value=435_000_000.0)
        rig.set_vfo_frequencies = MagicMock(return_value=True)
        w._dial_feedback_offset_hz = 5.0

        self._run_doppler_cycle(w, rig, rr=0.0, transmitter=dict(self._TRANSMITTER), trsp_lock=True)

        assert w._dial_feedback_offset_hz == 5.0  # unchanged
        # UL write still happens (using the pre-existing offset), just the
        # offset itself isn't updated from this discarded reading.
        rig.set_vfo_frequencies.assert_called_once_with(None, 435_000_005.0)

    def test_rig_send_ignores_implausible_jump(self, qtbot, db) -> None:
        w = self._make_window(qtbot, db)
        rig = self._make_yaesu_rig(connected=True)
        # +300kHz -- comfortably past _DIAL_FEEDBACK_SANITY_HZ (200kHz, raised
        # 2026-07-22 to cover a full-passband sweep on wide transponders).
        rig.get_frequency = MagicMock(return_value=146_100_000.0)
        rig.get_split_frequency = MagicMock(return_value=435_000_000.0)
        rig.set_vfo_frequencies = MagicMock(return_value=True)
        w._dial_feedback_offset_hz = 0.0

        self._run_doppler_cycle(w, rig, rr=0.0, transmitter=dict(self._TRANSMITTER), trsp_lock=True)

        assert w._dial_feedback_offset_hz == 0.0
        rig.set_vfo_frequencies.assert_called_once_with(None, 435_000_000.0)

    def test_rig_send_writes_dl_normally_when_lock_off(self, qtbot, db) -> None:
        """With Lock off, do_dial_feedback is False -- no read happens, and
        DL is written exactly as it always was (including any persisted
        offset via _doppler_cycle()'s unconditional fold, tested
        separately below)."""
        w = self._make_window(qtbot, db)
        rig = self._make_yaesu_rig(connected=True)
        rig.get_frequency = MagicMock(return_value=145_800_080.0)
        rig.get_split_frequency = MagicMock(return_value=435_000_000.0)
        rig.set_vfo_frequencies = MagicMock(return_value=True)
        w._dial_feedback_offset_hz = 0.0

        self._run_doppler_cycle(
            w, rig, rr=0.0, transmitter=dict(self._TRANSMITTER), trsp_lock=False
        )

        rig.get_frequency.assert_not_called()
        rig.get_split_frequency.assert_not_called()
        called_dl, called_ul = rig.set_vfo_frequencies.call_args[0]
        assert called_dl == 145_800_000.0
        assert called_ul == 435_000_000.0

    # -- NET mode generic "hamlib" bucket (2026-07-20) --

    def test_is_dial_feedback_rig_true_for_generic_net_hamlib(self, qtbot, db) -> None:
        """ctcss_method "hamlib" (the default) covers IC-705 NET mode and
        any other generic NET rig indistinguishably -- Rig Settings has no
        per-model ctcss_method value, unlike Direct mode's model_id."""
        from rig.controller import HamlibNetController

        w = self._make_window(qtbot, db)
        rig = HamlibNetController(host="localhost", port=4532, ctcss_method="hamlib")
        assert w._is_dial_feedback_rig(rig) is True

    def test_is_dial_feedback_rig_true_for_satmode_net(self, qtbot, db) -> None:
        """Satmode Icom NET rigs ("Icom SAT mode rig" checkbox checked) get
        their own explicit branch, independent of ctcss_method. Reads DL
        only -- never reads or writes UL at all while Lock is on (2026-07-22,
        confirmed live on IC-9100 NET mode: rigctld's "i" (get_split_freq)
        command internally hits Hamlib's rig_get_split_freq() "Assisted
        mode" VFO-swap-and-restore path for this rig, and the restore step
        can silently fail, leaving rigctld's own current-VFO stuck on Sub --
        observed live as "works on first Connect, unstable after
        reconnecting"). See _is_dial_feedback_rig()'s docstring."""
        from rig.controller import HamlibNetController

        w = self._make_window(qtbot, db)
        rig = HamlibNetController(
            host="localhost", port=4532, ctcss_method="hamlib", is_satmode_rig=True
        )
        assert w._is_dial_feedback_rig(rig) is True

    def test_rig_send_satmode_net_reads_dl_only_writes_nothing(self, qtbot, db) -> None:
        """Satmode NET mode reads DL only, via the same bare get_frequency()
        used by non-satmode NET rigs -- never calls get_split_frequency()
        ("i") or writes UL at all while Lock is on (2026-07-22, confirmed
        live on IC-9100 NET mode: the "i" command's Hamlib
        rig_get_split_freq() Assisted-mode VFO-swap-and-restore silently
        failed to restore Main on repeated calls, corrupting rigctld's own
        current-VFO tracking -- exactly the "implausible DL reading
        145840000.0 Hz" (a UL-band value) symptom seen live). Same fix and
        same rationale as the Direct-mode satmode branch: rely on the rig's
        own confirmed hardware Main->Sub auto-tracking instead."""
        from rig.controller import HamlibNetController, RigState

        w = self._make_window(qtbot, db)
        rig = HamlibNetController(
            host="localhost", port=4532, ctcss_method="hamlib", is_satmode_rig=True
        )
        rig._sock = MagicMock()
        with rig._lock:
            rig._state = RigState.CONNECTED
        rig.get_frequency = MagicMock(return_value=145_800_080.0)  # +80Hz manual retune
        rig.get_split_frequency = MagicMock(return_value=435_000_000.0)
        rig.set_vfo_frequencies = MagicMock(return_value=True)
        w._dial_feedback_offset_hz = 0.0

        self._run_doppler_cycle(w, rig, rr=0.0, transmitter=dict(self._TRANSMITTER), trsp_lock=True)

        rig.get_frequency.assert_called_once_with()
        rig.get_split_frequency.assert_not_called()
        rig.set_vfo_frequencies.assert_not_called()
        assert w._dial_feedback_offset_hz == 80.0

    def test_rig_send_satmode_net_read_failure_leaves_offset_unchanged(self, qtbot, db) -> None:
        from rig.controller import HamlibNetController, RigState

        w = self._make_window(qtbot, db)
        rig = HamlibNetController(
            host="localhost", port=4532, ctcss_method="hamlib", is_satmode_rig=True
        )
        rig._sock = MagicMock()
        with rig._lock:
            rig._state = RigState.CONNECTED
        rig.get_frequency = MagicMock(return_value=-1.0)
        rig.get_split_frequency = MagicMock(return_value=435_000_000.0)
        rig.set_vfo_frequencies = MagicMock(return_value=True)
        w._dial_feedback_offset_hz = 12.3

        self._run_doppler_cycle(w, rig, rr=0.0, transmitter=dict(self._TRANSMITTER), trsp_lock=True)

        rig.get_split_frequency.assert_not_called()
        rig.set_vfo_frequencies.assert_not_called()
        assert w._dial_feedback_offset_hz == 12.3

    def test_rig_send_satmode_net_ignores_implausible_jump(self, qtbot, db) -> None:
        from rig.controller import HamlibNetController, RigState

        w = self._make_window(qtbot, db)
        rig = HamlibNetController(
            host="localhost", port=4532, ctcss_method="hamlib", is_satmode_rig=True
        )
        rig._sock = MagicMock()
        with rig._lock:
            rig._state = RigState.CONNECTED
        # +300kHz -- comfortably past _DIAL_FEEDBACK_SANITY_HZ (200kHz).
        rig.get_frequency = MagicMock(return_value=146_100_000.0)
        rig.get_split_frequency = MagicMock(return_value=435_000_000.0)
        rig.set_vfo_frequencies = MagicMock(return_value=True)
        w._dial_feedback_offset_hz = 0.0

        self._run_doppler_cycle(w, rig, rr=0.0, transmitter=dict(self._TRANSMITTER), trsp_lock=True)

        rig.get_split_frequency.assert_not_called()
        rig.set_vfo_frequencies.assert_not_called()
        assert w._dial_feedback_offset_hz == 0.0

    def test_is_dial_feedback_rig_false_for_custom_cat_net(self, qtbot, db) -> None:
        """ctcss_method "custom_cat" is a distinct, unanalysed scheme --
        not folded into the generic "hamlib" bucket."""
        from rig.controller import HamlibNetController

        w = self._make_window(qtbot, db)
        rig = HamlibNetController(host="localhost", port=4532, ctcss_method="custom_cat")
        assert w._is_dial_feedback_rig(rig) is False

    def test_rig_send_generic_net_hamlib_never_writes_dl_but_keeps_writing_ul(
        self, qtbot, db
    ) -> None:
        """Connected-case integration for the generic NET "hamlib" bucket
        -- get_frequency()/get_split_frequency()/set_vfo_frequencies() are
        all already ctcss_method-agnostic, so this works the same as the
        "ftx1" NET-mode test above with no additional code path."""
        w = self._make_window(qtbot, db)
        rig = self._make_yaesu_rig(connected=True)
        rig._ctcss_method = "hamlib"
        rig.get_frequency = MagicMock(return_value=145_800_080.0)  # +80Hz manual retune
        rig.get_split_frequency = MagicMock(return_value=435_000_000.0)
        rig.set_vfo_frequencies = MagicMock(return_value=True)
        w._dial_feedback_offset_hz = 0.0

        self._run_doppler_cycle(w, rig, rr=0.0, transmitter=dict(self._TRANSMITTER), trsp_lock=True)

        rig.set_vfo_frequencies.assert_called_once_with(None, 435_000_000.0)
        assert w._dial_feedback_offset_hz == 80.0

    # -- Direct mode (FTX-1F): same connected-case integration (2026-07-20) --

    def test_is_dial_feedback_rig_true_for_ftx1_direct(self, qtbot, db) -> None:
        """Confirmed via Hamlib source (rigs/yaesu/ftx1/ftx1.c:
        .targetable_vfo = RIG_TARGETABLE_ALL) that get_freq(VFOA)/
        get_freq(VFOB) carry none of NET mode's "V" corruption risk for
        this specific model."""
        w = self._make_window(qtbot, db)
        rig = self._make_ftx1_direct_rig(connected=True)
        assert w._is_dial_feedback_rig(rig) is True

    def test_is_dial_feedback_rig_true_for_satmode_direct(self, qtbot, db) -> None:
        """Satmode Direct-mode rigs (IC-9100/9700/etc., 2026-07-20) get
        Lock too, for cross-band/linear-transponder use only -- same-band
        FM/repeater use is out of scope by explicit user decision, so its
        correctness there is untested and not a concern. See
        _is_dial_feedback_rig()'s docstring for why RIG_VFO_MAIN/
        RIG_VFO_TX reads (used by _rig_send() for this branch) are safe."""
        from rig.controller import HamlibDirectController

        w = self._make_window(qtbot, db)
        rig = HamlibDirectController(model_id=3081, port="/dev/null")  # IC-9700 satmode
        assert w._is_dial_feedback_rig(rig) is True

    def test_is_dial_feedback_rig_true_for_ic705_direct(self, qtbot, db) -> None:
        """Confirmed via Hamlib source (rigs/icom/ic7300.c: .targetable_vfo
        includes RIG_TARGETABLE_FREQ; rigs/icom/icom.c: icom_get_freq()
        uses CI-V command 0x25 to read the requested VFO directly, no VFO
        swap, since IC-705 has no Main/Sub+A/B combination that would force
        one) -- same safety property as FTX-1F/FT-991, confirmed
        2026-07-20. Deliberately its own explicit branch in
        _is_dial_feedback_rig(), not merged into the generic fallback."""
        from rig.controller import HamlibDirectController

        w = self._make_window(qtbot, db)
        rig = HamlibDirectController(model_id=3085, port="/dev/null")  # IC-705
        assert w._is_dial_feedback_rig(rig) is True

    def test_is_dial_feedback_rig_true_for_generic_non_satmode_direct(self, qtbot, db) -> None:
        """Any other non-satmode Direct-mode rig also gets dial feedback
        as a best-effort generic fallback (per explicit user decision
        2026-07-20), even though get_freq(VFOA)/get_freq(VFOB) safety for
        an arbitrary unverified rig's Hamlib backend has not been checked
        the way FTX-1F/FT-991/IC-705 were -- see _is_dial_feedback_rig()'s
        docstring."""
        from rig.controller import HamlibDirectController

        w = self._make_window(qtbot, db)
        rig = HamlibDirectController(model_id=9999, port="/dev/null")  # arbitrary, unlisted
        assert w._is_dial_feedback_rig(rig) is True

    def test_rig_send_direct_ftx1_never_writes_dl_but_keeps_writing_ul_while_locked(
        self, qtbot, db
    ) -> None:
        """Same behaviour as the NET-mode FTX-1F test above, but for
        HamlibDirectController. Reads use get_frequency("VFOA")/
        get_frequency("VFOB") instead of NET mode's get_frequency()/
        get_split_frequency() -- this path never calls set_split_freq/
        get_split_freq at all, so the Hamlib cache-slot-sharing quirk
        documented in ftx1_vfo.c (which only affects the split_freq
        calls) does not apply and read order does not matter."""
        w = self._make_window(qtbot, db)
        rig = self._make_ftx1_direct_rig(connected=True)

        def _fake_get_frequency(vfo: str = "VFOA") -> float:
            return 145_800_080.0 if vfo == "VFOA" else 435_000_000.0

        rig.get_frequency = MagicMock(side_effect=_fake_get_frequency)
        rig.set_vfo_frequencies = MagicMock(return_value=True)
        w._dial_feedback_offset_hz = 0.0

        self._run_doppler_cycle(w, rig, rr=0.0, transmitter=dict(self._TRANSMITTER), trsp_lock=True)

        rig.get_frequency.assert_any_call("VFOA")
        rig.get_frequency.assert_any_call("VFOB")
        # offset was still 0.0 when _doppler_cycle() computed this cycle's
        # ul -- same one-cycle-behind timing as the NET-mode case.
        rig.set_vfo_frequencies.assert_called_once_with(None, 435_000_000.0)
        assert w._dial_feedback_offset_hz == 80.0

    # -- Direct mode (FT-991/FT-991A): same integration (2026-07-20) --

    def test_is_dial_feedback_rig_true_for_ft991_direct(self, qtbot, db) -> None:
        """Confirmed via Hamlib source (rigs/yaesu/ft991.c: .targetable_vfo
        = RIG_TARGETABLE_FREQ; rigs/yaesu/newcat.c: newcat_get_freq() sends
        FA;/FB; for the requested VFO with no VFO switch) -- same safety
        property as FTX-1F, confirmed 2026-07-20."""
        w = self._make_window(qtbot, db)
        rig = self._make_ft991_direct_rig(connected=True)
        assert w._is_dial_feedback_rig(rig) is True

    def test_rig_send_direct_ft991_never_writes_dl_but_keeps_writing_ul_while_locked(
        self, qtbot, db
    ) -> None:
        """Same behaviour as the FTX-1F Direct-mode test above. FT-991's
        Direct-mode UL write never calls set_split_freq()/get_split_freq()
        either (raw CAT "FB;" bypassing Hamlib entirely), so read order
        does not matter here either."""
        w = self._make_window(qtbot, db)
        rig = self._make_ft991_direct_rig(connected=True)

        def _fake_get_frequency(vfo: str = "VFOA") -> float:
            return 145_800_080.0 if vfo == "VFOA" else 435_000_000.0

        rig.get_frequency = MagicMock(side_effect=_fake_get_frequency)
        rig.set_vfo_frequencies = MagicMock(return_value=True)
        w._dial_feedback_offset_hz = 0.0

        self._run_doppler_cycle(w, rig, rr=0.0, transmitter=dict(self._TRANSMITTER), trsp_lock=True)

        rig.get_frequency.assert_any_call("VFOA")
        rig.get_frequency.assert_any_call("VFOB")
        rig.set_vfo_frequencies.assert_called_once_with(None, 435_000_000.0)
        assert w._dial_feedback_offset_hz == 80.0

    # -- Direct mode (IC-705): same integration (2026-07-20) --

    def test_rig_send_direct_ic705_never_writes_dl_but_keeps_writing_ul_while_locked(
        self, qtbot, db
    ) -> None:
        """Same behaviour as the FTX-1F/FT-991 Direct-mode tests above.
        IC-705's Direct-mode UL write also never calls set_split_freq()/
        get_split_freq() (plain set_freq(VFOB), same as FTX-1F), so read
        order does not matter here either."""
        w = self._make_window(qtbot, db)
        rig = self._make_ic705_direct_rig(connected=True)

        def _fake_get_frequency(vfo: str = "VFOA") -> float:
            return 145_800_080.0 if vfo == "VFOA" else 435_000_000.0

        rig.get_frequency = MagicMock(side_effect=_fake_get_frequency)
        rig.set_vfo_frequencies = MagicMock(return_value=True)
        w._dial_feedback_offset_hz = 0.0

        self._run_doppler_cycle(w, rig, rr=0.0, transmitter=dict(self._TRANSMITTER), trsp_lock=True)

        rig.get_frequency.assert_any_call("VFOA")
        rig.get_frequency.assert_any_call("VFOB")
        rig.set_vfo_frequencies.assert_called_once_with(None, 435_000_000.0)
        assert w._dial_feedback_offset_hz == 80.0

    # -- Direct mode (satmode, cross-band): same integration (2026-07-20) --

    def test_rig_send_direct_satmode_reads_main_only_writes_nothing(self, qtbot, db) -> None:
        """Satmode Direct mode reads DL only, via "Main" -- never reads or
        writes UL/"TX" at all while Lock is on (2026-07-22, confirmed live
        on IC-9100: reading "TX" gets silently remapped to RIG_VFO_SUB by
        Hamlib's vfo_fixup() before icom_get_freq() ever runs, forcing a
        real VFO switch every cycle that corrupted Hamlib's internal VFO
        tracking badly enough to freeze DL reads and hang the serial link
        when a write resumed after Lock was turned back off. Separately,
        satmode rigs already move Sub/TX automatically in hardware
        whenever Main/RX is retuned, confirmed live by the user, so there
        is nothing useful for software to add by writing UL anyway)."""
        w = self._make_window(qtbot, db)
        rig = self._make_satmode_direct_rig(connected=True)
        rig.get_frequency = MagicMock(return_value=145_800_080.0)  # +80Hz manual retune
        rig.set_vfo_frequencies = MagicMock(return_value=True)
        w._dial_feedback_offset_hz = 0.0

        self._run_doppler_cycle(w, rig, rr=0.0, transmitter=dict(self._TRANSMITTER), trsp_lock=True)

        rig.get_frequency.assert_called_once_with("Main")
        rig.set_vfo_frequencies.assert_not_called()
        assert w._dial_feedback_offset_hz == 80.0

    def test_rig_send_direct_satmode_read_failure_leaves_offset_unchanged(self, qtbot, db) -> None:
        w = self._make_window(qtbot, db)
        rig = self._make_satmode_direct_rig(connected=True)
        rig.get_frequency = MagicMock(return_value=-1.0)
        rig.set_vfo_frequencies = MagicMock(return_value=True)
        w._dial_feedback_offset_hz = 12.3

        self._run_doppler_cycle(w, rig, rr=0.0, transmitter=dict(self._TRANSMITTER), trsp_lock=True)

        rig.set_vfo_frequencies.assert_not_called()
        assert w._dial_feedback_offset_hz == 12.3

    def test_rig_send_direct_satmode_ignores_implausible_jump(self, qtbot, db) -> None:
        w = self._make_window(qtbot, db)
        rig = self._make_satmode_direct_rig(connected=True)
        # +300kHz -- comfortably past _DIAL_FEEDBACK_SANITY_HZ (200kHz, raised
        # 2026-07-22 to cover a full-passband sweep on wide transponders).
        rig.get_frequency = MagicMock(return_value=146_100_000.0)
        rig.set_vfo_frequencies = MagicMock(return_value=True)
        w._dial_feedback_offset_hz = 0.0

        self._run_doppler_cycle(w, rig, rr=0.0, transmitter=dict(self._TRANSMITTER), trsp_lock=True)

        rig.set_vfo_frequencies.assert_not_called()
        assert w._dial_feedback_offset_hz == 0.0

    # -- reset points --

    def test_lock_toggle_does_not_reset_offset(self, qtbot, db) -> None:
        """Confirmed explicitly with the user (2026-07-16): turning Lock
        off must resume Doppler correction from wherever DL was left, not
        reset to 0 and snap back to the pure nominal-based trajectory."""
        w = self._make_window(qtbot, db)
        w._dial_feedback_offset_hz = 42.0
        w._on_lock_changed(False)
        assert w._dial_feedback_offset_hz == 42.0
        w._on_lock_changed(True)
        assert w._dial_feedback_offset_hz == 42.0

    def test_tune_button_resets_offset(self, qtbot, db) -> None:
        w = self._make_window(qtbot, db)
        w._current_transmitter = dict(self._TRANSMITTER)
        w._dial_feedback_offset_hz = 123.0
        w._on_tune_requested()
        assert w._dial_feedback_offset_hz == 0.0

    # -- _doppler_cycle(): offset folding (unconditional, Lock on or off) --

    def test_doppler_cycle_folds_offset_when_locked(self, qtbot, db) -> None:
        w = self._make_window(qtbot, db)
        w._rig_controller = None
        w._rig2_controller = None
        w._dial_feedback_offset_hz = 500.0
        received = self._run_doppler_cycle(
            w,
            rig=None,
            rr=0.0,
            transmitter=dict(self._TRANSMITTER),
            trsp_lock=True,
        )
        assert len(received) == 1
        result = received[0]
        assert result.dl_corr == 145_800_500.0
        assert result.ul_corr == 435_000_500.0
        assert result.dl_shift is None

    def test_doppler_cycle_folds_offset_when_unlocked(self, qtbot, db) -> None:
        """The offset applies even with Lock off -- see class docstring:
        this is what makes "resume Doppler correction from that frequency"
        work after Lock is turned off."""
        w = self._make_window(qtbot, db)
        w._rig_controller = None
        w._rig2_controller = None
        w._dial_feedback_offset_hz = 500.0
        received = self._run_doppler_cycle(
            w,
            rig=None,
            rr=0.0,
            transmitter=dict(self._TRANSMITTER),
            trsp_lock=False,
        )
        assert len(received) == 1
        result = received[0]
        assert result.dl_corr == 145_800_500.0
        assert result.ul_corr == 435_000_500.0

    def test_doppler_cycle_folds_offset_inverted(self, qtbot, db) -> None:
        """Inverted transponder: DL +500Hz manual offset -> UL -500Hz
        relative to its own baseline (uplink_low, matching
        DopplerCalculator.correct_uplink()'s own convention)."""
        w = self._make_window(qtbot, db)
        w._rig_controller = None
        w._rig2_controller = None
        w._dial_feedback_offset_hz = 500.0
        received = self._run_doppler_cycle(
            w,
            rig=None,
            rr=0.0,
            transmitter=dict(self._TRANSMITTER_INVERTED),
            trsp_lock=True,
        )
        assert len(received) == 1
        result = received[0]
        assert result.dl_corr == 145_800_500.0
        assert result.ul_corr == 434_999_500.0

    def test_doppler_cycle_ul_uses_own_scaled_doppler_not_dl_mirror(self, qtbot, db) -> None:
        """Regression test for a pre-existing Lock-branch flaw (found
        live, 2026-07-15, RS-44): UL must use its own carrier-frequency-
        scaled Doppler correction (DopplerCalculator.correct_uplink()),
        never a raw 1:1 mirror of DL's absolute shift-from-nominal. A V/U
        transponder like RS-44 (435MHz DL / 145MHz UL) needs roughly a 3:1
        DL:UL Doppler-shift ratio; the old Lock-branch formula collapsed
        this to 1:1 and also permanently blanked the UL Doppler-shift
        display (ul_shift was unconditionally None whenever Lock was on)."""
        from core.engine import DopplerCalculator

        w = self._make_window(qtbot, db)
        w._rig_controller = None
        w._rig2_controller = None
        dl_nom = 435_612_000.0
        ul_nom = 145_993_000.0
        transmitter = {
            "downlink_low": dl_nom,
            "downlink_high": dl_nom + 30_000,
            "uplink_low": ul_nom,
            "uplink_high": ul_nom + 10_000,
            "invert": False,
            "mode": "USB",
        }
        w._dial_feedback_offset_hz = 0.0

        expected_dl_corr, expected_dl_shift = DopplerCalculator.correct_downlink(dl_nom, 2.5)
        expected_ul_corr, expected_ul_shift = DopplerCalculator.correct_uplink(
            ul_nom, 2.5, invert=False
        )
        assert abs(expected_dl_shift) != abs(expected_ul_shift)

        received = self._run_doppler_cycle(
            w, rig=None, rr=2.5, transmitter=transmitter, trsp_lock=True
        )

        assert len(received) == 1
        result = received[0]
        assert result.dl_corr == expected_dl_corr
        assert result.ul_corr == expected_ul_corr
        assert result.ul_shift == expected_ul_shift


class TestRadioType:
    """Radio Type 設定テスト。"""

    def test_radio_type_combo_exists(self, qtbot, db) -> None:
        """RigSettingsDialog の Rig 1 パネルに _radio_type_combo が存在する。"""
        from ui.rig_dialog import RigSettingsDialog

        w = RigSettingsDialog(db)
        qtbot.addWidget(w)
        assert hasattr(w._panel1, "_radio_type_combo")

    def test_radio_type_default_full_duplex(self, qtbot, db) -> None:
        """デフォルトで full_duplex が選択されている。"""
        from ui.rig_dialog import RigSettingsDialog

        w = RigSettingsDialog(db)
        qtbot.addWidget(w)
        assert w._panel1._radio_type_combo.currentData() == "full_duplex"

    def test_radio_type_items(self, qtbot, db) -> None:
        """full_duplex / rx_only / tx_only の3選択肢がある。"""
        from ui.rig_dialog import RigSettingsDialog

        w = RigSettingsDialog(db)
        qtbot.addWidget(w)
        combo = w._panel1._radio_type_combo
        data_values = [combo.itemData(i) for i in range(combo.count())]
        assert "full_duplex" in data_values
        assert "rx_only" in data_values
        assert "tx_only" in data_values

    def test_net_controller_rx_only_skips_tx(self) -> None:
        """rx_only mode must not send the I command."""
        from unittest.mock import MagicMock

        from rig.controller import HamlibNetController

        ctrl = HamlibNetController(radio_type="rx_only")
        ctrl._sock = MagicMock()
        ctrl._state = __import__("rig.controller", fromlist=["RigState"]).RigState.CONNECTED

        sent = []

        def fake_cmd(c: str) -> str:
            sent.append(c)
            return "RPRT 0"

        ctrl._cmd_raw = fake_cmd  # type: ignore[method-assign]
        ctrl._last_dl_hz = None
        ctrl._last_ul_hz = None
        ctrl.set_vfo_frequencies(145_800_000, 435_000_000)
        assert any(c.startswith("F ") for c in sent)
        assert not any(c.startswith("I ") for c in sent)

    def test_net_controller_tx_only_skips_rx(self) -> None:
        """tx_only モードでは F コマンドを送信しない。"""
        from unittest.mock import MagicMock

        from rig.controller import HamlibNetController, RigState

        ctrl = HamlibNetController(radio_type="tx_only")
        ctrl._sock = MagicMock()
        ctrl._state = RigState.CONNECTED

        sent = []

        def fake_cmd(c: str) -> str:
            sent.append(c)
            return "RPRT 0"

        ctrl._cmd_raw = fake_cmd  # type: ignore[method-assign]
        ctrl._last_dl_hz = None
        ctrl._last_ul_hz = None
        ctrl.set_vfo_frequencies(145_800_000, 435_000_000)
        assert not any(c.startswith("F ") for c in sent)
        assert any(c.startswith("I ") for c in sent)

    def test_net_controller_full_duplex_sends_both(self) -> None:
        """full_duplex モードでは F と I 両方を送信する。"""
        from unittest.mock import MagicMock

        from rig.controller import HamlibNetController, RigState

        ctrl = HamlibNetController(radio_type="full_duplex")
        ctrl._sock = MagicMock()
        ctrl._state = RigState.CONNECTED

        sent = []

        def fake_cmd(c: str) -> str:
            sent.append(c)
            return "RPRT 0"

        ctrl._cmd_raw = fake_cmd  # type: ignore[method-assign]
        ctrl._last_dl_hz = None
        ctrl._last_ul_hz = None
        ctrl.set_vfo_frequencies(145_800_000, 435_000_000)
        assert any(c.startswith("F ") for c in sent)
        assert any(c.startswith("I ") for c in sent)


class TestModeInvertDataModes:
    """FT4 calling-frequency transponders (RS-44) use mode="USB-D" with
    invert=True; the uplink must flip to the DATA-LSB equivalent just like
    a plain USB/LSB inverting transponder."""

    def test_usb_d_inverts_to_lsb_d(self) -> None:
        assert _MODE_INVERT["USB-D"] == "LSB-D"

    def test_lsb_d_inverts_to_usb_d(self) -> None:
        assert _MODE_INVERT["LSB-D"] == "USB-D"
