"""Tests for comms/telemetry/cw_upload.py -- when a decoded CW frame may be sent
to the shared SatNOGS DB (repeated, reliable time, once)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from comms.telemetry.cw_upload import (
    auto_send,
    eligible_unsent,
    ensure_columns,
    logged_frames,
    mark_uploaded,
    reset_unconfirmed_marks,
    send_frames,
)
from comms.telemetry.satnogs_uploader import save_satnogs_upload_settings
from data.database import SCHEMA_SQL

ARICA2 = 68796
HK1 = "2FFE8594EB880124"
HK3 = "00D7C2A8D6B8EA"
T0 = datetime(2026, 9, 20, 7, 1, 48, tzinfo=UTC)


class _FakeUploader:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, int, datetime, bool]] = []
        self.callbacks: list[Any] = []

    def submit(
        self,
        conn: Any,
        raw: bytes,
        norad: int,
        received_at: datetime,
        force: bool = False,
        on_result: Any = None,
    ) -> bool:
        self.sent.append((raw, norad, received_at, force))
        self.callbacks.append(on_result)
        return True


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA_SQL)
    c.execute(
        """CREATE TABLE telemetry_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, received_at DATETIME NOT NULL,
            norad_cat_id INTEGER, callsign TEXT NOT NULL, raw_hex TEXT NOT NULL,
            parsed_json TEXT, signal_db REAL)"""
    )
    ensure_columns(c)
    for key, value in (
        ("callsign", "jf9som"),
        ("observer_location", json.dumps({"latitude_deg": 36.1, "longitude_deg": 136.4})),
    ):
        c.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)", (key, value))
    save_satnogs_upload_settings(c, {"enabled": True, "api_key": "KEY"})
    c.commit()
    return c


def _log(
    conn: sqlite3.Connection,
    hex_text: str,
    at: datetime,
    *,
    reliable: bool = True,
    uploaded: bool = False,
) -> int:
    cur = conn.execute(
        "INSERT INTO telemetry_log (received_at, norad_cat_id, callsign, raw_hex, "
        "satnogs_uploaded_at, time_reliable) VALUES (?, ?, 'JS1YSD', ?, ?, ?)",
        (
            at.isoformat(),
            ARICA2,
            hex_text,
            "2026-09-20T08:00:00+00:00" if uploaded else None,
            int(reliable),
        ),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def _uploaded(conn: sqlite3.Connection, frame_id: int) -> bool:
    row = conn.execute("SELECT satnogs_uploaded_at FROM telemetry_log WHERE id = ?", (frame_id,))
    return bool(row.fetchone()[0])


class TestSchema:
    def test_columns_are_added_to_an_existing_table_once(self) -> None:
        c = sqlite3.connect(":memory:")
        c.execute(
            "CREATE TABLE telemetry_log (id INTEGER PRIMARY KEY, received_at TEXT, "
            "norad_cat_id INTEGER, callsign TEXT, raw_hex TEXT, parsed_json TEXT, signal_db REAL)"
        )
        ensure_columns(c)
        ensure_columns(c)  # idempotent
        names = {r[1] for r in c.execute("PRAGMA table_info(telemetry_log)")}
        assert {"satnogs_uploaded_at", "time_reliable"} <= names


class TestRepeatRule:
    def test_a_single_reading_is_not_sent(self, conn: sqlite3.Connection) -> None:
        fid = _log(conn, HK1, T0)
        up = _FakeUploader()
        report = auto_send(conn, up, ARICA2, fid)  # type: ignore[arg-type]
        assert (report.queued, report.waiting) == (0, 1)
        assert up.sent == []
        assert not _uploaded(conn, fid)

    def test_a_repeat_sends_both_readings(self, conn: sqlite3.Connection) -> None:
        first = _log(conn, HK1, T0)
        second = _log(conn, HK1, T0 + timedelta(seconds=125))
        up = _FakeUploader()

        report = auto_send(conn, up, ARICA2, second)  # type: ignore[arg-type]

        assert report.queued == 2
        assert [s[2] for s in up.sent] == [
            T0,
            T0 + timedelta(seconds=125),
        ]  # each with its own time
        # Queued only: nothing counts as sent until SatNOGS accepted it.
        assert not _uploaded(conn, first) and not _uploaded(conn, second)
        assert all(not s[3] for s in up.sent)  # automatic: the switch must be on

    def test_the_same_transmission_decoded_twice_does_not_confirm_itself(
        self, conn: sqlite3.Connection
    ) -> None:
        _log(conn, HK1, T0)
        second = _log(conn, HK1, T0 + timedelta(seconds=2))
        report = auto_send(conn, _FakeUploader(), ARICA2, second)  # type: ignore[arg-type]
        assert report.queued == 0
        assert report.waiting == 2

    def test_a_recurrence_a_day_later_confirms_nothing(self, conn: sqlite3.Connection) -> None:
        _log(conn, HK1, T0)
        later = _log(conn, HK1, T0 + timedelta(days=1))
        assert auto_send(conn, _FakeUploader(), ARICA2, later).queued == 0  # type: ignore[arg-type]

    def test_different_frames_do_not_confirm_each_other(self, conn: sqlite3.Connection) -> None:
        _log(conn, HK1, T0)
        other = _log(conn, HK3, T0 + timedelta(seconds=60))
        assert auto_send(conn, _FakeUploader(), ARICA2, other).queued == 0  # type: ignore[arg-type]

    def test_a_one_digit_difference_is_a_different_frame(self, conn: sqlite3.Connection) -> None:
        _log(conn, HK1, T0)
        misread = _log(conn, "3FFE8594EB880124", T0 + timedelta(seconds=90))
        assert auto_send(conn, _FakeUploader(), ARICA2, misread).queued == 0  # type: ignore[arg-type]


class TestReliableTime:
    def test_a_frame_with_a_placeholder_time_is_never_sent(self, conn: sqlite3.Connection) -> None:
        a = _log(conn, HK1, T0, reliable=False)
        b = _log(conn, HK1, T0 + timedelta(seconds=90), reliable=False)
        up = _FakeUploader()

        auto = auto_send(conn, up, ARICA2, b)  # type: ignore[arg-type]
        manual = send_frames(conn, up, ARICA2, [a, b], force=True, require_repeat=False)  # type: ignore[arg-type]

        assert (auto.queued, auto.unreliable) == (0, 2)
        assert (manual.queued, manual.unreliable) == (0, 2)
        assert up.sent == []


class TestOnce:
    def test_a_frame_already_sent_is_not_sent_again(self, conn: sqlite3.Connection) -> None:
        done = _log(conn, HK1, T0, uploaded=True)
        again = _log(conn, HK1, T0 + timedelta(seconds=90))
        up = _FakeUploader()
        report = send_frames(conn, up, ARICA2, [done, again], force=True, require_repeat=False)  # type: ignore[arg-type]
        # `done` is skipped silently; `again` is a different reception (90 s later) and goes.
        assert report.queued == 1

    def test_the_same_reception_replayed_is_a_duplicate(self, conn: sqlite3.Connection) -> None:
        _log(conn, HK1, T0, uploaded=True)
        replayed = _log(conn, HK1, T0 + timedelta(seconds=1))
        up = _FakeUploader()
        report = send_frames(conn, up, ARICA2, [replayed], force=True, require_repeat=False)  # type: ignore[arg-type]
        assert (report.queued, report.duplicates) == (0, 1)
        assert up.sent == []


class TestManualSend:
    def test_a_selected_single_reading_is_sent_even_with_the_switch_off(
        self, conn: sqlite3.Connection
    ) -> None:
        save_satnogs_upload_settings(conn, {"enabled": False, "api_key": "KEY"})
        fid = _log(conn, HK1, T0)
        up = _FakeUploader()

        report = send_frames(conn, up, ARICA2, [fid], force=True, require_repeat=False)  # type: ignore[arg-type]

        assert report.queued == 1
        (raw, norad, at, forced) = up.sent[0]
        assert raw == b"arica-2\x01" + bytes.fromhex(HK1)
        assert (norad, at, forced) == (ARICA2, T0, True)
        assert not _uploaded(conn, fid)  # not before SatNOGS accepted it

    def test_missing_prerequisites_are_reported_and_nothing_is_marked(
        self, conn: sqlite3.Connection
    ) -> None:
        save_satnogs_upload_settings(conn, {"enabled": True, "api_key": ""})
        fid = _log(conn, HK1, T0)
        up = _FakeUploader()

        report = send_frames(conn, up, ARICA2, [fid], force=True, require_repeat=False)  # type: ignore[arg-type]

        assert report.blocker == "no_api_key"
        assert up.sent == []
        assert not _uploaded(conn, fid)

    def test_the_switch_off_blocks_the_automatic_path(self, conn: sqlite3.Connection) -> None:
        save_satnogs_upload_settings(conn, {"enabled": False, "api_key": "KEY"})
        _log(conn, HK1, T0)
        second = _log(conn, HK1, T0 + timedelta(seconds=90))
        report = auto_send(conn, _FakeUploader(), ARICA2, second)  # type: ignore[arg-type]
        assert report.blocker == "disabled"


class TestSendUnsent:
    def test_lists_only_confirmed_reliable_unsent_frames(self, conn: sqlite3.Connection) -> None:
        a = _log(conn, HK1, T0)
        b = _log(conn, HK1, T0 + timedelta(seconds=90))
        _log(conn, HK3, T0 + timedelta(seconds=30))  # single reading
        _log(conn, HK1, T0 + timedelta(seconds=180), reliable=False)  # placeholder time
        done = _log(conn, HK1, T0 + timedelta(seconds=270), uploaded=True)

        ids = {f.id for f in eligible_unsent(conn, ARICA2)}

        assert ids == {a, b}
        assert done not in ids

    def test_frames_of_other_satellites_are_not_listed(self, conn: sqlite3.Connection) -> None:
        _log(conn, HK1, T0)
        _log(conn, HK1, T0 + timedelta(seconds=90))
        assert eligible_unsent(conn, 25544) == []
        assert len(logged_frames(conn, ARICA2)) == 2


class TestResultsFromSatnogs:
    """A frame counts as sent only when SatNOGS accepted it."""

    def test_the_result_callback_names_the_frame(self, conn: sqlite3.Connection) -> None:
        fid = _log(conn, HK1, T0)
        up = _FakeUploader()
        results: list[tuple[int, bool, int, str]] = []

        send_frames(
            conn,
            up,  # type: ignore[arg-type]
            ARICA2,
            [fid],
            force=True,
            require_repeat=False,
            on_result=lambda *a: results.append(a),
        )
        up.callbacks[0](False, 401, '{"detail":"Invalid token."}')

        assert results == [(fid, False, 401, '{"detail":"Invalid token."}')]
        assert not _uploaded(conn, fid)  # a rejected upload leaves the frame unsent

    def test_a_frame_awaiting_an_answer_is_not_queued_twice(self, conn: sqlite3.Connection) -> None:
        fid = _log(conn, HK1, T0)
        up = _FakeUploader()
        pending: set[int] = set()

        first = send_frames(
            conn,
            up,
            ARICA2,
            [fid],
            force=True,
            require_repeat=False,
            pending=pending,  # type: ignore[arg-type]
        )
        second = send_frames(
            conn,
            up,
            ARICA2,
            [fid],
            force=True,
            require_repeat=False,
            pending=pending,  # type: ignore[arg-type]
        )

        assert (first.queued, second.queued, second.pending) == (1, 0, 1)
        assert pending == {fid}
        assert len(up.sent) == 1

    def test_after_a_rejection_the_frame_can_be_sent_again(self, conn: sqlite3.Connection) -> None:
        fid = _log(conn, HK1, T0)
        up = _FakeUploader()
        pending: set[int] = set()
        send_frames(conn, up, ARICA2, [fid], force=True, require_repeat=False, pending=pending)  # type: ignore[arg-type]
        pending.discard(fid)  # the answer (a rejection) arrived

        again = send_frames(
            conn,
            up,
            ARICA2,
            [fid],
            force=True,
            require_repeat=False,
            pending=pending,  # type: ignore[arg-type]
        )

        assert again.queued == 1

    def test_an_accepted_frame_is_marked_and_not_sent_again(self, conn: sqlite3.Connection) -> None:
        fid = _log(conn, HK1, T0)
        up = _FakeUploader()
        mark_uploaded(conn, fid)
        again = send_frames(conn, up, ARICA2, [fid], force=True, require_repeat=False)  # type: ignore[arg-type]
        assert again.queued == 0
        assert up.sent == []


class TestOneTimeRepair:
    def test_marks_written_when_a_frame_was_only_queued_are_cleared_once(
        self, conn: sqlite3.Connection
    ) -> None:
        marked = _log(conn, HK1, T0, uploaded=True)  # the first version's queue-time mark

        reset_unconfirmed_marks(conn)
        assert not _uploaded(conn, marked)

        mark_uploaded(conn, marked)  # a real, accepted upload later
        reset_unconfirmed_marks(conn)  # must not clear it again
        assert _uploaded(conn, marked)

    def test_without_the_tables_it_does_nothing(self) -> None:
        reset_unconfirmed_marks(sqlite3.connect(":memory:"))
