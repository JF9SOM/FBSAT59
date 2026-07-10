"""Unit tests for comms/aprs/engine.py — AX.25 baud (MODEM) selection.

Covers resolve_ax25_modem() and AprsEngine's owner-counted start_rig() /
restart_if_modem_changed() logic. DirewolfManager is swapped for a fake
that just records calls, so no real ``direwolf`` binary or subprocess is
needed.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from comms.aprs.engine import AprsEngine, resolve_ax25_modem

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    return c


class _FakeRadioControl:
    def __init__(self, transmitter: dict[str, Any] | None) -> None:
        self._transmitter = transmitter

    def current_transmitter(self) -> dict[str, Any] | None:
        return self._transmitter


class _FakeDirewolfManager:
    """Stands in for DirewolfManager — no real subprocess/binary needed."""

    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []
        self.stop_calls = 0
        self.kiss_client = None

    def start(
        self,
        *,
        callsign: str,
        ssid: int,
        via: str,
        in_device: int | None,
        out_device: int | None,
        modem: str = "1200",
        sdr_pipeline: Any = None,
    ) -> tuple[bool, str]:
        self.start_calls.append(
            {
                "callsign": callsign,
                "ssid": ssid,
                "via": via,
                "modem": modem,
                "sdr_pipeline": sdr_pipeline,
            }
        )
        return True, ""

    def stop(self) -> None:
        self.stop_calls += 1


class _FakePipeline:
    class _Device:
        sample_rate = 2_400_000

    _device = _Device()

    def subscribe(self, _cb: Any) -> None:
        pass

    def unsubscribe(self, _cb: Any) -> None:
        pass


@pytest.fixture
def engine(conn: sqlite3.Connection) -> AprsEngine:
    """A standalone AprsEngine (bypassing the process-wide singleton) with a
    fake DirewolfManager so start_rig()/restart_if_modem_changed() can run
    without a real direwolf binary."""
    e = AprsEngine(conn, parent=None)
    e._mgr = _FakeDirewolfManager()  # type: ignore[assignment]
    return e


# ---------------------------------------------------------------------------
# resolve_ax25_modem()
# ---------------------------------------------------------------------------


def test_resolve_auto_no_setting_no_transmitter_defaults_1200(conn: sqlite3.Connection) -> None:
    rc = _FakeRadioControl(None)
    assert resolve_ax25_modem(conn, rc) == "1200"


def test_resolve_auto_reads_9600_from_transmitter_baud(conn: sqlite3.Connection) -> None:
    rc = _FakeRadioControl({"baud": 9600})
    assert resolve_ax25_modem(conn, rc) == "9600"


@pytest.mark.parametrize("baud", [1200, None, 4800])
def test_resolve_auto_non_9600_baud_defaults_1200(
    conn: sqlite3.Connection, baud: int | None
) -> None:
    rc = _FakeRadioControl({"baud": baud})
    assert resolve_ax25_modem(conn, rc) == "1200"


def test_resolve_manual_mode_ignores_transmitter_baud(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO app_settings (key, value) VALUES ('ax25_baud_mode', '9600')")
    # Even a transponder with baud=1200 (or no transponder at all) must not
    # override an explicit manual selection.
    assert resolve_ax25_modem(conn, _FakeRadioControl({"baud": 1200})) == "9600"
    assert resolve_ax25_modem(conn, _FakeRadioControl(None)) == "9600"


def test_resolve_manual_1200_mode(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO app_settings (key, value) VALUES ('ax25_baud_mode', '1200')")
    assert resolve_ax25_modem(conn, _FakeRadioControl({"baud": 9600})) == "1200"


def test_resolve_garbage_setting_falls_back_to_auto(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO app_settings (key, value) VALUES ('ax25_baud_mode', 'nonsense')")
    rc = _FakeRadioControl({"baud": 9600})
    assert resolve_ax25_modem(conn, rc) == "9600"


# ---------------------------------------------------------------------------
# AprsEngine.start_rig() / restart_if_modem_changed()
# ---------------------------------------------------------------------------


def test_start_rig_passes_modem_to_direwolf(engine: AprsEngine) -> None:
    ok, err = engine.start_rig("aprs", "JF9SOM", 0, "ARISS", modem="9600")
    assert ok and err == ""
    fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]
    assert fake_mgr.start_calls[-1]["modem"] == "9600"
    assert engine.current_modem == "9600"
    assert engine.is_running


def test_second_owner_join_does_not_restart(engine: AprsEngine) -> None:
    engine.start_rig("aprs", "JF9SOM", 0, "ARISS", modem="9600")
    fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]
    assert len(fake_mgr.start_calls) == 1

    # Telemetry tab joins while already running — even with a different
    # modem argument, this must be a pure no-op (no second Direwolf spawn).
    ok, _err = engine.start_rig("telemetry", "N0CALL", 0, "", modem="1200")
    assert ok
    assert len(fake_mgr.start_calls) == 1
    assert engine.current_modem == "9600"


def test_restart_if_modem_changed_noop_when_same(engine: AprsEngine) -> None:
    engine.start_rig("aprs", "JF9SOM", 0, "ARISS", modem="9600")
    fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]
    engine.restart_if_modem_changed("9600")
    assert fake_mgr.stop_calls == 0
    assert len(fake_mgr.start_calls) == 1


def test_restart_if_modem_changed_restarts_and_keeps_owners(engine: AprsEngine) -> None:
    engine.start_rig("aprs", "JF9SOM", 0, "ARISS", modem="1200")
    engine.add_owner("telemetry")
    assert engine._owners == {"aprs", "telemetry"}

    engine.restart_if_modem_changed("9600")

    fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]
    assert fake_mgr.stop_calls == 1
    assert len(fake_mgr.start_calls) == 2
    assert fake_mgr.start_calls[-1]["modem"] == "9600"
    assert engine.current_modem == "9600"
    assert engine.is_running
    # Restarting in place must not drop either owner's claim.
    assert engine._owners == {"aprs", "telemetry"}


def test_restart_if_modem_changed_noop_when_not_running(engine: AprsEngine) -> None:
    engine.restart_if_modem_changed("9600")
    fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]
    assert fake_mgr.start_calls == []
    assert fake_mgr.stop_calls == 0


def test_restart_if_modem_changed_noop_on_sdr_path(engine: AprsEngine) -> None:
    """The SDR/AFSK receive path has no MODEM concept — nothing to restart."""
    ok, _err = engine.start_sdr("aprs", _FakePipeline())
    assert ok
    try:
        assert engine.current_modem is None

        fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]
        engine.restart_if_modem_changed("9600")
        assert fake_mgr.start_calls == []
        assert fake_mgr.stop_calls == 0
    finally:
        # start_sdr() spins up a real AfskDemodulator QThread — must stop it
        # or interpreter shutdown can abort the process mid-test-run.
        engine.stop("aprs")


def test_stop_only_tears_down_after_last_owner_releases(engine: AprsEngine) -> None:
    engine.start_rig("aprs", "JF9SOM", 0, "ARISS", modem="9600")
    engine.add_owner("telemetry")

    engine.stop("aprs")
    fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]
    assert fake_mgr.stop_calls == 0
    assert engine.is_running

    engine.stop("telemetry")
    assert fake_mgr.stop_calls == 1
    assert not engine.is_running
    assert engine.current_modem is None


# ---------------------------------------------------------------------------
# start_sdr_direwolf() / sync_sdr_baud() — SDR-fed 9600 G3RUH path
# ---------------------------------------------------------------------------


def test_start_sdr_direwolf_uses_modem_9600(engine: AprsEngine) -> None:
    ok, err = engine.start_sdr_direwolf("aprs", _FakePipeline())
    assert ok and err == ""
    fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]
    assert fake_mgr.start_calls[-1]["modem"] == "9600"
    assert fake_mgr.start_calls[-1]["sdr_pipeline"] is not None
    assert engine.current_modem == "9600"
    assert engine.is_running


def test_sync_sdr_baud_switches_afsk_to_direwolf(engine: AprsEngine) -> None:
    """1200 (AfskDemodulator) -> 9600 (SDR-fed Direwolf) is a full mechanism
    switch, not just a MODEM restart — verify it happens and owners survive."""
    pipeline = _FakePipeline()
    ok, _err = engine.start_sdr("aprs", pipeline)
    assert ok
    try:
        engine.add_owner("telemetry")
        fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]

        engine.sync_sdr_baud(pipeline, "9600")

        assert fake_mgr.stop_calls == 1
        assert fake_mgr.start_calls[-1]["modem"] == "9600"
        assert engine.current_modem == "9600"
        assert engine.is_running
        assert engine._owners == {"aprs", "telemetry"}
    finally:
        engine.stop("aprs")
        engine.stop("telemetry")


def test_sync_sdr_baud_switches_direwolf_to_afsk(engine: AprsEngine) -> None:
    """9600 (SDR-fed Direwolf) -> 1200 (AfskDemodulator) is also a full switch."""
    pipeline = _FakePipeline()
    ok, _err = engine.start_sdr_direwolf("aprs", pipeline)
    assert ok
    fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]
    try:
        engine.sync_sdr_baud(pipeline, "1200")
        assert fake_mgr.stop_calls == 1
        assert engine.current_modem is None
        assert engine.is_running
    finally:
        # sync_sdr_baud() just started a real AfskDemodulator QThread.
        engine.stop("aprs")


def test_sync_sdr_baud_noop_when_already_correct(engine: AprsEngine) -> None:
    pipeline = _FakePipeline()
    ok, _err = engine.start_sdr_direwolf("aprs", pipeline)
    assert ok
    fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]
    engine.sync_sdr_baud(pipeline, "9600")
    assert fake_mgr.stop_calls == 0
    assert len(fake_mgr.start_calls) == 1


def test_sync_sdr_baud_noop_when_rig_session_active(engine: AprsEngine) -> None:
    """A Rig + Sound Card Direwolf session — even one at 9600 — is not
    sync_sdr_baud()'s to touch; only restart_if_modem_changed() owns it."""
    engine.start_rig("aprs", "JF9SOM", 0, "ARISS", modem="9600")
    fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]

    engine.sync_sdr_baud(_FakePipeline(), "1200")

    assert fake_mgr.stop_calls == 0
    assert len(fake_mgr.start_calls) == 1
    assert engine.current_modem == "9600"


def test_sync_sdr_baud_noop_when_not_running(engine: AprsEngine) -> None:
    engine.sync_sdr_baud(_FakePipeline(), "9600")
    fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]
    assert fake_mgr.start_calls == []
    assert fake_mgr.stop_calls == 0
