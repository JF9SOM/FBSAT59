"""Tests for comms/cw/block_extractor.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from comms.cw.block_extractor import CwBlockExtractor

T0 = datetime(2026, 9, 20, 7, 0, 0, tzinfo=UTC)


def _chars(text: str, start: float, step: float = 0.4) -> list[tuple[str, float, datetime]]:
    """*text* as timed characters, one every *step* seconds from audio time *start*."""
    return [
        (c, start + i * step, T0 + timedelta(seconds=start + i * step)) for i, c in enumerate(text)
    ]


def test_a_block_finishes_once_the_confirmed_audio_is_past_the_gap() -> None:
    ex = CwBlockExtractor()
    chars = _chars("2FFE8594EB880124", 10.0)
    assert ex.feed(chars, confirmed_until=17.0) == []  # still inside the block
    (block,) = ex.feed([], confirmed_until=chars[-1][1] + 3.5)
    assert block.text == "2FFE8594EB880124"
    assert block.start_utc == chars[0][2]
    assert block.end_utc == chars[-1][2]


def test_a_new_character_after_a_long_pause_starts_a_new_block() -> None:
    ex = CwBlockExtractor()
    first = _chars("2FFE8594EB880124", 10.0)
    second = _chars("00D7C2A8D6B8EA", 60.0)
    assert ex.feed(first, confirmed_until=17.0) == []
    (block,) = ex.feed(second, confirmed_until=65.5)
    assert block.text == "2FFE8594EB880124"
    (block2,) = ex.flush()
    assert block2.text == "00D7C2A8D6B8EA"


def test_spaces_are_dropped_and_do_not_extend_the_block() -> None:
    ex = CwBlockExtractor()
    chars = _chars("2FFE 8594", 10.0)
    ex.feed(chars, confirmed_until=12.0)
    (block,) = ex.flush()
    assert block.text == "2FFE8594"


def test_short_bursts_of_noise_are_not_blocks() -> None:
    ex = CwBlockExtractor()
    ex.feed(_chars("EE", 10.0), confirmed_until=30.0)
    assert ex.flush() == []


def test_blocks_are_cut_across_several_feeds() -> None:
    ex = CwBlockExtractor()
    chars = _chars("2FFE8594EB880124", 10.0)
    ex.feed(chars[:6], confirmed_until=12.0)
    ex.feed(chars[6:], confirmed_until=17.0)
    (block,) = ex.feed([], confirmed_until=25.0)
    assert block.text == "2FFE8594EB880124"


def test_a_jump_of_the_utc_clock_does_not_glue_or_split_blocks() -> None:
    """The gap logic uses audio time only, so a replay seek (UTC jumps) inside a
    block does not cut it."""
    ex = CwBlockExtractor()
    a = _chars("2FFE8594", 10.0)
    b = [(c, t, u + timedelta(hours=1)) for c, t, u in _chars("EB880124", 13.2)]
    assert ex.feed(a + b, confirmed_until=17.0) == []  # one block, still open
    (block,) = ex.flush()
    assert block.text == "2FFE8594EB880124"


def test_reset_forgets_the_unfinished_block() -> None:
    ex = CwBlockExtractor()
    ex.feed(_chars("2FFE8594EB880124", 10.0), confirmed_until=12.0)
    ex.reset()
    assert ex.flush() == []
