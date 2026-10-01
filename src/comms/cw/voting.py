"""Majority vote over the overlapping decode windows of the CW Decoder.

The decoder reads the last 20 s of audio every 5 s, so every character is read
three or four times. Confirming it from the first reading alone (what the
transcript does) makes the result depend on how the windows happen to line up
with the signal: a strong OrigamiSat-2 pass (2026-10-01) read its first frame
correctly for 6 of 7 window alignments, and one wrong or inserted character
ruins a telemetry frame, since CW has no error detection.

Here every reading is kept and the characters are decided together:

* A reading near either edge of its window has little audio on one side and is
  the least reliable (the model cuts, or invents, characters there), so only the
  middle of each window votes (``edge_s`` from each edge). The first windows of a
  session have no earlier audio, so their left edge is real and votes too.
* Readings of the same character land within a few hundredths of a second of each
  other from window to window, so those within ``tol_s`` are one character.
* Whether the character exists is decided by the share of the covering windows
  that read something there; which character it is, by the most common reading
  (the one furthest from its window's edges breaks a tie). That repairs a wrong,
  an inserted and a dropped character alike.
* A stretch of audio is final once no later window can still cover it.

Pure logic, no Qt: see :class:`WindowVoter`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Seconds at each end of a window whose readings do not vote.
DEFAULT_EDGE_S = 3.0
# Readings of one character from different windows are within this many seconds.
DEFAULT_TOLERANCE_S = 0.12


@dataclass
class _Vote:
    t: float
    ch: str
    window: int
    margin: float  # seconds to the nearer edge of its window


@dataclass
class _Window:
    ident: int
    lo: float  # the part of the window that votes
    hi: float


@dataclass
class _Cluster:
    votes: list[_Vote] = field(default_factory=list)

    @property
    def mean_t(self) -> float:
        return sum(v.t for v in self.votes) / len(self.votes)


class WindowVoter:
    """Collects the windows' readings and releases finished characters.

    Times are audio seconds since the decoder was started (the same clock for
    every window).
    """

    def __init__(
        self, edge_s: float = DEFAULT_EDGE_S, tolerance_s: float = DEFAULT_TOLERANCE_S
    ) -> None:
        self._edge_s = edge_s
        self._tol_s = tolerance_s
        self.reset()

    def reset(self) -> None:
        """Forget everything (the audio buffer was discarded or the decoder restarted)."""
        self._votes: list[_Vote] = []
        self._windows: list[_Window] = []
        self._next_id = 0
        self._final_until = 0.0

    @property
    def final_until(self) -> float:
        """Audio time up to which every character has been released."""
        return self._final_until

    def add_window(
        self,
        start: float,
        duration: float,
        offsets: list[tuple[str, float]],
    ) -> tuple[list[tuple[str, float]], float]:
        """Add one decode window; return ``(finished characters, final_until)``.

        *start* is the audio time of the window's first sample, *duration* its
        length, *offsets* the model's ``(character, seconds from the window start)``.
        """
        at_start = start <= 1e-6  # the buffer still begins where the audio began
        lo = start + (0.0 if at_start else self._edge_s)
        hi = start + duration - self._edge_s
        window = _Window(self._next_id, lo, hi)
        self._next_id += 1
        if hi > lo:
            self._windows.append(window)
            for ch, rel in offsets:
                t = start + rel
                if lo <= t <= hi:
                    margin = min(t - start, start + duration - t)
                    self._votes.append(_Vote(t, ch, window.ident, margin))
        # While windows still begin at the very start of the audio, a later window
        # covers the same stretch with more context: nothing is final yet.
        if at_start:
            return [], self._final_until
        boundary = max(self._final_until, start + self._edge_s)
        return self._release(boundary), self._final_until

    def flush(self) -> tuple[list[tuple[str, float]], float]:
        """Release everything still held (the decoder was stopped)."""
        boundary = max([v.t for v in self._votes], default=self._final_until) + 1.0
        return self._release(boundary), self._final_until

    def _release(self, boundary: float) -> list[tuple[str, float]]:
        ready = sorted((v for v in self._votes if v.t < boundary), key=lambda v: v.t)
        self._votes = [v for v in self._votes if v.t >= boundary]
        self._final_until = max(self._final_until, boundary)
        decided = [self._decide(c) for c in self._clusters(ready)]
        # Only now: a window that ends before the boundary can no longer cover anything new.
        self._windows = [w for w in self._windows if w.hi >= boundary]
        return [d for d in decided if d is not None]

    def _clusters(self, votes: list[_Vote]) -> list[_Cluster]:
        clusters: list[_Cluster] = []
        for vote in votes:
            current = clusters[-1] if clusters else None
            if (
                current is not None
                and vote.t - current.mean_t <= self._tol_s
                and all(v.window != vote.window for v in current.votes)
            ):
                current.votes.append(vote)
            else:
                clusters.append(_Cluster([vote]))
        return clusters

    def _coverage(self, t: float, extra: set[int]) -> int:
        """How many windows had *t* in their voting part."""
        return len({w.ident for w in self._windows if w.lo <= t <= w.hi} | extra)

    def _decide(self, cluster: _Cluster) -> tuple[str, float] | None:
        t = cluster.mean_t
        covering = self._coverage(t, {v.window for v in cluster.votes})
        if len(cluster.votes) * 2 < covering:
            return None  # most windows that heard this stretch read nothing here
        counts: dict[str, int] = {}
        best_margin: dict[str, float] = {}
        for v in cluster.votes:
            counts[v.ch] = counts.get(v.ch, 0) + 1
            best_margin[v.ch] = max(best_margin.get(v.ch, 0.0), v.margin)
        ch = max(counts, key=lambda c: (counts[c], best_margin[c]))
        return ch, t
