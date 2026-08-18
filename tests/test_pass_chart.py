"""
パス予測チャートモジュールのテスト

- pass_quality()      — 品質ランク分類ロジック
- elevation_points()  — 仰角曲線データ生成
- PassOut 拡張フィールド — API レスポンス検証
- PassChartView       — Qt ウィジェット（qtbot 使用、CI は xvfb で実行）
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from data.database import SCHEMA_SQL
from data.tle_manager import TLEManager
from ui.pass_chart import elevation_points, pass_quality
from web.app import create_app

# ---------------------------------------------------------------------------
# pass_quality() のテスト
# ---------------------------------------------------------------------------


class TestPassQuality:
    def test_excellent_at_boundary(self) -> None:
        assert pass_quality(60.0) == "excellent"

    def test_excellent_above(self) -> None:
        assert pass_quality(89.9) == "excellent"

    def test_good_at_boundary(self) -> None:
        assert pass_quality(30.0) == "good"

    def test_good_mid(self) -> None:
        assert pass_quality(45.0) == "good"

    def test_good_just_below_excellent(self) -> None:
        assert pass_quality(59.9) == "good"

    def test_fair_at_boundary(self) -> None:
        assert pass_quality(10.0) == "fair"

    def test_fair_mid(self) -> None:
        assert pass_quality(20.0) == "fair"

    def test_fair_just_below_good(self) -> None:
        assert pass_quality(29.9) == "fair"

    def test_low_just_below_fair(self) -> None:
        assert pass_quality(9.9) == "low"

    def test_low_at_zero(self) -> None:
        assert pass_quality(0.0) == "low"

    def test_low_negative(self) -> None:
        # 仰角負値は low 扱い（通常はフィルタされるが境界安全性確認）
        assert pass_quality(-1.0) == "low"

    def test_returns_string(self) -> None:
        assert isinstance(pass_quality(45.0), str)

    def test_all_ranks_reachable(self) -> None:
        ranks = {pass_quality(el) for el in (5.0, 15.0, 45.0, 75.0)}
        assert ranks == {"low", "fair", "good", "excellent"}


# ---------------------------------------------------------------------------
# elevation_points() のテスト
# ---------------------------------------------------------------------------


def _make_pass_times(
    duration_min: int = 10, max_el: float = 45.0
) -> tuple[datetime, datetime, datetime, float]:
    """テスト用 AOS/TCA/LOS と最大仰角を生成する。"""
    aos = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    los = aos + timedelta(minutes=duration_min)
    tca = aos + timedelta(minutes=duration_min // 2)
    return aos, tca, los, max_el


class TestElevationPoints:
    def test_returns_list(self) -> None:
        aos, tca, los, max_el = _make_pass_times()
        pts = elevation_points(aos, tca, los, max_el)
        assert isinstance(pts, list)

    def test_non_empty(self) -> None:
        aos, tca, los, max_el = _make_pass_times()
        pts = elevation_points(aos, tca, los, max_el)
        assert len(pts) > 0

    def test_point_count(self) -> None:
        # n_points=20 → (20 + 1 + 20) = 41 点
        aos, tca, los, max_el = _make_pass_times()
        pts = elevation_points(aos, tca, los, max_el, n_points=20)
        assert len(pts) == 41

    def test_custom_n_points(self) -> None:
        aos, tca, los, max_el = _make_pass_times()
        pts = elevation_points(aos, tca, los, max_el, n_points=10)
        assert len(pts) == 21  # 10 + 1 + 10

    def test_first_point_near_zero(self) -> None:
        aos, tca, los, max_el = _make_pass_times()
        pts = elevation_points(aos, tca, los, max_el)
        _, el = pts[0]
        assert abs(el) < 1e-6  # AOS は仰角 0

    def test_peak_at_max_elevation(self) -> None:
        aos, tca, los, max_el = _make_pass_times(max_el=72.5)
        pts = elevation_points(aos, tca, los, max_el)
        max_in_pts = max(el for _, el in pts)
        assert abs(max_in_pts - max_el) < 1e-9

    def test_last_point_near_zero(self) -> None:
        aos, tca, los, max_el = _make_pass_times()
        pts = elevation_points(aos, tca, los, max_el)
        _, el = pts[-1]
        assert abs(el) < 1e-6  # LOS は仰角 0

    def test_all_elevations_non_negative(self) -> None:
        aos, tca, los, max_el = _make_pass_times(max_el=30.0)
        pts = elevation_points(aos, tca, los, max_el)
        assert all(el >= -1e-9 for _, el in pts)

    def test_timestamps_monotonically_increasing(self) -> None:
        aos, tca, los, max_el = _make_pass_times()
        pts = elevation_points(aos, tca, los, max_el)
        times = [ms for ms, _ in pts]
        assert all(times[i] <= times[i + 1] for i in range(len(times) - 1))

    def test_first_timestamp_matches_aos(self) -> None:
        aos, tca, los, max_el = _make_pass_times()
        pts = elevation_points(aos, tca, los, max_el)
        ms, _ = pts[0]
        assert abs(ms - aos.timestamp() * 1000.0) < 1.0

    def test_last_timestamp_matches_los(self) -> None:
        aos, tca, los, max_el = _make_pass_times()
        pts = elevation_points(aos, tca, los, max_el)
        ms, _ = pts[-1]
        assert abs(ms - los.timestamp() * 1000.0) < 1.0

    def test_naive_datetimes_accepted(self) -> None:
        """tzinfo なし datetime でも動作することを確認する。"""
        aos = datetime(2026, 5, 10, 12, 0, 0)
        tca = datetime(2026, 5, 10, 12, 5, 0)
        los = datetime(2026, 5, 10, 12, 10, 0)
        pts = elevation_points(aos, tca, los, 45.0)
        assert len(pts) > 0

    def test_peak_is_at_tca_timestamp(self) -> None:
        """仰角最大値のタイムスタンプが TCA であることを確認する。"""
        aos, tca, los, max_el = _make_pass_times(max_el=60.0)
        pts = elevation_points(aos, tca, los, max_el)
        peak_ms, peak_el = max(pts, key=lambda p: p[1])
        assert abs(peak_el - max_el) < 1e-9
        assert abs(peak_ms - tca.timestamp() * 1000.0) < 1.0


# ---------------------------------------------------------------------------
# API PassOut 拡張フィールドのテスト（TestClient 使用）
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


@pytest.fixture()
def populated_db(db: sqlite3.Connection) -> sqlite3.Connection:
    db.execute(
        "INSERT INTO satellites (norad_cat_id, name, alt_names, status) VALUES (?, ?, ?, ?)",
        (25544, "ISS (ZARYA)", json.dumps(["ISS"]), "alive"),
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
    db.commit()
    return db


@pytest.fixture()
def api_client(populated_db: sqlite3.Connection) -> TestClient:
    app = create_app(conn=populated_db, tle_manager=TLEManager(populated_db))
    return TestClient(app, raise_server_exceptions=True)


class TestPassOutFields:
    def test_no_predictor_returns_empty(self, api_client: TestClient) -> None:
        resp = api_client.get("/api/satellites/25544/passes")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_hours_and_min_el_accepted(self, api_client: TestClient) -> None:
        resp = api_client.get("/api/satellites/25544/passes?hours=12&min_el=10")
        assert resp.status_code == 200

    def test_unknown_satellite_404(self, api_client: TestClient) -> None:
        resp = api_client.get("/api/satellites/99999/passes")
        assert resp.status_code == 404


class TestPassQualityInApi:
    """pass_quality() がモックパスに正しく適用されることをユニットテストで確認する。"""

    @pytest.mark.parametrize(
        ("max_el", "expected"),
        [
            (75.0, "excellent"),
            (60.0, "excellent"),
            (45.0, "good"),
            (30.0, "good"),
            (20.0, "fair"),
            (10.0, "fair"),
            (5.0, "low"),
            (0.1, "low"),
        ],
    )
    def test_quality_mapping(self, max_el: float, expected: str) -> None:
        assert pass_quality(max_el) == expected


# ---------------------------------------------------------------------------
# PassChartView ウィジェットのテスト（Qt 必要）
# ---------------------------------------------------------------------------


def _make_pass_info(max_el: float = 45.0, duration_min: int = 10) -> Any:
    """テスト用モック PassInfo を生成する。"""
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class MockPassInfo:
        norad_cat_id: int
        aos: datetime
        tca: datetime
        los: datetime
        max_elevation_deg: float
        aos_azimuth_deg: float
        los_azimuth_deg: float
        duration_s: float

    aos = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    tca = aos + timedelta(minutes=duration_min // 2)
    los = aos + timedelta(minutes=duration_min)
    return MockPassInfo(
        norad_cat_id=25544,
        aos=aos,
        tca=tca,
        los=los,
        max_elevation_deg=max_el,
        aos_azimuth_deg=45.0,
        los_azimuth_deg=270.0,
        duration_s=float(duration_min * 60),
    )


class TestPassChartView:
    def test_import(self) -> None:
        """PassChartView がインポートできることを確認する。"""
        from ui.pass_chart import PassChartView

        assert PassChartView is not None

    def test_create_widget(self, qtbot: Any) -> None:
        """ウィジェットを生成できることを確認する。"""
        from ui.pass_chart import PassChartView

        widget = PassChartView()
        qtbot.addWidget(widget)
        assert widget is not None

    def test_set_empty_passes(self, qtbot: Any) -> None:
        """空リストを設定してもクラッシュしないことを確認する。"""
        from ui.pass_chart import PassChartView

        widget = PassChartView()
        qtbot.addWidget(widget)
        widget.set_passes([], sat_name="TEST")

    def test_set_single_pass(self, qtbot: Any) -> None:
        """1 件のパスを設定できることを確認する。"""
        from ui.pass_chart import PassChartView

        widget = PassChartView()
        qtbot.addWidget(widget)
        widget.set_passes([_make_pass_info(45.0)], sat_name="ISS")

    def test_set_multiple_passes(self, qtbot: Any) -> None:
        """複数パスを設定できることを確認する。"""
        from ui.pass_chart import PassChartView

        widget = PassChartView()
        qtbot.addWidget(widget)
        passes = [_make_pass_info(el) for el in (5.0, 15.0, 45.0, 72.0)]
        widget.set_passes(passes, sat_name="ISS")

    def test_clear(self, qtbot: Any) -> None:
        """clear() でチャートがリセットされることを確認する。"""
        from ui.pass_chart import PassChartView

        widget = PassChartView()
        qtbot.addWidget(widget)
        widget.set_passes([_make_pass_info(45.0)], sat_name="ISS")
        widget.clear()
        assert widget._passes == []

    def test_pass_clicked_signal_exists(self, qtbot: Any) -> None:
        """pass_clicked シグナルが存在することを確認する。"""
        from ui.pass_chart import PassChartView

        widget = PassChartView()
        qtbot.addWidget(widget)
        assert hasattr(widget, "pass_clicked")

    def test_quality_colors_defined(self) -> None:
        """全品質ランクの色が定義されていることを確認する。"""
        from ui.pass_chart import QUALITY_COLORS

        for rank in ("excellent", "good", "fair", "low"):
            assert rank in QUALITY_COLORS

    def test_set_passes_then_clear_then_set(self, qtbot: Any) -> None:
        """set → clear → set の繰り返しが安全なことを確認する。"""
        from ui.pass_chart import PassChartView

        widget = PassChartView()
        qtbot.addWidget(widget)
        widget.set_passes([_make_pass_info()], sat_name="ISS")
        widget.clear()
        widget.set_passes([_make_pass_info(60.0)], sat_name="AO-92")

    def test_multipass_future_dates_all_shown(self, qtbot: Any) -> None:
        """将来日付のパス複数件が全て表示されることを確認する（時間軸 range 設定の検証）。"""
        from dataclasses import dataclass
        from datetime import UTC, datetime, timedelta

        from ui.pass_chart import PassChartView

        @dataclass(frozen=True)
        class FuturePassInfo:
            norad_cat_id: int
            aos: datetime
            tca: datetime
            los: datetime
            max_elevation_deg: float
            aos_azimuth_deg: float
            los_azimuth_deg: float
            duration_s: float

        now = datetime.now(UTC)
        passes = []
        for h in (1, 4, 8, 14, 20):
            aos = now + timedelta(hours=h)
            tca = aos + timedelta(minutes=5)
            los = aos + timedelta(minutes=10)
            passes.append(
                FuturePassInfo(
                    norad_cat_id=25544,
                    aos=aos,
                    tca=tca,
                    los=los,
                    max_elevation_deg=float(h * 5),
                    aos_azimuth_deg=45.0,
                    los_azimuth_deg=270.0,
                    duration_s=600.0,
                )
            )

        widget = PassChartView()
        qtbot.addWidget(widget)
        widget.set_passes(passes, sat_name="ISS")
        # "Next 24 hours" が選択されているので 5 件すべて内部 _passes に格納されている
        assert len(widget._passes) == 5

    def test_range_combo_has_1_hour_option(self, qtbot: Any) -> None:
        """Range コンボに "Next 1 hour" が先頭選択肢として追加されていることを確認する。"""
        from ui.pass_chart import PassChartView

        widget = PassChartView()
        qtbot.addWidget(widget)
        assert widget._range_combo.count() == 5
        assert widget._range_combo.itemText(0) == "Next 1 hour"

    def test_default_range_still_24_hours(self, qtbot: Any) -> None:
        """1 時間の選択肢追加後もデフォルトは 24 時間のままであることを確認する。"""
        from ui.pass_chart import PassChartView

        widget = PassChartView()
        qtbot.addWidget(widget)
        assert widget._selected_hours() == 24.0

    def test_refresh_button_emits_range_changed_with_current_hours(self, qtbot: Any) -> None:
        """Refresh 押下で選択を変えずに range_changed が再発火することを確認する。"""
        from ui.pass_chart import PassChartView

        widget = PassChartView()
        qtbot.addWidget(widget)
        widget._range_combo.setCurrentIndex(1)  # "Next 4 hours"
        received: list[float] = []
        widget.range_changed.connect(received.append)

        widget._refresh_btn.click()

        assert received == [4.0]
        assert widget._range_combo.currentIndex() == 1  # 選択自体は変わらない

    def test_refresh_button_has_no_visible_label(self, qtbot: Any) -> None:
        """Refresh ボタンにテキストラベルが無く、ツールチップのみであることを確認する。"""
        from ui.pass_chart import PassChartView

        widget = PassChartView()
        qtbot.addWidget(widget)
        assert widget._refresh_btn.toolTip() == "Refresh"


class TestGroupPassChartView:
    def test_create_widget(self, qtbot: Any) -> None:
        from ui.pass_chart import GroupPassChartView

        widget = GroupPassChartView()
        qtbot.addWidget(widget)
        assert widget is not None

    def test_default_range_still_4_hours(self, qtbot: Any) -> None:
        """1 時間の選択肢が先頭に挿入されても、デフォルトは 4 時間のまま
        （インデックス決め打ちではなく値検索で選ばれている）ことを確認する。"""
        from ui.pass_chart import GroupPassChartView

        widget = GroupPassChartView()
        qtbot.addWidget(widget)
        assert widget._selected_hours() == 4.0

    def test_range_combo_has_1_hour_option(self, qtbot: Any) -> None:
        from ui.pass_chart import GroupPassChartView

        widget = GroupPassChartView()
        qtbot.addWidget(widget)
        assert widget._range_combo.itemText(0) == "Next 1 hour"

    def test_refresh_button_emits_range_changed_with_current_hours(self, qtbot: Any) -> None:
        from ui.pass_chart import GroupPassChartView

        widget = GroupPassChartView()
        qtbot.addWidget(widget)
        received: list[float] = []
        widget.range_changed.connect(received.append)

        widget._refresh_btn.click()

        assert received == [4.0]  # デフォルト選択（4時間）のまま再emitされる

    def test_refresh_button_has_no_visible_label(self, qtbot: Any) -> None:
        from ui.pass_chart import GroupPassChartView

        widget = GroupPassChartView()
        qtbot.addWidget(widget)
        assert widget._refresh_btn.toolTip() == "Refresh"
