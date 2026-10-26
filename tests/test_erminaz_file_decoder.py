"""Fast decode of an ERMINAZ-2 I/Q recording: SSDV packets out of a WAV file."""

from __future__ import annotations

from math import gcd
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("scipy")
pytest.importorskip("reed_solomon_ccsds")

from scipy import signal  # noqa: E402

from comms.ax100digi import rs_ccsds  # noqa: E402
from comms.ax100digi.frame import _bytes_to_bits, _syncword_bits  # noqa: E402
from comms.ax100digi.randomizer import ccsds_xor_sequence  # noqa: E402
from comms.erminaz.rx import ErminazFileDecoder  # noqa: E402
from comms.erminaz.spp import TmReassembler, ssdv_packets  # noqa: E402
from comms.erminaz.tx import ASM  # noqa: E402
from comms.foresail.tx import AUDIO_RATE, gaussian_nrz_audio  # noqa: E402
from tests.test_erminaz_spp import _frame, _space_packet  # noqa: E402
from tests.test_iq_wav_fast_decode import _write_wav  # noqa: E402
from tests.test_ssdv import make_packet  # noqa: E402

FS = 250_000


def _frame_audio(tm: bytes) -> np.ndarray:
    codeword = ccsds_xor_sequence(rs_ccsds.encode_shortened(tm, dual_basis=False))
    pre = np.tile(np.array([0, 1], dtype=np.uint8), 64)
    bits = np.concatenate([pre, _syncword_bits(ASM), _bytes_to_bits(codeword), pre[:24]])
    return gaussian_nrz_audio(bits.astype(np.uint8))


def test_file_decoder_recovers_ssdv_packets(tmp_path: Path) -> None:
    packets = [make_packet(nofec=True, image=4, pid=n) for n in range(3)]
    audio: list[np.ndarray] = [np.zeros(AUDIO_RATE, dtype=np.float32)]
    for packet in packets:
        sp = _space_packet(500, packet)
        for tm in (_frame(sp[:213], 0), _frame(sp[213:], len(sp) - 213)):
            audio += [_frame_audio(tm), np.zeros(AUDIO_RATE // 4, dtype=np.float32)]
    audio.append(np.zeros(AUDIO_RATE, dtype=np.float32))
    baseband = np.concatenate(audio).astype(np.float32)
    g = gcd(FS, AUDIO_RATE)
    up = signal.resample_poly(baseband, FS // g, AUDIO_RATE // g)
    iq = np.exp(1j * 2 * np.pi * np.cumsum(up * 2400.0) / FS).astype(np.complex64)
    path = tmp_path / "rec.iq.wav"
    _write_wav(path, iq, rate=FS)

    frames: list[bytes] = []
    done: list[tuple[float, float, int]] = []
    job = ErminazFileDecoder(path)
    job.frame_found.connect(frames.append)
    job.decoded.connect(lambda dur, el, n: done.append((dur, el, n)))
    job.run()  # in this thread: the signals are delivered at once

    assert done and done[0][2] == len(frames) == 6
    assembler = TmReassembler()
    found = [p for frame in frames for p in ssdv_packets(assembler.feed(frame))]
    assert found == packets


def test_file_decoder_reports_a_bad_file(tmp_path: Path) -> None:
    bad = tmp_path / "x.wav"
    bad.write_bytes(b"not a wav")
    errors: list[str] = []
    job = ErminazFileDecoder(bad)
    job.failed.connect(errors.append)
    job.run()
    assert errors
