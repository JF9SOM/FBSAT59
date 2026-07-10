"""APRS engine — ties DirewolfManager, KissClient, and APRS parser together.

Connects to RadioControlWidget signals to start/stop automatically when the
rig or SDR connects or disconnects.  Emits ``packet_received`` for each
decoded APRS packet so the UI tab can display it without coupling to the
backend.

This is a process-wide singleton (see ``get_aprs_engine()``): both the APRS
tab and the Telemetry tab's Bell 202 AFSK mode need the same underlying
Direwolf process / AfskDemodulator, since a second independent Direwolf
instance would collide with the first over the hardcoded KISS TCP port and
the shared audio output lock. ``start_rig()``/``start_sdr()``/``stop()``
take an ``owner`` tag and are reference-counted so closing one tab never
tears down the pipeline while another tab is still using it.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from PySide6.QtCore import QObject, Signal

from comms.aprs.afsk_demod import AfskDemodulator
from comms.aprs.direwolf import DirewolfManager, find_direwolf
from comms.aprs.parser import AprsPacket, Ax25Frame, decode_ax25, parse_aprs


class AprsEngine(QObject):
    """Coordinates Direwolf, KISS, and APRS parsing for the APRS tab.

    Signals
    -------
    packet_received(AprsPacket)
        Emitted on the Qt main thread for each decoded APRS packet.
    status_changed(str)
        Short human-readable status string ("Connected", "Stopped", …).
    error_occurred(str)
        Emitted when a non-fatal error occurs (e.g. Direwolf crash).
    """

    packet_received: Signal = Signal(object)
    raw_frame_received: Signal = Signal(bytes)
    status_changed: Signal = Signal(str)
    error_occurred: Signal = Signal(str)

    # How long to wait after PTT ON before sending audio (rig key-up time)
    _PTT_LEAD_S: float = 0.15
    # Approximate duration of a typical APRS message packet audio at 1200 baud
    _TX_AUDIO_S: float = 0.55
    # How long to wait after audio ends before releasing PTT
    _PTT_TAIL_S: float = 0.10

    _instance: AprsEngine | None = None
    _instance_lock = threading.Lock()

    def __init__(self, conn: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._conn = conn
        self._mgr = DirewolfManager()
        self._demod: AfskDemodulator | None = None
        self._sdr_pipeline: Any | None = None  # SDRPipeline reference
        self._rig: Any | None = None  # RigController for PTT
        self._ptt_active: bool = False
        self._running = False
        self._owners: set[str] = set()

    @classmethod
    def instance(cls, conn: Any) -> AprsEngine:
        """Return the process-wide singleton, constructing it on first call.

        ``conn`` is only used the first time (there is a single DB
        connection for the whole app); later calls ignore it and return the
        existing instance. The singleton has no Qt parent — it must outlive
        any single tab.
        """
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(conn, parent=None)
            return cls._instance

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    @property
    def is_running(self) -> bool:
        return self._running

    @staticmethod
    def direwolf_available() -> bool:
        """Return True when a direwolf binary can be located."""
        return find_direwolf() is not None

    def set_rig(self, rig: Any | None) -> None:
        """Set the RigController used for CAT PTT during transmission.

        Pass None when the rig disconnects.  The engine does not own the rig;
        it holds only a weak reference via this attribute.
        """
        self._rig = rig

    def add_owner(self, owner: str) -> None:
        """Register `owner`'s interest in an already-running pipeline.

        For passive consumers (e.g. SSTV/SSDV tapping raw_frame_received off
        whatever the APRS/Telemetry tabs already started) that must keep the
        pipeline alive while they're subscribed, but should never trigger a
        start themselves — use start_rig()/start_sdr() for that. Release
        with stop(owner), same as any other owner.
        """
        self._owners.add(owner)

    def start_rig(
        self,
        owner: str,
        callsign: str,
        ssid: int,
        via: str,
    ) -> tuple[bool, str]:
        """Start Direwolf using the configured Sound Card audio devices.

        Reads ``soundcard_settings`` from the DB to pick the right
        input / output device indices. ``owner`` registers the caller's
        interest in the pipeline (see ``stop()``); if another owner already
        has it running, this just adds `owner` and returns success without
        touching the running pipeline.
        """
        self._owners.add(owner)
        if self._running:
            return True, ""

        in_dev, out_dev = self._load_soundcard_devices()
        ok, err = self._mgr.start(
            callsign=callsign,
            ssid=ssid,
            via=via,
            in_device=in_dev,
            out_device=out_dev,
        )
        if not ok:
            self.error_occurred.emit(err)
            return False, err

        self._wire_kiss()
        self._running = True
        self.status_changed.emit("Connected (Rig + Direwolf)")
        return True, ""

    def start_sdr(self, owner: str, pipeline: Any) -> tuple[bool, str]:
        """Start Bell 202 AFSK demodulation on an SDR pipeline (receive only).

        *pipeline* must be an SDRPipeline instance with ``subscribe()`` and
        a ``_device.sample_rate`` attribute. ``owner`` registers the
        caller's interest in the pipeline (see ``stop()``).
        """
        self._owners.add(owner)
        if self._running:
            return True, ""
        try:
            sr = int(pipeline._device.sample_rate)
        except AttributeError:
            return False, "Cannot determine SDR sample rate."

        self._sdr_pipeline = pipeline
        self._demod = AfskDemodulator(sample_rate=sr, parent=self)
        self._demod.frame_received.connect(self._on_kiss_frame)
        self._demod.start()
        pipeline.subscribe(self._demod.push_samples)

        self._running = True
        self.status_changed.emit("Connected (SDR — receive only)")
        return True, ""

    def stop(self, owner: str) -> None:
        """Release `owner`'s interest in the pipeline.

        The underlying Direwolf process / AfskDemodulator only actually
        stops once every owner has released it — otherwise closing one tab
        (e.g. APRS) would silently kill AX.25 reception for another tab
        that is still using it (e.g. Telemetry).
        """
        self._owners.discard(owner)
        if self._owners:
            return
        if self._sdr_pipeline is not None and self._demod is not None:
            self._sdr_pipeline.unsubscribe(self._demod.push_samples)
            self._demod.stop()
            self._demod = None
            self._sdr_pipeline = None
        self._mgr.stop()
        self._running = False
        self.status_changed.emit("Stopped")

    def send_message(
        self,
        src_callsign: str,
        src_ssid: int,
        via: str,
        dest: str,
        message: str,
    ) -> None:
        """Build an APRS message packet and transmit it.

        If a RigController is registered via set_rig(), the full PTT sequence
        runs in a background thread so the Qt main thread is never blocked:
            1. PTT ON  (CAT T 1)
            2. Wait _PTT_LEAD_S  (rig key-up time)
            3. Send KISS frame → Direwolf encodes and plays audio
            4. Wait _TX_AUDIO_S  (audio duration estimate)
            5. Wait _PTT_TAIL_S  (brief tail)
            6. PTT OFF (CAT T 0)

        Without a rig controller the frame is sent immediately (no PTT).
        """
        kiss = self._mgr.kiss_client
        if kiss is None:
            return
        frame = _build_aprs_message(src_callsign, src_ssid, via, dest, message)

        if self._rig is not None:
            threading.Thread(
                target=self._ptt_send,
                args=(frame,),
                daemon=True,
            ).start()
        else:
            kiss.send_frame(frame)

    def send_position(
        self,
        src_callsign: str,
        src_ssid: int,
        via: str,
        lat_deg: float,
        lon_deg: float,
        symbol: str = "/-",
        comment: str = "",
    ) -> None:
        """Build an APRS position packet and transmit it.

        Uses the uncompressed position format (no timestamp, no messaging):
            !DDMM.hhN/DDDMM.hhES<comment>

        Args:
            src_callsign: Operator callsign (e.g. "JF9SOM")
            src_ssid:     SSID (0–15)
            via:          Digipeater path (e.g. "ARISS")
            lat_deg:      Latitude in decimal degrees (positive = north)
            lon_deg:      Longitude in decimal degrees (positive = east)
            symbol:       Two-character APRS symbol (table + code). Default
                          ``/-`` = house / fixed station.
            comment:      Free-text comment appended after the symbol.
        """
        kiss = self._mgr.kiss_client
        if kiss is None:
            return
        frame = _build_aprs_position(src_callsign, src_ssid, via, lat_deg, lon_deg, symbol, comment)
        if self._rig is not None:
            threading.Thread(
                target=self._ptt_send,
                args=(frame,),
                daemon=True,
            ).start()
        else:
            kiss.send_frame(frame)

    def _ptt_send(self, frame: bytes) -> None:
        """PTT sequence executed in a daemon thread."""
        rig = self._rig
        kiss = self._mgr.kiss_client
        if rig is None or kiss is None:
            return
        try:
            self._ptt_active = True
            rig.set_ptt(True)
            time.sleep(self._PTT_LEAD_S)
            kiss.send_frame(frame)
            time.sleep(self._TX_AUDIO_S)
            time.sleep(self._PTT_TAIL_S)
        finally:
            rig.set_ptt(False)
            self._ptt_active = False

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _wire_kiss(self) -> None:
        """Connect KissClient signals after Direwolf starts."""
        kiss = self._mgr.kiss_client
        if kiss is None:
            return
        kiss.frame_received.connect(self._on_kiss_frame)
        kiss.connection_lost.connect(self._on_kiss_lost)

    def _on_kiss_frame(self, raw: bytes) -> None:
        """Decode an AX.25 frame and emit packet_received."""
        self.raw_frame_received.emit(raw)
        frame: Ax25Frame | None = decode_ax25(raw)
        if frame is None:
            return
        packet: AprsPacket = parse_aprs(frame)
        self.packet_received.emit(packet)

    def _on_kiss_lost(self) -> None:
        self._running = False
        self.error_occurred.emit("Direwolf connection lost.")
        self.status_changed.emit("Disconnected")

    def _load_soundcard_devices(
        self,
    ) -> tuple[int | None, int | None]:
        """Read soundcard_settings from DB and return (in_idx, out_idx)."""
        if not hasattr(self._conn, "execute"):
            return None, None
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'soundcard_settings'"
        ).fetchone()
        if not row or not row["value"]:
            return None, None
        try:
            data = json.loads(row["value"])
            in_idx = data.get("input_device_index")
            out_idx = data.get("output_device_index")
            return (
                int(in_idx) if in_idx is not None else None,
                int(out_idx) if out_idx is not None else None,
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            return None, None


def get_aprs_engine(conn: Any) -> AprsEngine:
    """Return the process-wide AprsEngine singleton (see AprsEngine.instance)."""
    return AprsEngine.instance(conn)


# ---------------------------------------------------------------------------
# AX.25 frame builder for APRS message packets
# ---------------------------------------------------------------------------


def _encode_addr(callsign: str, ssid: int, last: bool = False) -> bytes:
    """Encode one AX.25 address field (7 bytes)."""
    cs = callsign.upper().ljust(6)[:6]
    addr = bytes(ord(c) << 1 for c in cs)
    ssid_byte = ((ssid & 0x0F) << 1) | 0x60
    if last:
        ssid_byte |= 0x01
    return addr + bytes([ssid_byte])


def _build_aprs_message(
    src_call: str,
    src_ssid: int,
    via: str,
    dest_call: str,
    message: str,
) -> bytes:
    """Build a raw AX.25 UI frame containing an APRS message packet.

    The destination is set to ``APRS`` per convention.  The via path is
    encoded as a single digipeater address (e.g. "ARISS").
    """
    via_call = via.strip().upper() or "ARISS"
    via_ssid = 0

    dest_field = _encode_addr("APRS", 0)
    src_field = _encode_addr(src_call, src_ssid)
    via_field = _encode_addr(via_call, via_ssid, last=True)

    # Pad destination callsign to 6 chars in APRS info
    dest_padded = dest_call.upper().ljust(9)[:9]
    info = f":{dest_padded}:{message}"

    frame = (
        dest_field
        + src_field
        + via_field
        + bytes([0x03, 0xF0])  # UI frame, no layer 3
        + info.encode("ascii", errors="replace")
    )
    return frame


def _latlon_to_aprs(lat_deg: float, lon_deg: float) -> tuple[str, str]:
    """Convert decimal-degree lat/lon to APRS uncompressed position strings.

    Returns (lat_str, lon_str) in DDmm.hhN / DDDmm.hhE format.
    """
    lat_abs = abs(lat_deg)
    lat_d = int(lat_abs)
    lat_m = (lat_abs - lat_d) * 60.0
    lat_hemi = "N" if lat_deg >= 0 else "S"
    lat_str = f"{lat_d:02d}{lat_m:05.2f}{lat_hemi}"

    lon_abs = abs(lon_deg)
    lon_d = int(lon_abs)
    lon_m = (lon_abs - lon_d) * 60.0
    lon_hemi = "E" if lon_deg >= 0 else "W"
    lon_str = f"{lon_d:03d}{lon_m:05.2f}{lon_hemi}"

    return lat_str, lon_str


def _build_aprs_position(
    src_call: str,
    src_ssid: int,
    via: str,
    lat_deg: float,
    lon_deg: float,
    symbol: str = "/-",
    comment: str = "",
) -> bytes:
    """Build a raw AX.25 UI frame containing an APRS position packet.

    Format: !DDmm.hhN/DDDmm.hhES<comment>
    where S is the two-character APRS symbol (table + code, default ``/-``).
    """
    via_call = via.strip().upper() or "ARISS"
    sym_table = symbol[0] if len(symbol) >= 1 else "/"
    sym_code = symbol[1] if len(symbol) >= 2 else "-"

    dest_field = _encode_addr("APRS", 0)
    src_field = _encode_addr(src_call, src_ssid)
    via_field = _encode_addr(via_call, 0, last=True)

    lat_str, lon_str = _latlon_to_aprs(lat_deg, lon_deg)
    info = f"!{lat_str}{sym_table}{lon_str}{sym_code}{comment}"

    return (
        dest_field
        + src_field
        + via_field
        + bytes([0x03, 0xF0])
        + info.encode("ascii", errors="replace")
    )
