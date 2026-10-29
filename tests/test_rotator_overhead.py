"""Over-the-top plan for high-elevation passes (core.rotator_overhead)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from core.rotator_overhead import (
    HOLD_START_EL_DEG,
    OverheadPlan,
    build_overhead_plan,
)

T0 = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)  # time of closest approach
_ALT_KM = 450.0
_SPEED_KMS = 7.0
_HALF_SPAN_S = 600.0


def _pass(
    track_bearing: float, miss_km: float
) -> tuple[datetime, datetime, datetime, float, object]:
    """Flat-earth straight pass: (aos, tca, los, max_el, observe)."""
    ux, uy = math.sin(math.radians(track_bearing)), math.cos(math.radians(track_bearing))
    px, py = uy, -ux

    def observe(t: datetime) -> tuple[float, float] | None:
        x = _SPEED_KMS * (t - T0).total_seconds()
        e, n = ux * x + px * miss_km, uy * x + py * miss_km
        az = math.degrees(math.atan2(e, n)) % 360.0
        return az, math.degrees(math.atan2(_ALT_KM, math.hypot(e, n)))

    max_el = math.degrees(math.atan2(_ALT_KM, miss_km))
    span = timedelta(seconds=_HALF_SPAN_S)
    return T0 - span, T0, T0 + span, max_el, observe


def _plan(track_bearing: float, miss_km: float, seam: float) -> OverheadPlan:
    aos, tca, los, max_el, observe = _pass(track_bearing, miss_km)
    plan = build_overhead_plan(aos, tca, los, max_el, seam, observe)  # type: ignore[arg-type]
    assert plan is not None
    return plan


def _targets(plan: OverheadPlan) -> list[tuple[float, float, bool]]:
    out = []
    t = plan.aos
    while t <= plan.los:
        tgt = plan.target(t)
        assert tgt is not None
        out.append(tgt)
        t += timedelta(seconds=1)
    return out


def _max_az_rate(targets: list[tuple[float, float, bool]]) -> float:
    return max(abs(b[0] - a[0]) for a, b in zip(targets, targets[1:], strict=False))


def test_no_plan_below_the_threshold() -> None:
    aos, tca, los, max_el, observe = _pass(10.0, 100.0)  # max el about 77 deg
    assert max_el < 80.0
    assert build_overhead_plan(aos, tca, los, max_el, 0.0, observe) is None  # type: ignore[arg-type]


def test_north_to_south_pass_turns_only_a_little_with_a_north_stop() -> None:
    # Rises in the north, sets in the south, max elevation about 85 deg.
    plan = _plan(183.0, 40.0, seam=0.0)
    # The ascent is flown flipped so the azimuth does not wrap through north.
    assert plan.flip_up is True
    targets = _targets(plan)
    azimuths = [t[0] for t in targets]
    # Whole-pass travel stays small (a normal pose would sweep ~180 deg).
    assert max(azimuths) - min(azimuths) < 20.0
    assert _max_az_rate(targets) < 1.0


def test_normal_tracking_would_need_a_fast_azimuth_swing() -> None:
    # Reference for the test above: the real azimuth of this pass.
    _aos, _tca, _los, _max_el, observe = _pass(183.0, 40.0)
    rates = []
    prev = None
    for s in range(-600, 600):
        o = observe(T0 + timedelta(seconds=s))
        assert o is not None
        if prev is not None:
            d = abs(o[0] - prev)
            rates.append(min(d, 360.0 - d))
        prev = o[0]
    assert max(rates) > 8.0


def test_elevation_goes_to_exactly_90_at_closest_approach() -> None:
    plan = _plan(225.0, 40.0, seam=0.0)
    tgt = plan.target(T0)
    assert tgt is not None
    assert abs(tgt[1] - 90.0) < 0.5
    # Pose changes at the top; both poses are the same direction there.
    before = plan.target(T0 - timedelta(seconds=1))
    after = plan.target(T0 + timedelta(seconds=1))
    assert before is not None and after is not None
    assert before[2] != after[2]
    assert abs(before[0] - after[0]) < 1.0


def test_follows_the_real_azimuth_and_elevation_below_the_hold_elevation() -> None:
    aos, tca, los, max_el, observe = _pass(225.0, 40.0)
    plan = build_overhead_plan(aos, tca, los, max_el, 0.0, observe)  # type: ignore[arg-type]
    assert plan is not None
    t = aos + timedelta(seconds=30)
    tgt = plan.target(t)
    real = observe(t)  # type: ignore[operator]
    assert tgt is not None and real is not None
    assert abs(tgt[1] - real[1]) < 1e-6
    assert real[1] < HOLD_START_EL_DEG
    # The azimuth is the real bearing shifted into the controller frame.
    expected = (real[0] + (180.0 if tgt[2] else 0.0)) % 360.0
    assert abs(tgt[0] - expected) < 1e-6


def test_azimuth_never_wraps_through_the_stop() -> None:
    for bearing in (3.0, 183.0, 90.0, 180.0, 225.0, 300.0, 355.0):
        for seam in (0.0, 180.0):
            targets = _targets(_plan(bearing, 30.0, seam))
            assert _max_az_rate(targets) < 5.0, (bearing, seam)


def test_outside_the_pass_there_is_no_target() -> None:
    plan = _plan(225.0, 40.0, seam=0.0)
    assert plan.target(plan.aos - timedelta(seconds=1)) is None
    assert plan.target(plan.los + timedelta(seconds=1)) is None
