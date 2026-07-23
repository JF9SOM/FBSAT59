"""Unit tests for comms/ax100digi/message.py's GreenCube/MARMOTSat digipeater
message format ($Src > $Dst, $SatName, STORE=$Time $Message)."""

from __future__ import annotations

import pytest

from comms.ax100digi.message import MAX_MESSAGE_LENGTH, build_message, parse_message


def test_parse_manual_example_message() -> None:
    text = "IU0POY > IU0BFO, GreenCube, STORE=5 This a message relayed by GreenCube in 5 seconds"
    parsed = parse_message(text)
    assert parsed is not None
    assert parsed.source == "IU0POY"
    assert parsed.dest == "IU0BFO"
    assert parsed.sat_name == "GreenCube"
    assert parsed.store_seconds == 5
    assert parsed.content == "This a message relayed by GreenCube in 5 seconds"


def test_build_then_parse_roundtrip() -> None:
    text = build_message("JF9SOM", "VA7ABC", "MARMOTSat", "hello via digipeater", store_seconds=0)
    parsed = parse_message(text)
    assert parsed is not None
    assert parsed.source == "JF9SOM"
    assert parsed.dest == "VA7ABC"
    assert parsed.sat_name == "MARMOTSat"
    assert parsed.store_seconds == 0
    assert parsed.content == "hello via digipeater"


def test_parse_rejects_non_matching_text() -> None:
    assert parse_message("this is just raw telemetry, not a digi message") is None


def test_build_raises_if_over_length_limit() -> None:
    with pytest.raises(ValueError):
        build_message("JF9SOM", "VA7ABC", "MARMOTSat", "x" * MAX_MESSAGE_LENGTH)
