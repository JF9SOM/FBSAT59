"""Tests for the rotator stop-point / flip frame helpers."""

from __future__ import annotations

import pytest

from core.rotator_frame import path_crosses_azimuth, to_bearing, to_frame_az


class TestFrameConversion:
    def test_north_stop_without_flip_is_identity(self) -> None:
        assert to_frame_az(123.0, False, 0.0) == pytest.approx(123.0)

    def test_south_stop_puts_south_at_the_seam(self) -> None:
        assert to_frame_az(180.0, False, 180.0) == pytest.approx(0.0)
        assert to_frame_az(170.0, False, 180.0) == pytest.approx(350.0)

    def test_flip_turns_azimuth_by_180(self) -> None:
        # South stop + flipped: the real bearing is the frame azimuth.
        assert to_frame_az(150.0, True, 180.0) == pytest.approx(150.0)
        # North stop + flipped.
        assert to_frame_az(20.0, True, 0.0) == pytest.approx(200.0)

    @pytest.mark.parametrize("stop", [0.0, 180.0])
    @pytest.mark.parametrize("flip", [False, True])
    @pytest.mark.parametrize("bearing", [0.0, 1.0, 90.0, 179.0, 180.0, 181.0, 270.0, 359.0])
    def test_round_trip(self, bearing: float, flip: bool, stop: float) -> None:
        frame = to_frame_az(bearing, flip, stop)
        assert to_bearing(frame, flip, stop) == pytest.approx(bearing % 360.0)


class TestPathCrosses:
    def test_path_over_the_stop_crosses(self) -> None:
        assert path_crosses_azimuth([150.0, 170.0, 190.0, 210.0], 180.0)

    def test_path_touching_the_stop_crosses(self) -> None:
        assert path_crosses_azimuth([170.0, 180.0, 190.0], 180.0)

    def test_path_that_stays_on_one_side_does_not(self) -> None:
        assert not path_crosses_azimuth([10.0, 60.0, 120.0, 175.0], 180.0)

    def test_north_stop_crossing_over_the_wrap(self) -> None:
        assert path_crosses_azimuth([350.0, 355.0, 5.0, 10.0], 0.0)

    def test_wrap_is_not_mistaken_for_crossing_the_opposite_point(self) -> None:
        # Moving 350 -> 10 jumps the +-180 seam of the offset from 180 but
        # does not go through 180.
        assert not path_crosses_azimuth([350.0, 355.0, 5.0, 10.0], 180.0)

    def test_empty_and_single_sample(self) -> None:
        assert not path_crosses_azimuth([], 180.0)
        assert not path_crosses_azimuth([180.0], 180.0)
