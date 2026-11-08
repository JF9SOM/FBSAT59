"""Tests for the Iridium ACARS message parser and subprocess chain (stubbed stages)."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from comms.iridium.acars import IridiumAcarsMessage, parse_acars_json_line
from comms.iridium.pipeline import IridiumAcarsPipeline, build_stage_commands

_ACARS_LINE = json.dumps(
    {
        "app": {"name": "iridium-toolkit", "version": "0.0.2"},
        "timestamp": "2026-11-08T12:00:00Z",
        "link_direction": "downlink",
        "acars": {
            "err": False,
            "crc_ok": True,
            "more": False,
            "reg": ".JA123A",
            "mode": "2",
            "label": "H1",
            "blk_id": "3",
            "ack": "!",
            "flight": "NH0012",
            "msg_text": "POSITION REPORT\r\nN35E139",
        },
    }
)


def test_parse_acars_line() -> None:
    msg = parse_acars_json_line(_ACARS_LINE)
    assert msg is not None
    assert msg.registration == "JA123A"
    assert msg.flight == "NH0012"
    assert msg.label == "H1"
    assert msg.direction == "downlink"
    assert msg.crc_ok is True
    assert "POSITION REPORT" in msg.summary()


def test_parse_rejects_non_acars() -> None:
    assert parse_acars_json_line("") is None
    assert parse_acars_json_line("not json") is None
    assert parse_acars_json_line("{broken") is None
    assert parse_acars_json_line("[1, 2]") is None
    # JSON without an "acars" member (e.g. any other traffic) must be dropped.
    assert parse_acars_json_line(json.dumps({"timestamp": "x", "sms": {"text": "hi"}})) is None


def test_build_stage_commands() -> None:
    stages = build_stage_commands(
        ["iridium-extractor"],
        Path("/tk"),
        ["python3"],
        center_hz=1_622_000_000,
        sample_rate_hz=10_000_000,
        station="JF9SOM",
    )
    extractor, parser, reassembler = stages
    assert extractor[:1] == ["iridium-extractor"]
    assert extractor[extractor.index("-f") + 1] == "ci8"
    assert extractor[extractor.index("-r") + 1] == "10000000"
    # Only data frames are forwarded, and only ACARS comes out.
    assert parser[parser.index("--filter") + 1] == "IDA"
    assert "libacars" in reassembler
    assert reassembler[-2:] == ["--station", "JF9SOM"]


def _stub(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


def test_pipeline_end_to_end_with_stubs() -> None:
    passthrough = "import sys\nfor l in sys.stdin: sys.stdout.write(l)"
    # The final stage emits an ACARS line and a non-ACARS line for every input line.
    final = (
        "import sys\n"
        "for l in sys.stdin:\n"
        f"    print({_ACARS_LINE!r})\n"
        '    print(\'{"sms": {"text": "private"}}\')\n'
    )
    got: list[str] = []
    done = threading.Event()

    def on_message(m: IridiumAcarsMessage) -> None:
        got.append(m.registration)
        done.set()

    pipe = IridiumAcarsPipeline(
        [_stub(passthrough), _stub(passthrough), _stub(final)], on_message=on_message
    )
    pipe.start()
    assert pipe.write_iq(b"frame\n")
    assert done.wait(10)
    pipe.stop()
    assert got == ["JA123A"]
    assert not pipe.write_iq(b"x")


def test_stop_without_start_is_safe() -> None:
    pipe = IridiumAcarsPipeline([["a"], ["b"], ["c"]], on_message=lambda m: None)
    pipe.stop()
    assert not pipe.running
