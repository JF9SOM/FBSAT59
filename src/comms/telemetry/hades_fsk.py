"""Telemetry from the AMSAT-EA "Genesis"-family 2FSK downlink at 200 baud.

Why this exists
---------------
UNNE-1B (NORAD 98205, 436.888 MHz, SatNOGS "Mode U - 200bps FSK TLM") sends bare 2FSK
bursts, about 1.6-2.5 s long every 30 s. The framing is the same one HADES-SA and
HADES-R use (training ``AA..``, sync ``BF35``, type/address nibbles, data, CRC-16), so
neither Direwolf (AX.25) nor a CCSDS demodulator can see it.

Frame format (checked on a real UNNE-1B pass on 2026-10-18, five frames CRC-verified)
------------------------------------------------------------------------------------
* 200 baud 2FSK, tone shift about 1.7 kHz. After the 128-bit ``AA..`` training the sync
  word ``BF 35`` follows.
* Then ``type<<4 | address`` (1 byte, address 12 on UNNE-1B), the data, and a CRC-16
  (CRC-CCITT-FALSE, big endian) over everything after the sync word. The length is fixed
  per type (:data:`HADES_R_LENGTHS`, counting the CRC).
* Every byte after the first one is scrambled with the 1+x^12+x^17 multiplicative
  scrambler of AMSAT-EA's reference code, including its quirks (see :func:`descramble`).
  The CRC is computed over the *scrambled* bytes. This is gr-satellites' ``HADES-R``
  deframer, reimplemented here so it can run on the app's own SDR stream.
* The bytes 1-4 of types 1, 3, 4, 5 and 10 are a little-endian seconds counter that steps
  by exactly 30 s per burst, which is a useful extra sanity check of a decoded frame.

Why a carrier search
--------------------
The TLE of a freshly launched satellite can be far off (UNNE-1B's was about 80 s of
along-track error, which put the signal 3-11 kHz below the Doppler-corrected centre),
and SatNOGS stations that only demodulate a narrow band around the predicted frequency
miss it. Here the I/Q is wide, so every burst is first *located*: the strongest 3.5 kHz
band within ``search_hz`` of the centre is taken as the carrier, then demodulated.

Processing
----------
The I/Q is decimated to 50-100 kHz, cut into overlapping 10 s windows (5 s step; a burst is
handled by the window whose first part contains its start, so one is never split), the
burst is found by a sliding band-power search, band-passed, FM-discriminated, sliced to
bits and searched for the sync word with at most two bit errors. Only a frame whose CRC
is valid is reported as verified; one more that a single flipped bit repairs is reported
with ``repaired=True`` and must not be treated as verified (do not upload it).

Needs only numpy.
"""

from __future__ import annotations

from typing import NamedTuple

import numpy as np

BAUD = 200
SYNC_BITS = "1011111100110101"  # 0xBF35, MSB first

# Packet length per type, *including* the 2 CRC bytes, from gr-satellites' HADES-R table
# (UNNE-1, HADES-R, MARIA-G, HADES-ICM); types 1, 3, 4, 5 and 10 were confirmed on UNNE-1B.
HADES_R_LENGTHS: dict[int, int] = {
    1: 31,
    2: 17,
    3: 29,
    4: 35,
    5: 27,
    6: 135,
    7: 101,
    8: 31,
    9: 123,
    10: 17,
    11: 9,
    12: 64,
    14: 38,
    15: 41,
}
UNNE1B_ADDRESS = 12
# Types whose bytes 1-4 were observed to be the little-endian uptime counter in seconds.
_COUNTER_TYPES = frozenset({1, 3, 4, 5, 10})

# Packet types named in AMSAT-EA's HADES-SA description (UNNE-1B's types 1, 3, 4 and 5 carry
# the same kind of data; the rest are shown as "Type N").
TYPE_NAMES: dict[int, str] = {
    1: "Power",
    2: "Temperature",
    3: "Status",
    4: "Power range",
    5: "Temperature range",
    8: "Deployment",
    9: "Extended power",
    12: "Orbital elements",
    14: "Time series",
    15: "BBS data",
}

_TARGET_RATE = 50_000.0  # decimated rate is the first integer division at or above this
_STFT_N = 512
_STFT_HOP_S = 0.01
_BAND_HZ = 3500.0
_BURST_WINDOW_S = 1.4  # sliding mean length used to find a burst
_WINDOW_S = 10.0
_STEP_S = 5.0
_ACCEPT_END_S = 6.5  # a burst located before this (s) in the window is handled by it
_DEMOD_BEFORE_S = 1.2
_DEMOD_AFTER_S = 2.8
_DEMOD_HALF_BW_HZ = 3000.0
_MIN_SNR_DB = 3.0
_MAX_BURSTS_PER_WINDOW = 3
_MAX_SYNC_ERRORS = 2
_DEDUP_S = 3.0
_MIN_BITS = 140  # shorter than this cannot hold a sync word and the shortest packet


class HadesFrame(NamedTuple):
    """One received packet."""

    sample_index: int  # first sample of the burst, counted in input samples since reset()
    time_s: float  # the same, in seconds
    carrier_hz: float  # burst carrier relative to the I/Q centre
    snr_db: float  # band-power SNR of the burst search (rough)
    packet_type: int
    address: int
    payload: bytes  # descrambled, CRC removed, starts with the type/address byte
    repaired: bool  # a single bit flip made the CRC valid: not verified

    @property
    def type_name(self) -> str:
        """Short name of the packet type ("Status", "Type 10", ...)."""
        return TYPE_NAMES.get(self.packet_type, f"Type {self.packet_type}")

    @property
    def uptime_s(self) -> int | None:
        """The satellite's uptime counter, for the types where it was observed."""
        if self.packet_type in _COUNTER_TYPES and len(self.payload) >= 5:
            return int.from_bytes(self.payload[1:5], "little")
        return None


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def descramble(packet: bytes) -> bytes:
    """Undo AMSAT-EA's scrambler (gr-satellites ``hades_descrambler``).

    ``packet`` is the type/address byte plus data, CRC removed. The first byte is not
    scrambled. Each later byte has only its 7 upper bits scrambled, and the 17-bit shift
    register advances only on those: the AMSAT-EA reference code has that quirk and the
    satellites follow it. The shift register starts as 1<<16 (the top bits of the
    reference's 0x2C350000 do not matter for a 17-bit register).
    """
    out = bytearray(packet)
    state = 1 << 16
    for j in range(1, len(out)):
        value = 0
        for k in range(7):
            bit = (out[j] >> (7 - k)) & 1
            clear = bit ^ (((state >> 16) ^ (state >> 11)) & 1)
            value = (value << 1) | clear
            state = ((state << 1) | bit) & 0x1FFFF
        out[j] = (value << 1) | (out[j] & 1)
    return bytes(out)


def scramble(packet: bytes) -> bytes:
    """Inverse of :func:`descramble` (used by the tests to build frames)."""
    out = bytearray(packet)
    state = 1 << 16
    for j in range(1, len(out)):
        value = 0
        for k in range(7):
            bit = (out[j] >> (7 - k)) & 1
            sent = bit ^ (((state >> 16) ^ (state >> 11)) & 1)
            value = (value << 1) | sent
            state = ((state << 1) | sent) & 0x1FFFF
        out[j] = (value << 1) | (out[j] & 1)
    return bytes(out)


def build_packet(payload: bytes) -> bytes:
    """Scramble ``payload`` and append its CRC: the bytes that follow the sync word."""
    scrambled = scramble(payload)
    return scrambled + crc16_ccitt_false(scrambled).to_bytes(2, "big")


def parse_packet(
    raw: bytes,
    address: int | None = UNNE1B_ADDRESS,
    allow_repair: bool = True,
) -> tuple[bytes, bool] | None:
    """Check the bytes following a sync word; return ``(payload, repaired)`` or ``None``.

    ``raw`` may be longer than the packet. The type/address byte is not scrambled, so it
    selects the length. With ``address`` set, a packet for another address is refused.
    When the CRC fails and ``allow_repair`` is set, every single bit flip is tried; the
    packet is only accepted that way if the type/address byte is plausible.
    """
    if not raw:
        return None
    packet_type = raw[0] >> 4
    length = HADES_R_LENGTHS.get(packet_type)
    if length is None or len(raw) < length:
        return None
    if address is not None and (raw[0] & 15) != address:
        return None
    body = bytearray(raw[:length])
    if crc16_ccitt_false(bytes(body)) == 0:
        return descramble(bytes(body[:-2])), False
    if not allow_repair:
        return None
    for bit in range(length * 8):
        body[bit // 8] ^= 0x80 >> (bit % 8)
        if crc16_ccitt_false(bytes(body)) == 0 and body[0] == raw[0]:
            return descramble(bytes(body[:-2])), True
        body[bit // 8] ^= 0x80 >> (bit % 8)
    return None


def _bits_to_bytes(bits: str) -> bytes:
    return bytes(int(bits[i : i + 8], 2) for i in range(0, len(bits) - 7, 8))


def _invert(bits: str) -> str:
    return bits.translate(str.maketrans("01", "10"))


def frames_from_bits(
    bits: str,
    address: int | None = UNNE1B_ADDRESS,
    allow_repair: bool = True,
) -> list[tuple[bytes, bool]]:
    """Find packets in a bit string (either polarity); returns ``(payload, repaired)``."""
    found: list[tuple[bytes, bool]] = []
    for text in (bits, _invert(bits)):
        for start in range(max(0, len(text) - 16 - 8 * 4)):
            errors = sum(a != b for a, b in zip(text[start : start + 16], SYNC_BITS, strict=True))
            if errors > _MAX_SYNC_ERRORS:
                continue
            raw = _bits_to_bytes(text[start + 16 :])
            # A repair needs an exact sync word: it is the weaker check already.
            result = parse_packet(raw, address, allow_repair and errors == 0)
            if result is not None and result not in found:
                found.append(result)
        if found:
            break
    return found


def _design_lowpass(rate: float, cutoff_hz: float, taps: int) -> np.ndarray:
    """Hamming-windowed sinc low-pass FIR (``taps`` odd)."""
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff_hz / rate * n) * 2 * cutoff_hz / rate * np.hamming(taps)
    taps_normalised: np.ndarray = (h / h.sum()).astype(np.float32)
    return taps_normalised


def _split_factor(factor: int) -> list[int]:
    """Split a decimation factor into stages of at most 10 (largest divisors first)."""
    stages: list[int] = []
    remaining = factor
    while remaining > 1:
        stage = next((d for d in range(10, 1, -1) if remaining % d == 0), remaining)
        stages.append(stage)
        remaining //= stage
    return stages


def _decimate_stage(x: np.ndarray, taps: np.ndarray, decim: int) -> tuple[np.ndarray, np.ndarray]:
    """FIR-filter and decimate ``x``; returns ``(output, samples to keep for the next call)``.

    ``taps`` is symmetric (linear phase), so the correlation below equals the convolution.
    Only the kept outputs are computed (one strided multiply-add per tap), which is far
    cheaper than convolving at the full rate when ``decim`` is large.
    """
    count = len(taps)
    if len(x) < count:
        return np.zeros(0, dtype=np.complex64), x
    n_out = (len(x) - count) // decim + 1
    y = np.zeros(n_out, dtype=np.complex64)
    for k in range(count):
        y += taps[k] * x[k : k + (n_out - 1) * decim + 1 : decim]
    return y, x[n_out * decim :]


def _slice_bits(z: np.ndarray, rate: float, baud: float) -> list[str]:
    """Slice an instantaneous-frequency trace into bits, in a few variants.

    The symbol timing comes from the phase of the transitions. Each variant decides a
    symbol either from the single sample at its centre or from the mean over its central
    60 %, and the timing is also nudged by a fraction of a symbol; the caller keeps the
    first variant whose CRC is valid (a CRC-16 plus the sync word keeps false accepts
    negligible). Integrating over the symbol removes most of the isolated errors that the
    noisy discriminator output gives on a weak burst.
    """
    if len(z) < 8:
        return []
    mid = (np.percentile(z, 15) + np.percentile(z, 85)) / 2
    hard = (z > mid).astype(np.int8)
    edges = np.flatnonzero(np.diff(hard))
    if len(edges) == 0:
        return []
    sps = rate / baud
    phase = np.angle(np.sum(np.exp(2j * np.pi * edges / sps))) / (2 * np.pi) * sps
    csum = np.concatenate([[0.0], np.cumsum(z - mid)])
    variants: list[str] = []
    for shift in (0.0, -0.12, 0.12):
        centres = np.arange((phase + shift * sps) % sps + sps / 2, len(z) - 1, sps)
        centres = centres[centres < len(z)]
        point = (z[centres.astype(int)] > mid).astype(int)
        lo = np.clip((centres - 0.3 * sps).astype(int), 0, len(z))
        hi = np.clip((centres + 0.3 * sps).astype(int) + 1, 0, len(z))
        mean = (csum[hi] - csum[lo]) / np.maximum(hi - lo, 1)
        for decision in (mean > 0, point.astype(bool)):
            variants.append("".join("1" if b else "0" for b in decision))
    unique: list[str] = []
    for text in variants:
        if text not in unique:
            unique.append(text)
    return unique


class HadesFskStream:
    """Feed I/Q chunks, get the packets of every 200 baud burst in them.

    ``sample_rate`` is the I/Q rate (any rate of 50 kHz or more, the Doppler-corrected
    stream of the app's SDR pipeline). ``search_hz`` is how far from the centre a burst's
    carrier is looked for. Call :meth:`flush` at the end of a finite recording.
    """

    def __init__(
        self,
        sample_rate: float,
        search_hz: float = 20_000.0,
        address: int | None = UNNE1B_ADDRESS,
        allow_repair: bool = True,
        baud: float = BAUD,
    ) -> None:
        if sample_rate < _TARGET_RATE:
            raise ValueError("sample_rate must be at least 50 kHz")
        self._input_rate = float(sample_rate)
        self._factor = max(1, int(sample_rate // _TARGET_RATE))
        self._rate = self._input_rate / self._factor
        # Cascade of low-pass + decimate stages; the last one sets the final band edge.
        self._stages: list[tuple[int, np.ndarray]] = []
        rate_in = self._input_rate
        stage_factors = _split_factor(self._factor)
        for i, decim in enumerate(stage_factors):
            rate_out = rate_in / decim
            last = i == len(stage_factors) - 1
            taps = (32 if last else 8) * decim + 1
            self._stages.append((decim, _design_lowpass(rate_in, 0.45 * rate_out, taps)))
            rate_in = rate_out
        if search_hz > self._rate / 2 - _BAND_HZ / 2 - 500:
            search_hz = self._rate / 2 - _BAND_HZ / 2 - 500
        self._search_hz = search_hz
        self._address = address
        self._allow_repair = allow_repair
        self._baud = baud
        self.reset()

    def reset(self) -> None:
        """Forget all buffered samples (e.g. after a retune)."""
        self._rests = [np.zeros(0, dtype=np.complex64) for _ in self._stages]
        self._buf = np.zeros(0, dtype=np.complex64)
        self._buf_start = 0  # decimated index of self._buf[0]
        self._recent: list[int] = []  # decimated starts of the bursts already reported
        self._first_window = True

    def _decimate(self, iq: np.ndarray) -> np.ndarray:
        x = iq.astype(np.complex64)
        for i, (decim, taps) in enumerate(self._stages):
            x, self._rests[i] = _decimate_stage(np.concatenate([self._rests[i], x]), taps, decim)
        return x

    def feed(self, iq: np.ndarray) -> list[HadesFrame]:
        """Add a chunk of complex I/Q; return the frames completed by it."""
        self._buf = np.concatenate([self._buf, self._decimate(iq)])
        frames: list[HadesFrame] = []
        window = int(_WINDOW_S * self._rate)
        step = int(_STEP_S * self._rate)
        while len(self._buf) >= window:
            frames += self._process(self._buf[:window], final=False)
            self._buf = self._buf[step:]
            self._buf_start += step
            self._first_window = False
        return frames

    def flush(self) -> list[HadesFrame]:
        """Process what is left of a finite recording."""
        frames = self._process(self._buf, final=True) if len(self._buf) else []
        self._buf = np.zeros(0, dtype=np.complex64)
        return frames

    def _band_search(self, x: np.ndarray) -> list[tuple[float, float, float]]:
        """Bursts in ``x`` as ``(start_s, carrier_hz, snr_db)``, strongest first."""
        rate = self._rate
        hop = max(1, int(round(_STFT_HOP_S * rate)))
        if len(x) < _STFT_N + hop:
            return []
        windows = np.lib.stride_tricks.sliding_window_view(x, _STFT_N)[::hop]
        spectra = np.fft.fftshift(np.fft.fft(windows * np.hanning(_STFT_N), axis=1), axes=1)
        power = (spectra.real**2 + spectra.imag**2).astype(np.float64)
        bins = max(2, int(round(_BAND_HZ / (rate / _STFT_N))))
        cum = np.cumsum(np.pad(power, ((0, 0), (1, 0))), axis=1)
        band = cum[:, bins:] - cum[:, :-bins]  # frames x band start bins
        freqs = np.fft.fftshift(np.fft.fftfreq(_STFT_N, 1 / rate))
        centres = freqs[: band.shape[1]] + (bins - 1) / 2 * (rate / _STFT_N)
        usable = np.abs(centres) <= self._search_hz
        frames_per_window = int(round(_BURST_WINDOW_S / (hop / rate)))
        if band.shape[0] <= frames_per_window or not usable.any():
            return []
        csum = np.cumsum(np.pad(band, ((1, 0), (0, 0))), axis=0)
        mean = (csum[frames_per_window:] - csum[:-frames_per_window]) / frames_per_window
        mean = mean / np.median(mean[:, usable])
        mean[:, ~usable] = 0.0
        found: list[tuple[float, float, float]] = []
        for _ in range(_MAX_BURSTS_PER_WINDOW):
            row, col = np.unravel_index(int(np.argmax(mean)), mean.shape)
            snr = 10 * np.log10(max(mean[row, col], 1e-12))
            if snr < _MIN_SNR_DB:
                break
            found.append((float(row * hop / rate), float(centres[col]), float(snr)))
            lo = max(0, int(row - _WINDOW_S / 2 / (hop / rate) * 0.6))
            hi = int(row + _WINDOW_S / 2 / (hop / rate) * 0.6)
            mean[lo:hi, :] = 0.0
        return found

    def _demodulate(
        self, x: np.ndarray, start_s: float, carrier_hz: float
    ) -> tuple[list[str], int]:
        """Bit variants of the burst near ``start_s``/``carrier_hz`` and its first sample."""
        rate = self._rate
        lo = max(0, int((start_s - _DEMOD_BEFORE_S) * rate))
        hi = min(len(x), int((start_s + _DEMOD_AFTER_S) * rate))
        seg = x[lo:hi]
        if len(seg) < int(0.5 * rate):
            return [], lo
        spectrum = np.fft.fft(seg)
        freqs = np.fft.fftfreq(len(seg), 1 / rate)
        spectrum[np.abs(freqs - carrier_hz) > _DEMOD_HALF_BW_HZ] = 0
        y = np.fft.ifft(spectrum)
        env = np.convolve(np.abs(y) ** 2, np.ones(100) / 100, mode="same")
        active = np.flatnonzero(env > env.max() * 0.2)
        if len(active) < int(0.3 * rate):
            return [], lo
        burst = y[active[0] : active[-1]]
        freq = np.angle(burst[1:] * np.conj(burst[:-1])) * rate / (2 * np.pi)
        freq = np.convolve(freq, np.ones(20) / 20, mode="same")
        decim = max(1, int(round(rate / 10_000)))
        return _slice_bits(freq[::decim], rate / decim, self._baud), lo + int(active[0])

    def _decode_variants(self, variants: list[str]) -> list[tuple[bytes, bool]]:
        """Packets from the first bit variant that holds a verified one.

        Verified packets win over repaired ones across all variants, so a variant that
        needs a bit repair is only used when none of the others is clean.
        """
        repaired: list[tuple[bytes, bool]] = []
        for bits in variants:
            if len(bits) < _MIN_BITS:
                continue
            found = frames_from_bits(bits, self._address, self._allow_repair)
            if any(not rep for _, rep in found):
                return found
            repaired = repaired or found
        return repaired

    def _process(self, x: np.ndarray, final: bool) -> list[HadesFrame]:
        frames: list[HadesFrame] = []
        first = self._first_window
        for start_s, carrier, snr in self._band_search(x):
            if not final and not ((first or start_s >= 1.0) and start_s < _ACCEPT_END_S):
                continue
            variants, offset = self._demodulate(x, start_s, carrier)
            absolute = self._buf_start + offset
            if any(abs(absolute - r) < _DEDUP_S * self._rate for r in self._recent):
                continue
            decoded = self._decode_variants(variants)
            if not decoded:
                continue
            self._recent = [*self._recent[-7:], absolute]
            for payload, repaired in decoded:
                sample = absolute * self._factor
                frames.append(
                    HadesFrame(
                        sample_index=sample,
                        time_s=sample / self._input_rate,
                        carrier_hz=carrier,
                        snr_db=snr,
                        packet_type=payload[0] >> 4,
                        address=payload[0] & 15,
                        payload=payload,
                        repaired=repaired,
                    )
                )
        return frames


def decode_iq(
    iq: np.ndarray,
    sample_rate: float,
    chunk: int = 16384,
    **kwargs: float | int | bool | None,
) -> list[HadesFrame]:
    """Decode a whole I/Q array at once (a convenience for tests and offline analysis)."""
    stream = HadesFskStream(sample_rate, **kwargs)  # type: ignore[arg-type]
    frames: list[HadesFrame] = []
    for i in range(0, len(iq), chunk):
        frames += stream.feed(iq[i : i + chunk])
    return frames + stream.flush()
