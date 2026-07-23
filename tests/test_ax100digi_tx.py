"""End-to-end test for comms/ax100digi/tx.py's build_tx_audio(): synthesize
a full outgoing digipeater message and confirm it decodes back correctly
through the Rig + Sound Card RX bridge (audio_bridge.RxAudioBridge)."""

from __future__ import annotations

import pytest

from comms.ax100digi import frame, gmsk_demod, rs_ccsds
from comms.ax100digi.audio_bridge import RxAudioBridge
from comms.ax100digi.csp import split_csp_packet
from comms.ax100digi.message import parse_message
from comms.ax100digi.tx import build_tx_audio

pytestmark = pytest.mark.skipif(
    not rs_ccsds.is_available(),
    reason="reed-solomon-ccsds not installed",
)

_SAMPLE_RATE = 48_000.0


def _decode_audio(audio) -> list[bytes]:
    bridge = RxAudioBridge(sample_rate=_SAMPLE_RATE)
    iq = bridge.process(audio)
    demod = gmsk_demod.GmskDiscriminator(input_rate=_SAMPLE_RATE, baud=gmsk_demod.DEFAULT_BAUD)
    discrim = demod.process(iq)
    oversampled = gmsk_demod.resample_to_sps(discrim, _SAMPLE_RATE, gmsk_demod.DEFAULT_BAUD)
    candidates = gmsk_demod.slice_bits_all_phases(oversampled)
    payloads = []
    for bits in candidates:
        payloads.extend(f.payload for f in frame.find_frames(bits))
    return payloads


def test_build_tx_audio_decodes_back_to_original_message() -> None:
    result = build_tx_audio(
        my_call="JF9SOM",
        dest_call="VA7UVS",
        sat_name="MARMOTSat",
        content="hello from FBSAT59",
        store_seconds=5,
        sample_rate=_SAMPLE_RATE,
    )

    payloads = _decode_audio(result.audio)
    assert result.payload in payloads

    _header, body = split_csp_packet(result.payload)
    parsed = parse_message(body.decode("ascii"))
    assert parsed is not None
    assert parsed.source == "JF9SOM"
    assert parsed.dest == "VA7UVS"
    assert parsed.sat_name == "MARMOTSat"
    assert parsed.store_seconds == 5
    assert parsed.content == "hello from FBSAT59"


def test_build_tx_audio_immediate_relay_default() -> None:
    result = build_tx_audio("JF9SOM", "VA7UVS", "MARMOTSat", "immediate relay")
    _header, body = split_csp_packet(result.payload)
    parsed = parse_message(body.decode("ascii"))
    assert parsed is not None
    assert parsed.store_seconds == 0
