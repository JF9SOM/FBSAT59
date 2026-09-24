"""SSDV decoder — hex-frame parsing, packet extraction and the ssdv CLI wrapper.

SSDV (Slow Scan Digital Video) transmits JPEG images as fixed-size packets
(256 bytes by default; ``ssdv -l`` allows shorter ones). Each packet carries the
entropy-coded data of a run of JPEG MCUs; the ssdv tool reassembles them into a
JPEG file.

Packet layout (ssdv.h): sync ``0x55``, type ``0x66`` (with 32 bytes of Reed-Solomon
parity at the end) or ``0x67`` (no FEC), then callsign (4), image id (1), packet
id (2), width/8 (1), height/8 (1), flags (1), MCU offset (1), MCU index (2) --
15 header bytes -- the payload, a big-endian CRC-32 of everything after the sync
byte, and the parity for type ``0x66``.

Reception paths:
  - AX.25 path: the packet sits inside an AX.25 (or bare HDLC) frame. Frames come
    live from the shared AprsEngine, or are pasted as hex text (the Telemetry tab,
    SatNOGS Network's "Data" tab ...). find_ssdv_packet() locates the packet in
    a frame, whatever header precedes it.
  - Audio path: SSDV packets as FM audio tones (some CubeSats), not implemented here.

SsdvDecoder groups packets by image id, drops duplicates, orders them by packet
id and hands them to the ``ssdv`` binary. The binary is located as: user-installed
copy in the app data directory, then PATH.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import zlib
from collections import Counter
from pathlib import Path

from PySide6.QtCore import QObject, QStandardPaths, QTimer, Signal
from PySide6.QtGui import QImage

SYNC_BYTE = 0x55
TYPE_NORMAL = 0x66  # 0x66 + SSDV_TYPE_NORMAL: with Reed-Solomon parity
TYPE_NOFEC = 0x67  # 0x66 + SSDV_TYPE_NOFEC
PACKET_SIZE = 256  # ssdv's default (and maximum) packet length
_HEADER_SIZE = 15
_CRC_SIZE = 4
_FEC_SIZE = 32
# Shortest packets ssdv accepts: at least 2 payload bytes.
MIN_PACKET_SIZE_NOFEC = _HEADER_SIZE + _CRC_SIZE + 2
MIN_PACKET_SIZE_NORMAL = _HEADER_SIZE + _CRC_SIZE + _FEC_SIZE + 2

_HEX_PAIRS = re.compile(r"^(?:0x)?[0-9a-f]{2}(?:[\s,:;\-]*(?:0x)?[0-9a-f]{2})*$", re.IGNORECASE)
_HEX_TOKEN = re.compile(r"[0-9a-f]{2}", re.IGNORECASE)


def extract_hex_frames(text: str) -> list[bytes]:
    """Parse pasted / logged hex text into frames, one frame per line.

    A line counts as a frame when it consists only of hex bytes, written as
    ``94 A6 62``, ``94a662``, ``0x94,0xA6``, ``94:A6:62`` or any mix. A line copied
    from a table (tab-separated cells) contributes its last cell. Everything else
    -- headers such as SatNOGS' ``data_obs/2026/9/23/...`` file names, blank lines,
    timestamps -- is ignored, so text copied from a web page can be pasted as is.
    """
    frames: list[bytes] = []
    for line in text.splitlines():
        cell = line.rsplit("\t", 1)[-1].strip()
        if not cell or not _HEX_PAIRS.match(cell):
            continue
        frames.append(
            bytes(
                int(tok, 16) for tok in _HEX_TOKEN.findall(cell.replace("0x", "").replace("0X", ""))
            )
        )
    return frames


def format_hex_line(frame: bytes) -> str:
    """*frame* as one line of upper-case hex bytes separated by spaces."""
    return frame.hex(" ").upper()


def _crc_ok(packet: bytes) -> bool:
    """True if *packet*'s CRC-32 (over everything after the sync byte) matches."""
    size = len(packet)
    crc_end = size - _CRC_SIZE if packet[1] == TYPE_NOFEC else size - _CRC_SIZE - _FEC_SIZE
    if crc_end <= _HEADER_SIZE + 1:
        return False
    return zlib.crc32(packet[1:crc_end]) == int.from_bytes(packet[crc_end : crc_end + 4], "big")


def _plausible_length(packet_type: int, size: int) -> bool:
    minimum = MIN_PACKET_SIZE_NOFEC if packet_type == TYPE_NOFEC else MIN_PACKET_SIZE_NORMAL
    return minimum <= size <= PACKET_SIZE


def find_ssdv_packet(frame: bytes) -> bytes | None:
    """Return the SSDV packet contained in *frame*, or None.

    The packet is located by its sync byte + type byte (``55 66`` / ``55 67``)
    at any offset -- an AX.25 header, a KISS byte or nothing at all may precede
    it -- and taken to run to the end of the frame (at most 256 bytes). A
    candidate whose CRC-32 checks out wins. A full-length type-0x66 candidate
    with a bad CRC is returned as a last resort, because ssdv's Reed-Solomon
    decoder can still repair it.
    """
    fallback: bytes | None = None
    for i in range(len(frame) - 1):
        if frame[i] != SYNC_BYTE or frame[i + 1] not in (TYPE_NORMAL, TYPE_NOFEC):
            continue
        candidate = frame[i : i + PACKET_SIZE]
        if not _plausible_length(candidate[1], len(candidate)):
            continue
        if _crc_ok(candidate):
            return candidate
        if fallback is None and candidate[1] == TYPE_NORMAL and len(candidate) == PACKET_SIZE:
            fallback = candidate
    return fallback


def packet_image_id(packet: bytes) -> int:
    """The image id (0-255) an SSDV packet belongs to."""
    return packet[6]


def packet_id(packet: bytes) -> int:
    """The packet's index within its image."""
    return (packet[7] << 8) | packet[8]


def find_ssdv() -> str | None:
    """Return path to the ssdv binary, or None if not found."""
    # 1. User-installed
    data_dir = (
        Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
        / "ssdv"
    )
    for candidate in (data_dir / "ssdv", data_dir / "ssdv.exe"):
        if candidate.is_file():
            return str(candidate)
    # 2. System PATH
    found = shutil.which("ssdv")
    if found:
        return found
    return None


class SsdvDecoder(QObject):
    """Reassemble SSDV packets into images using the ssdv CLI tool.

    Packets are grouped by image id (duplicates dropped, ordered by packet id).
    The image with the most packets is decoded and reported; partially received
    images decode too (missing MCUs stay blank), so the picture builds up as
    packets arrive.

    Signals
    -------
    image_updated(QImage)
        Emitted after each successful decode with the current best image.
    status_changed(str)
        Short human-readable status string.
    error_occurred(str)
        Emitted when the ssdv binary is missing or returns an error.
    """

    image_updated: Signal = Signal(object)
    status_changed: Signal = Signal(str)
    error_occurred: Signal = Signal(str)

    # Delay before a live decode: packets arrive in bursts, decode once per burst.
    _DECODE_DELAY_MS: int = 700

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # (image id, packet id) -> packet bytes; insertion order = arrival order
        self._packets: dict[tuple[int, int], bytes] = {}
        self._ssdv_path: str | None = find_ssdv()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self._DECODE_DELAY_MS)
        self._timer.timeout.connect(self.decode_now)

    @property
    def is_available(self) -> bool:
        """Return True when the ssdv binary can be located."""
        return self._ssdv_path is not None

    @property
    def packet_count(self) -> int:
        """Number of distinct packets currently buffered."""
        return len(self._packets)

    def push_packet(self, data: bytes) -> bool:
        """Buffer one SSDV packet (starting at its sync byte); returns False if it isn't one.

        A live decode is scheduled shortly afterwards (see _DECODE_DELAY_MS).
        """
        if (
            len(data) < 2
            or data[0] != SYNC_BYTE
            or data[1] not in (TYPE_NORMAL, TYPE_NOFEC)
            or not _plausible_length(data[1], len(data))
        ):
            return False
        self._packets[(packet_image_id(data), packet_id(data))] = bytes(data)
        self._timer.start()
        return True

    def reset(self) -> None:
        """Forget every buffered packet."""
        self._timer.stop()
        self._packets.clear()

    def flush(self) -> None:
        """Decode whatever is buffered one last time, then forget it."""
        self._timer.stop()
        if self._packets:
            self.decode_now()
        self._packets.clear()

    def decode_now(self) -> bool:
        """Run ssdv on the buffered packets; emit the best image. True on success."""
        self._timer.stop()
        if not self._packets:
            return False
        if not self._ssdv_path:
            self._ssdv_path = find_ssdv()
        if not self._ssdv_path:
            self.error_occurred.emit(
                "ssdv binary not found. Build it from https://github.com/fsphil/ssdv "
                "and put it on PATH (or in the app data 'ssdv' folder)."
            )
            return False

        image_id, packets = self._best_image()
        lengths = Counter(len(p) for p in packets)
        length = lengths.most_common(1)[0][0]  # ssdv takes one packet length for the run
        packets = [p for p in packets if len(p) == length]
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            out_path = tmp.name
        try:
            result = subprocess.run(  # noqa: S603
                [self._ssdv_path, "-d", "-l", str(length), "-", out_path],
                input=b"".join(packets),
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                self.error_occurred.emit(f"ssdv failed (exit code {result.returncode}).")
                return False
            qimg = QImage(out_path)
            if qimg.isNull():
                self.error_occurred.emit("ssdv produced no image.")
                return False
            self.image_updated.emit(qimg.copy())
            self.status_changed.emit(
                f"SSDV: image {image_id}, {len(packets)} packets ({length} bytes each)"
            )
            return True
        except subprocess.TimeoutExpired:
            self.error_occurred.emit("ssdv decode timed out.")
        except OSError:
            self.error_occurred.emit(f"ssdv binary not executable: {self._ssdv_path}")
        finally:
            Path(out_path).unlink(missing_ok=True)
        return False

    def _best_image(self) -> tuple[int, list[bytes]]:
        """The (image id, packets ordered by packet id) with the most packets."""
        by_image: dict[int, list[bytes]] = {}
        for (image, _pid), pkt in self._packets.items():
            by_image.setdefault(image, []).append(pkt)
        best = max(by_image, key=lambda k: len(by_image[k]))
        return best, sorted(by_image[best], key=packet_id)
