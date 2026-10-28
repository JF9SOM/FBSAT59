"""Rotator frame helpers: stop point and antenna flip.

A rotator cannot turn through its mechanical stop without unwinding (a full
turn back, a minute or more). Where that stop is differs by model: a typical
rotator stops at north, a south-stop rotator (north in the middle of its
range) at south. The tracking code treats azimuth 0/360 as the stop, so
azimuths are shifted into a frame measured from the real stop. A pass that
would cross the stop can instead be flown flipped: the azimuth is turned by
180 degrees and the elevation sent as 180 - el, which points the antenna at
the same spot from the other side without crossing the stop.
"""

from __future__ import annotations

from collections.abc import Sequence


def to_frame_az(bearing_deg: float, flip: bool, stop_az: float) -> float:
    """Real-world bearing -> azimuth measured from the rotator's stop."""
    return (bearing_deg + (180.0 if flip else 0.0) - stop_az) % 360.0


def to_bearing(frame_az: float, flipped: bool, stop_az: float) -> float:
    """Inverse of to_frame_az()."""
    return (frame_az + stop_az - (180.0 if flipped else 0.0)) % 360.0


def path_crosses_azimuth(azimuths: Sequence[float], target_az: float) -> bool:
    """Whether a sampled azimuth path goes through target_az.

    Consecutive samples are taken to move by the short way round, so a jump
    of 180 degrees or more between two samples is not read as a crossing.
    """
    prev: float | None = None
    for az in azimuths:
        # Signed offset from the target, in [-180, 180).
        d = (az - target_az + 180.0) % 360.0 - 180.0
        if prev is not None and prev * d <= 0.0 and abs(prev - d) < 180.0:
            return True
        prev = d
    return False
