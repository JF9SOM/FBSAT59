"""HADES-L telemetry from the raw 800 baud 2FSK downlink (weak bursts).

HADES-L (NORAD 98206, 436.665 MHz, SatNOGS "Mode U - 800bps FSK TLM") sends the AMSAT-EA
"Genesis" framing of HADES-SA (training ``AA..``, sync ``BF 35``, a ``size`` byte, type/address,
scrambled data, CRC-16/CCITT-FALSE) at 800 baud with the two tones 1.6 kHz apart, but as short
bursts: 0.5 s for a status packet, 1.5 s for the longest, one every minute or so (checked on a
real pass on 2026-10-20: the address is 5, packet types 4, 8 and 9 were decoded, 0.5 s bursts at
a few dB of tone-to-noise). Those are too weak and too short for the continuous-stream front end
of ``hades_sa_fsk`` (an FM discriminator after a 6 s tone search), which found nothing in the
recording.

This stream keeps the burst *locator* of the 200 baud decoder (``hades_fsk.HadesFskStream``:
the strongest 3.5 kHz band within +-20 kHz of the centre, so a poor TLE does not matter) and
replaces the demodulator:

  1. the two tones are found by a joint search (a pair 1.6 kHz apart that maximises the burst's
     spectrum), then the energy of each tone is integrated over one symbol -- a matched filter
     instead of a discriminator;
  2. the symbol timing, a small tone offset and the tone drift are searched by brute force (the
     CRC and the sync word decide), both polarities;
  3. a packet is *verified* when its CRC is valid as received. If none is, the bits are
     repaired: up to three of the 16 least reliable bits of the best candidate are flipped until
     the CRC is valid, and the result is flagged ``repaired`` (never to be uploaded).

The size byte is not used: it is not covered by the CRC, so a single bit error in it must not
cost the packet; the packet length comes from the type nibble (the HADES-R and HADES-SA tables
both, the CRC tells which). Needs only numpy.
"""

from __future__ import annotations

import itertools

import numpy as np

from comms.telemetry.hades_fsk import (
    _ACCEPT_END_S,
    _DEDUP_S,
    _DEMOD_AFTER_S,
    _DEMOD_BEFORE_S,
    HADES_R_LENGTHS,
    HADES_SA_LENGTHS,
    SYNC_BITS,
    HadesFrame,
    HadesFskStream,
    crc16_ccitt_false,
    descramble,
)

HADES_L_ADDRESS = 5
BAUD_L = 800.0
# The tones were measured 1617 Hz apart; HADES-SA's description says 1600 Hz.
_SPACINGS_HZ = (1580.0, 1600.0, 1620.0, 1640.0)
_CARRIER_SEARCH_HZ = 2500.0  # tone pair search range around the located carrier
_TONE_SMOOTH_HZ = 60.0
_TONE_OFFSETS_HZ = (-50.0, 0.0, 50.0)
_DRIFTS_HZ_S = (-100.0, -50.0, 0.0, 50.0, 100.0)  # the carrier moves 30-100 Hz/s
_PHASES = 20  # symbol timing steps
_MAX_SYNC_ERRORS = 1
# The bursts are short (0.5-1.5 s) and weak: locate them with a short sliding mean and a lower
# threshold than the 200 baud bursts (the three real bursts measured 4.6-5.9 dB, the next
# strongest noise band 3.5 dB, in a 0.6 s mean).
_BURST_WINDOW_S = 0.6
_MIN_SNR_DB = 2.0
# A candidate whose first, plain demodulation shows no sync word at all (<= this many errors,
# at any timing and polarity) is noise: skip the brute-force search over tone offset and drift.
_QUICK_SYNC_ERRORS = 3
_FLIP_POOL = 16
_MAX_FLIPS = 3
_REPAIR_CANDIDATES = 40
_TRAINING_BITS = 48  # the alternating bits expected just before the sync word
_TRAINING_MIN = 0.8
_MAX_PACKET_BITS = 8 * (1 + 123)  # size byte + the longest packet (type 9)
_SYNC = np.array([int(c) for c in SYNC_BITS], dtype=np.int8)


def _lengths_for(packet_type: int) -> list[int]:
    """Packet lengths (CRC included) this type can have: HADES-R's and HADES-SA's."""
    found = {HADES_R_LENGTHS.get(packet_type), HADES_SA_LENGTHS.get(packet_type)}
    return sorted(n for n in found if n is not None)


def _bits_to_bytes(bits: np.ndarray) -> bytes:
    count = len(bits) // 8
    return bytes(np.packbits(bits[: count * 8].astype(np.uint8)).tobytes())


def _verified(data: bytes, address: int) -> bytes | None:
    """The descrambled payload if *data* (size byte first) holds a packet with a valid CRC."""
    if len(data) < 4 or data[1] & 15 != address:
        return None
    for length in _lengths_for(data[1] >> 4):
        body = data[1 : 1 + length]
        if len(body) == length and crc16_ccitt_false(body) == 0:
            return descramble(body[:-2])
    return None


def _repair(bits: np.ndarray, reliability: np.ndarray, address: int) -> tuple[bytes, int] | None:
    """Flip up to ``_MAX_FLIPS`` of the least reliable bits until a packet's CRC is valid."""
    first = _bits_to_bytes(bits[:16])
    if len(first) < 2 or first[1] & 15 != address:
        return None
    for length in _lengths_for(first[1] >> 4):
        count = 8 * (1 + length)
        if len(bits) < count:
            continue
        pool = [int(i) for i in np.argsort(reliability[:count])[:_FLIP_POOL]]
        for flips in range(1, _MAX_FLIPS + 1):
            for combo in itertools.combinations(pool, flips):
                trial = bits[:count].copy()
                trial[list(combo)] ^= 1
                data = _bits_to_bytes(trial)
                body = data[1 : 1 + length]
                if crc16_ccitt_false(body) == 0:
                    return descramble(body[:-2]), flips
    return None


class HadesLFskStream(HadesFskStream):
    """Feed I/Q chunks, get the packets of every HADES-L burst (800 baud 2FSK) in them."""

    def __init__(
        self,
        sample_rate: float,
        search_hz: float = 20_000.0,
        allow_repair: bool = True,
    ) -> None:
        super().__init__(
            sample_rate,
            search_hz,
            address=HADES_L_ADDRESS,
            allow_repair=allow_repair,
            baud=BAUD_L,
        )
        self._burst_window_s = _BURST_WINDOW_S
        self._min_snr_db = _MIN_SNR_DB

    # -- tone pair ------------------------------------------------------------------- #

    def _tone_pair(self, seg: np.ndarray, carrier_hz: float) -> tuple[float, float] | None:
        """The two tones ``(low, high)`` in Hz: the pair 1.6 kHz apart with the most energy."""
        rate = self._rate
        window = np.hanning(len(seg))
        power = np.abs(np.fft.fftshift(np.fft.fft(seg * window))) ** 2
        freqs = np.fft.fftshift(np.fft.fftfreq(len(seg), 1 / rate))
        width = max(1, int(round(_TONE_SMOOTH_HZ / (rate / len(seg)))))
        smooth = np.convolve(power, np.ones(width) / width, mode="same")
        grid = np.arange(carrier_hz - _CARRIER_SEARCH_HZ, carrier_hz + _CARRIER_SEARCH_HZ, 20.0)
        best: tuple[float, float, float] | None = None
        for spacing in _SPACINGS_HZ:
            score = np.interp(grid, freqs, smooth) + np.interp(grid + spacing, freqs, smooth)
            k = int(np.argmax(score))
            if best is None or score[k] > best[0]:
                best = (float(score[k]), float(grid[k]), spacing)
        if best is None:
            return None
        return best[1], best[1] + best[2]

    def _energies(self, seg: np.ndarray, tone_hz: float, drift_hz_s: float) -> np.ndarray:
        """Energy of the signal at *tone_hz* (drifting) over a sliding one-symbol window."""
        rate = self._rate
        t = np.arange(len(seg)) / rate
        phase = 2 * np.pi * (tone_hz * t + 0.5 * drift_hz_s * t * t)
        mixed = seg * np.exp(-1j * phase)
        cumulative = np.concatenate([[0], np.cumsum(mixed)])
        window = int(round(rate / BAUD_L))
        return np.abs(cumulative[window:] - cumulative[:-window]) ** 2  # type: ignore[no-any-return]

    # -- one burst --------------------------------------------------------------------- #

    def _decode_burst(
        self, x: np.ndarray, start_s: float, carrier_hz: float
    ) -> tuple[list[tuple[bytes, bool, float]], int]:
        """Packets of the burst near *start_s*: ``(payload, repaired, tone centre)`` and offset."""
        rate = self._rate
        lo = max(0, int((start_s - _DEMOD_BEFORE_S) * rate))
        hi = min(len(x), int((start_s + _DEMOD_AFTER_S) * rate))
        seg = x[lo:hi]
        if len(seg) < int(0.5 * rate):
            return [], lo
        tones = self._tone_pair(seg, carrier_hz)
        if tones is None:
            return [], lo
        symbol = rate / BAUD_L
        verified: dict[bytes, tuple[float, int]] = {}
        candidates: list[tuple[float, np.ndarray, np.ndarray, float, int]] = []
        # The plain demodulation (no tone offset, no drift) comes first: it usually decodes a
        # good burst, and without a sync word in it the candidate is noise.
        combos = [(0.0, 0.0)] + [
            (o, d) for o in _TONE_OFFSETS_HZ for d in _DRIFTS_HZ_S if (o, d) != (0.0, 0.0)
        ]
        for combo_number, (offset_hz, drift) in enumerate(combos):
            hit = False
            e_low = self._energies(seg, tones[0] + offset_hz, drift)
            e_high = self._energies(seg, tones[1] + offset_hz, drift)
            length = min(len(e_low), len(e_high))
            for step in range(_PHASES):
                first = step * symbol / _PHASES
                index = (first + np.arange(int((length - first) / symbol)) * symbol).astype(int)
                index = index[index < length]
                if len(index) < 200:
                    continue
                margin = (e_high[index] - e_low[index]) / (e_high[index] + e_low[index] + 1e-12)
                for polarity in (1.0, -1.0):
                    soft = polarity * margin
                    bits = (soft > 0).astype(np.int8)
                    windows = np.lib.stride_tricks.sliding_window_view(bits, 16)
                    errors = (windows != _SYNC).sum(axis=1)
                    hit = hit or bool((errors <= _QUICK_SYNC_ERRORS).any())
                    for i in np.flatnonzero(errors <= _MAX_SYNC_ERRORS):
                        after = bits[i + 16 : i + 16 + _MAX_PACKET_BITS]
                        payload = _verified(_bits_to_bytes(after), HADES_L_ADDRESS)
                        at = float(first + i * symbol)
                        if payload is not None:
                            verified.setdefault(payload, (at, 0))
                        elif errors[i] == 0 and len(after) >= 8 * 20:
                            # A real sync word follows the alternating training; one that
                            # merely appears inside the data does not.
                            before = bits[max(0, i - _TRAINING_BITS) : i]
                            if len(before) < _TRAINING_BITS // 2:
                                continue
                            alternation = float(np.mean(before[1:] != before[:-1]))
                            if alternation < _TRAINING_MIN:
                                continue
                            reliability = np.abs(soft[i + 16 : i + 16 + _MAX_PACKET_BITS])
                            quality = alternation + float(np.mean(reliability[:200]))
                            candidates.append((quality, after, reliability, at, 0))
            if combo_number == 0 and (verified or not hit):
                break
        centre = (tones[0] + tones[1]) / 2
        out = [(p, False, centre) for p in verified]
        offset = lo + (int(min(a for a, _ in verified.values())) if verified else 0)
        if out or not self._allow_repair:
            return out, offset
        candidates.sort(key=lambda c: -c[0])
        tried: set[bytes] = set()
        for _quality, after, reliability, at, _n in candidates:
            key = after.tobytes()
            if key in tried:  # the same hard decisions from a neighbouring timing step
                continue
            tried.add(key)
            repaired = _repair(after, reliability, HADES_L_ADDRESS)
            if repaired is not None:
                return [(repaired[0], True, centre)], lo + int(at)
            if len(tried) >= _REPAIR_CANDIDATES:
                break
        return [], lo

    def _process(self, x: np.ndarray, final: bool) -> list[HadesFrame]:
        frames: list[HadesFrame] = []
        first = self._first_window
        for start_s, _carrier, snr in self._band_search(x):
            if not final and not ((first or start_s >= 1.0) and start_s < _ACCEPT_END_S):
                continue
            decoded, offset = self._decode_burst(x, start_s, _carrier)
            absolute = self._buf_start + offset
            if any(abs(absolute - r) < _DEDUP_S * self._rate for r in self._recent):
                continue
            if not decoded:
                continue
            self._recent = [*self._recent[-7:], absolute]
            for payload, repaired, centre in decoded:
                sample = absolute * self._factor
                frames.append(
                    HadesFrame(
                        sample_index=sample,
                        time_s=sample / self._input_rate,
                        carrier_hz=centre,
                        snr_db=snr,
                        packet_type=payload[0] >> 4,
                        address=payload[0] & 15,
                        payload=payload,
                        repaired=repaired,
                    )
                )
        return frames
