"""
SatelliteEngine / PassPredictor / DopplerCalculator のユニットテスト

ISSのサンプルTLEを使い、ネットワーク接続なしで実行できる。
"""

from __future__ import annotations

import math
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from skyfield.api import EarthSatellite, load

from core.engine import (
    _C_KM_S,
    DopplerCalculator,
    DopplerCorrection,
    Observation,
    PassInfo,
    PassPredictor,
    SatelliteEngine,
)

# ---------------------------------------------------------------------------
# テスト用TLEデータ（2024-01-01 エポック付近のISS）
# ---------------------------------------------------------------------------
_ISS_LINE1 = "1 25544U 98067A   24001.50000000  .00016717  00000+0  10270-3 0  9994"
_ISS_LINE2 = "2 25544  51.6400 208.9163 0006828  86.9922 273.1770 15.49212693420559"
_ISS_NORAD = 25544

# 地上局: 東京（国立天文台付近）
_LAT = 35.6762
_LON = 139.6503
_ALT_M = 40.0


# ---------------------------------------------------------------------------
# フィクスチャ
# ---------------------------------------------------------------------------


@pytest.fixture
def ts():
    return load.timescale()


@pytest.fixture
def tle_manager_mock():
    """DB不要のTLEManagerモック。ISSのEarthSatelliteだけ返す。"""
    ts = load.timescale()
    sat = EarthSatellite(_ISS_LINE1, _ISS_LINE2, "ISS (ZARYA)", ts)

    mock = MagicMock()
    mock.get_earth_satellite.side_effect = lambda norad: sat if norad == _ISS_NORAD else None
    return mock


@pytest.fixture
def engine(tle_manager_mock):
    return SatelliteEngine(tle_manager_mock, _LAT, _LON, _ALT_M)


@pytest.fixture
def predictor(tle_manager_mock):
    return PassPredictor(tle_manager_mock, _LAT, _LON, _ALT_M)


# ---------------------------------------------------------------------------
# SatelliteEngine テスト
# ---------------------------------------------------------------------------


class TestSatelliteEngine:
    def test_observe_returns_observation_type(self, engine: SatelliteEngine) -> None:
        obs = engine.observe(_ISS_NORAD, at=datetime(2024, 1, 2, 0, 36, 57, tzinfo=UTC))
        assert isinstance(obs, Observation)

    def test_observe_fields_are_finite(self, engine: SatelliteEngine) -> None:
        obs = engine.observe(_ISS_NORAD, at=datetime(2024, 1, 2, 0, 36, 57, tzinfo=UTC))
        assert obs is not None
        assert math.isfinite(obs.elevation_deg)
        assert math.isfinite(obs.azimuth_deg)
        assert math.isfinite(obs.range_km)
        assert math.isfinite(obs.range_rate_km_s)

    def test_observe_elevation_range(self, engine: SatelliteEngine) -> None:
        obs = engine.observe(_ISS_NORAD, at=datetime(2024, 1, 2, 0, 36, 57, tzinfo=UTC))
        assert obs is not None
        assert -90.0 <= obs.elevation_deg <= 90.0

    def test_observe_azimuth_range(self, engine: SatelliteEngine) -> None:
        obs = engine.observe(_ISS_NORAD, at=datetime(2024, 1, 2, 0, 36, 57, tzinfo=UTC))
        assert obs is not None
        assert 0.0 <= obs.azimuth_deg < 360.0

    def test_observe_range_positive(self, engine: SatelliteEngine) -> None:
        obs = engine.observe(_ISS_NORAD, at=datetime(2024, 1, 2, 0, 36, 57, tzinfo=UTC))
        assert obs is not None
        assert obs.range_km > 0.0

    def test_tca_has_high_elevation(self, engine: SatelliteEngine) -> None:
        """TCA付近では仰角が高いはず（このパスは35度超）"""
        obs = engine.observe(_ISS_NORAD, at=datetime(2024, 1, 2, 0, 36, 57, tzinfo=UTC))
        assert obs is not None
        assert obs.elevation_deg > 30.0

    def test_observe_is_above_horizon_consistent(self, engine: SatelliteEngine) -> None:
        obs = engine.observe(_ISS_NORAD, at=datetime(2024, 1, 2, 0, 36, 57, tzinfo=UTC))
        assert obs is not None
        assert obs.is_above_horizon == (obs.elevation_deg > 0.0)

    def test_observe_below_horizon(self, engine: SatelliteEngine) -> None:
        """地平線下（正午UTC）では is_above_horizon=False"""
        obs = engine.observe(_ISS_NORAD, at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC))
        assert obs is not None
        assert not obs.is_above_horizon
        assert obs.elevation_deg < 0.0

    def test_observe_unknown_norad_returns_none(self, engine: SatelliteEngine) -> None:
        obs = engine.observe(99999)
        assert obs is None

    def test_observe_naive_datetime_treated_as_utc(self, engine: SatelliteEngine) -> None:
        """タイムゾーンなしのdatetimeはUTCとして扱う"""
        aware = engine.observe(_ISS_NORAD, at=datetime(2024, 1, 2, 0, 36, 57, tzinfo=UTC))
        naive = engine.observe(_ISS_NORAD, at=datetime(2024, 1, 2, 0, 36, 57))
        assert aware is not None and naive is not None
        assert abs(aware.elevation_deg - naive.elevation_deg) < 1e-9

    def test_observe_now_does_not_raise(self, engine: SatelliteEngine) -> None:
        """at=Noneで現在時刻を使っても例外が出ない"""
        obs = engine.observe(_ISS_NORAD)
        assert obs is not None

    def test_observe_multi_returns_dict(self, engine: SatelliteEngine) -> None:
        at = datetime(2024, 1, 2, 0, 36, 57, tzinfo=UTC)
        result = engine.observe_multi([_ISS_NORAD, 99999], at=at)
        assert _ISS_NORAD in result
        assert 99999 not in result

    def test_observe_multi_with_subpoints_returns_dict(self, engine: SatelliteEngine) -> None:
        at = datetime(2024, 1, 2, 0, 36, 57, tzinfo=UTC)
        result = engine.observe_multi_with_subpoints([_ISS_NORAD, 99999], at=at)
        assert _ISS_NORAD in result
        assert 99999 not in result

    def test_observe_multi_with_subpoints_matches_separate_calls(
        self, engine: SatelliteEngine
    ) -> None:
        """The combined method computes each satellite's position once
        instead of twice (see observe_multi_with_subpoints' docstring) — it
        must return results identical to calling subpoint() and observe()
        separately at the same instant, not just approximately close."""
        at = datetime(2024, 1, 2, 0, 36, 57, tzinfo=UTC)

        expected_subpoint = engine.subpoint(_ISS_NORAD, at=at)
        expected_obs = engine.observe(_ISS_NORAD, at=at)
        assert expected_subpoint is not None
        assert expected_obs is not None

        combined = engine.observe_multi_with_subpoints([_ISS_NORAD], at=at)
        result = combined[_ISS_NORAD]

        assert result.subpoint_lat_deg == expected_subpoint[0]
        assert result.subpoint_lon_deg == expected_subpoint[1]
        assert result.observation.elevation_deg == expected_obs.elevation_deg
        assert result.observation.azimuth_deg == expected_obs.azimuth_deg
        assert result.observation.range_km == expected_obs.range_km
        assert result.observation.range_rate_km_s == expected_obs.range_rate_km_s
        assert result.observation.is_above_horizon == expected_obs.is_above_horizon

    def test_orbital_period_minutes_iss_is_about_92(self, engine: SatelliteEngine) -> None:
        """ISS orbital period is ~92 min (mean motion ~15.5 rev/day)."""
        period = engine.orbital_period_minutes(_ISS_NORAD)
        assert period is not None
        assert 88.0 < period < 96.0

    def test_orbital_period_minutes_unknown_norad_returns_none(
        self, engine: SatelliteEngine
    ) -> None:
        assert engine.orbital_period_minutes(99999) is None

    def test_ground_track_returns_expected_sample_count(self, engine: SatelliteEngine) -> None:
        start = datetime(2024, 1, 2, 0, 0, 0, tzinfo=UTC)
        end = datetime(2024, 1, 2, 1, 0, 0, tzinfo=UTC)
        track = engine.ground_track(_ISS_NORAD, start, end, step_s=60.0)
        # 3600 s / 60 s + 1 endpoint = 61 samples
        assert len(track) == 61
        for lat, lon in track:
            assert -90.0 <= lat <= 90.0
            assert -180.0 <= lon <= 180.0

    def test_ground_track_first_sample_matches_subpoint(self, engine: SatelliteEngine) -> None:
        start = datetime(2024, 1, 2, 0, 0, 0, tzinfo=UTC)
        end = datetime(2024, 1, 2, 0, 30, 0, tzinfo=UTC)
        track = engine.ground_track(_ISS_NORAD, start, end, step_s=60.0)
        sp = engine.subpoint(_ISS_NORAD, at=start)
        assert sp is not None
        assert track[0][0] == pytest.approx(sp[0], abs=1e-6)
        assert track[0][1] == pytest.approx(sp[1], abs=1e-6)

    def test_ground_track_moves_over_time(self, engine: SatelliteEngine) -> None:
        start = datetime(2024, 1, 2, 0, 0, 0, tzinfo=UTC)
        end = datetime(2024, 1, 2, 0, 10, 0, tzinfo=UTC)
        track = engine.ground_track(_ISS_NORAD, start, end, step_s=60.0)
        assert track[0] != track[-1]

    def test_ground_track_naive_datetime_treated_as_utc(self, engine: SatelliteEngine) -> None:
        aware = engine.ground_track(
            _ISS_NORAD,
            datetime(2024, 1, 2, 0, 0, 0, tzinfo=UTC),
            datetime(2024, 1, 2, 0, 10, 0, tzinfo=UTC),
            step_s=60.0,
        )
        naive = engine.ground_track(
            _ISS_NORAD,
            datetime(2024, 1, 2, 0, 0, 0),  # noqa: DTZ001
            datetime(2024, 1, 2, 0, 10, 0),  # noqa: DTZ001
            step_s=60.0,
        )
        assert aware == naive

    def test_ground_track_unknown_norad_returns_empty(self, engine: SatelliteEngine) -> None:
        assert (
            engine.ground_track(
                99999,
                datetime(2024, 1, 2, 0, 0, 0, tzinfo=UTC),
                datetime(2024, 1, 2, 1, 0, 0, tzinfo=UTC),
            )
            == []
        )

    def test_ground_track_non_positive_window_returns_empty(self, engine: SatelliteEngine) -> None:
        t = datetime(2024, 1, 2, 0, 0, 0, tzinfo=UTC)
        assert engine.ground_track(_ISS_NORAD, t, t) == []
        assert engine.ground_track(_ISS_NORAD, t, t - timedelta(minutes=5)) == []

    def test_cache_populated_after_first_observe(self, engine: SatelliteEngine) -> None:
        engine.observe(_ISS_NORAD, at=datetime(2024, 1, 2, 0, 36, 57, tzinfo=UTC))
        with engine._cache_lock:
            assert _ISS_NORAD in engine._sat_cache

    def test_invalidate_cache_single(self, engine: SatelliteEngine) -> None:
        engine.observe(_ISS_NORAD, at=datetime(2024, 1, 2, 0, 36, 57, tzinfo=UTC))
        engine.invalidate_cache(_ISS_NORAD)
        with engine._cache_lock:
            assert _ISS_NORAD not in engine._sat_cache

    def test_invalidate_cache_all(self, engine: SatelliteEngine) -> None:
        engine.observe(_ISS_NORAD, at=datetime(2024, 1, 2, 0, 36, 57, tzinfo=UTC))
        engine.invalidate_cache()
        with engine._cache_lock:
            assert len(engine._sat_cache) == 0

    def test_thread_safety(self, engine: SatelliteEngine) -> None:
        """複数スレッドから同時にobserveを呼んでも例外が出ない"""
        errors: list[Exception] = []
        at = datetime(2024, 1, 2, 0, 36, 57, tzinfo=UTC)

        def worker() -> None:
            try:
                engine.observe(_ISS_NORAD, at=at)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"

    def test_range_rate_sign_approaching(self, engine: SatelliteEngine) -> None:
        """AOS直前は接近中なので range_rate < 0 のはず"""
        # AOS は 2024-01-02 00:32:53 UTC なので少し前
        obs = engine.observe(_ISS_NORAD, at=datetime(2024, 1, 2, 0, 32, 0, tzinfo=UTC))
        assert obs is not None
        # この時点では接近中
        assert obs.range_rate_km_s < 0.0

    def test_range_rate_sign_receding(self, engine: SatelliteEngine) -> None:
        """LOS直後は離遠中なので range_rate > 0 のはず"""
        # LOS は 2024-01-02 00:41:02 UTC なので少し後
        obs = engine.observe(_ISS_NORAD, at=datetime(2024, 1, 2, 0, 42, 0, tzinfo=UTC))
        assert obs is not None
        assert obs.range_rate_km_s > 0.0


# ---------------------------------------------------------------------------
# PassPredictor テスト
# ---------------------------------------------------------------------------


class TestPassPredictor:
    _START = datetime(2024, 1, 2, 0, 0, 0, tzinfo=UTC)
    _END = datetime(2024, 1, 3, 0, 0, 0, tzinfo=UTC)

    def test_returns_list(self, predictor: PassPredictor) -> None:
        passes = predictor.get_passes(_ISS_NORAD, self._START, self._END)
        assert isinstance(passes, list)

    def test_finds_passes(self, predictor: PassPredictor) -> None:
        passes = predictor.get_passes(_ISS_NORAD, self._START, self._END)
        assert len(passes) > 0

    def test_pass_info_type(self, predictor: PassPredictor) -> None:
        passes = predictor.get_passes(_ISS_NORAD, self._START, self._END)
        assert all(isinstance(p, PassInfo) for p in passes)

    def test_aos_before_los(self, predictor: PassPredictor) -> None:
        passes = predictor.get_passes(_ISS_NORAD, self._START, self._END)
        for p in passes:
            assert p.aos < p.tca < p.los

    def test_duration_consistent(self, predictor: PassPredictor) -> None:
        passes = predictor.get_passes(_ISS_NORAD, self._START, self._END)
        for p in passes:
            expected = (p.los - p.aos).total_seconds()
            assert abs(p.duration_s - expected) < 1.0

    def test_max_elevation_above_min(self, predictor: PassPredictor) -> None:
        min_el = 5.0
        passes = predictor.get_passes(_ISS_NORAD, self._START, self._END, min_elevation_deg=min_el)
        for p in passes:
            assert p.max_elevation_deg >= min_el - 0.5  # Skyfield境界の微小誤差を許容

    def test_higher_min_elevation_fewer_passes(self, predictor: PassPredictor) -> None:
        passes_5 = predictor.get_passes(_ISS_NORAD, self._START, self._END, min_elevation_deg=5.0)
        passes_30 = predictor.get_passes(_ISS_NORAD, self._START, self._END, min_elevation_deg=30.0)
        assert len(passes_30) <= len(passes_5)

    def test_azimuth_in_range(self, predictor: PassPredictor) -> None:
        passes = predictor.get_passes(_ISS_NORAD, self._START, self._END)
        for p in passes:
            assert 0.0 <= p.aos_azimuth_deg < 360.0
            assert 0.0 <= p.los_azimuth_deg < 360.0

    def test_unknown_norad_returns_empty(self, predictor: PassPredictor) -> None:
        passes = predictor.get_passes(99999, self._START, self._END)
        assert passes == []

    def test_known_pass_time(self, predictor: PassPredictor) -> None:
        """最初のパスのAOSが期待する時刻付近（±2分）か確認"""
        passes = predictor.get_passes(_ISS_NORAD, self._START, self._END)
        assert len(passes) > 0
        first = passes[0]
        expected_aos = datetime(2024, 1, 2, 0, 32, 53, tzinfo=UTC)
        delta = abs((first.aos - expected_aos).total_seconds())
        assert delta < 120, f"AOS差分 {delta:.0f}秒 (期待: ±120秒)"


# ---------------------------------------------------------------------------
# DopplerCalculator テスト
# ---------------------------------------------------------------------------


class TestDopplerCalculator:
    # ISS VHF FM ダウンリンク
    _DL_HZ = 145_800_000.0  # 145.800 MHz
    _UL_HZ = 144_200_000.0  # 144.200 MHz (仮)

    def test_shift_zero_at_rest(self) -> None:
        assert DopplerCalculator.shift_hz(self._DL_HZ, 0.0) == pytest.approx(0.0)

    def test_shift_negative_when_receding(self) -> None:
        """衛星が離れていく → 受信周波数は低い → shift < 0"""
        shift = DopplerCalculator.shift_hz(self._DL_HZ, range_rate_km_s=5.0)
        assert shift < 0.0

    def test_shift_positive_when_approaching(self) -> None:
        shift = DopplerCalculator.shift_hz(self._DL_HZ, range_rate_km_s=-5.0)
        assert shift > 0.0

    def test_shift_magnitude(self) -> None:
        """145.800 MHzで range_rate=7 km/s のシフト量を確認"""
        expected = -self._DL_HZ * 7.0 / _C_KM_S
        assert DopplerCalculator.shift_hz(self._DL_HZ, 7.0) == pytest.approx(expected, rel=1e-9)

    def test_correct_downlink_approaching(self) -> None:
        freq, shift = DopplerCalculator.correct_downlink(self._DL_HZ, range_rate_km_s=-7.0)
        assert freq > self._DL_HZ  # 接近時は補正後周波数が高い
        assert shift > 0.0

    def test_correct_downlink_receding(self) -> None:
        freq, shift = DopplerCalculator.correct_downlink(self._DL_HZ, range_rate_km_s=7.0)
        assert freq < self._DL_HZ
        assert shift < 0.0

    def test_correct_uplink_is_opposite_to_downlink(self) -> None:
        """UL correction is ALWAYS opposite to DL (transmit lower when approaching)."""
        _, dl_shift = DopplerCalculator.correct_downlink(self._DL_HZ, range_rate_km_s=-7.0)
        _, ul_shift = DopplerCalculator.correct_uplink(self._UL_HZ, range_rate_km_s=-7.0)
        # DL shift is positive (approaching); UL shift is negative (transmit lower).
        assert dl_shift > 0 and ul_shift < 0

    def test_correct_uplink_arrives_at_satellite_on_nominal(self) -> None:
        """The whole point of the UL correction: whatever the range rate, the
        signal must reach the satellite's receiver on the nominal frequency."""
        for rr in (-7.0, -3.0, 0.0, 3.0, 7.0):
            ul_corr, _ = DopplerCalculator.correct_uplink(self._UL_HZ, range_rate_km_s=rr)
            at_satellite = ul_corr * (1.0 - rr / _C_KM_S)
            assert at_satellite == pytest.approx(self._UL_HZ, rel=1e-9)

    def test_correct_transponder_returns_dataclass(self) -> None:
        result = DopplerCalculator.correct_transponder(
            self._DL_HZ, self._UL_HZ, range_rate_km_s=-7.0
        )
        assert isinstance(result, DopplerCorrection)

    def test_correct_transponder_rx_only(self) -> None:
        """アップリンクNoneでも動く（受信専用ビーコン）"""
        result = DopplerCalculator.correct_transponder(self._DL_HZ, None, range_rate_km_s=3.0)
        assert result.uplink_hz is None
        assert result.uplink_shift_hz is None
        assert result.downlink_hz != self._DL_HZ

    def test_inverting_transponder_own_signal_stays_put(self) -> None:
        """GitHub Issue #16 regression: through an INVERTING transponder, your
        own signal must land on the same spot in your own receiver's passband
        no matter what the range rate is.

        correct_uplink() used to take an `invert` flag that flipped the UL
        correction to match the DL direction. That was physically wrong: it
        made an operator's own signal sweep across the passband by
        2 x uplink_freq x (range_rate/c) -- about +-5.8 kHz over a pass on
        RS-44 -- which is exactly what was observed on-air.
        """
        dl_nom, ul_nom = 435.612e6, 145.993e6  # RS-44 FT4 calling frequency
        transponder_sum = dl_nom + ul_nom  # inverting: f_down_sat = sum - f_up_at_sat
        audio_offset = 1500.0

        positions = []
        for rr in (-6.0, -3.0, 0.0, 3.0, 6.0):
            corr = DopplerCalculator.correct_transponder(dl_nom, ul_nom, range_rate_km_s=rr)
            assert corr.uplink_hz is not None
            # Round trip: our TX -> uplink Doppler -> transponder -> downlink Doppler
            at_satellite = (corr.uplink_hz + audio_offset) * (1.0 - rr / _C_KM_S)
            sat_transmits = transponder_sum - at_satellite
            received = sat_transmits * (1.0 - rr / _C_KM_S)
            positions.append(received - corr.downlink_hz)

        # Well under one FT4 tone spacing (20.83 Hz) of movement across the pass.
        assert max(positions) - min(positions) < 1.0

    def test_iss_doppler_realistic(self, engine: SatelliteEngine) -> None:
        """ISSのTCA付近でドップラーシフトが ±10 kHz 以内か確認（145 MHz帯）"""
        obs = engine.observe(_ISS_NORAD, at=datetime(2024, 1, 2, 0, 36, 57, tzinfo=UTC))
        assert obs is not None
        _, shift = DopplerCalculator.correct_downlink(self._DL_HZ, obs.range_rate_km_s)
        assert abs(shift) < 10_000  # TCA付近ではシフトが小さい

    def test_doppler_shift_large_before_aos(self, engine: SatelliteEngine) -> None:
        """AOS直前は高速接近中 → シフトが大きい（>1 kHz）"""
        obs = engine.observe(_ISS_NORAD, at=datetime(2024, 1, 2, 0, 32, 0, tzinfo=UTC))
        assert obs is not None
        _, shift = DopplerCalculator.correct_downlink(self._DL_HZ, obs.range_rate_km_s)
        assert shift > 1_000  # 接近中なのでシフトは正・大
