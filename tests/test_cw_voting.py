"""Majority vote over overlapping CW decode windows."""

from __future__ import annotations

from comms.cw.voting import WindowVoter

TEXT = "JS1YRU 81 7E 7B A4 81 1C"


def _timed(text: str, t0: float = 1.0, step: float = 0.5) -> list[tuple[str, float]]:
    return [(c, t0 + i * step) for i, c in enumerate(text) if c != " "]


def _windows(
    truth: list[tuple[str, float]], starts: list[float], length: float = 20.0
) -> list[tuple[float, float, list[tuple[str, float]]]]:
    """The truth as each window (start, length, readings relative to its start) sees it."""
    return [(s, length, [(c, t - s) for c, t in truth if s <= t <= s + length]) for s in starts]


def _run(windows: list[tuple[float, float, list[tuple[str, float]]]]) -> str:
    voter = WindowVoter()
    out: list[tuple[str, float]] = []
    for start, length, offsets in windows:
        chars, _until = voter.add_window(start, length, offsets)
        out += chars
    chars, _until = voter.flush()
    out += chars
    return "".join(c for c, _t in sorted(out, key=lambda x: x[1]))


def test_clean_readings_come_out_unchanged_and_in_order() -> None:
    truth = _timed(TEXT)
    assert _run(_windows(truth, [0.0, 5.0, 10.0])) == TEXT.replace(" ", "")


def test_a_wrong_reading_in_one_window_is_outvoted() -> None:
    truth = _timed(TEXT)
    windows = _windows(truth, [0.0, 3.0, 6.0, 9.0])
    # window 2 misreads the character at t = 8.0 ('A' -> 'R')
    start, length, offsets = windows[2]
    windows[2] = (
        start,
        length,
        [("R" if abs(start + t - 8.0) < 0.01 else c, t) for c, t in offsets],
    )
    assert _run(windows) == TEXT.replace(" ", "")


def test_an_inserted_character_is_outvoted() -> None:
    truth = _timed(TEXT)
    windows = _windows(truth, [0.0, 3.0, 6.0, 9.0])
    start, length, offsets = windows[2]
    windows[2] = (start, length, sorted(offsets + [("S", 8.25 - start)], key=lambda x: x[1]))
    assert _run(windows) == TEXT.replace(" ", "")


def test_a_dropped_character_is_restored() -> None:
    truth = _timed(TEXT)
    windows = _windows(truth, [0.0, 3.0, 6.0, 9.0])
    start, length, offsets = windows[1]
    windows[1] = (start, length, [(c, t) for c, t in offsets if abs(start + t - 6.0) > 0.01])
    assert _run(windows) == TEXT.replace(" ", "")


def test_readings_at_a_window_edge_do_not_vote() -> None:
    truth = _timed("ABCDEFGHIJKLMNOPQRST", t0=0.5, step=1.0)
    windows = _windows(truth, [0.0, 4.0, 8.0], length=14.0)
    # every window's last reading (right at its edge) is garbage
    garbled = [
        (s, ln, [(("#" if i == len(o) - 1 else c), t) for i, (c, t) in enumerate(o)])
        for s, ln, o in windows
    ]
    assert "#" not in _run(garbled)


def test_characters_are_released_only_when_no_later_window_can_cover_them() -> None:
    voter = WindowVoter()
    truth = _timed(TEXT)
    got: list[tuple[str, float]] = []
    for start in (0.0, 5.0):
        chars, until = voter.add_window(
            start, 20.0, [(c, t - start) for c, t in truth if start <= t <= start + 20.0]
        )
        got += chars
    # the window at 5.0 releases the audio before 5.0 + 3.0 only
    assert until == 8.0
    assert all(t < 8.0 for _c, t in got)
    assert len(got) < len(TEXT.replace(" ", ""))


def test_reset_forgets_everything() -> None:
    voter = WindowVoter()
    voter.add_window(0.0, 20.0, [("A", 5.0)])
    voter.add_window(5.0, 20.0, [("B", 5.0)])
    assert voter.final_until == 8.0
    voter.reset()
    assert voter.final_until == 0.0
    assert voter.flush()[0] == []
