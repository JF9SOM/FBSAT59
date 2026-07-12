"""Unit tests for comms/aprs/direwolf.py — direwolf.conf MODEM line generation.

DirewolfManager._write_config() writes a temp file and never touches a
real subprocess, so this is fully isolated (no direwolf binary needed).
"""

from __future__ import annotations

from pathlib import Path

from comms.aprs.direwolf import DirewolfManager


def _modem_line(conf_path: str) -> str:
    text = Path(conf_path).read_text()
    for line in text.splitlines():
        if line.startswith("MODEM"):
            return line
    raise AssertionError(f"No MODEM line found in {conf_path!r}:\n{text}")


def test_write_config_always_declares_arate_48000() -> None:
    """AudioBridge/G3ruhSdrDemod always feed stdin at 48kHz; without an
    explicit ARATE, direwolf has no WAV header to infer it from over a raw
    pipe. Confirmed via real-signal testing (ARICA-2 4800 G3RUH) that
    omitting this line breaks decoding."""
    mgr = DirewolfManager()
    path = mgr._write_config("JF9SOM", 9, modem="4800")
    try:
        text = Path(path).read_text()
        assert "ARATE 48000" in text.splitlines()
    finally:
        Path(path).unlink()


def test_write_config_1200_default() -> None:
    mgr = DirewolfManager()
    path = mgr._write_config("JF9SOM", 9, modem="1200")
    try:
        assert _modem_line(path) == "MODEM 1200"
    finally:
        Path(path).unlink()


def test_write_config_9600_gets_implicit_g3ruh() -> None:
    """Direwolf auto-selects G3RUH above 7200 baud, so 9600 needs no
    explicit modem-type token."""
    mgr = DirewolfManager()
    path = mgr._write_config("JF9SOM", 9, modem="9600")
    try:
        assert _modem_line(path) == "MODEM 9600"
    finally:
        Path(path).unlink()


def test_write_config_4800_forces_g3ruh() -> None:
    """4800 baud must explicitly request G3RUH — Direwolf's own default at
    this speed is 8PSK (V.27, for HF SSB packet), which would silently
    misdecode a G3RUH-compatible satellite beacon instead of failing loudly."""
    mgr = DirewolfManager()
    path = mgr._write_config("JF9SOM", 9, modem="4800")
    try:
        assert _modem_line(path) == "MODEM 4800 G3RUH"
    finally:
        Path(path).unlink()


def test_write_config_unknown_modem_falls_back_to_1200() -> None:
    mgr = DirewolfManager()
    path = mgr._write_config("JF9SOM", 9, modem="2400")
    try:
        assert _modem_line(path) == "MODEM 1200"
    finally:
        Path(path).unlink()
