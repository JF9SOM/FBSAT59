"""Unit tests for sdr/burst_detector.py (pure numpy, no scipy, no Qt).

Signals are synthetic: unit-power complex Gaussian noise with band-limited
noise "bursts" of a known S/N (in an 11 kHz channel, the detector's own
reference bandwidth) added at known times. Blocks and rows are fed exactly as
SDRPipeline does: 16384-sample blocks, one row per two blocks (~131 ms).
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from sdr.burst_detector import FFT_SIZE, BurstDetector, BurstRow, RowKind

_FS = 250_000.0
_BLOCK = 16_384
_CENTER = 437_000_000.0


def _noise(n: int, rng: np.random.Generator) -> NDArray[np.complex64]:
    """Unit-power complex Gaussian noise."""
    z = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / np.sqrt(2.0)
    return z.astype(np.complex64)


def _burst(
    n: int,
    snr_db: float,
    offset_hz: float,
    rng: np.random.Generator,
    width_hz: float = 12_000.0,
) -> NDArray[np.complex64]:
    """Band-limited noise burst, snr_db above the noise in an 11 kHz channel."""
    x = rng.standard_normal(n) + 1j * rng.standard_normal(n)
    spec = np.fft.fft(x)
    freqs = np.fft.fftfreq(n, d=1.0 / _FS)
    spec[np.abs(freqs) > width_hz / 2.0] = 0.0
    y = np.fft.ifft(spec)
    target = 10.0 ** (snr_db / 10.0) * (11_000.0 / _FS)  # noise has unit power
    y *= np.sqrt(target / np.mean(np.abs(y) ** 2))
    y *= np.exp(2j * np.pi * offset_hz * np.arange(n) / _FS)
    return y.astype(np.complex64)


class _Injection:
    """A signal added to the noise starting at a given time (seconds)."""

    def __init__(self, start_s: float, samples: NDArray[np.complex64]) -> None:
        self.start = int(start_s * _FS)
        self.samples = samples

    def overlap(self, first: int, n: int) -> tuple[slice, slice] | None:
        lo = max(first, self.start)
        hi = min(first + n, self.start + len(self.samples))
        if hi <= lo:
            return None
        return slice(lo - first, hi - first), slice(lo - self.start, hi - self.start)


def _run(
    seconds: float,
    injections: list[_Injection] | None = None,
    seed: int = 1,
    detector: BurstDetector | None = None,
) -> list[BurstRow]:
    rng = np.random.default_rng(seed)
    det = detector or BurstDetector(_FS)
    rows: list[BurstRow] = []
    for k in range(int(seconds * _FS / _BLOCK)):
        block = _noise(_BLOCK, rng)
        for inj in injections or []:
            ov = inj.overlap(k * _BLOCK, _BLOCK)
            if ov is not None:
                block[ov[0]] += inj.samples[ov[1]]
        det.feed(block)
        if k % 2 == 1:
            row = det.finish_row(_CENTER)
            assert row is not None
            rows.append(row)
    return rows


def _bursts(rows: list[BurstRow]) -> list[BurstRow]:
    return [r for r in rows if r.kind == RowKind.BURST]


def test_noise_only_never_fires() -> None:
    rows = _run(40.0)
    assert all(r.kind == RowKind.NONE for r in rows)
    assert rows[-1].event_count == 0


def test_burst_is_detected_counted_and_measured() -> None:
    rng = np.random.default_rng(7)
    burst = _burst(int(0.25 * _FS), 8.0, 10_000.0, rng)
    rows = _run(30.0, [_Injection(15.0, burst)])

    hits = _bursts(rows)
    assert hits, "the burst was not detected"
    assert len({r.event_id for r in hits}) == 1
    assert rows[-1].event_count == 1

    # Rows before the burst start are clean.
    first_hit = next(i for i, r in enumerate(rows) if r is hits[0])
    assert all(r.kind == RowKind.NONE for r in rows[:first_hit])

    # Peak S/N of the event is close to what was injected.
    best = max(r.snr_db for r in hits if r.snr_db is not None)
    assert 6.0 <= best <= 10.0

    # The marked span sits on the +10 kHz burst (bin width ~244 Hz).
    row = hits[0]
    assert row.burst_bins is not None
    centre_hz = (sum(row.burst_bins) / 2 - FFT_SIZE / 2) * _FS / FFT_SIZE
    assert abs(centre_hz - 10_000.0) < 3_000.0
    # ... and the row carries the matching frequency axis and spectrum.
    assert len(row.freqs_hz) == len(row.power_dbfs) == FFT_SIZE
    assert abs(float(row.freqs_hz[FFT_SIZE // 2]) - _CENTER) < 1.0


def test_two_bursts_a_couple_of_seconds_apart_count_twice() -> None:
    rng = np.random.default_rng(3)
    inj = [
        _Injection(15.0, _burst(int(0.25 * _FS), 8.0, 0.0, rng)),
        _Injection(17.4, _burst(int(0.25 * _FS), 8.0, 0.0, rng)),
    ]
    rows = _run(30.0, inj)
    assert len({r.event_id for r in _bursts(rows)}) == 2
    assert rows[-1].event_count == 2


def test_nothing_is_detected_during_the_warmup() -> None:
    rng = np.random.default_rng(5)
    rows = _run(8.0, [_Injection(4.0, _burst(int(0.25 * _FS), 12.0, 0.0, rng))])
    assert all(r.warming_up for r in rows)
    assert all(r.kind == RowKind.NONE for r in rows)
    assert rows[-1].event_count == 0


def test_warming_up_flag_clears_after_ten_seconds() -> None:
    rows = _run(14.0)
    assert rows[0].warming_up is True
    assert rows[-1].warming_up is False


def test_stationary_spurs_do_not_fire() -> None:
    n = int(40.0 * _FS)
    t = np.arange(n) / _FS
    spurs = sum(0.6 * np.exp(2j * np.pi * f * t) for f in (-52_000.0, -20_000.0, 7_000.0, 63_000.0))
    rows = _run(40.0, [_Injection(0.0, np.asarray(spurs, dtype=np.complex64))])
    assert all(r.kind != RowKind.BURST for r in rows)
    assert rows[-1].event_count == 0


def test_a_carrier_that_switches_on_is_not_counted_as_a_burst() -> None:
    n = int(30.0 * _FS)
    tone = (2.0 * np.exp(2j * np.pi * 15_000.0 * np.arange(n) / _FS)).astype(np.complex64)
    rows = _run(50.0, [_Injection(15.0, tone)])
    # It may be marked for its first moments, but never for longer than a
    # burst can last, and it is never counted.
    marked_s = sum(1 for r in rows if r.kind == RowKind.BURST) * _BLOCK * 2 / _FS
    assert marked_s <= 3.5
    assert rows[-1].event_count == 0


def test_broadband_impulse_is_flagged_but_not_counted() -> None:
    rng = np.random.default_rng(9)
    n = int(0.3 * _FS)
    impulse = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * 1.5
    rows = _run(25.0, [_Injection(15.0, impulse)])
    assert any(r.kind == RowKind.IMPULSE for r in rows)
    assert all(r.kind != RowKind.BURST for r in rows)
    assert rows[-1].event_count == 0


def test_a_wide_signal_is_not_a_burst() -> None:
    rng = np.random.default_rng(11)
    wide = _burst(int(0.4 * _FS), 12.0, 0.0, rng, width_hz=70_000.0)
    rows = _run(25.0, [_Injection(15.0, wide)])
    assert all(r.kind != RowKind.BURST for r in rows)
    assert rows[-1].event_count == 0


def test_finish_row_without_samples_returns_none() -> None:
    det = BurstDetector(_FS)
    assert det.finish_row(_CENTER) is None
    det.feed(np.zeros(FFT_SIZE - 1, dtype=np.complex64))  # shorter than one FFT
    assert det.finish_row(_CENTER) is None


def test_silence_does_not_crash_or_fire() -> None:
    det = BurstDetector(_FS)
    for k in range(200):
        det.feed(np.zeros(_BLOCK, dtype=np.complex64))
        if k % 2 == 1:
            row = det.finish_row(_CENTER)
            assert row is not None
            assert row.kind == RowKind.NONE
