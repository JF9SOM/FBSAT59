"""The rotator keeps the pass's pose during the stand-by time after LOS."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from ui.main_window import MainWindow


def _stub(next_pass_flip: bool, margin_s: float = 60.0) -> Any:
    """A minimal stand-in for MainWindow with just what _rot_target_for needs."""
    stub = SimpleNamespace(
        _rot_last_up=None,
        _rot_flip_for=lambda norad, above: next_pass_flip,
        _rot_active_plan=lambda norad, at: None,
        _pass_margin_seconds=lambda: margin_s,
        _rot_az=lambda bearing, flip: bearing + (180.0 if flip else 0.0),
    )
    return stub


def test_pose_held_after_los_within_margin() -> None:
    """After LOS the next pass is flipped, but the old pose is kept for the margin."""
    stub = _stub(next_pass_flip=True)
    stub._rot_last_up = (25544, time.monotonic(), False)
    az, el, flip = MainWindow._rot_target_for(stub, 25544, 80.0, -0.1)
    assert flip is False
    assert az == 80.0
    assert el == -0.1


def test_pose_follows_next_pass_after_margin() -> None:
    """Once the margin has passed the next pass's pose is used."""
    stub = _stub(next_pass_flip=True, margin_s=60.0)
    stub._rot_last_up = (25544, time.monotonic() - 120.0, False)
    _, _, flip = MainWindow._rot_target_for(stub, 25544, 80.0, -1.0)
    assert flip is True


def test_pose_recorded_while_above_horizon() -> None:
    """A command sent above the horizon becomes the pose to hold."""
    stub = _stub(next_pass_flip=False)
    MainWindow._rot_target_for(stub, 25544, 80.0, 10.0)
    assert stub._rot_last_up is not None
    assert stub._rot_last_up[0] == 25544
    assert stub._rot_last_up[2] is False


def test_no_hold_for_another_satellite() -> None:
    """The held pose belongs to the satellite that was tracked."""
    stub = _stub(next_pass_flip=True)
    stub._rot_last_up = (99999, time.monotonic(), False)
    _, _, flip = MainWindow._rot_target_for(stub, 25544, 80.0, -0.1)
    assert flip is True
