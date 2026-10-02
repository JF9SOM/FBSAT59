"""Tests for comms.telemetry.baud_detect."""

import pytest

from comms.telemetry.baud_detect import (
    detect_baud_from_satyaml,
    detect_baud_from_text,
    detect_baud_from_transmitter,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("9k6 FSK AX25", "9600"),
        ("FSK9K6 TLM", "9600"),
        ("Mode V - R2 Backup APRS 1K2", "1200"),
        ("FSK  9600 AX.25", "9600"),
        ("4k8 GMSK", "4800"),
        ("Mode 4800bps FSK CW", "4800"),
        ("AFSK1k2", "1200"),
        ("19200 FSK", None),
        ("CW beacon", None),
        (None, None),
    ],
)
def test_detect_baud_from_text(text: str | None, expected: str | None) -> None:
    assert detect_baud_from_text(text) == expected


def test_transmitter_prefers_description_then_baud_column() -> None:
    assert detect_baud_from_transmitter({"description": "9k6 FSK", "baud": 1200}) == "9600"
    assert detect_baud_from_transmitter({"description": "TLM", "baud": 4800}) == "4800"
    assert detect_baud_from_transmitter({"description": "TLM", "baud": 2400}) is None
    assert detect_baud_from_transmitter(None) is None


def test_satyaml_names_then_baudrates() -> None:
    assert detect_baud_from_satyaml(["1k2 AFSK AX.25"], [9600]) == "1200"
    assert detect_baud_from_satyaml(["FSK AX.25"], [9600]) == "9600"
    assert detect_baud_from_satyaml(["BPSK"], [2400]) is None
