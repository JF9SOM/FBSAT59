"""Field-level decoding of the HADES-family telemetry packets (UNNE-1B and relatives).

The layouts follow AMSAT-EA's "MARIA-G, UNNE-1, HADES-R, HADES-ICM description of
transmissions" (V1.04, 2025-01-06). That document lists the fields with their widths, but the
satellites do not send the 12-bit ones the way its tables suggest: they are packed MSB first
into a stream whose bytes are swapped in pairs (each 16-bit word is sent low byte first). The
packing, the scale factors and the sign conventions below were checked against the 667 real
frames SatNOGS Network collected from UNNE-1B between 2026-10-03 and 2026-10-16 (all the
voltages of a power packet agree with each other, charging shows as a negative battery
current, temperatures are physically plausible, uptime counters step by whole 30 s slots) and
agree with the SatNOGS decoder ``hadesr.ksy``.

A packet's *payload* is what :mod:`comms.telemetry.hades_fsk` hands out: the type/address byte,
the data, no CRC, already descrambled.

Shown as raw numbers: the units of the deployment packet (type 8) and of the extended power
packet (type 9) are not stated in the document, nor are the encoding of the TLE elements and
position of the ephemeris packet (type 12) (UNNE-1B has only sent zeros so far) or the meaning of
the sun-sensor values (type 6; its 16-bit words are sent high byte first, the other packets send
them low byte first).

Packet type 15 is not the "SMART-IR experiment" of the document on UNNE-1B: it is a CODEC2 700C
voice message, in the same form as HADES-SA's type 11 (a packet number, then 35 bytes XOR-masked
with the fixed mask of comms.voice.hades_voice; 37 packets, about 15 s). Decoded from the 325
frames SatNOGS collected, it is a Spanish reading of the first chapter of Don Quixote.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple

from comms.telemetry.decoder import TelemetryField, TelemetryFrame
from comms.telemetry.hades_fsk import HADES_SA_ADDRESS as _HADES_SA_ADDRESS

# --- low-level readers ----------------------------------------------------------------- #


def _u(data: bytes, offset: int, size: int) -> int:
    """Little-endian unsigned integer of ``size`` bytes at ``offset``."""
    return int.from_bytes(data[offset : offset + size], "little")


def _packed(data: bytes, start: int, length: int, bit_offset: int, width: int) -> int:
    """``width`` bits at ``bit_offset`` (MSB first) of ``data[start:start+length]``.

    The region is first read with its bytes swapped in pairs (a trailing odd byte stays), which
    is how the satellite sends its 16-bit words.
    """
    region = bytearray(data[start : start + length])
    for i in range(0, len(region) - 1, 2):
        region[i], region[i + 1] = region[i + 1], region[i]
    total = len(region) * 8
    value = int.from_bytes(region, "big")
    return (value >> (total - bit_offset - width)) & ((1 << width) - 1)


def _signed12(value: int) -> int:
    return value - 4096 if value > 2047 else value


def _temperature(raw: int) -> float | None:
    """0.5 degC steps from -40 degC; 255 is "error / not used" (the document's wording)."""
    return None if raw == 255 else raw / 2 - 40.0


def _cpu_voltage_mv(adc: int) -> float | None:
    """The CPU supply is measured against the 1.21 V reference: V = 1210 * 4096 / reading."""
    return 1210 * 4096 / adc if adc else None


# --- field table ----------------------------------------------------------------------- #

# A field's value is a number, a text (hex, a state name, a duration) or None (shown as "n/a").
Value = float | int | str | None


@dataclass(frozen=True)
class FieldDef:
    """One field of a packet type: how to read it and how to show it."""

    name: str
    label: str
    unit: str
    read: Callable[[bytes], Value]
    integer: bool = False  # shown without decimals


@dataclass(frozen=True)
class TypeDef:
    """One packet type: its name, the payload length it needs and its fields."""

    number: int
    name: str
    length: int  # payload bytes incl. the type/address byte, CRC excluded
    fields: tuple[FieldDef, ...]


def _duration(seconds: int) -> str:
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{seconds:,} s  ({days} d {hours:02d}:{minutes:02d}:{secs:02d})"


def _clock(offset: int) -> Callable[[bytes], Value]:
    return lambda d: _duration(_u(d, offset, 4))


def _byte(offset: int, scale: float = 1) -> Callable[[bytes], Value]:
    return lambda d: _u(d, offset, 1) * scale


def _word(offset: int, scale: float = 1) -> Callable[[bytes], Value]:
    return lambda d: _u(d, offset, 2) * scale


def _temp(offset: int) -> Callable[[bytes], Value]:
    return lambda d: _temperature(_u(d, offset, 1))


def _hex_byte(offset: int, size: int = 1) -> Callable[[bytes], Value]:
    return lambda d: f"0x{_u(d, offset, size):0{size * 2}X}"


def _field(name: str, label: str, unit: str, read: Callable[[bytes], Value]) -> FieldDef:
    return FieldDef(name, label, unit, read)


def _count(name: str, label: str, read: Callable[[bytes], Value], unit: str = "") -> FieldDef:
    return FieldDef(name, label, unit, read, integer=True)


# Power (type 1) and the packed 12-bit readers shared with the power statistics.
_P = 11  # start of the packed block of a power packet
_ADC_MV = 1.4  # VBUS1 / VBAT1 readings are 12-bit ADC counts of 1.4 mV
_STAT_MV = 64  # the 8-bit voltage statistics count 64 mV


def _raw12(bit_offset: int) -> Callable[[bytes], int]:
    return lambda d: _packed(d, _P, 14, bit_offset, 12)


def _p12(bit_offset: int, scale: float = 1.0) -> Callable[[bytes], Value]:
    return lambda d: _raw12(bit_offset)(d) * scale


_POWER = TypeDef(
    1,
    "Power",
    29,
    (
        _field("sclock", "System clock", "", _clock(1)),
        _count("spa", "Panel A power (max, last 3 min)", _byte(5, 2), "mW"),
        _count("spb", "Panel B power (max, last 3 min)", _byte(6, 2), "mW"),
        _count("spc", "Panel C power (max, last 3 min)", _byte(7, 2), "mW"),
        _count("spd", "Panel D power (max, last 3 min)", _byte(8, 2), "mW"),
        _count("spi", "Total instant solar power", _word(9, 2), "mW"),
        _count("vbus1", "VBUS1 (MPPT output)", _p12(0, _ADC_MV), "mV"),
        _count("vbat1", "VBAT1 (battery)", _p12(12, _ADC_MV), "mV"),
        _count("vcpu", "VCPU (CPU supply)", lambda d: _cpu_voltage_mv(_raw12(24)(d)), "mV"),
        _count("vbus2", "VBUS2 (EPS bus)", _p12(36, 4), "mV"),
        _count("vbus3", "VBUS3 (CPU bus)", _p12(48, 4), "mV"),
        _count("vbat2", "VBAT2 (payload voltage)", _p12(60, 4), "mV"),
        _count(
            "ibat", "Battery current (negative: charging)", lambda d: _signed12(_raw12(76)(d)), "mA"
        ),
        _count("icpu", "CPU current", _raw12(88), "mA"),
        _count("ipl", "Payload current", _raw12(100), "mA"),
        _field("peaksignal", "Peak signal, last POWER transmission", "dB", _byte(25, 0.5)),
        _field("modasignal", "Noise level, last POWER transmission", "dB", _byte(26, 0.5)),
        _field("lastcmdsignal", "Signal of the last command received", "dB", _byte(27, 0.5)),
        _field("lastcmdnoise", "Noise of the last command received", "dB", _byte(28, 0.5)),
    ),
)

_TEMPERATURES = (
    ("tpa", "Panel A"),
    ("tpb", "Panel B"),
    ("tpc", "Panel C"),
    ("tpd", "Panel D"),
    ("tpe", "Panel E (not used)"),
    ("teps", "EPS"),
    ("ttx", "TX"),
    ("ttx2", "TX (NTC)"),
    ("trx", "RX (NTC)"),
    ("tcpu", "CPU"),
)

_TEMP = TypeDef(
    2,
    "Temperature",
    15,
    (
        _field("sclock", "System clock", "", _clock(1)),
        *(
            _field(n, f"{label} temperature", "°C", _temp(5 + i))
            for i, (n, label) in enumerate(_TEMPERATURES)
        ),
    ),
)

_ANTENNA = {0: "not deployed", 1: "deployed", 2: "unknown"}
_TRANSPONDER = {0: "off", 1: "FM → FM", 2: "FSK → FSK regenerative"}


def _named(
    offset: int, names: dict[int, str], high: bool | None = None
) -> Callable[[bytes], Value]:
    def read(d: bytes) -> Value:
        raw = _u(d, offset, 1)
        if high is not None:
            raw = raw >> 4 if high else raw & 15
        return f"{names.get(raw, 'unknown')} ({raw})"

    return read


def _nibble(offset: int, high: bool) -> Callable[[bytes], Value]:
    return lambda d: (_u(d, offset, 1) >> 4) if high else (_u(d, offset, 1) & 15)


_STATUS = TypeDef(
    3,
    "Status",
    27,
    (
        _field("sclock", "System clock", "", _clock(1)),
        _field("uptime", "Uptime", "", _clock(5)),
        _count("nrun", "CPU runs", _word(9)),
        _count("npayload", "Payload activations (incl. failed)", _byte(11)),
        _count("nwire", "Antenna deployment attempts", _byte(12)),
        _count("ntransponder", "Transponder activations", _byte(13)),
        _count("npayloadfails", "Payload failures", _nibble(14, True)),
        _count("lstrst", "Last reset reason (code)", _nibble(14, False)),
        _count("bate", "Battery status (code 0-F)", _nibble(15, True)),
        _field("mote", "Transponder mode", "", _named(15, _TRANSPONDER, high=False)),
        _count("ntasksnotexecuted", "Tasks lost by the scheduler", _byte(16)),
        _field("antennadeployed", "Antenna", "", _named(17, _ANTENNA)),
        _count("nexteepromerrors", "EEPROM checksum failures (recoverable)", _byte(18)),
        _field("failedtaskid", "Last failed task id", "", _hex_byte(19)),
        _count("messaging", "Messaging enabled", _byte(20)),
        _field("strfwd0", "Store & forward: last id", "", _hex_byte(21)),
        _field("strfwd1", "Store & forward: last command", "", _hex_byte(22, 2)),
        _field("strfwd2", "Store & forward: last value", "", _hex_byte(24, 2)),
        _count("strfwd3", "Store & forward: commands executed", _byte(26)),
    ),
)


def _stat12(start: int, bit_offset: int, scale: float) -> Callable[[bytes], Value]:
    return lambda d: _packed(d, start, 5, bit_offset, 12) * scale


def _stat_cpu(start: int) -> Callable[[bytes], Value]:
    return lambda d: _cpu_voltage_mv(_packed(d, start, 5, 24, 12))


def _power_stats_block(
    prefix: str, label: str, start: int, current_sign: int
) -> tuple[FieldDef, ...]:
    """The 11-byte min or max block of a power statistics packet, starting at ``start``."""
    return (
        _count(f"{prefix}vbus1", f"{label} VBUS1", _stat12(start, 0, _ADC_MV), "mV"),
        _count(f"{prefix}vbat1", f"{label} VBAT1", _stat12(start, 12, _ADC_MV), "mV"),
        _count(f"{prefix}vcpu", f"{label} VCPU", _stat_cpu(start), "mV"),
        _count(f"{prefix}vbus2", f"{label} VBUS2", _byte(start + 5, _STAT_MV), "mV"),
        _count(f"{prefix}vbus3", f"{label} VBUS3", _byte(start + 6, _STAT_MV), "mV"),
        _count(f"{prefix}vbat2", f"{label} VBAT2", _byte(start + 7, _STAT_MV), "mV"),
        _count(f"{prefix}ibat", f"{label} battery current", _byte(start + 8, current_sign), "mA"),
        _count(f"{prefix}icpu", f"{label} CPU current", _byte(start + 9), "mA"),
        _count(
            f"{prefix}ipl",
            f"{label} payload current",
            _byte(start + 10, 4 if prefix == "max" else 1),
            "mA",
        ),
    )


_POWER_STATS = TypeDef(
    4,
    "Power stats",
    33,
    (
        _field("sclock", "System clock", "", _clock(1)),
        *_power_stats_block("min", "Min", 5, -1),
        *_power_stats_block("max", "Max", 16, 1),
        _count("ibat_rx_charging", "Battery current, RX, charging", _byte(27), "mA"),
        _count("ibat_rx_discharging", "Battery current, RX, discharging", _byte(28), "mA"),
        _count("ibat_txlow_charging", "Battery current, TX low power, charging", _byte(29), "mA"),
        _count(
            "ibat_txlow_discharging", "Battery current, TX low power, discharging", _byte(30), "mA"
        ),
        _count("ibat_txhigh_charging", "Battery current, TX high power, charging", _byte(31), "mA"),
        _count(
            "ibat_txhigh_discharging",
            "Battery current, TX high power, discharging",
            _byte(32),
            "mA",
        ),
    ),
)

_TEMP_STATS = TypeDef(
    5,
    "Temp stats",
    25,
    (
        _field("sclock", "System clock", "", _clock(1)),
        *(
            _field(f"min{n}", f"Min {label} temperature", "°C", _temp(5 + i))
            for i, (n, label) in enumerate(_TEMPERATURES)
        ),
        *(
            _field(f"max{n}", f"Max {label} temperature", "°C", _temp(15 + i))
            for i, (n, label) in enumerate(_TEMPERATURES)
        ),
    ),
)


def _word_be(offset: int) -> Callable[[bytes], Value]:
    """A 16-bit value sent high byte first (the sun-sensor packet)."""
    return lambda d: int.from_bytes(d[offset : offset + 2], "big")


_SUN_SENSORS = 8
_SUN_SAMPLES = 6
_SUN = TypeDef(
    6,
    "Sun sensors",
    133,
    (
        *(
            _count(f"td{i}", f"Seconds before sample {i}", _word_be(1 + 2 * i), "s")
            for i in range(_SUN_SAMPLES)
        ),
        *(
            _count(
                f"v{sensor}_{i}",
                f"Sensor {sensor} light, sample {i} (raw)",
                _word_be(13 + 2 * (_SUN_SAMPLES * sensor + i)),
            )
            for sensor in range(_SUN_SENSORS)
            for i in range(_SUN_SAMPLES)
        ),
        *(
            _count(f"p{sensor}", f"Sensor {sensor} peak (raw)", _word_be(109 + 2 * sensor))
            for sensor in range(_SUN_SENSORS)
        ),
        *(
            _count(f"err{sensor}", f"Sensor {sensor} error code (0: ok)", _byte(125 + sensor))
            for sensor in range(_SUN_SENSORS)
        ),
    ),
)

_VOICE_PACKET_S = 0.4  # ten CODEC2 700C frames of 40 ms


def _voice_data(d: bytes) -> Value:
    """The 35 voice bytes with the satellite's fixed XOR mask taken off (the CODEC2 data)."""
    from comms.voice.hades_voice import unmask

    return unmask(bytes(d[2:37])).hex(" ").upper()


_VOICE = TypeDef(
    15,
    "Voice",
    37,
    (
        _count("number", "Packet number in the message", _byte(1)),
        _field("time", "Start within the message", "s", lambda d: _u(d, 1, 1) * _VOICE_PACKET_S),
        _field("data", "CODEC2 700C data (mask removed)", "", _voice_data),
    ),
)

_DEPLOY_WORDS = ("v1oc", "v1", "i1", "i1pk", "r1", "v2oc", "v2", "r2")
_DEPLOY_BYTES = ("state_begin", "state_end", "state_now", "enable", "counter", "tmp")
_DEPLOY = TypeDef(
    8,
    "Deploy",
    29,
    (
        *(_count(n, f"{n} (raw)", _word(1 + 2 * i)) for i, n in enumerate(_DEPLOY_WORDS)),
        _count("t0", "t0 (raw)", lambda d: _u(d, 17, 4)),
        _count("td", "td (raw)", _word(21)),
        *(_count(n, f"{n} (raw)", _byte(23 + i)) for i, n in enumerate(_DEPLOY_BYTES)),
    ),
)

_EXT_CHANNELS = ("SPA", "SPB", "SPC", "SPD", "SUN", "BAT", "BATP", "BATN", "CPU", "PL")
_EXT_QUANTITIES = (
    ("v", "V instant voltage"),
    ("i", "I instant current"),
    ("p", "P mean power"),
    ("vp", "VP peak voltage"),
    ("ip", "IP peak current"),
    ("pp", "PP peak power"),
)
_EXT_POWER = TypeDef(
    9,
    "Extended power",
    121,
    tuple(
        _count(f"{short}{c}", f"{name} {label} (raw)", _word(1 + 2 * (6 * c + q)))
        for c, name in enumerate(_EXT_CHANNELS)
        for q, (short, label) in enumerate(_EXT_QUANTITIES)
    ),
)

_GAME = TypeDef(
    10,
    "University game",
    15,
    (
        _field("clock_tx", "System clock at transmission", "", _clock(1)),
        _count("week_number", "Weeks since deployment", _byte(5)),
        _field("stored_status", "Payload status", "", _hex_byte(6)),
        *(_count(f"data{i}", f"data{i}", _byte(7 + i)) for i in range(8)),
    ),
)


def _text_to_end(offset: int) -> Callable[[bytes], Value]:
    """The rest of the payload from *offset* as text (UTF-8; unreadable bytes shown as ``·``)."""

    def read(d: bytes) -> Value:
        text = d[offset:].decode("utf-8", errors="replace")
        return "".join(c if c.isprintable() else "·" for c in text).rstrip("·").rstrip() or "—"

    return read


# ICM game message (type 7): SatNOGS hadesr.ksy / hadesicm.ksy ``icmgamemsg_frame`` -- a 32-bit
# clock, a message number and the message text (UTF-8, to the end of the packet). 101 bytes with
# the CRC, so 99 here.
_ICM_GAME_MESSAGE = TypeDef(
    7,
    "ICM game message",
    99,
    (
        _field("clock_tx", "System clock (sclock)", "", _clock(1)),
        _count("msg_num", "Message number", _byte(5)),
        _field("message", "Message", "", _text_to_end(6)),
    ),
)

_EPHEMERIS = TypeDef(
    12,
    "Ephemeris",
    62,
    (
        _count("utc", "UTC (raw)", lambda d: _u(d, 1, 4)),
        _count("adr", "Satellite address (raw)", _word(5)),
        *(
            _field(n, f"TLE {n} (raw)", "", _hex_byte(15 + 4 * i, 4))
            for i, n in enumerate(
                (
                    "epoch",
                    "xndt2o",
                    "xndd6o",
                    "bstar",
                    "xincl",
                    "xnodeo",
                    "eo",
                    "omegao",
                    "xmo",
                    "xno",
                )
            )
        ),
        _count("lat", "Latitude (raw)", _word(55)),
        _count("lon", "Longitude (raw)", _word(57)),
        _count("alt", "Altitude (raw)", _word(59)),
        _count("cnt", "Counter", _byte(61)),
    ),
)

# Time series (type 14): what the samples mean depends on the "variable" byte.
_SERIES_VARIABLES: dict[int, tuple[str, str, Callable[[int], float | None]]] = {
    0: ("Peak signal", "dB", lambda raw: raw * 0.5),
    1: ("Noise level", "dB", lambda raw: raw * 0.5),
    2: ("VBAT1", "mV", lambda raw: raw * 16 * _ADC_MV),
    3: ("CPU temperature", "°C", _temperature),
    4: ("Panel A temperature", "°C", _temperature),
    5: ("Mean panel temperature (A-D)", "°C", _temperature),
}


def _series_variable(d: bytes) -> Value:
    raw = _u(d, 5, 1)
    entry = _SERIES_VARIABLES.get(raw)
    return f"{entry[0]} ({raw})" if entry else f"unknown ({raw})"


def _series_sample(index: int) -> Callable[[bytes], Value]:
    def read(d: bytes) -> Value:
        entry = _SERIES_VARIABLES.get(_u(d, 5, 1))
        raw = _u(d, 6 + index, 1)
        return entry[2](raw) if entry else raw

    return read


_TIME_SERIES = TypeDef(
    14,
    "Time series",
    36,
    (
        _field("sclock", "System clock of sample 0", "", _clock(1)),
        _field("variable", "Variable", "", _series_variable),
        *(
            _field(
                f"s{i:02d}",
                f"Sample {i}" + (" (oldest)" if i == 0 else " (newest)" if i == 29 else ""),
                "",
                _series_sample(i),
            )
            for i in range(30)
        ),
    ),
)

# --- HADES-SA (SpinnyONE): status (type 3) and BBS data (type 15) ----------------------- #
# The other telemetry types of HADES-SA have the layouts of the HADES-R family above (checked
# against the 49 + 31 + 28 + 23 + 23 + 21 + 21 + 38 + 20 frames SatNOGS Network collected from
# HADES-SA, 2026-10-11..18: voltages agree with each other, temperatures are plausible).
# Status is longer, and BBS data replaces the experiment packet. AMSAT-EA's "HADES-SA SpinnyONE
# Transmissions Description" V1.0 gives the fields; in the real data the camera fields agree
# with the SSDV downlink (payload_frames = 34 while image 48 was sent, which has 34 packets;
# current_image_id follows the SSDV image id: 48, then 64 ... 67).

_SA_STATUS = TypeDef(
    3,
    "Status",
    39,
    (
        _field("sclock", "System clock", "", _clock(1)),
        _field("uptime", "Uptime", "", _clock(5)),
        _count("nrun", "CPU runs", _word(9)),
        _count("npayload", "Payload (camera) starts", _byte(11)),
        _count("nwire", "Antenna deployment attempts", _byte(12)),
        _count("ntransponder", "Transponder activations (not used)", _byte(13)),
        _count("npayloadfails", "Payload communication errors", _nibble(14, True)),
        _count("lstrst", "Last reset cause (code)", _nibble(14, False)),
        _count("bate", "Battery state machine (0-6)", _nibble(15, True)),
        _count("mote", "Mode (not used)", _nibble(15, False)),
        _field("systems_status", "Subsystems status", "", _hex_byte(16)),
        _count("ntasksnotexecuted", "Tasks lost by the scheduler", _byte(17)),
        _field("antennadeployed", "Antenna", "", _named(18, _ANTENNA)),
        _count("nexteepromerrors", "EEPROM checksum failures (recoverable)", _byte(19)),
        _field("failedtaskid", "Last failed task id", "", _hex_byte(20)),
        _count("messaging", "Messaging enabled", _byte(21)),
        _field("strfwd0", "Store & forward: last id", "", _hex_byte(22)),
        _field("strfwd1", "Store & forward: last command", "", _hex_byte(23, 2)),
        _field("strfwd2", "Store & forward: last value", "", _hex_byte(25, 2)),
        _count("strfwd3", "Store & forward: commands executed", _byte(27)),
        _count("rx_percentage", "Time receiving", _byte(28), "%"),
        _count("telemetry_percentage", "Time sending telemetry", _byte(29), "%"),
        _count("transponder_percentage", "Time as transponder", _byte(30), "%"),
        _count("ptt_hp_percentage", "Time transmitting, high power", _byte(31), "%"),
        _count("ptt_lp_percentage", "Time transmitting, low power", _byte(32), "%"),
        _count("ple_percentage", "ple", _byte(33), "%"),
        _count("bwe_percentage", "bwe", _byte(34), "%"),
        _count(
            "vbat_higher_than_vbus", "Time the battery is above the bus voltage", _byte(35), "%"
        ),
        _count("payload_frames", "Camera frames (packets) of this picture", _byte(36)),
        _count("payload_params", "Configured camera parameters", _byte(37)),
        _count("current_image_id", "Current SSDV image id", _byte(38)),
    ),
)

_BBS_ENTRIES = 5
_BBS_CALL = 6
_BBS_TEXT = 7


def _bbs_text(offset: int, size: int) -> Callable[[bytes], Value]:
    def read(d: bytes) -> Value:
        raw = d[offset : offset + size]
        return "".join(chr(c) if 32 <= c < 127 else "·" for c in raw).rstrip("·").rstrip() or "—"

    return read


# BBS data (type 15): five callsigns (6 characters), five messages (7 characters) and five
# CODEC2 frame counts, each as a block. The document's table lists them entry by entry, but the
# real packets (every one seen so far has an empty BBS: "------" / "No data" / 0) are blocks.
_BBS = TypeDef(
    15,
    "BBS data",
    71,
    tuple(
        fd
        for i in range(_BBS_ENTRIES)
        for fd in (
            _field(
                f"callsign{i}",
                f"Callsign {i}",
                "",
                _bbs_text(1 + i * _BBS_CALL, _BBS_CALL),
            ),
            _field(
                f"message{i}",
                f"Message {i}",
                "",
                _bbs_text(1 + _BBS_ENTRIES * _BBS_CALL + i * _BBS_TEXT, _BBS_TEXT),
            ),
            _count(
                f"codec2frames{i}",
                f"CODEC2 frames {i}",
                _byte(1 + _BBS_ENTRIES * (_BBS_CALL + _BBS_TEXT) + i),
            ),
        )
    ),
)

# The table of a HADES-SA packet: the family's, with its own status and BBS data.
TYPES_SA: dict[int, TypeDef] = {}  # filled below, after TYPES

TYPES: dict[int, TypeDef] = {
    t.number: t
    for t in (
        _POWER,
        _TEMP,
        _STATUS,
        _POWER_STATS,
        _TEMP_STATS,
        _SUN,
        _ICM_GAME_MESSAGE,
        _DEPLOY,
        _EXT_POWER,
        _GAME,
        _EPHEMERIS,
        _TIME_SERIES,
        _VOICE,
    )
}


TYPES_SA.update({n: t for n, t in TYPES.items() if n not in (6, 7, 10, 15)})
TYPES_SA.update({3: _SA_STATUS, 15: _BBS})

# --- decoding -------------------------------------------------------------------------- #


class DecodedField(NamedTuple):
    """A decoded field ready to show."""

    name: str
    label: str
    value: Value
    unit: str
    integer: bool


def decode_payload(payload: bytes) -> tuple[TypeDef, list[DecodedField]] | None:
    """Decode a packet payload; ``None`` for a type without a definition or a short payload."""
    if not payload:
        return None
    table = TYPES_SA if payload[0] & 15 == _HADES_SA_ADDRESS else TYPES
    definition = table.get(payload[0] >> 4)
    if definition is None or len(payload) < definition.length:
        return None
    fields: list[DecodedField] = []
    for fd in definition.fields:
        unit = fd.unit
        if definition.number == 14 and fd.name.startswith("s") and fd.name[1:].isdigit():
            entry = _SERIES_VARIABLES.get(_u(payload, 5, 1))
            unit = entry[1] if entry else ""
        fields.append(DecodedField(fd.name, fd.label, fd.read(payload), unit, fd.integer))
    return definition, fields


def type_tabs(address: int | None = None) -> dict[str, dict[str, object]]:
    """The "Decoded Fields" sub-tab skeleton, in the shape of ``get_telemetry_id_defs()``.

    ``address`` 3 gives HADES-SA's tabs; anything else the HADES-R family's (UNNE-1B).
    """
    table = TYPES_SA if address == _HADES_SA_ADDRESS else TYPES
    return {
        str(number): {
            "label": f"{number}  {definition.name}",
            "fields": [{"name": f.name, "label": f.label} for f in definition.fields],
        }
        for number, definition in table.items()
    }


def telemetry_frame(payload: bytes, norad: int, satellite_name: str = "") -> TelemetryFrame | None:
    """The decoded fields of ``payload`` as a :class:`TelemetryFrame` (None if undecodable)."""
    decoded = decode_payload(payload)
    if decoded is None:
        return None
    definition, fields = decoded
    out: list[TelemetryField] = []
    for f in fields:
        if isinstance(f.value, str):
            out.append(TelemetryField(f.name, f.label, 0, 0.0, f.value, is_string=True))
        elif f.value is None:
            out.append(TelemetryField(f.name, f.label, 0, 0.0, "n/a", is_string=True))
        elif f.integer:
            out.append(
                TelemetryField(f.name, f.label, f.value, float(f.value), f.unit, is_integer=True)
            )
        else:
            # temperatures, levels and series samples come in 0.5 steps: one decimal is enough
            text = f"{f.value:.1f} {f.unit}".strip()
            out.append(
                TelemetryField(f.name, f.label, f.value, float(f.value), text, is_string=True)
            )
    return TelemetryFrame(
        norad=norad,
        callsign="HADES",
        satellite_name=satellite_name,
        raw_hex=payload.hex(),
        fields=out,
        telemetry_id=str(definition.number),
        telemetry_label=definition.name,
    )
