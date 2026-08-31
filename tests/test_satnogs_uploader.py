"""Unit tests for comms/telemetry/satnogs_uploader.py.

No network access: the worker's POST function is replaced with a fake that
records its arguments. Field-formatting and gating are checked directly via
build_submission().
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest

from comms.telemetry.satnogs_uploader import (
    SATNOGS_UPLOAD_SETTINGS_KEY,
    SatnogsUploader,
    build_submission,
    get_satnogs_uploader,
    get_station_callsign,
    get_station_latlon,
    load_satnogs_upload_settings,
    save_satnogs_upload_settings,
)
from data.database import SCHEMA_SQL

_FRAME = bytes.fromhex("9c86aa8e a662e0a0 8a82a498 86e103f0".replace(" ", ""))
_TS = datetime(2026, 8, 31, 4, 12, 33, 418000, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def db() -> sqlite3.Connection:
    """In-memory SQLite DB with the full schema (provides app_settings)."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def _set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()


def _configure(
    conn: sqlite3.Connection,
    *,
    enabled: bool = True,
    api_key: str = "TESTKEY123",
    callsign: str = "jf9som",
    lat: float | None = 35.6895,
    lon: float | None = 139.6917,
) -> None:
    save_satnogs_upload_settings(conn, {"enabled": enabled, "api_key": api_key})
    if callsign is not None:
        _set(conn, "callsign", callsign)
    if lat is not None and lon is not None:
        _set(
            conn,
            "observer_location",
            json.dumps({"latitude_deg": lat, "longitude_deg": lon}),
        )


class _FakePost:
    """Records (api_key, fields) and signals an Event on each call."""

    def __init__(self, status: int = 200, body: str = "") -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.status = status
        self.body = body
        self.called = threading.Event()

    def __call__(self, api_key: str, fields: dict[str, str]) -> tuple[int, str]:
        self.calls.append((api_key, dict(fields)))
        self.called.set()
        return self.status, self.body


def _wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    """Poll *predicate* until true or *timeout* elapses. Used for assertions
    on the worker thread's log output, which lands slightly after the fake
    post function returns."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture()
def uploader_factory() -> Iterator[list[SatnogsUploader]]:
    made: list[SatnogsUploader] = []
    yield made
    for u in made:
        u.shutdown()


# --------------------------------------------------------------------------- #
# Settings persistence
# --------------------------------------------------------------------------- #


def test_settings_default_when_unset(db: sqlite3.Connection) -> None:
    assert load_satnogs_upload_settings(db) == {"enabled": False, "api_key": ""}


def test_settings_round_trip(db: sqlite3.Connection) -> None:
    save_satnogs_upload_settings(db, {"enabled": True, "api_key": "abc"})
    assert load_satnogs_upload_settings(db) == {"enabled": True, "api_key": "abc"}


def test_settings_corrupt_json_falls_back_to_defaults(db: sqlite3.Connection) -> None:
    _set(db, SATNOGS_UPLOAD_SETTINGS_KEY, "{not json")
    assert load_satnogs_upload_settings(db) == {"enabled": False, "api_key": ""}


def test_station_helpers(db: sqlite3.Connection) -> None:
    assert get_station_callsign(db) == ""
    assert get_station_latlon(db) is None
    _set(db, "callsign", "  JF9SOM  ")
    _set(db, "observer_location", json.dumps({"latitude_deg": -12.5, "longitude_deg": -70.0}))
    assert get_station_callsign(db) == "JF9SOM"
    assert get_station_latlon(db) == (-12.5, -70.0)


def test_station_latlon_bad_json_is_none(db: sqlite3.Connection) -> None:
    _set(db, "observer_location", "garbage")
    assert get_station_latlon(db) is None


# --------------------------------------------------------------------------- #
# build_submission() — gating
# --------------------------------------------------------------------------- #


def test_build_none_when_disabled(db: sqlite3.Connection) -> None:
    _configure(db, enabled=False)
    assert build_submission(db, _FRAME, 43803, _TS) is None


def test_build_none_when_no_api_key(db: sqlite3.Connection) -> None:
    _configure(db, api_key="   ")
    assert build_submission(db, _FRAME, 43803, _TS) is None


def test_build_none_when_no_callsign(db: sqlite3.Connection) -> None:
    _configure(db)
    db.execute("DELETE FROM app_settings WHERE key = 'callsign'")
    db.commit()
    assert build_submission(db, _FRAME, 43803, _TS) is None


def test_build_none_when_no_location(db: sqlite3.Connection) -> None:
    _configure(db, lat=None, lon=None)
    assert build_submission(db, _FRAME, 43803, _TS) is None


# --------------------------------------------------------------------------- #
# build_submission() — field formatting (matches SkyRoof SatnogsUploader.cs)
# --------------------------------------------------------------------------- #


def test_build_fields_northeast(db: sqlite3.Connection) -> None:
    _configure(db, callsign="jf9som", lat=35.6895, lon=139.6917)
    built = build_submission(db, _FRAME, 43803, _TS)
    assert built is not None
    api_key, fields = built
    assert api_key == "TESTKEY123"
    assert fields["noradID"] == "43803"
    assert fields["source"] == "JF9SOM"
    assert fields["locator"] == "longLat"
    assert fields["longitude"] == "139.6917E"
    assert fields["latitude"] == "35.6895N"
    assert fields["timestamp"] == "2026-08-31T04:12:33.418Z"
    assert fields["frame"] == _FRAME.hex()
    assert fields["version"].startswith("FBSAT59")


def test_build_fields_southwest_signs(db: sqlite3.Connection) -> None:
    _configure(db, lat=-33.8688, lon=-70.6693)
    built = build_submission(db, _FRAME, 1, _TS)
    assert built is not None
    _, fields = built
    assert fields["longitude"] == "70.6693W"
    assert fields["latitude"] == "33.8688S"


def test_build_timestamp_coerced_to_utc(db: sqlite3.Connection) -> None:
    _configure(db)
    jst = datetime(2026, 8, 31, 9, 0, 0, 0, tzinfo=timezone(timedelta(hours=9)))
    built = build_submission(db, _FRAME, 1, jst)
    assert built is not None
    _, fields = built
    assert fields["timestamp"] == "2026-08-31T00:00:00.000Z"


# --------------------------------------------------------------------------- #
# SatnogsUploader — worker path
# --------------------------------------------------------------------------- #


def test_submit_enqueues_and_worker_posts(
    db: sqlite3.Connection, uploader_factory: list[SatnogsUploader]
) -> None:
    _configure(db, api_key="KEY", callsign="jf9som")
    fake = _FakePost()
    up = SatnogsUploader(post_fn=fake)
    uploader_factory.append(up)

    assert up.submit(db, _FRAME, 43803, _TS) is True
    assert fake.called.wait(timeout=2.0)
    assert len(fake.calls) == 1
    api_key, fields = fake.calls[0]
    assert api_key == "KEY"
    assert fields["source"] == "JF9SOM"
    assert fields["noradID"] == "43803"


def test_submit_noop_when_disabled(
    db: sqlite3.Connection, uploader_factory: list[SatnogsUploader]
) -> None:
    _configure(db, enabled=False)
    fake = _FakePost()
    up = SatnogsUploader(post_fn=fake)
    uploader_factory.append(up)

    assert up.submit(db, _FRAME, 43803, _TS) is False
    assert not fake.called.wait(timeout=0.3)
    assert fake.calls == []


def test_submit_noop_when_norad_none(
    db: sqlite3.Connection, uploader_factory: list[SatnogsUploader]
) -> None:
    _configure(db)
    fake = _FakePost()
    up = SatnogsUploader(post_fn=fake)
    uploader_factory.append(up)

    assert up.submit(db, _FRAME, None, _TS) is False
    assert not fake.called.wait(timeout=0.3)


def test_worker_logs_rejection_and_keeps_running(
    db: sqlite3.Connection,
    uploader_factory: list[SatnogsUploader],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _configure(db)
    fake = _FakePost(status=401, body='{"detail":"Invalid token."}')
    up = SatnogsUploader(post_fn=fake)
    uploader_factory.append(up)

    with caplog.at_level(logging.WARNING, logger="comms.telemetry.satnogs_uploader"):
        up.submit(db, _FRAME, 43803, _TS)
        assert fake.called.wait(timeout=2.0)
        # worker survived the rejection and still processes a second frame
        fake.called.clear()
        up.submit(db, _FRAME, 43803, _TS)
        assert fake.called.wait(timeout=2.0)
        assert _wait_for(lambda: any("HTTP 401" in r.message for r in caplog.records))

    assert len(fake.calls) == 2


def test_worker_swallows_post_exception(
    db: sqlite3.Connection,
    uploader_factory: list[SatnogsUploader],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _configure(db)
    boom = threading.Event()

    def raiser(_api_key: str, _fields: dict[str, str]) -> tuple[int, str]:
        boom.set()
        raise RuntimeError("network down")

    up = SatnogsUploader(post_fn=raiser)
    uploader_factory.append(up)

    with caplog.at_level(logging.WARNING, logger="comms.telemetry.satnogs_uploader"):
        up.submit(db, _FRAME, 43803, _TS)
        assert boom.wait(timeout=2.0)
        assert _wait_for(lambda: any("upload failed" in r.message for r in caplog.records))


def test_get_satnogs_uploader_is_singleton() -> None:
    assert get_satnogs_uploader() is get_satnogs_uploader()
