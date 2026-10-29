"""Over-the-top plan for high-elevation passes on rotators that can reach 180 deg.

A pass that goes (nearly) through the zenith makes a normal az/el rotator
swing its azimuth by up to 180 degrees within a minute. A rotator whose
elevation axis reaches 180 degrees can instead go over the top: below
HOLD_START_EL the antenna follows the real azimuth and elevation, above it the
azimuth is held (and only turned by the small difference between the two
sides) while the elevation follows the satellite's angle along the vertical
plane of the pass, which is 90 degrees at the time of closest approach, then
continues down the far side in the opposite pose (az + 180, el sent as
180 - el), and follows the real azimuth and elevation
again once the satellite is back below HOLD_START_EL.

Azimuths here are in the controller frame ("u", measured from the rotator's
mechanical stop, see core.rotator_frame). Which of the two poses is used for
the ascent is chosen per pass so that the azimuth needs the least travel and
does not wrap through the stop.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timedelta

from core.rotator_frame import to_frame_az

# Passes whose maximum elevation reaches this are flown over the top.
OVERHEAD_MIN_MAX_EL_DEG = 80.0
# Below this elevation the real azimuth and elevation are followed.
HOLD_START_EL_DEG = 50.0

# (real-world bearing, elevation) of the satellite at a time, or None.
Observe = Callable[[datetime], tuple[float, float] | None]


class OverheadPlan:
    """Rotator targets for one over-the-top pass: target(t) -> (u az, el, flip)."""

    def __init__(
        self,
        aos: datetime,
        tca: datetime,
        los: datetime,
        t_in: datetime,
        t_out: datetime,
        flip_up: bool,
        seam: float,
        observe: Observe,
        az_in: float,
        az_out: float,
    ) -> None:
        self.aos = aos
        self.tca = tca
        self.los = los
        self.t_in = t_in
        self.t_out = t_out
        self.flip_up = flip_up
        self._seam = seam
        self._observe = observe
        # Frame azimuths at the two ends of the held section.
        self.u_in = to_frame_az(az_in, flip_up, seam)
        self.u_out = to_frame_az(az_out, not flip_up, seam)
        # Bearing of the vertical plane the antenna stays in: the average of
        # the ascent bearing and the (reversed) descent bearing.
        a, b = math.radians(az_in), math.radians(az_out + 180.0)
        self._plane_bearing = math.degrees(
            math.atan2(math.sin(a) + math.sin(b), math.cos(a) + math.cos(b))
        )

    def target(self, t: datetime) -> tuple[float, float, bool] | None:
        """(controller-frame azimuth, true elevation, flipped) at t, or None
        if t is outside the pass."""
        if t < self.aos or t > self.los:
            return None
        if t < self.t_in or t > self.t_out:
            flip = self.flip_up if t < self.t_in else not self.flip_up
            obs = self._observe(t)
            if obs is None:
                return None
            return to_frame_az(obs[0], flip, self._seam), obs[1], flip
        # Held section: azimuth moves linearly from u_in to u_out, elevation
        # goes to exactly 90 degrees at closest approach.
        span = (self.t_out - self.t_in).total_seconds()
        frac = (t - self.t_in).total_seconds() / span if span > 0.0 else 0.0
        az = self.u_in + (self.u_out - self.u_in) * frac
        obs = self._observe(t)
        if obs is None:
            return None
        # Angle of the satellite along the plane (tan x = tan(zenith angle) *
        # cos(azimuth offset from the plane)): 90 - |x| is the elevation that
        # keeps the antenna in the plane, and equals the real elevation except
        # near the zenith, where it reaches exactly 90 at closest approach.
        zenith = math.radians(90.0 - obs[1])
        along = math.degrees(
            math.atan(math.tan(zenith) * abs(math.cos(math.radians(obs[0] - self._plane_bearing))))
        )
        return az, 90.0 - along, self.flip_up if t <= self.tca else not self.flip_up


def _cross_time(observe: Observe, early: datetime, late: datetime, rising: bool) -> datetime | None:
    """Time in [early, late] where the elevation crosses HOLD_START_EL_DEG
    (rising: low at early, high at late; otherwise the reverse)."""
    lo, hi = early, late
    for _ in range(24):
        mid = lo + (hi - lo) / 2
        obs = observe(mid)
        if obs is None:
            return None
        if (obs[1] < HOLD_START_EL_DEG) == rising:
            lo = mid
        else:
            hi = mid
    return lo + (hi - lo) / 2


def _path_travel(plan: OverheadPlan) -> float:
    """Total azimuth travel (frame degrees, no wrapping) along the pass."""
    total = 0.0
    prev: float | None = None
    t = plan.aos
    step = timedelta(seconds=2)
    while t <= plan.los:
        tgt = plan.target(t)
        if tgt is not None:
            if prev is not None:
                total += abs(tgt[0] - prev)
            prev = tgt[0]
        t += step
    return total


def build_overhead_plan(
    aos: datetime,
    tca: datetime,
    los: datetime,
    max_el_deg: float,
    seam: float,
    observe: Observe,
) -> OverheadPlan | None:
    """Plan for a pass, or None if it does not need to go over the top."""
    if max_el_deg < OVERHEAD_MIN_MAX_EL_DEG:
        return None
    t_in = _cross_time(observe, aos, tca, rising=True)
    t_out = _cross_time(observe, tca, los, rising=False)
    if t_in is None or t_out is None or not (aos < t_in < tca < t_out < los):
        return None
    obs_in = observe(t_in)
    obs_out = observe(t_out)
    if obs_in is None or obs_out is None:
        return None
    best: OverheadPlan | None = None
    best_cost = 0.0
    for flip_up in (False, True):
        plan = OverheadPlan(
            aos, tca, los, t_in, t_out, flip_up, seam, observe, obs_in[0], obs_out[0]
        )
        cost = _path_travel(plan)
        if best is None or cost < best_cost:
            best, best_cost = plan, cost
    return best
