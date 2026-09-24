"""Unit tests for comms/aprs/engine.py — AX.25 baud (MODEM) selection.

Covers resolve_ax25_modem() and AprsEngine's owner-counted start_rig() /
restart_if_modem_changed() / start_sdr_direwolf() / sync_sdr_baud() logic.
DirewolfManager is swapped for a fake that just records calls, so no real
``direwolf`` binary or subprocess is needed.
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
        sdr_satellite: bool = False,
    ) -> tuple[bool, str]:
        self.start_calls.append(
            {
                "callsign": callsign,
                "ssid": ssid,
                "via": via,
                "modem": modem,
                "sdr_pipeline": sdr_pipeline,
                "sdr_satellite": sdr_satellite,
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


def test_resolve_auto_reads_4800_from_transmitter_baud(conn: sqlite3.Connection) -> None:
    rc = _FakeRadioControl({"baud": 4800})
    assert resolve_ax25_modem(conn, rc) == "4800"


@pytest.mark.parametrize("baud", [1200, None, 2400])
def test_resolve_auto_other_baud_defaults_1200(conn: sqlite3.Connection, baud: int | None) -> None:
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


def test_restart_if_modem_changed_noop_on_sdr_session(engine: AprsEngine) -> None:
    """restart_if_modem_changed() only owns Rig + Sound Card sessions
    (_last_rig_params) — an SDR-fed Direwolf session must not be touched;
    sync_sdr_baud() is the one that restarts those."""
    ok, _err = engine.start_sdr_direwolf("aprs", _FakePipeline(), modem="1200")
    assert ok
    try:
        assert engine.current_modem == "1200"

        fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]
        engine.restart_if_modem_changed("9600")
        assert len(fake_mgr.start_calls) == 1
        assert fake_mgr.stop_calls == 0
    finally:
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
# start_sdr_direwolf() / sync_sdr_baud() — SDR path (1200: Direwolf, 4800/9600: coherent MSK)
# ---------------------------------------------------------------------------


def test_start_sdr_direwolf_defaults_to_modem_1200(engine: AprsEngine) -> None:
    ok, err = engine.start_sdr_direwolf("aprs", _FakePipeline())
    assert ok and err == ""
    fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]
    assert fake_mgr.start_calls[-1]["modem"] == "1200"
    assert fake_mgr.start_calls[-1]["sdr_pipeline"] is not None
    assert engine.current_modem == "1200"
    assert engine.is_running


def test_start_sdr_direwolf_passes_satellite_flag_for_1200(engine: AprsEngine) -> None:
    ok, _err = engine.start_sdr_direwolf("telemetry", _FakePipeline(), modem="1200", satellite=True)
    assert ok
    try:
        fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]
        assert fake_mgr.start_calls[-1]["sdr_satellite"] is True
    finally:
        engine.stop("telemetry")


@pytest.mark.parametrize("modem", ["9600", "4800"])
def test_start_sdr_direwolf_runs_coherent_decoder_next_to_direwolf_for_g3ruh(
    engine: AprsEngine, modem: str
) -> None:
    ok, err = engine.start_sdr_direwolf("aprs", _FakePipeline(), modem=modem)
    try:
        assert ok and err == ""
        fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]
        assert fake_mgr.start_calls[-1]["modem"] == modem  # Direwolf too (other deviations)
        assert engine._coherent_demod is not None
        assert engine.current_modem == modem
        assert engine.is_running
    finally:
        engine.stop("aprs")
    assert engine._coherent_demod is None
    assert not engine.is_running


def test_coherent_session_unsubscribes_from_pipeline_on_stop(engine: AprsEngine) -> None:
    class _Pipeline(_FakePipeline):
        def __init__(self) -> None:
            self.subscribed: list[Any] = []

        def subscribe(self, cb: Any) -> None:
            self.subscribed.append(cb)

        def unsubscribe(self, cb: Any) -> None:
            self.subscribed.remove(cb)

    pipeline = _Pipeline()
    engine.start_sdr_direwolf("aprs", pipeline, modem="4800")
    assert len(pipeline.subscribed) == 1
    engine.stop("aprs")
    assert pipeline.subscribed == []


def test_coherent_session_survives_a_missing_direwolf_binary(engine: AprsEngine) -> None:
    class _NoDirewolf(_FakeDirewolfManager):
        def start(self, **kwargs: Any) -> tuple[bool, str]:
            return False, "Direwolf not found"

    engine._mgr = _NoDirewolf()  # type: ignore[assignment]
    ok, err = engine.start_sdr_direwolf("aprs", _FakePipeline(), modem="4800")
    try:
        assert ok and err == ""
        assert engine._coherent_demod is not None
        assert engine.is_running
    finally:
        engine.stop("aprs")


def test_same_frame_from_both_decoders_is_published_once(engine: AprsEngine) -> None:
    raw_frames: list[bytes] = []
    engine.raw_frame_received.connect(raw_frames.append)
    engine.start_sdr_direwolf("aprs", _FakePipeline(), modem="9600")
    try:
        frame = bytes([0x76, 1]) + bytes(range(60))
        engine._on_kiss_frame(frame)  # Direwolf reports it first ...
        engine._on_coherent_frame(frame)  # ... the coherent decoder a chunk later
        assert raw_frames == [frame]
        other = bytes([0x76, 2]) + bytes(range(60))
        engine._on_coherent_frame(other)
        assert raw_frames == [frame, other]
    finally:
        engine.stop("aprs")


def test_repeated_frame_is_reported_again_after_the_duplicate_window(
    engine: AprsEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_frames: list[bytes] = []
    engine.raw_frame_received.connect(raw_frames.append)
    engine.start_sdr_direwolf("aprs", _FakePipeline(), modem="4800")
    now = [100.0]
    monkeypatch.setattr("comms.aprs.engine.time.monotonic", lambda: now[0])
    try:
        frame = bytes([0x76, 1]) + bytes(range(60))
        engine._on_coherent_frame(frame)
        now[0] += 10.0  # a genuine retransmission, well outside the window
        engine._on_coherent_frame(frame)
        assert raw_frames == [frame, frame]
    finally:
        engine.stop("aprs")


def test_single_direwolf_session_never_drops_repeated_frames(engine: AprsEngine) -> None:
    """Without the coherent decoder there is nothing to de-duplicate against."""
    raw_frames: list[bytes] = []
    engine.raw_frame_received.connect(raw_frames.append)
    engine.start_sdr_direwolf("aprs", _FakePipeline(), modem="1200")
    try:
        frame = bytes([1, 2, 3])
        engine._on_kiss_frame(frame)
        engine._on_kiss_frame(frame)
        assert raw_frames == [frame, frame]
    finally:
        engine.stop("aprs")


def test_start_sdr_coherent_fails_without_sample_rate(engine: AprsEngine) -> None:
    class _NoRate:
        _device = object()

        def subscribe(self, _cb: Any) -> None:
            pass

    ok, err = engine.start_sdr_direwolf("aprs", _NoRate(), modem="9600")
    assert not ok and err
    assert not engine.is_running


def test_coherent_frame_is_published_raw_even_when_not_ax25(engine: AprsEngine) -> None:
    raw_frames: list[bytes] = []
    packets: list[Any] = []
    engine.raw_frame_received.connect(raw_frames.append)
    engine.packet_received.connect(packets.append)
    frame = bytes([0x76, 0x20]) + bytes(range(98))  # ARICA-2 style: HDLC, not an AX.25 header
    engine._on_coherent_frame(frame)
    assert raw_frames == [frame]
    assert packets == []


def test_sync_sdr_baud_switches_modem_within_coherent_mechanism(engine: AprsEngine) -> None:
    """9600 <-> 4800 both run the coherent decoder next to Direwolf: a fresh
    decoder and Direwolf for the new baud, owners must survive."""
    pipeline = _FakePipeline()
    ok, _err = engine.start_sdr_direwolf("aprs", pipeline, modem="9600")
    assert ok
    engine.add_owner("telemetry")
    fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]
    try:
        first = engine._coherent_demod
        engine.sync_sdr_baud(pipeline, "4800")

        assert fake_mgr.start_calls[-1]["modem"] == "4800"
        assert engine._coherent_demod is not None and engine._coherent_demod is not first
        assert engine.current_modem == "4800"
        assert engine.is_running
        assert engine._owners == {"aprs", "telemetry"}
    finally:
        engine.stop("aprs")
        engine.stop("telemetry")


def test_sync_sdr_baud_switches_to_1200(engine: AprsEngine) -> None:
    """Switching to 1200 from 9600 stops the coherent decoder and restarts
    Direwolf at 1200; owners must survive."""
    pipeline = _FakePipeline()
    ok, _err = engine.start_sdr_direwolf("aprs", pipeline, modem="9600")
    assert ok
    try:
        engine.add_owner("telemetry")
        fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]

        engine.sync_sdr_baud(pipeline, "1200")

        assert engine._coherent_demod is None
        assert fake_mgr.start_calls[-1]["modem"] == "1200"
        assert engine.current_modem == "1200"
        assert engine.is_running
        assert engine._owners == {"aprs", "telemetry"}
    finally:
        engine.stop("aprs")
        engine.stop("telemetry")


def test_sync_sdr_baud_switches_from_1200_to_coherent(engine: AprsEngine) -> None:
    pipeline = _FakePipeline()
    ok, _err = engine.start_sdr_direwolf("aprs", pipeline, modem="1200")
    assert ok
    try:
        fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]
        engine.sync_sdr_baud(pipeline, "9600")

        assert fake_mgr.stop_calls == 1
        assert engine._coherent_demod is not None
        assert engine.current_modem == "9600"
    finally:
        engine.stop("aprs")


def test_sync_sdr_baud_noop_when_already_correct(engine: AprsEngine) -> None:
    pipeline = _FakePipeline()
    ok, _err = engine.start_sdr_direwolf("aprs", pipeline, modem="9600")
    assert ok
    try:
        fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]
        demod = engine._coherent_demod
        engine.sync_sdr_baud(pipeline, "9600")
        assert fake_mgr.stop_calls == 0
        assert engine._coherent_demod is demod
    finally:
        engine.stop("aprs")


def test_sync_sdr_baud_restarts_1200_when_front_end_changes(engine: AprsEngine) -> None:
    """The satellite/terrestrial front end only exists at 1200 baud: changing it
    restarts Direwolf, while at 9600 the flag is irrelevant and nothing restarts."""
    pipeline = _FakePipeline()
    ok, _err = engine.start_sdr_direwolf("aprs", pipeline, modem="1200", satellite=False)
    assert ok
    try:
        fake_mgr: _FakeDirewolfManager = engine._mgr  # type: ignore[assignment]
        engine.sync_sdr_baud(pipeline, "1200", satellite=False)
        assert fake_mgr.stop_calls == 0
        engine.sync_sdr_baud(pipeline, "1200", satellite=True)
        assert fake_mgr.stop_calls == 1
        assert fake_mgr.start_calls[-1]["sdr_satellite"] is True

        engine.sync_sdr_baud(pipeline, "9600", satellite=True)
        demod = engine._coherent_demod
        engine.sync_sdr_baud(pipeline, "9600", satellite=False)
        assert engine._coherent_demod is demod
    finally:
        engine.stop("aprs")


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
