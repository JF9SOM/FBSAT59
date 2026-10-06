"""CTCSS tone database indexed by NORAD ID."""

from __future__ import annotations

# Source: https://www.amsat.org/live-fm-satellites/ (Updated May 14, 2026)
CTCSS_DB: dict[int, dict[str, float | None]] = {
    25544: {"tone_hz": 67.0, "activation_hz": None},  # ISS
    27607: {"tone_hz": 67.0, "activation_hz": 74.4},  # SO-50 (SaudiSat-1C)
    40931: {"tone_hz": 88.5, "activation_hz": None},  # IO-86 (LAPAN-A2)
    42017: {"tone_hz": 67.0, "activation_hz": None},  # AO-91 (RadFxSat/Fox-1B)
    57167: {"tone_hz": 141.3, "activation_hz": None},  # PO-101 (Diwata-2)
    61781: {"tone_hz": 67.0, "activation_hz": None},  # AO-123 (ASRTU-1)
    67291: {"tone_hz": 67.0, "activation_hz": None},  # RS-95S (QMR-KWT 2)
}


def get_ctcss(norad_cat_id: int) -> dict[str, float | None] | None:
    """Return CTCSS info for a satellite, or None if not in database."""
    return CTCSS_DB.get(norad_cat_id)


def _band(hz: float) -> str:
    """Coarse band label (same thresholds as RigController._freq_band)."""
    if hz < 30e6:
        return "HF"
    if hz < 300e6:
        return "VHF"
    if hz < 3000e6:
        return "UHF"
    return "SHF"


def resolve_ctcss(
    norad_cat_id: int | None, transmitter: dict[str, object]
) -> tuple[float | None, float | None]:
    """Return ``(tone_hz, activation_hz)`` for *transmitter*, either may be None.

    The transmitter's own SATNOGS ``ctcss_tone`` always wins. The satellite-wide
    table above is only a fallback, and it describes the satellite's FM voice
    *repeater* (the access tone for its cross-band uplink): applying it to every
    transmitter of the satellite put ENC on the ISS APRS (V/V, 145.825 MHz) and
    crew V/V FM operation, and added a sub-audible tone to data uplinks. The
    fallback is therefore used only for a transmitter that has an uplink and is
    not same-band (V/V, U/U); downlink-only transmitters never get a tone.
    """
    own = transmitter.get("ctcss_tone")
    tone: float | None = None
    if isinstance(own, (int, float)) and own:
        tone = float(own)

    up = transmitter.get("uplink_low")
    down = transmitter.get("downlink_low")
    uplink_hz = float(up) if isinstance(up, (int, float)) else 0.0
    downlink_hz = float(down) if isinstance(down, (int, float)) else 0.0
    has_uplink = uplink_hz > 0
    same_band = has_uplink and downlink_hz > 0 and _band(uplink_hz) == _band(downlink_hz)
    info = get_ctcss(norad_cat_id) if norad_cat_id else None
    if info is None or not has_uplink or same_band:
        return tone, None
    if tone is None and info.get("tone_hz"):
        tone = info["tone_hz"]
    return tone, info.get("activation_hz") or None
