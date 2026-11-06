"""Direwolf transmit worker: config text and the fallback to the built-in generator."""

from __future__ import annotations

from typing import Any

import pytest

from comms.aprs import direwolf_tx
from comms.aprs.direwolf_tx import DirewolfStartError, DirewolfTxWorker, build_config


def test_build_config_uses_real_devices_and_valid_values() -> None:
    conf = build_config("Mic In", "USB Audio CODEC", "1200", 300, 100, 8850)
    assert 'ADEVICE "Mic In" "USB Audio CODEC"' in conf
    assert "MODEM 1200" in conf
    assert "TXDELAY 30" in conf  # 10 ms units
    assert "TXTAIL 10" in conf
    # Direwolf only accepts KISS ports 1000..10000, SLOTTIME >= 5, and 0 turns 8001 off.
    assert "KISSPORT 0\nKISSPORT 8850" in conf
    assert "SLOTTIME 5" in conf
    assert "stdin" not in conf  # a stdin input never opens the output device


def test_build_config_4800_uses_g3ruh() -> None:
    assert "MODEM 4800 G3RUH" in build_config("a", "b", "4800", 300, 100, 8850)


def test_free_port_is_in_direwolf_range() -> None:
    assert 1000 <= direwolf_tx._free_port() <= 10000


def _worker(rig: Any = None) -> DirewolfTxWorker:
    return DirewolfTxWorker("APRS", 1, rig, frame=b"x", modem="1200", txdelay_ms=300, txtail_ms=100)


def test_start_failure_is_reported_as_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: Any) -> None:
        raise DirewolfStartError("direwolf not found")

    monkeypatch.setattr(DirewolfTxWorker, "_run_direwolf", boom)
    w = _worker()
    errors: list[str] = []
    done: list[bool] = []
    w.error.connect(errors.append)
    w.finished.connect(lambda: done.append(True))
    w.run()
    assert len(errors) == 1 and "direwolf not found" in errors[0]
    assert done == []


def test_estimated_time_grows_with_delays_and_shrinks_with_baud() -> None:
    slow = direwolf_tx._estimate_seconds(30, "1200", 300, 100)
    fast = direwolf_tx._estimate_seconds(30, "9600", 300, 100)
    assert slow > fast > 0.4
    assert direwolf_tx._estimate_seconds(30, "1200", 600, 100) == pytest.approx(slow + 0.3)


def test_config_encoding_is_ansi_on_windows_and_utf8_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import locale

    from comms.aprs import direwolf_tx

    monkeypatch.setattr(direwolf_tx.sys, "platform", "win32")
    monkeypatch.setattr(locale, "getpreferredencoding", lambda do_setlocale=True: "cp932")
    assert direwolf_tx._config_encoding() == "cp932"
    monkeypatch.setattr(direwolf_tx.sys, "platform", "darwin")
    assert direwolf_tx._config_encoding() == "utf-8"


class _FakeSession:
    """Stands in for DirewolfTxSession: scripted failures, call counting."""

    def __init__(self, fail_sends: int = 0, busy: str | None = None) -> None:
        self.fail_sends = fail_sends
        self.busy = busy
        self.opened = 0
        self.closed = 0
        self.sent: list[bytes] = []
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        if self.busy is not None:
            raise direwolf_tx.OutputBusyError(self.busy)
        self.opened += 1
        self._open = True

    def close(self, drain: bool = False) -> None:
        self.closed += 1
        self._open = False

    def send(self, frame: bytes, rig: Any) -> bool:
        if self.fail_sends > 0:
            self.fail_sends -= 1
            raise DirewolfStartError("connection lost")
        self.sent.append(frame)
        return True


def _session_worker(session: Any) -> DirewolfTxWorker:
    return DirewolfTxWorker(
        "APRS",
        1,
        None,
        frame=b"x",
        modem="1200",
        txdelay_ms=300,
        txtail_ms=100,
        session=session,
    )


def test_kept_open_session_is_used_and_left_open() -> None:
    session = _FakeSession()
    w = _session_worker(session)
    done: list[bool] = []
    w.finished.connect(lambda: done.append(True))
    w.run()
    assert done == [True]
    assert session.sent == [b"x"] and session.opened == 1 and session.closed == 0


def test_kept_open_session_that_fails_is_restarted_once() -> None:
    session = _FakeSession(fail_sends=1)
    w = _session_worker(session)
    done: list[bool] = []
    w.finished.connect(lambda: done.append(True))
    w.run()
    assert done == [True]
    assert session.opened == 2 and session.closed == 1 and session.sent == [b"x"]


def test_kept_open_session_that_keeps_failing_reports_an_error() -> None:
    session = _FakeSession(fail_sends=2)
    w = _session_worker(session)
    errors: list[str] = []
    w.error.connect(errors.append)
    w.run()
    assert len(errors) == 1 and "connection lost" in errors[0]
    assert session.closed >= 1  # not trusted any more


def test_busy_sound_card_names_the_other_owner() -> None:
    w = _session_worker(_FakeSession(busy="FT4"))
    errors: list[str] = []
    w.error.connect(errors.append)
    w.run()
    assert errors == ["Sound card output is in use by FT4"]


def test_session_config_key_changes_with_the_settings() -> None:
    a = direwolf_tx.DirewolfTxSession("APRS", 1, 2, "1200", 300, 100)
    assert a.config_key == direwolf_tx.DirewolfTxSession("APRS", 1, 2, "1200", 300, 100).config_key
    assert a.config_key != direwolf_tx.DirewolfTxSession("APRS", 1, 2, "4800", 300, 100).config_key
    assert a.config_key != direwolf_tx.DirewolfTxSession("APRS", 1, 2, "1200", 250, 100).config_key
    assert not a.is_open
