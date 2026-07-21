"""
Web モジュールのテスト

FastAPI TestClient を使って REST/WebSocket エンドポイントを検証する。
ネットワーク不要・インメモリ DB・エンジン省略で実行できる。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from data.database import SCHEMA_SQL
from data.tle_manager import TLEManager
from web.app import APP_VERSION, create_app
from web.qrcode_helper import generate_qr_png
from web.server import get_lan_ip

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
    """インメモリ DB を使う TLEManager。"""
    return TLEManager(db)


@pytest.fixture()
def client(db: sqlite3.Connection, tle_manager: TLEManager) -> TestClient:
    """エンジンなし（pass_predictor=None）の TestClient。"""
    app = create_app(conn=db, tle_manager=tle_manager)
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def populated_db(db: sqlite3.Connection) -> sqlite3.Connection:
    """衛星・トランスポンダ・TLE レコードを持つ DB。"""
    db.execute(
        "INSERT INTO satellites (norad_cat_id, name, alt_names, status) VALUES (?, ?, ?, ?)",
        (25544, "ISS (ZARYA)", json.dumps(["ISS"]), "alive"),
    )
    db.execute(
        "INSERT INTO satellites (norad_cat_id, name, alt_names, status) VALUES (?, ?, ?, ?)",
        (43017, "FOX-1D (AO-92)", json.dumps([]), "alive"),
    )
    db.execute(
        """INSERT INTO transmitters
           (uuid, norad_cat_id, description, type, downlink_low, mode, alive, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(uuid.uuid4()),
            25544,
            "APRS 145.825 MHz",
            "Transmitter",
            145_825_000,
            "FM",
            1,
            "satnogs",
        ),
    )
    db.execute(
        """INSERT INTO tle_data (norad_cat_id, name, line1, line2, epoch, source, quality_score)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            25544,
            "ISS (ZARYA)",
            "1 25544U 98067A   21001.00000000  .00001000  00000-0  10000-3 0  9990",
            "2 25544  51.6416  95.2127 0001000  10.0000 350.0000 15.48900000100000",
            "2021-01-01T00:00:00",
            "celestrak",
            "fair",
        ),
    )
    db.commit()
    return db


@pytest.fixture()
def populated_client(
    populated_db: sqlite3.Connection,
    tle_manager: TLEManager,
) -> TestClient:
    """データ入り DB を使う TestClient。TLEManager は同じ DB を参照する。"""
    app = create_app(conn=populated_db, tle_manager=TLEManager(populated_db))
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# GET /api/status
# ---------------------------------------------------------------------------


class TestApiStatus:
    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/api/status")
        assert resp.status_code == 200

    def test_version_matches(self, client: TestClient) -> None:
        data = client.get("/api/status").json()
        assert data["version"] == APP_VERSION

    def test_status_ok(self, client: TestClient) -> None:
        data = client.get("/api/status").json()
        assert data["status"] == "ok"

    def test_counts_reflect_db(self, populated_client: TestClient) -> None:
        data = populated_client.get("/api/status").json()
        assert data["satellite_count"] == 2
        assert data["tle_count"] == 1

    def test_uptime_is_non_negative(self, client: TestClient) -> None:
        data = client.get("/api/status").json()
        assert data["uptime_s"] >= 0.0


# ---------------------------------------------------------------------------
# GET /api/satellites
# ---------------------------------------------------------------------------


class TestApiSatellites:
    def test_empty_db_returns_empty_list(self, client: TestClient) -> None:
        resp = client.get("/api/satellites")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_all_satellites(self, populated_client: TestClient) -> None:
        data = populated_client.get("/api/satellites").json()
        assert len(data) == 2

    def test_satellite_fields(self, populated_client: TestClient) -> None:
        sats: list[dict[str, Any]] = populated_client.get("/api/satellites").json()
        iss = next(s for s in sats if s["norad_cat_id"] == 25544)
        assert iss["name"] == "ISS (ZARYA)"
        assert iss["status"] == "alive"
        assert isinstance(iss["alt_names"], list)

    def test_alt_names_parsed(self, populated_client: TestClient) -> None:
        sats: list[dict[str, Any]] = populated_client.get("/api/satellites").json()
        iss = next(s for s in sats if s["norad_cat_id"] == 25544)
        assert "ISS" in iss["alt_names"]

    def test_sorted_by_name(self, populated_client: TestClient) -> None:
        sats: list[dict[str, Any]] = populated_client.get("/api/satellites").json()
        names = [s["name"] for s in sats]
        assert names == sorted(names)


# ---------------------------------------------------------------------------
# GET /api/satellites/{norad}/transmitters
# ---------------------------------------------------------------------------


class TestApiTransmitters:
    def test_unknown_satellite_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/satellites/99999/transmitters")
        assert resp.status_code == 404

    def test_satellite_with_no_transmitters(self, populated_client: TestClient) -> None:
        # FOX-1D はトランスポンダなし
        resp = populated_client.get("/api/satellites/43017/transmitters")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_transmitters(self, populated_client: TestClient) -> None:
        data = populated_client.get("/api/satellites/25544/transmitters").json()
        assert len(data) == 1
        assert data[0]["description"] == "APRS 145.825 MHz"

    def test_transmitter_fields(self, populated_client: TestClient) -> None:
        tx: dict[str, Any] = populated_client.get("/api/satellites/25544/transmitters").json()[0]
        assert tx["norad_cat_id"] == 25544
        assert tx["downlink_low"] == 145_825_000
        assert tx["mode"] == "FM"
        assert tx["alive"] is True
        assert tx["source"] == "satnogs"
        assert tx["manual_override"] is False


# ---------------------------------------------------------------------------
# GET /api/satellites/{norad}/passes
# ---------------------------------------------------------------------------


class TestApiPasses:
    def test_unknown_satellite_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/satellites/99999/passes")
        assert resp.status_code == 404

    def test_no_predictor_returns_empty_list(self, populated_client: TestClient) -> None:
        # pass_predictor=None → 空リスト
        resp = populated_client.get("/api/satellites/25544/passes")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_hours_query_param_accepted(self, populated_client: TestClient) -> None:
        resp = populated_client.get("/api/satellites/25544/passes?hours=48&min_el=10")
        assert resp.status_code == 200

    def test_invalid_hours_rejected(self, populated_client: TestClient) -> None:
        resp = populated_client.get("/api/satellites/25544/passes?hours=0")
        assert resp.status_code == 422

    def test_invalid_min_el_rejected(self, populated_client: TestClient) -> None:
        resp = populated_client.get("/api/satellites/25544/passes?min_el=91")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/tle/status
# ---------------------------------------------------------------------------


class TestApiTleStatus:
    def test_empty_db_returns_empty_list(self, client: TestClient) -> None:
        resp = client.get("/api/tle/status")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_tle_quality(self, populated_client: TestClient) -> None:
        data = populated_client.get("/api/tle/status").json()
        # ISS は TLE あり、FOX-1D はなし → 2 件
        assert len(data) == 2

    def test_iss_quality_fields(self, populated_client: TestClient) -> None:
        data: list[dict[str, Any]] = populated_client.get("/api/tle/status").json()
        iss = next(r for r in data if r["norad_cat_id"] == 25544)
        assert iss["quality_score"] == "fair"
        assert iss["source"] == "celestrak"

    def test_satellite_without_tle_has_null_score(self, populated_client: TestClient) -> None:
        data: list[dict[str, Any]] = populated_client.get("/api/tle/status").json()
        fox = next(r for r in data if r["norad_cat_id"] == 43017)
        assert fox["quality_score"] is None


# ---------------------------------------------------------------------------
# WebSocket /ws/tracking
# ---------------------------------------------------------------------------


class TestWsTracking:
    def test_connect_and_receive_error_when_no_engine(self, client: TestClient) -> None:
        """エンジンなし時はエラーペイロードが来る。"""
        with client.websocket_connect("/ws/tracking?norad=25544") as ws:
            data: dict[str, Any] = ws.receive_json()
        assert data["norad"] == 25544
        assert "error" in data

    def test_default_norad_used(self, client: TestClient) -> None:
        with client.websocket_connect("/ws/tracking") as ws:
            data: dict[str, Any] = ws.receive_json()
        # デフォルト norad=25544
        assert data["norad"] == 25544

    def test_custom_norad_reflected(self, client: TestClient) -> None:
        with client.websocket_connect("/ws/tracking?norad=43017") as ws:
            data: dict[str, Any] = ws.receive_json()
        assert data["norad"] == 43017


# ---------------------------------------------------------------------------
# QR コード生成
# ---------------------------------------------------------------------------


class TestQrCode:
    def test_returns_bytes(self) -> None:
        result = generate_qr_png("http://192.168.1.10:8080")
        assert isinstance(result, bytes)

    def test_png_magic_bytes(self) -> None:
        result = generate_qr_png("http://192.168.1.10:8080")
        assert result[:8] == b"\x89PNG\r\n\x1a\n"

    def test_non_empty(self) -> None:
        result = generate_qr_png("http://example.com")
        assert len(result) > 100

    def test_different_urls_differ(self) -> None:
        a = generate_qr_png("http://192.168.1.1:8080")
        b = generate_qr_png("http://10.0.0.1:8080")
        assert a != b


# ---------------------------------------------------------------------------
# LAN IP ヘルパー
# ---------------------------------------------------------------------------


class TestGetLanIp:
    def test_returns_string(self) -> None:
        ip = get_lan_ip()
        assert isinstance(ip, str)

    def test_dotted_quad_or_loopback(self) -> None:
        ip = get_lan_ip()
        parts = ip.split(".")
        assert len(parts) == 4
        assert all(p.isdigit() for p in parts)


# ---------------------------------------------------------------------------
# GET / (スマホ向けメインページ)
# ---------------------------------------------------------------------------


class TestRootPage:
    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 200

    def test_content_type_html(self, client: TestClient) -> None:
        resp = client.get("/")
        assert "text/html" in resp.headers["content-type"]

    def test_contains_app_title(self, client: TestClient) -> None:
        resp = client.get("/")
        assert "FBSAT59" in resp.text


# ---------------------------------------------------------------------------
# GET /api/amsat
# ---------------------------------------------------------------------------


class TestApiSatellitesGroupFilter:
    def test_no_group_returns_all(self, populated_client: TestClient) -> None:
        data = populated_client.get("/api/satellites").json()
        assert len(data) == 2

    def test_group_all_returns_all(self, populated_client: TestClient) -> None:
        data = populated_client.get("/api/satellites?group=all").json()
        assert len(data) == 2

    def test_group_amateur_returns_subset(self, populated_client: TestClient) -> None:
        # ISS has tle_data with tle_group='amateur' (default), FOX-1D has no TLE
        data = populated_client.get("/api/satellites?group=amateur").json()
        assert len(data) == 1
        assert data[0]["norad_cat_id"] == 25544

    def test_group_unknown_returns_empty(self, populated_client: TestClient) -> None:
        data = populated_client.get("/api/satellites?group=nonexistent_xyz").json()
        assert data == []

    def test_group_fav_prefix_with_no_matching_satellite_returns_empty(
        self, populated_client: TestClient
    ) -> None:
        # group=fav:1 filters by favorite_group=1; populated_db has no favorites by default
        data = populated_client.get("/api/satellites?group=fav:1").json()
        assert data == []

    def test_group_fav_prefix_invalid_id_returns_422(self, populated_client: TestClient) -> None:
        resp = populated_client.get("/api/satellites?group=fav:notanumber")
        assert resp.status_code == 422

    def test_group_operational_no_amsat_data_returns_empty(
        self, populated_client: TestClient
    ) -> None:
        data = populated_client.get("/api/satellites?group=operational").json()
        assert data == []

    def test_group_operational_with_amsat_data(
        self, db: sqlite3.Connection, tle_manager: TLEManager
    ) -> None:
        db.execute(
            "INSERT INTO satellites (norad_cat_id, name) VALUES (?, ?)",
            (25544, "ISS (ZARYA)"),
        )
        db.execute(
            "INSERT INTO tle_data (norad_cat_id, name, line1, line2) VALUES (?, ?, ?, ?)",
            (
                25544,
                "ISS (ZARYA)",
                "1 25544U 98067A   21001.00000000  .00001000  00000-0  10000-3 0  9990",
                "2 25544  51.6416  95.2127 0001000  10.0000 350.0000 15.48900000100000",
            ),
        )
        db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("amsat_status_data", json.dumps({"iss": "operational"}), "2026-01-01"),
        )
        db.commit()
        app = create_app(conn=db, tle_manager=TLEManager(db))
        from fastapi.testclient import TestClient as TC

        c = TC(app, raise_server_exceptions=True)
        data = c.get("/api/satellites?group=operational").json()
        assert len(data) == 1
        assert data[0]["norad_cat_id"] == 25544


class TestApiGroupPasses:
    def test_no_predictor_returns_empty(self, populated_client: TestClient) -> None:
        resp = populated_client.get("/api/passes/group?group=amateur")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_default_group_returns_200(self, populated_client: TestClient) -> None:
        resp = populated_client.get("/api/passes/group")
        assert resp.status_code == 200

    def test_invalid_from_dt_returns_422(self, populated_client: TestClient) -> None:
        resp = populated_client.get("/api/passes/group?from_dt=not-a-date")
        assert resp.status_code == 422

    def test_invalid_to_dt_returns_422(self, populated_client: TestClient) -> None:
        resp = populated_client.get("/api/passes/group?from_dt=2026-05-18T00:00&to_dt=bad")
        assert resp.status_code == 422

    def test_to_before_from_returns_422(self, populated_client: TestClient) -> None:
        resp = populated_client.get(
            "/api/passes/group?from_dt=2026-05-18T12:00&to_dt=2026-05-18T10:00"
        )
        assert resp.status_code == 422

    def test_min_el_out_of_range_returns_422(self, populated_client: TestClient) -> None:
        resp = populated_client.get("/api/passes/group?min_el=91")
        assert resp.status_code == 422

    def test_group_stations_returns_200(self, populated_client: TestClient) -> None:
        resp = populated_client.get("/api/passes/group?group=stations")
        assert resp.status_code == 200


class TestApiPassesDateRange:
    def test_from_to_dt_accepted(self, populated_client: TestClient) -> None:
        resp = populated_client.get(
            "/api/satellites/25544/passes?from_dt=2026-05-18T00:00&to_dt=2026-05-19T00:00"
        )
        assert resp.status_code == 200

    def test_invalid_from_dt_rejected(self, populated_client: TestClient) -> None:
        resp = populated_client.get("/api/satellites/25544/passes?from_dt=not-a-date")
        assert resp.status_code == 422

    def test_to_before_from_returns_422(self, populated_client: TestClient) -> None:
        resp = populated_client.get(
            "/api/satellites/25544/passes?from_dt=2026-05-19T00:00&to_dt=2026-05-18T00:00"
        )
        assert resp.status_code == 422

    def test_only_from_dt_uses_hours_fallback(self, populated_client: TestClient) -> None:
        # Only from_dt without to_dt → fallback to hours-based range
        resp = populated_client.get("/api/satellites/25544/passes?from_dt=2026-05-18T00:00")
        assert resp.status_code == 200


class TestApiFavorites:
    def test_get_favorites_empty(self, client: TestClient) -> None:
        resp = client.get("/api/favorites")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_favorites_returns_empty_when_none_set(self, populated_client: TestClient) -> None:
        data = populated_client.get("/api/favorites").json()
        assert data == []

    def test_set_favorite_group_returns_204(self, populated_client: TestClient) -> None:
        resp = populated_client.put("/api/satellites/25544/favorite-group", json={"group_id": 1})
        assert resp.status_code == 204

    def test_set_favorite_group_unknown_returns_404(self, populated_client: TestClient) -> None:
        resp = populated_client.put("/api/satellites/99999/favorite-group", json={"group_id": 1})
        assert resp.status_code == 404

    def test_get_favorites_after_set(self, populated_client: TestClient) -> None:
        populated_client.put("/api/satellites/25544/favorite-group", json={"group_id": 2})
        data = populated_client.get("/api/favorites").json()
        assert len(data) == 1
        assert data[0]["norad_cat_id"] == 25544
        assert data[0]["favorite_group"] == 2

    def test_clear_favorite_group_returns_204(self, populated_client: TestClient) -> None:
        populated_client.put("/api/satellites/25544/favorite-group", json={"group_id": 1})
        resp = populated_client.put("/api/satellites/25544/favorite-group", json={"group_id": 0})
        assert resp.status_code == 204

    def test_get_favorites_after_clear(self, populated_client: TestClient) -> None:
        populated_client.put("/api/satellites/25544/favorite-group", json={"group_id": 1})
        populated_client.put("/api/satellites/25544/favorite-group", json={"group_id": 0})
        data = populated_client.get("/api/favorites").json()
        assert data == []

    def test_satellites_group_fav_prefix_returns_only_matching_group(
        self, populated_client: TestClient
    ) -> None:
        populated_client.put("/api/satellites/25544/favorite-group", json={"group_id": 1})
        # 43017 (FOX-1D) is put in a different group and must not appear in group=fav:1
        populated_client.put("/api/satellites/43017/favorite-group", json={"group_id": 2})
        data = populated_client.get("/api/satellites?group=fav:1").json()
        assert len(data) == 1
        assert data[0]["norad_cat_id"] == 25544


class TestApiCustomGroups:
    def test_empty_db_returns_empty_list(self, client: TestClient) -> None:
        resp = client.get("/api/custom-groups")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_groups_ordered_by_sort_order(
        self, db: sqlite3.Connection, tle_manager: TLEManager
    ) -> None:
        db.executemany(
            "INSERT INTO custom_groups (id, name, sort_order) VALUES (?, ?, ?)",
            [(1, "Favorite 1", 1), (2, "Favorite 2", 2), (3, "Favorite 3", 3)],
        )
        db.commit()
        app = create_app(conn=db, tle_manager=tle_manager)
        c = TestClient(app, raise_server_exceptions=True)
        data = c.get("/api/custom-groups").json()
        assert [g["name"] for g in data] == ["Favorite 1", "Favorite 2", "Favorite 3"]


class TestApiAmsat:
    def test_empty_db_returns_empty_dict(self, client: TestClient) -> None:
        resp = client.get("/api/amsat")
        assert resp.status_code == 200
        assert resp.json() == {}

    def test_returns_dict_with_status_data(
        self, db: sqlite3.Connection, tle_manager: TLEManager
    ) -> None:
        import json as _json

        status_map = {"iss": "operational", "ao-91": "non_operational"}
        db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("amsat_status_data", _json.dumps(status_map), "2026-05-15T00:00:00"),
        )
        db.commit()
        app = create_app(conn=db, tle_manager=tle_manager)
        from fastapi.testclient import TestClient as TC

        c = TC(app, raise_server_exceptions=True)
        data = c.get("/api/amsat").json()
        assert data["iss"] == "operational"
        assert data["ao-91"] == "non_operational"

    def test_invalid_json_returns_empty_dict(
        self, db: sqlite3.Connection, tle_manager: TLEManager
    ) -> None:
        db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("amsat_status_data", "not-valid-json", "2026-05-15T00:00:00"),
        )
        db.commit()
        app = create_app(conn=db, tle_manager=tle_manager)
        from fastapi.testclient import TestClient as TC

        c = TC(app, raise_server_exceptions=True)
        resp = c.get("/api/amsat")
        assert resp.status_code == 200
        assert resp.json() == {}
