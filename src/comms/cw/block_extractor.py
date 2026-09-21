"""Group decoded CW characters into blocks separated by silence.

A CW telemetry beacon sends one item at a time (an ID text, a hex housekeeping
frame, ...) with a long pause after each. The CW Decoder tab feeds every newly
*confirmed* character here together with its time, and gets back the finished
blocks; whether a block is a valid frame is decided elsewhere
(comms.telemetry.cw_frames), this class only cuts the character stream.

Two clocks are carried per character: ``audio_t`` (monotonic seconds of audio
since the decoder was started -- used for the gap logic, so a jump in the wall
clock or in the replay position can not glue two items together) and ``utc``
(the time the character was received, live clock or recording start time plus
playback position -- what gets reported).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

# A pause longer than this ends a block. Letter gaps inside a beacon frame are
# a fraction of a second; the pause between two beacon items is tens of seconds.
DEFAULT_GAP_S = 3.0
# Blocks with fewer characters than this are noise, not beacon items.
DEFAULT_MIN_CHARS = 8


@dataclass(frozen=True)
class CwBlock:
    """One finished block of decoded characters."""

    text: str
    start_utc: datetime
    end_utc: datetime


class CwBlockExtractor:
    """Cuts a stream of timed characters into blocks at long pauses."""

    def __init__(self, gap_s: float = DEFAULT_GAP_S, min_chars: int = DEFAULT_MIN_CHARS) -> None:
        self._gap_s = gap_s
        self._min_chars = min_chars
        self._chars: list[tuple[str, datetime]] = []
        self._last_audio_t = 0.0

    def reset(self) -> None:
        """Forget any unfinished block."""
        self._chars = []
        self._last_audio_t = 0.0

    def feed(
        self,
        chars: Sequence[tuple[str, float, datetime]],
        confirmed_until: float,
    ) -> list[CwBlock]:
        """Add newly confirmed ``(char, audio_t, utc)`` triples; return finished blocks.

        *confirmed_until* is how far (audio seconds) the decoder's output is
        final: a block whose last character lies more than the gap behind it
        is complete even though no further character has arrived.
        """
        done: list[CwBlock] = []
        for ch, audio_t, utc in chars:
            if ch.isspace():
                continue
            if self._chars and audio_t - self._last_audio_t > self._gap_s:
                done.extend(self._close())
            self._chars.append((ch, utc))
            self._last_audio_t = audio_t
        if self._chars and confirmed_until - self._last_audio_t > self._gap_s:
            done.extend(self._close())
        return done

    def flush(self) -> list[CwBlock]:
        """Finish the block in progress (the decoder was stopped)."""
        return self._close()

    def _close(self) -> list[CwBlock]:
        chars, self._chars = self._chars, []
        if len(chars) < self._min_chars:
            return []
        return [
            CwBlock(
                text="".join(c for c, _ in chars).upper(),
                start_utc=chars[0][1],
                end_utc=chars[-1][1],
            )
        ]
