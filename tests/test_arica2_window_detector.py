"""BeaconWindowDetector: keyed-carrier end detection on synthetic I/Q."""

from __future__ import annotations

import numpy as np
import pytest
from numpy.typing import NDArray

from comms.arica2.window_detector import BeaconEnded, BeaconWindowDetector

_RATE = 250_000.0
_BLOCK = 16_384


def _noise(n: int, rng: np.random.Generator, level: float = 0.05) -> NDArray[np.complex64]:
    return (level * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(np.complex64)


def _carrier(n: int, offset_hz: float, start: int, level: float = 0.2) -> NDArray[np.complex64]:
    t = (start + np.arange(n)) / _RATE
    return (level * np.exp(2j * np.pi * offset_hz * t)).astype(np.complex64)


def _signal(
    rng: np.random.Generator, segments: list[tuple[float, bool]], offset_hz: float = -927.0
) -> NDArray[np.complex64]:
    """Concatenate (seconds, carrier_on) segments of noise (+ a carrier)."""
    parts = []
    pos = 0
    for seconds, on in segments:
        n = int(seconds * _RATE)
        chunk = _noise(n, rng)
        if on:
            chunk = chunk + _carrier(n, offset_hz, pos)
        parts.append(chunk)
        pos += n
    return np.concatenate(parts)


def _feed(det: BeaconWindowDetector, iq: NDArray[np.complex64]) -> list[BeaconEnded]:
    events: list[BeaconEnded] = []
    for i in range(0, len(iq), _BLOCK):
        events.extend(det.push_samples(iq[i : i + _BLOCK]))
    return events


def test_reports_end_of_a_keyed_burst() -> None:
    rng = np.random.default_rng(1)
    det = BeaconWindowDetector(_RATE)
    events = _feed(det, _signal(rng, [(5, False), (6, True), (6, False)]))
    assert len(events) == 1
    assert events[0].burst_s == pytest.approx(6.0, abs=0.4)
    # detection needs gap_s of silence, which is charged against the window
    assert events[0].remaining_s == pytest.approx(15.0 - 1.5, abs=0.4)


def test_no_event_while_the_carrier_is_present() -> None:
    rng = np.random.default_rng(2)
    det = BeaconWindowDetector(_RATE)
    assert _feed(det, _signal(rng, [(2, False), (8, True)])) == []
    assert det.carrier_present


def test_short_key_gaps_inside_a_burst_do_not_end_it() -> None:
    rng = np.random.default_rng(3)
    det = BeaconWindowDetector(_RATE)
    keyed = [(1.0, True), (0.3, False)] * 6 + [(6, False)]
    events = _feed(det, _signal(rng, keyed))
    assert len(events) == 1


def test_short_carrier_is_not_a_beacon() -> None:
    rng = np.random.default_rng(4)
    det = BeaconWindowDetector(_RATE)
    assert _feed(det, _signal(rng, [(2, False), (1, True), (5, False)])) == []


def test_each_burst_gives_its_own_event() -> None:
    rng = np.random.default_rng(5)
    det = BeaconWindowDetector(_RATE)
    segs = [(2, False), (5, True), (4, False), (5, True), (4, False)]
    assert len(_feed(det, _signal(rng, segs))) == 2


def test_carrier_anywhere_in_the_search_band_is_found() -> None:
    rng = np.random.default_rng(6)
    det = BeaconWindowDetector(_RATE)
    events = _feed(det, _signal(rng, [(2, False), (5, True), (4, False)], offset_hz=+1500.0))
    assert len(events) == 1


def test_wideband_noise_burst_is_not_a_carrier() -> None:
    """A GMSK-like wideband signal raises no single line above the band median."""
    rng = np.random.default_rng(7)
    det = BeaconWindowDetector(_RATE)
    parts = [_noise(int(2 * _RATE), rng), _noise(int(6 * _RATE), rng, level=0.5)]
    parts.append(_noise(int(5 * _RATE), rng))
    assert _feed(det, np.concatenate(parts)) == []


def test_works_at_a_high_sample_rate_and_odd_block_sizes() -> None:
    rate = 2_400_000.0
    rng = np.random.default_rng(8)

    def seg(seconds: float, on: bool, pos: int) -> NDArray[np.complex64]:
        n = int(seconds * rate)
        x = _noise(n, rng)
        if on:
            t = (pos + np.arange(n)) / rate
            x = x + (0.2 * np.exp(2j * np.pi * -927.0 * t)).astype(np.complex64)
        return x

    iq = np.concatenate([seg(2, False, 0), seg(5, True, int(2 * rate)), seg(4, False, 0)])
    det = BeaconWindowDetector(rate)
    events: list[BeaconEnded] = []
    for i in range(0, len(iq), 10_007):
        events.extend(det.push_samples(iq[i : i + 10_007]))
    assert len(events) == 1


def test_rejects_bad_sample_rate() -> None:
    with pytest.raises(ValueError):
        BeaconWindowDetector(0.0)
