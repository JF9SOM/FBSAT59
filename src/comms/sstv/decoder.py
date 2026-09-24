"""SSTV decoder -- Robot36 and PD120 (the modes amateur satellites use).

A streaming decoder: audio comes in through push_samples() in chunks of any size
and images are decoded line by line as the signal goes by. It is verified
against pySSTV (an independent encoder) in tests/test_sstv_decoder.py.

How it works
------------
1. Frequency: the audio is mixed to complex baseband around 1700 Hz, low-passed
   and FM-discriminated (a streaming filter, so chunk boundaries are invisible),
   giving one frequency estimate per audio sample (1100-2300 Hz carries the
   picture; 1200 Hz is sync).
2. Start of an image: the VIS header (300 ms 1900 Hz leader, 10 ms 1200 Hz break,
   300 ms leader, start bit, 7 data bits + even parity at 1100/1300 Hz, stop bit)
   names the mode and gives the timing of the first line. When the header was
   missed (a weak signal, a recording that starts mid-image) a run of sync pulses
   spaced exactly one line period apart starts the image instead.
3. Lines: every line's 1200 Hz sync pulse is located near where the previous
   line predicts it, and the line is re-timed to it -- so a sound card whose clock
   is a little off (slant) does not skew the picture. Pixels are the mean
   frequency over each pixel's time slot (much less noise than sampling one
   instant), mapped 1500 Hz = black ... 2300 Hz = white.
4. Colour: JFIF YCbCr, as pySSTV and most SSTV software use. Robot36 sends R-Y on
   even lines and B-Y on odd ones (half the vertical chroma resolution); PD120
   sends Y, R-Y, B-Y, Y for two picture rows per radio line.
5. An image ends when its last line is decoded, or -- if the signal is lost -- after
   several lines without a sync pulse (the picture so far is kept), or when the
   decoder is stopped.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

try:
    from scipy import ndimage as _ndi
    from scipy import signal as _sp

    _SCIPY: bool = True
except ImportError:
    _ndi = None
    _sp = None
    _SCIPY = False

FREQ_SYNC = 1200.0
FREQ_BLACK = 1500.0
FREQ_WHITE = 2300.0
FREQ_LEADER = 1900.0
_FREQ_RANGE = FREQ_WHITE - FREQ_BLACK

# Frequency-estimation front end
_MIX_HZ = 1700.0  # centre of the 1100-2300 Hz band
# Narrow enough to reject noise (at 12 dB audio SNR a 1000 Hz cutoff gives ~25 % less error than
# 1300 Hz, ~40 % less than 2200 Hz), wide enough for the 1100-2300 Hz tones around the mixer.
_LP_CUTOFF_HZ = 1000.0
_LP_TAPS = 255

# VIS header (ms)
_LEADER_MS = 300.0
_BREAK_MS = 10.0
_VIS_BIT_MS = 30.0
_VIS_TOTAL_MS = 10 * _VIS_BIT_MS  # start bit + 8 data/parity bits + stop bit
_MIN_LEADER_MS = 200.0

# Lines
_SYNC_SEARCH_BEFORE_MS = 12.0
_SYNC_SEARCH_AFTER_MS = 20.0
_MAX_MISSED_SYNCS = 8  # consecutive lines without a sync pulse before the image is given up
_MIN_PARTIAL_LINES = 5  # a lost/stopped image is kept only if at least this many lines came in
_FALLBACK_PULSES = 5  # consecutive equally spaced sync pulses that start an image without a VIS


@dataclass(frozen=True)
class _Mode:
    """Timing and geometry of one SSTV mode."""

    name: str
    vis: int
    width: int
    height: int  # picture rows
    line_ms: float  # one radio line
    sync_ms: float
    radio_lines: int


ROBOT36 = _Mode("Robot36", 8, 320, 240, 150.0, 9.0, 240)
PD120 = _Mode("PD120", 95, 640, 496, 508.48, 20.0, 248)
_MODES = {m.vis: m for m in (ROBOT36, PD120)}

# Robot36 line: sync 9, porch 3, Y 88, separator 4.5, porch 1.5, chroma 44 (ms)
_R36_Y_START_MS = 9.0 + 3.0
_R36_Y_MS = 88.0
_R36_SEP_MS = 4.5
_R36_C_START_MS = _R36_Y_START_MS + _R36_Y_MS + _R36_SEP_MS + 1.5
_R36_C_MS = 44.0
_R36_CHROMA_WIDTH = 160

# PD120 line: sync 20, porch 2.08, then Y(even row), R-Y, B-Y, Y(odd row), 121.6 ms each
_PD_START_MS = 20.0 + 2.08
_PD_SCAN_MS = 121.6


def _to_pixel(freq_hz: np.ndarray) -> np.ndarray:
    """Frequency (Hz) -> 0..255 (1500 Hz black, 2300 Hz white)."""
    return np.clip((freq_hz - FREQ_BLACK) / _FREQ_RANGE * 255.0, 0.0, 255.0)


def _runs(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Start and end (exclusive) indices of the runs of True in *mask*."""
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return np.flatnonzero(d == 1), np.flatnonzero(d == -1)


class _FreqDemod:
    """Streaming audio -> instantaneous frequency (Hz), one value per input sample.

    The output is delayed by the FIR's group delay, which is removed by dropping
    that many leading samples, so output sample k belongs to input sample k.
    """

    def __init__(self, sample_rate: int) -> None:
        self._rate = float(sample_rate)
        self._taps = _sp.firwin(_LP_TAPS, _LP_CUTOFF_HZ, fs=self._rate).astype(np.float64)
        self._delay = (_LP_TAPS - 1) // 2
        self._zi = np.zeros(_LP_TAPS - 1, dtype=np.complex128)
        self._n = 0  # input samples seen (mixer phase)
        self._skip = self._delay  # leading outputs still to drop
        self._prev = 0j

    def process(self, audio: np.ndarray) -> np.ndarray:
        n = np.arange(self._n, self._n + len(audio))
        self._n += len(audio)
        mixed = audio.astype(np.float64) * np.exp(-2j * np.pi * _MIX_HZ * n / self._rate)
        filtered, self._zi = _sp.lfilter(self._taps, [1.0], mixed, zi=self._zi)
        shifted = np.concatenate(([self._prev], filtered))
        self._prev = filtered[-1] if len(filtered) else self._prev
        freq = _MIX_HZ + np.angle(shifted[1:] * np.conj(shifted[:-1])) * self._rate / (2 * np.pi)
        if self._skip:
            drop = min(self._skip, len(freq))
            self._skip -= drop
            freq = freq[drop:]
        result: np.ndarray = freq.astype(np.float32)
        return result


class _FreqBuffer:
    """Growing array of frequency estimates addressed by absolute sample index."""

    def __init__(self) -> None:
        self._data = np.zeros(1 << 16, dtype=np.float32)
        self._head = 0  # index in _data of the oldest kept sample
        self._len = 0  # kept samples
        self._base = 0  # absolute index of _data[_head]

    @property
    def end(self) -> int:
        """Absolute index one past the newest sample."""
        return self._base + self._len

    @property
    def start(self) -> int:
        """Absolute index of the oldest sample still held."""
        return self._base

    def append(self, values: np.ndarray) -> None:
        tail = self._head + self._len
        if tail + len(values) > len(self._data):
            # compact to the front, growing if the kept samples themselves need more room
            capacity = max(len(self._data), 2 * (self._len + len(values)))
            grown = (
                np.zeros(capacity, dtype=np.float32) if capacity != len(self._data) else self._data
            )
            grown[: self._len] = self._data[self._head : tail].copy()
            self._data = grown
            self._head = 0
            tail = self._len
        self._data[tail : tail + len(values)] = values
        self._len += len(values)

    def view(self, a: int, b: int) -> np.ndarray:
        """Samples [a, b) (clipped to what is held)."""
        a = max(a, self._base)
        b = min(b, self.end)
        if b <= a:
            return np.zeros(0, dtype=np.float32)
        lo = self._head + (a - self._base)
        return self._data[lo : lo + (b - a)]

    def trim(self, before: int) -> None:
        """Forget samples older than absolute index *before*."""
        drop = min(max(before - self._base, 0), self._len)
        self._head += drop
        self._base += drop
        self._len -= drop


class SstvDecoder(QObject):
    """Decode SSTV images from streaming audio.

    Signals
    -------
    line_received(line_number, QImage)
        Emitted after each decoded line for progressive display.
    image_complete(QImage, str)
        Emitted when an image is finished (or given up on with most of it in
        hand). Second arg is the mode name.
    mode_detected(str)
        Emitted when the mode of a new image is identified.
    status_changed(str)
        Short human-readable status string.
    """

    line_received: Signal = Signal(int, object)
    image_complete: Signal = Signal(object, str)
    mode_detected: Signal = Signal(str)
    status_changed: Signal = Signal(str)

    def __init__(self, sample_rate: int = 44100, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.RLock()
        self._active = False
        self._rate = int(sample_rate)
        self._reset_state()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    @property
    def sample_rate(self) -> int:
        return self._rate

    def start(self) -> None:
        """Begin accepting audio."""
        with self._lock:
            self._reset_state()
            self._active = True
        self.status_changed.emit("Listening for SSTV signal…")

    def stop(self) -> None:
        """Stop; an image in progress is kept (emitted) if enough of it arrived."""
        pending: tuple[QImage, str] | None = None
        with self._lock:
            if self._active and self._mode is not None:
                pending = self._finish_image(partial=True)
            self._active = False
            self._reset_state()
        if pending is not None:
            self.image_complete.emit(*pending)
        self.status_changed.emit("Stopped")

    def set_sample_rate(self, rate: int) -> None:
        """Change the audio sample rate (the source changed); starts a fresh search."""
        with self._lock:
            active = self._active
            self._rate = int(rate)
            self._reset_state()
            self._active = active

    def push_samples(self, samples: np.ndarray) -> None:
        """Receive audio (float32/float64, mono or (n, channels)), any chunk size.

        Safe to call from any thread. Images are decoded inline; signals are
        emitted after the internal lock is released.
        """
        if not _SCIPY:
            return
        events: list[tuple[str, tuple[Any, ...]]] = []
        with self._lock:
            if not self._active:
                return
            mono = np.asarray(samples, dtype=np.float64)
            if mono.ndim == 2:
                mono = mono.mean(axis=1)
            if len(mono) == 0:
                return
            self._buf.append(self._demod.process(mono))
            self._advance(events)
        for name, args in events:
            getattr(self, name).emit(*args)

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #

    def _reset_state(self) -> None:
        self._demod: _FreqDemod = _FreqDemod(self._rate) if _SCIPY else None  # type: ignore[assignment]
        self._buf = _FreqBuffer()
        self._search_from = 0  # abs index: nothing before this can start an image
        self._mode: _Mode | None = None
        self._origin = 0.0  # abs sample index (float) where the next line's sync pulse should start
        self._line = 0
        self._missed = 0
        self._decoded = 0
        self._y: np.ndarray | None = None  # float (rows, width) luma
        self._cr: np.ndarray | None = None  # float (rows, width) chroma, neutral 128
        self._cb: np.ndarray | None = None
        self._rgb: np.ndarray | None = None  # uint8 (rows, width, 3)

    def _ms(self, ms: float) -> float:
        return ms * self._rate / 1000.0

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    def _advance(self, events: list[tuple[str, tuple[Any, ...]]]) -> None:
        """Process everything the buffered audio allows."""
        while True:
            if self._mode is None:
                if not self._search_start(events):
                    self._trim_idle()
                    return
            elif not self._decode_line(events):
                return

    def _trim_idle(self) -> None:
        # a leader + VIS header is ~0.9 s; keep enough to find one that is still arriving
        self._search_from = max(self._search_from, self._buf.end - int(self._ms(1500.0)))
        self._buf.trim(self._search_from)

    # ------------------------------------------------------------------ #
    # Finding the start of an image
    # ------------------------------------------------------------------ #

    def _search_start(self, events: list[tuple[str, tuple[Any, ...]]]) -> bool:
        """Look for a VIS header (or, failing that, a train of sync pulses).

        Returns True when an image was started (state switched to decoding).
        """
        a = max(self._search_from, self._buf.start)
        freq = self._buf.view(a, self._buf.end)
        if len(freq) < self._ms(400.0):
            return False
        smooth = _ndi.uniform_filter1d(freq, max(1, int(self._ms(2.0))), mode="nearest")

        found = self._find_vis(smooth, a)
        if found is not None:
            mode, image_start = found
            self._start_image(mode, image_start, events, via="VIS")
            return True
        found = self._find_sync_train(smooth, a)
        if found is not None:
            mode, first_sync = found
            self._start_image(mode, first_sync, events, via="sync")
            return True
        return False

    def _find_vis(self, smooth: np.ndarray, base: int) -> tuple[_Mode, float] | None:
        """(mode, absolute index of the first line's sync pulse) of the first valid VIS header."""
        starts, ends = _runs(np.abs(smooth - FREQ_LEADER) < 120.0)
        long_runs = [
            (int(s), int(e))
            for s, e in zip(starts, ends, strict=True)
            if e - s >= self._ms(_MIN_LEADER_MS)
        ]
        for (_s1, e1), (s2, e2) in zip(long_runs, long_runs[1:], strict=False):
            gap = s2 - e1
            if not self._ms(3.0) <= gap <= self._ms(25.0):
                continue
            t0 = e2  # start of the VIS start bit
            if t0 + self._ms(_VIS_TOTAL_MS + 10.0) > len(smooth):
                # header still arriving: resume the search from this leader once more audio is in
                self._search_from = max(self._search_from, base + _s1 - int(self._ms(50.0)))
                return None
            vis = self._read_vis(smooth, t0)
            if vis is not None and vis in _MODES:
                return _MODES[vis], base + t0 + self._ms(_VIS_TOTAL_MS)
        return None

    def _read_vis(self, smooth: np.ndarray, t0: int) -> int | None:
        """The VIS code (even parity checked) whose start bit begins at index *t0*, or None."""

        def mean_at(centre_ms: float) -> float:
            c = t0 + self._ms(centre_ms)
            half = self._ms(_VIS_BIT_MS * 0.3)
            return float(np.mean(smooth[int(c - half) : int(c + half)]))

        if abs(mean_at(_VIS_BIT_MS / 2) - FREQ_SYNC) > 100.0:  # start bit
            return None
        bits = [1 if mean_at(_VIS_BIT_MS * (1.5 + k)) < FREQ_SYNC else 0 for k in range(8)]
        if sum(bits) % 2 != 0:
            return None
        return sum(b << k for k, b in enumerate(bits[:7]))

    def _find_sync_train(self, smooth: np.ndarray, base: int) -> tuple[_Mode, float] | None:
        """A run of sync pulses one line period apart: (mode, index of the first pulse)."""
        starts, ends = _runs(np.abs(smooth - FREQ_SYNC) < 150.0)
        for mode in (ROBOT36, PD120):
            period = self._ms(mode.line_ms)
            tol = self._ms(2.5 if mode is ROBOT36 else 6.0)
            pulses = [
                int(s)
                for s, e in zip(starts, ends, strict=True)
                if self._ms(mode.sync_ms * 0.55) <= e - s <= self._ms(mode.sync_ms * 1.5)
            ]
            run_start = 0
            for i in range(1, len(pulses) + 1):
                if i < len(pulses) and abs((pulses[i] - pulses[i - 1]) - period) <= tol:
                    continue
                if i - run_start >= _FALLBACK_PULSES:
                    return mode, float(base + pulses[run_start])
                run_start = i
        return None

    def _start_image(
        self, mode: _Mode, first_sync: float, events: list[tuple[str, tuple[Any, ...]]], via: str
    ) -> None:
        self._mode = mode
        self._origin = first_sync
        self._line = 0
        self._missed = 0
        self._decoded = 0
        h, w = mode.height, mode.width
        self._y = np.zeros((h, w))
        self._cr = np.full((h, w), 128.0)
        self._cb = np.full((h, w), 128.0)
        self._rgb = np.zeros((h, w, 3), dtype=np.uint8)
        self._buf.trim(int(first_sync - self._ms(60.0)))
        events.append(("mode_detected", (mode.name,)))
        events.append(("status_changed", (f"{mode.name} image started ({via})",)))

    # ------------------------------------------------------------------ #
    # Decoding lines
    # ------------------------------------------------------------------ #

    def _decode_line(self, events: list[tuple[str, tuple[Any, ...]]]) -> bool:
        """Decode the next line if all of its audio is here. False = wait for more audio."""
        mode = self._mode
        assert mode is not None
        period = self._ms(mode.line_ms)
        if self._buf.end < self._origin + period + self._ms(_SYNC_SEARCH_AFTER_MS) + 8:
            return False

        sync = self._locate_sync(mode)
        if sync is None:
            self._missed += 1
            sync = self._origin
        else:
            self._missed = 0

        if mode is ROBOT36:
            self._decode_robot36_line(sync)
        else:
            self._decode_pd120_line(sync)
        self._decoded += 1
        self._origin = sync + period
        self._line += 1

        events.append(("line_received", (self._line - 1, self._qimage())))
        events.append(("status_changed", (f"{mode.name}: line {self._line}/{mode.radio_lines}",)))
        self._buf.trim(int(self._origin - self._ms(_SYNC_SEARCH_BEFORE_MS) - 8))

        if self._line >= mode.radio_lines:
            events.append(("image_complete", (self._qimage(), mode.name)))
            events.append(("status_changed", (f"{mode.name}: image received",)))
            self._end_image()
        elif self._missed >= _MAX_MISSED_SYNCS:
            # the lines since the last sync pulse are noise: blank them, keep the rest
            first_bad = self._line - self._missed
            self._rgb[(first_bad if mode is ROBOT36 else 2 * first_bad) :] = 0  # type: ignore[index]
            self._decoded = first_bad
            pending = self._finish_image(partial=True)
            if pending is not None:
                events.append(("image_complete", pending))
                events.append(
                    ("status_changed", (f"{pending[1]}: signal lost, partial image kept",))
                )
        return True

    def _locate_sync(self, mode: _Mode) -> float | None:
        """Absolute index of the sync pulse near where it is expected, or None."""
        a = int(self._origin - self._ms(_SYNC_SEARCH_BEFORE_MS))
        freq = self._buf.view(a, int(self._origin + self._ms(_SYNC_SEARCH_AFTER_MS + mode.sync_ms)))
        if len(freq) == 0:
            return None
        smooth = _ndi.uniform_filter1d(freq, max(1, int(self._ms(1.0))), mode="nearest")
        starts, ends = _runs(np.abs(smooth - FREQ_SYNC) < 150.0)
        base = max(a, self._buf.start)
        # The pulse start is biased late (the tone before it can be anywhere from 1500 to
        # 2300 Hz, so the 1350 Hz threshold is crossed after the true edge); its end, a
        # 1200 -> 1500 Hz step, is crossed at the midpoint. So position it by its end.
        best: float | None = None
        best_dist = float("inf")
        for s, e in zip(starts, ends, strict=True):
            if e - s < self._ms(mode.sync_ms * 0.5):
                continue
            start = base + e - self._ms(mode.sync_ms)
            dist = abs(start - self._origin)
            if dist < best_dist:
                best, best_dist = float(start), dist
        return best

    def _scan(self, sync: float, start_ms: float, dur_ms: float, pixels: int) -> np.ndarray:
        """Mean frequency of *pixels* equal slots covering [sync+start, +dur) (Hz)."""
        a = sync + self._ms(start_ms)
        n = self._ms(dur_ms)
        seg = self._buf.view(int(round(a)), int(round(a + n)))
        if len(seg) < pixels:
            return np.full(pixels, FREQ_BLACK)
        csum = np.concatenate(([0.0], np.cumsum(seg, dtype=np.float64)))
        edges = np.round(np.linspace(0, len(seg), pixels + 1)).astype(int)
        widths = np.maximum(np.diff(edges), 1)
        means: np.ndarray = (csum[edges[1:]] - csum[edges[:-1]]) / widths
        return means

    def _decode_robot36_line(self, sync: float) -> None:
        w = ROBOT36.width
        y = _to_pixel(self._scan(sync, _R36_Y_START_MS, _R36_Y_MS, w))
        c = _to_pixel(self._scan(sync, _R36_C_START_MS, _R36_C_MS, _R36_CHROMA_WIDTH))
        chroma = np.repeat(c, w // _R36_CHROMA_WIDTH)
        row = self._line
        self._y[row] = y  # type: ignore[index]
        # even lines carry R-Y, odd lines B-Y; a pair of rows shares both
        pair = row - (row % 2)
        target = self._cr if row % 2 == 0 else self._cb
        target[pair] = chroma  # type: ignore[index]
        target[pair + 1] = chroma  # type: ignore[index]
        self._render_rows(pair, row + 1)

    def _decode_pd120_line(self, sync: float) -> None:
        w = PD120.width
        s = _PD_START_MS
        y0 = _to_pixel(self._scan(sync, s, _PD_SCAN_MS, w))
        cr = _to_pixel(self._scan(sync, s + _PD_SCAN_MS, _PD_SCAN_MS, w))
        cb = _to_pixel(self._scan(sync, s + 2 * _PD_SCAN_MS, _PD_SCAN_MS, w))
        y1 = _to_pixel(self._scan(sync, s + 3 * _PD_SCAN_MS, _PD_SCAN_MS, w))
        r0 = 2 * self._line
        self._y[r0] = y0  # type: ignore[index]
        self._y[r0 + 1] = y1  # type: ignore[index]
        for r in (r0, r0 + 1):
            self._cr[r] = cr  # type: ignore[index]
            self._cb[r] = cb  # type: ignore[index]
        self._render_rows(r0, r0 + 2)

    def _render_rows(self, a: int, b: int) -> None:
        """YCbCr (JFIF) -> RGB for picture rows [a, b)."""
        y, cr, cb = self._y[a:b], self._cr[a:b] - 128.0, self._cb[a:b] - 128.0  # type: ignore[index]
        rgb = np.stack((y + 1.402 * cr, y - 0.344136 * cb - 0.714136 * cr, y + 1.772 * cb), axis=-1)
        self._rgb[a:b] = np.clip(np.round(rgb), 0, 255).astype(np.uint8)  # type: ignore[index]

    def _qimage(self) -> QImage:
        assert self._rgb is not None
        h, w = self._rgb.shape[:2]
        contiguous = np.ascontiguousarray(self._rgb)
        return QImage(contiguous.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()

    # ------------------------------------------------------------------ #
    # Ending an image
    # ------------------------------------------------------------------ #

    def _finish_image(self, *, partial: bool) -> tuple[QImage, str] | None:
        """End the current image; returns (image, mode) if enough of it is worth keeping."""
        mode = self._mode
        result: tuple[QImage, str] | None = None
        if partial and mode is not None and self._decoded >= _MIN_PARTIAL_LINES:
            result = (self._qimage(), mode.name)
        self._end_image()
        return result

    def _end_image(self) -> None:
        """Back to searching for the next image, after the audio decoded so far."""
        self._search_from = int(self._origin) if self._mode is not None else self._search_from
        self._mode = None
        self._y = self._cr = self._cb = self._rgb = None
        self._buf.trim(self._search_from)
