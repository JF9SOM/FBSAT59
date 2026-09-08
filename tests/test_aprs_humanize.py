"""Regression set for comms/aprs/humanize.py — the APRS tab's "Plain" view.

Each case pins a real (or canonical-spec) APRS packet to the exact plain-
language line the humanizer should produce. Expected coordinates/values are
aprslib's (a well-tested decoder) and were eyeballed against the APRS spec /
aprs.fi; the sentence wording is this module's own. When humanize returns
None the APRS tab falls back to the raw info field, so "None" is a valid,
tested outcome for packets we deliberately don't dress up.

Language is pinned to English (the default) so expectations don't depend on
the .po catalog. Qt is not involved.
"""

from __future__ import annotations

import pytest

from comms.aprs.humanize import humanize_frame, humanize_tnc2
from comms.aprs.parser import decode_ax25, parse_aprs
from i18n import get_language, set_language

# (tnc2 string, expected plain line or None)
CASES: list[tuple[str, str | None]] = [
    # --- from the user's screenshot: terrestrial APRS gated to RF by an I-Gate ---
    (
        'JE1DFQ-9>SURXY0,TCPIP,JH9YVX-10*:`CG6q4Y>/`"3o}_1',
        "car moving · 35.4817°N 139.7210°E · NE 96 km/h",
    ),
    (
        "JG1RBF-7>APK004,TCPIP,JH9YVX-10*::JF1BQZ-9 :Good afternoon{10",
        "JG1RBF-7 → JF1BQZ-9: “Good afternoon” (ack requested #10)",
    ),
    (
        "JF1BQZ-9>APY500,TCPIP,JH9YVX-10*::JG1RBF-7 :ack10",
        "JF1BQZ-9 → JG1RBF-7: acknowledged message #10",
    ),
    (
        'JA4GWS-7>S4RWS4,TCPIP,JH9YVX-10*:`=SBl 7[/`"4,}_3',
        "person · 34.4557°N 133.9230°E · in service",
    ),
    (
        'JR5EOY-8>SSTVW8,TCPIP,JH9YVX-10*:`<F1n+h>/`"3w}Matsuyama ZN8',
        "car moving · 33.7797°N 132.7035°E · S 39 km/h · “Matsuyama ZN8”",
    ),
    (
        'JR0BAQ-9>SVTVP1,TCPIP,JH9YVX-10*:`B(Ynp2>/`";8}haroharo-net_4',
        "car moving · 36.7668°N 138.2102°E · N 52 km/h · alt 670 m · “haroharo-net”",
    ),
    # --- canonical / common forms ---
    (
        "JA1ZRL>APRS,TCPIP*:!3540.00N/13945.00E-Tokyo club shack",
        "house · 35.6667°N 139.7500°E · “Tokyo club shack”",
    ),
    (
        "JA1ZRL>APRS:@092345z3540.00N/13945.00E>123/045Running",
        "car moving · 35.6667°N 139.7500°E · SE 83 km/h · “Running”",
    ),
    (
        "W1ABC>APRS:=/5L!!<*e7>7P[compressed pos",
        "car moving · 49.5000°N 72.7500°W · E 67 km/h · “compressed pos”",
    ),
    ("JA1ABC>APRS:>QRV 435.500 SSB", "Status: “QRV 435.500 SSB”"),
    (
        "JA1ABC>APRS:;ISS      *111111z0000.00N/00000.00Es432.500MHz",
        "Object ISS · 0.0000°N 0.0000°E · “432.500MHz”",
    ),
    (
        "JA1ABC-13>APRS:!3540.00N/13945.00E_180/010g015t070h55b10130",
        "Weather · 35.6667°N 139.7500°E · 21.1°C · humidity 55%",
    ),
    (
        "JA1ABC>APRS:T#123,10,20,30,40,50,10101010",
        "Telemetry #123: 10, 20, 30, 40, 50 (bits 10101010)",
    ),
    (
        "JA1ABC>APRS::BLN2     :FBSAT59 net Sunday 21:00 JST",
        "Bulletin 2 from JA1ABC: “FBSAT59 net Sunday 21:00 JST”",
    ),
    (
        "N0CALL>APRS:}JR1YPU-1>APRS,ARISS,qAR,JA1RL:=3540.00N/13945.00E-ISS relayed",
        "house · 35.6667°N 139.7500°E · “ISS relayed” · via the ISS",
    ),
    # --- deliberately not dressed up: caller shows the raw info field ---
    ("JA1ABC>APRS:;text is malformed", None),
    ("JA1ABC>APRS:garbage nonsense", None),
]


@pytest.fixture(autouse=True)
def _english() -> None:
    prev = get_language()
    set_language("en")
    yield
    set_language(prev)


@pytest.mark.parametrize(("tnc2", "expected"), CASES, ids=[c[0][:24] for c in CASES])
def test_humanize_tnc2(tnc2: str, expected: str | None) -> None:
    assert humanize_tnc2(tnc2) == expected


def test_humanize_frame_matches_tnc2_path() -> None:
    """humanize_frame(Ax25Frame) must agree with humanize_tnc2 on the same packet.

    Builds a real AX.25 UI frame for an uncompressed position and round-trips
    it through decode_ax25 so the latin-1 reconstruction is exercised.
    """
    raw = _ax25_ui_frame(
        src="JA1ZRL",
        dest="APRS",
        via=["TCPIP*"],
        info=b"!3540.00N/13945.00E-Tokyo club shack",
    )
    frame = decode_ax25(raw)
    assert frame is not None
    assert humanize_frame(frame) == "house · 35.6667°N 139.7500°E · “Tokyo club shack”"


def test_parse_aprs_populates_plain_and_falls_back() -> None:
    good = decode_ax25(_ax25_ui_frame("JA1ZRL", "APRS", ["TCPIP*"], b"!3540.00N/13945.00E-hi"))
    assert good is not None
    assert parse_aprs(good).plain == "house · 35.6667°N 139.7500°E · “hi”"

    # A status packet aprslib parses fine.
    st = decode_ax25(_ax25_ui_frame("JA1ZRL", "APRS", [], b">just monitoring"))
    assert st is not None
    assert parse_aprs(st).plain == "Status: “just monitoring”"

    # Unrenderable → plain falls back to the raw info field, never blank.
    junk = decode_ax25(_ax25_ui_frame("JA1ZRL", "APRS", [], b"garbage nonsense"))
    assert junk is not None
    assert parse_aprs(junk).plain == "garbage nonsense"


def test_japanese_catalog_translates_plain_view() -> None:
    """The Plain view must be translatable: under ja the sentence templates
    and the symbol labels both come out Japanese (not English-hardcoded)."""
    set_language("ja")
    try:
        status = humanize_tnc2("JA1ZRL>APRS:>QRV")
        car = humanize_tnc2("JA1ZRL>APRS:!3540.00N/13945.00E>Test")
    finally:
        set_language("en")
    assert status == "ステータス: 「QRV」"
    assert car is not None and car.startswith("車 ·")


# --------------------------------------------------------------------------- #
# Minimal AX.25 UI-frame builder (KISS-less, FCS-less) for the frame tests
# --------------------------------------------------------------------------- #


def _encode_addr(call: str, last: bool) -> bytes:
    call = call.upper()
    ssid = 0
    repeated = call.endswith("*")
    if repeated:
        call = call[:-1]
    if "-" in call:
        base, _, s = call.partition("-")
        call, ssid = base, int(s)
    else:
        base = call
    padded = f"{call:<6}"[:6]
    out = bytes(((ord(ch) << 1) & 0xFF) for ch in padded)
    ssid_byte = 0x60 | ((ssid & 0x0F) << 1) | (0x01 if last else 0x00)
    if repeated:
        ssid_byte |= 0x80
    return out + bytes([ssid_byte])


def _ax25_ui_frame(src: str, dest: str, via: list[str], info: bytes) -> bytes:
    addrs = [_encode_addr(dest, False), _encode_addr(src, not via)]
    for i, v in enumerate(via):
        addrs.append(_encode_addr(v, i == len(via) - 1))
    return b"".join(addrs) + bytes([0x03, 0xF0]) + info
