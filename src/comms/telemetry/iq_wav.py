"""Read an I/Q recording (.iq.wav) straight from disk, in blocks.

The app's own recorder (``sdr.recorder.IQRecorder``) writes float32 stereo WAV with I in the
left and Q in the right channel. Its header is *not* the 44-byte canonical one: a ``fact``
chunk sits between ``fmt `` and ``data`` (data starts at byte 56), so the chunks have to be
walked, never assumed at fixed offsets (a fixed 44 swaps I and Q and mirrors every frequency).

Used for decoding a whole recording much faster than real time, without the SDR pipeline
(``comms.telemetry.hades_fsk_sdr.HadesFileDecoder``). 16-bit PCM stereo files are read too.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import numpy as np

_FORMAT_PCM = 1
_FORMAT_FLOAT = 3
_FORMAT_EXTENSIBLE = 0xFFFE


class WavInfo(NamedTuple):
    """What the block reader needs to know about a recording."""

    sample_rate: float
    float_samples: bool  # True: float32; False: 16-bit integer PCM
    data_offset: int  # byte offset of the first sample
    frames: int  # number of I/Q samples (frames) in the data chunk

    @property
    def duration_s(self) -> float:
        return self.frames / self.sample_rate


class WavError(ValueError):
    """The file is not an I/Q WAV this reader understands."""


def read_wav_info(path: Path) -> WavInfo:
    """Parse the RIFF chunks of ``path``; raise :class:`WavError` if unsupported."""
    try:
        with open(path, "rb") as f:
            riff = f.read(12)
            if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
                raise WavError("not a WAV file")
            fmt: tuple[int, int, int, int] | None = None
            while True:
                head = f.read(8)
                if len(head) < 8:
                    raise WavError("no data chunk")
                chunk_id, size = head[:4], struct.unpack("<I", head[4:])[0]
                if chunk_id == b"fmt ":
                    body = f.read(size + (size & 1))
                    tag, channels, rate, _byte_rate, _align, bits = struct.unpack(
                        "<HHIIHH", body[:16]
                    )
                    if tag == _FORMAT_EXTENSIBLE and len(body) >= 26:
                        tag = struct.unpack("<H", body[24:26])[0]
                    fmt = (tag, channels, rate, bits)
                elif chunk_id == b"data":
                    if fmt is None:
                        raise WavError("data chunk before fmt chunk")
                    tag, channels, rate, bits = fmt
                    if channels != 2:
                        raise WavError(f"{channels} channel(s): an I/Q file has two")
                    if tag == _FORMAT_FLOAT and bits == 32:
                        float_samples = True
                    elif tag == _FORMAT_PCM and bits == 16:
                        float_samples = False
                    else:
                        raise WavError(f"unsupported sample format (tag {tag}, {bits} bit)")
                    offset = f.tell()
                    available = Path(path).stat().st_size - offset
                    data_bytes = min(size, available)  # a truncated file is read as far as it goes
                    bytes_per_frame = 8 if float_samples else 4
                    return WavInfo(
                        float(rate), float_samples, offset, data_bytes // bytes_per_frame
                    )
                else:
                    f.seek(size + (size & 1), 1)
    except OSError as exc:
        raise WavError(str(exc)) from exc


def iter_iq_blocks(path: Path, info: WavInfo, block: int = 1 << 18) -> Iterator[np.ndarray]:
    """Yield the recording as complex64 blocks of ``block`` samples (the last may be shorter)."""
    bytes_per_frame = 8 if info.float_samples else 4
    with open(path, "rb") as f:
        f.seek(info.data_offset)
        remaining = info.frames
        while remaining > 0:
            count = min(block, remaining)
            raw = f.read(count * bytes_per_frame)
            got = len(raw) // bytes_per_frame
            if got == 0:
                return
            if info.float_samples:
                pairs = np.frombuffer(raw[: got * 8], dtype="<f4").reshape(-1, 2)
            else:
                pairs = np.frombuffer(raw[: got * 4], dtype="<i2").reshape(-1, 2) / 32768.0
            yield (pairs[:, 0] + 1j * pairs[:, 1]).astype(np.complex64)
            remaining -= got
