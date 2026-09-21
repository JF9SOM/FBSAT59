"""IQ recording start time taken from the file name (sdr/file_device.py).

Pure functions only, so this runs without scipy (SdrFileDevice itself needs it
and is covered in test_file_device.py).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sdr.file_device import parse_start_time_from_filename


def test_recorder_file_names_carry_a_utc_start_time() -> None:
    got = parse_start_time_from_filename("0_unknown_20260920T065551Z.iq.wav")
    assert got == datetime(2026, 9, 20, 6, 55, 51, tzinfo=UTC)


def test_norad_and_satellite_name_do_not_confuse_it() -> None:
    got = parse_start_time_from_filename("68796_ARICA-2_20260920T070734Z.iq.wav")
    assert got == datetime(2026, 9, 20, 7, 7, 34, tzinfo=UTC)


@pytest.mark.parametrize(
    "name", ["recording.iq.wav", "20260920_065551.iq.wav", "x_20261340T256199Z.iq.wav", ""]
)
def test_names_without_a_valid_time_give_none(name: str) -> None:
    assert parse_start_time_from_filename(name) is None
