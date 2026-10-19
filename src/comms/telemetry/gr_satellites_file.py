"""Decode a whole I/Q recording with gr_satellites, much faster than real time.

``gr_satellites --wavfile FILE --iq`` reads the recording straight from disk and runs as fast
as the machine allows (a 11 minute, 1.3 GB recording takes about half a minute), unlike the
SDR-pipeline route, which replays it in real time. gr_satellites prints no sample position
with a decoded frame, so a frame's time in the recording is *estimated* from when its text
arrived: the file is read at an even rate, which makes arrival time proportional to the
position. The estimate is good to a few seconds, not to the sample, so these frames are
never sent to SatNOGS.
"""

from __future__ import annotations

import contextlib
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QThread, Signal

from comms.aprs.direwolf import _kiss_decode_frames
from comms.telemetry.gr_satellites_backend import (
    FrameBlockCollector,
    _supports_kiss_server,
    build_gr_env,
    satellite_argument,
)
from comms.telemetry.gr_satellites_install import resolve_gr_satellites_command
from comms.telemetry.gr_satellites_log import get_gr_satellites_logger
from comms.telemetry.iq_wav import WavError, iter_iq_blocks, read_wav_info

# How much of the recording is decoded to measure gr_satellites' own start-up time.
_CALIBRATION_S = 1.0
# How many lines of gr_satellites' output are kept for an error message.
_TAIL_LINES = 6


def write_iq_wav(path: Path, samples: np.ndarray, sample_rate: float) -> None:
    """Write complex64 *samples* as a canonical float32 stereo (I, Q) WAV file."""
    data = np.column_stack([samples.real, samples.imag]).astype(np.float32).tobytes()
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 3, 2, int(sample_rate), int(sample_rate) * 8, 8, 32)
    header += b"data" + struct.pack("<I", len(data))
    path.write_bytes(header + data)


def estimate_frame_seconds(
    arrival_s: float, total_s: float, overhead_s: float, duration_s: float
) -> float:
    """Position in the recording of a frame whose text arrived *arrival_s* into the run.

    The run took *total_s* in all. *overhead_s* is what a run over almost nothing takes
    (gr_satellites starting up and shutting down); it is shared half and half between the
    two, and the rest of the run reads the *duration_s* of recording at an even rate.
    """
    reading = max(total_s - overhead_s, 1e-6)
    fraction = (arrival_s - overhead_s / 2) / reading
    return min(max(fraction, 0.0), 1.0) * duration_s


class GrFileDecoder(QThread):
    """Run gr_satellites over a recording file and report the frames it decodes.

    Usage
    -----
    job = GrFileDecoder(path, norad)
    job.frame_found.connect(consumer)   # (str: text, float: estimated seconds in, bytes: frame)
    job.progress.connect(show)          # (-1.0: unknown, int frames so far)
    job.decoded.connect(done)           # (float: recording seconds, float: elapsed, int frames)
    job.failed.connect(error)           # (str)
    job.start()
    ...
    job.cancel()                        # stops early; decoded is then not emitted

    The frames are reported together at the end, once their times can be estimated. The
    bytes are the deframed frame as gr_satellites writes it with ``--kiss_out`` (what SatNOGS
    takes), empty when that could not be paired with the text.
    """

    frame_found: Signal = Signal(str, float, bytes)
    progress: Signal = Signal(float, int)
    decoded: Signal = Signal(float, float, int)
    failed: Signal = Signal(str)

    def __init__(
        self,
        path: Path,
        norad: int,
        catalog_norad: int | None = None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._path = Path(path)
        self._sat_arg = satellite_argument(catalog_norad if catalog_norad is not None else norad)
        self._cancel = threading.Event()
        self._proc: subprocess.Popen[str] | None = None

    def cancel(self) -> None:
        """Ask the worker to stop (kills gr_satellites) and wait for it."""
        self._cancel.set()
        proc = self._proc
        if proc is not None:
            with contextlib.suppress(OSError):
                proc.kill()
        self.wait(5000)

    def _run_gr(
        self, wav: Path, collector: FrameBlockCollector | None, kiss_out: Path | None = None
    ) -> tuple[list[tuple[str, float]], float, int, list[str]]:
        """Run gr_satellites on *wav*; return (frame blocks, seconds taken, exit code, tail)."""
        resolved = resolve_gr_satellites_command()
        if resolved is None:
            raise OSError("gr_satellites not found — install via Help > gr-satellites…")
        argv_prefix, is_bundled = resolved
        env = build_gr_env(is_bundled)
        cmd = [*argv_prefix, self._sat_arg, "--wavfile", str(wav), "--iq"]
        if kiss_out is not None and _supports_kiss_server(argv_prefix, env):
            cmd += ["--kiss_out", str(kiss_out)]
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        logger = get_gr_satellites_logger()
        logger.info("--- fast decode: %s", " ".join(cmd))
        started = time.monotonic()
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            **kwargs,
        )
        blocks: list[tuple[str, float]] = []
        tail: list[str] = []
        assert self._proc.stdout is not None
        for raw_line in self._proc.stdout:
            now = time.monotonic() - started
            line = raw_line.rstrip()
            logger.info(line)
            if line:
                tail = [*tail, line][-_TAIL_LINES:]
            if collector is not None:
                block = collector.feed(raw_line, now)
                if block is not None:
                    blocks.append(block)
                    self.progress.emit(-1.0, len(blocks))
        code = self._proc.wait()
        elapsed = time.monotonic() - started
        self._proc = None
        if collector is not None:
            last = collector.finish()
            if last is not None:
                blocks.append(last)
        return blocks, elapsed, code, tail

    def _measure_overhead(self, info_rate: float) -> float:
        """Seconds gr_satellites takes to start, read almost nothing and exit."""
        first = next(iter_iq_blocks(self._path, read_wav_info(self._path)), None)
        if first is None:
            return 0.0
        snippet = np.asarray(first[: int(info_rate * _CALIBRATION_S)])
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "calibration.wav"
            write_iq_wav(wav, snippet, info_rate)
            _blocks, elapsed, _code, _tail = self._run_gr(wav, None)
        return elapsed

    @staticmethod
    def _read_kiss(path: Path) -> list[bytes]:
        """The data frames gr_satellites wrote to its ``--kiss_out`` file (none if absent)."""
        try:
            return _kiss_decode_frames(bytearray(path.read_bytes()))
        except OSError:
            return []

    def run(self) -> None:
        try:
            info = read_wav_info(self._path)
        except WavError as exc:
            self.failed.emit(str(exc))
            return
        started = time.monotonic()
        try:
            overhead = self._measure_overhead(info.sample_rate)
            if self._cancel.is_set():
                return
            with tempfile.TemporaryDirectory() as tmp:
                kiss_path = Path(tmp) / "frames.kiss"
                blocks, total, code, tail = self._run_gr(
                    self._path, FrameBlockCollector(), kiss_path
                )
                frames = self._read_kiss(kiss_path)
        except OSError as exc:
            self.failed.emit(str(exc))
            return
        if self._cancel.is_set():
            return
        if code != 0 and not blocks:
            self.failed.emit(" | ".join(tail) or f"gr_satellites exited with code {code}")
            return
        overhead = min(overhead, total / 2)
        # The text blocks and the KISS frames come in the same order; only pair them when
        # there are as many of each.
        paired = len(frames) == len(blocks)
        for i, (text, arrival) in enumerate(blocks):
            self.frame_found.emit(
                text,
                estimate_frame_seconds(arrival, total, overhead, info.duration_s),
                frames[i] if paired else b"",
            )
        self.progress.emit(1.0, len(blocks))
        self.decoded.emit(info.duration_s, time.monotonic() - started, len(blocks))
