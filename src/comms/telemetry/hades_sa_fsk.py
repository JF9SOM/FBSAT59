"""HADES-SA (SpinnyONE) telemetry from the raw 800 baud 2FSK downlink.

HADES-SA sends its telemetry in the same AMSAT-EA "Genesis" framing as UNNE-1B and HADES-R
(training, sync ``BF 35``, type/address, scrambled data, CRC-16/CCITT-FALSE), but at 800 baud
with the two tones 1600 Hz apart and a ``size`` byte between the sync word and the
type/address byte (AMSAT-EA "HADES-SA SpinnyONE Transmissions Description" V1.0). The packets
of one 20 s slot of its transmit cycle (status, temperature, power, ...) are short and
follow each other with gaps; SSDV, CODEC2 voice and PN9 packets share the stream and are
ignored here (the SSTV/SSDV and Digital Voice tabs handle them).

The 2FSK front end is the one of the SSDV mode (comms.sstv.fsk_ssdv: decimation to
12.5 kHz, tone-pair search, per-symbol tone energies, per-packet re-timing). The output is
the same :class:`~comms.telemetry.hades_fsk.HadesFrame` the 200 baud stream gives, so the
Telemetry tab handles both alike. Needs scipy for the decimation (an app dependency of the
SDR features).
"""

from __future__ import annotations

import numpy as np

from comms.sstv import fsk_ssdv as fsk
from comms.telemetry.hades_fsk import (
    HADES_SA_ADDRESS,
    HADES_SA_LENGTHS,
    HadesFrame,
    parse_packet,
)

# size byte + the longest telemetry packet (type 9: 123 bytes) after the sync word
_MAX_BODY = 1 + max(HADES_SA_LENGTHS.values())
_NEED_BITS = 16 + 8 * _MAX_BODY
_MAX_SYNC_ERRORS = 1
_DEDUP_S = 0.5
# A burst is short against the 6 s window, so its tones are weaker in the window's spectrum
# than in the continuous SSDV stream: accept a lower line-to-noise ratio than the SSDV mode.
_TONE_RATIO = 3.0


class HadesSaFskStream:
    """Feed I/Q chunks, get the telemetry packets of HADES-SA in them."""

    def __init__(
        self, sample_rate: float, address: int = HADES_SA_ADDRESS, allow_repair: bool = True
    ) -> None:
        self._rate = float(sample_rate)
        self._address = address
        self._allow_repair = allow_repair
        self.reset()

    def reset(self) -> None:
        """Forget all buffered samples."""
        self._decimator = fsk.Decimator(self._rate)
        self._buf = np.zeros(0, dtype=np.complex64)
        self._buf_start_s = 0.0  # stream time of self._buf[0]
        self._seen: list[tuple[float, bytes]] = []  # (time, payload) of reported packets

    def feed(self, iq: np.ndarray) -> list[HadesFrame]:
        """Add a chunk of complex I/Q; return the packets completed by it."""
        if not fsk._SCIPY_AVAILABLE:
            return []
        y = self._decimator.push(iq)
        if len(y):
            self._buf = np.concatenate([self._buf, y])
        out: list[HadesFrame] = []
        while len(self._buf) >= fsk._WINDOW:
            out += self._process(self._buf[: fsk._WINDOW])
            self._buf = self._buf[fsk._STEP :]
            self._buf_start_s += fsk._STEP / fsk.WORK_RATE
        return out

    def flush(self) -> list[HadesFrame]:
        """Process what is left of a finite recording."""
        frames: list[HadesFrame] = []
        if len(self._buf) >= fsk.WORK_RATE:
            frames = self._process(self._buf, last=True)
        self._buf = np.zeros(0, dtype=np.complex64)
        return frames

    def _process(self, seg: np.ndarray, last: bool = False) -> list[HadesFrame]:
        tones = fsk.find_tones(seg, ratio=_TONE_RATIO)
        if tones is None:
            return []
        c_high, c_low = fsk.tone_sums(seg, tones[0], tones[1])
        phase = fsk.best_phase(c_high, c_low)
        n = len(c_high) - 1
        count = int((n - phase) / fsk.SAMPLES_PER_SYMBOL) - 1
        e_high, e_low = fsk.symbol_energies(c_high, c_low, phase, count)
        margin = e_high - e_low
        noise = float(np.mean(np.minimum(e_high, e_low))) or 1e-12
        candidates: list[tuple[int, int, float]] = []
        for polarity in (-1.0, 1.0):  # bit 1 is the low tone
            bits = ((polarity * margin) > 0).astype(np.uint8)
            candidates += [(i, e, polarity) for i, e in fsk.sync_candidates(bits, _NEED_BITS)]
        candidates.sort(key=lambda c: (c[1], c[0]))
        kept: list[tuple[int, int, float]] = []
        for cand in candidates:
            if all(abs(cand[0] - k[0]) >= 40 for k in kept):
                kept.append(cand)
        kept.sort(key=lambda c: c[0])
        centre = (tones[0] + tones[1]) / 2
        frames: list[HadesFrame] = []
        for index, _errors, polarity in kept:
            start = phase + index * fsk.SAMPLES_PER_SYMBOL
            eh, el = fsk.refine_packet(c_high, c_low, start, count=min(_NEED_BITS, count - index))
            bits = ((polarity * (eh - el)) > 0).astype(np.uint8)
            if (
                len(bits) < 16 + 16
                or np.count_nonzero(bits[:16] != fsk.SYNC_BITS) > _MAX_SYNC_ERRORS
            ):
                continue
            raw = np.packbits(bits[16:]).tobytes()
            if len(raw) < 2 or raw[0] != HADES_SA_LENGTHS.get(raw[1] >> 4, -1):
                continue
            exact = np.count_nonzero(bits[:16] != fsk.SYNC_BITS) == 0
            result = parse_packet(raw[1:], self._address, self._allow_repair and exact)
            if result is None:
                continue
            payload, repaired = result
            when = self._buf_start_s + start / fsk.WORK_RATE
            if any(abs(when - t) < _DEDUP_S and payload == p for t, p in self._seen[-40:]):
                continue
            self._seen.append((when, payload))
            snr = 10 * np.log10(
                max(float(np.sum(np.maximum(eh, el)) / max(len(eh), 1)) / noise, 1.0)
            )
            frames.append(
                HadesFrame(
                    sample_index=int(when * self._rate),
                    time_s=when,
                    carrier_hz=float(centre),
                    snr_db=float(snr),
                    packet_type=payload[0] >> 4,
                    address=payload[0] & 15,
                    payload=payload,
                    repaired=repaired,
                )
            )
        return frames
