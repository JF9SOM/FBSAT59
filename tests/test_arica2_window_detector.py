"""BeaconWindowDetector: keyed-carrier end detection on synthetic I/Q."""

from __future__ import annotations

import numpy as np
import pytest
from numpy.typing import NDArray

from comms.arica2.window_detector import (
    AudioBeaconWindowDetector,
    BeaconEnded,
    BeaconWindowDetector,
)

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


# -- Detection from the rig's receive audio ------------------------------------------------


_RATE = 48_000


def _audio(quiet: list[tuple[float, float]], total_s: float, seed: int = 1) -> np.ndarray:
    """Receiver noise; carrier keyed on (quieter noise) during the (start, end) stretches."""
    rng = np.random.default_rng(seed)
    audio = (rng.standard_normal(int(total_s * _RATE)) * 0.006).astype(np.float32)
    for a, b in quiet:
        audio[int(a * _RATE) : int(b * _RATE)] *= 0.3
    return audio


def _run(det: AudioBeaconWindowDetector, audio: np.ndarray) -> list:
    events = []
    for i in range(0, len(audio), 2400):
        events.extend(det.push_samples(audio[i : i + 2400]))
    return events


def _cw(start: float, seconds: float = 20.0, pulse: float = 0.25, step: float = 0.5):
    n = int(seconds / step)
    return [(start + k * step, start + k * step + pulse) for k in range(n)]


def test_audio_detector_reports_the_end_of_a_keyed_beacon() -> None:
    det = AudioBeaconWindowDetector(_RATE)
    events = _run(det, _audio(_cw(10.0), 40.0))
    assert len(events) == 1
    assert events[0].burst_s == pytest.approx(19.75, abs=1.0)
    # The 3 s needed to be sure the beacon is over counts against the 15 s window.
    assert events[0].remaining_s == pytest.approx(12.0, abs=0.3)


def test_audio_detector_ignores_a_pause_inside_the_cw() -> None:
    pulses = _cw(10.0, 8.0) + _cw(10.0 + 8.0 + 2.4, 8.0)  # a 2.4 s word gap in the middle
    events = _run(AudioBeaconWindowDetector(_RATE), _audio(pulses, 40.0))
    assert len(events) == 1  # one beacon item, not two


def test_audio_detector_says_nothing_about_plain_noise() -> None:
    for seed in range(5):
        assert _run(AudioBeaconWindowDetector(_RATE), _audio([], 300.0, seed)) == []


def test_audio_detector_ignores_a_short_dip() -> None:
    assert _run(AudioBeaconWindowDetector(_RATE), _audio([(20.0, 21.0)], 40.0)) == []


def test_audio_detector_ignores_our_own_muted_receive_audio() -> None:
    det = AudioBeaconWindowDetector(_RATE)
    audio = _audio([(20.0, 21.0)], 40.0)  # the receiver goes quiet while we transmit
    events = []
    for i in range(0, len(audio), 2400):
        now = i / _RATE
        det.set_transmitting(19.9 <= now <= 21.1)
        events.extend(det.push_samples(audio[i : i + 2400]))
    assert events == []
    assert det.carrier_present is False


def test_audio_detector_sees_two_beacons_in_a_row() -> None:
    pulses = _cw(10.0) + _cw(10.0 + 42.0)
    events = _run(AudioBeaconWindowDetector(_RATE), _audio(pulses, 80.0))
    assert len(events) == 2


def test_audio_detector_survives_signal_loss_of_the_noise_floor() -> None:
    det = AudioBeaconWindowDetector(_RATE)
    assert _run(det, np.zeros(10 * _RATE, dtype=np.float32)) == []  # muted audio: nothing to judge
