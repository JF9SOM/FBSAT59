"""Sending decoded CW telemetry frames to the SatNOGS DB, carefully.

A CW frame has no CRC and the Morse decode of a weak signal is error-prone, so
a frame can pass every plausibility check and still contain a wrong digit. The
SatNOGS DB is shared and public, so nothing is sent on a single, unconfirmed
reading. Rules:

* **Repeated**: the same frame must have been received at least twice, in two
  *different* transmissions (beacon items repeat every minute or so, never
  closer than MIN_REPEAT_GAP_S) within REPEAT_WINDOW_S. Two decodes of the same
  recording do not count as two receptions.
* **Reliable time**: a frame decoded from an IQ recording whose start time was
  only a placeholder (SDR Control's "00:00") carries no usable time and is never
  sent.
* **Once**: a frame already sent, or the same frame at (almost) the same time
  from a repeated replay, is not sent again.

The user can override the *repeated* rule for frames they select themselves
(``require_repeat=False``); the time and once rules always hold.

Everything is recorded in the ``telemetry_log`` table: ``satnogs_uploaded_at``
(when SatNOGS *accepted* the frame -- set by mark_uploaded() from the upload's
result, never when it is merely queued: a rejected upload, e.g. a wrong API key,
must leave the frame unsent) and ``time_reliable``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from comms.telemetry.cw_frames import build_satnogs_frame
from comms.telemetry.satnogs_uploader import SatnogsUploader, upload_blocker

# (frame id, accepted, http status, body) -- the outcome of one frame's upload, see
# SatnogsUploader.submit(on_result=...). Called on the uploader's thread.
FrameResultFn = Callable[[int, bool, int, str], None]

# Two receptions of a frame confirm each other only if this far apart (a repeat of
# the beacon item, not the same transmission decoded twice) ...
MIN_REPEAT_GAP_S = 20.0
# ... and no further apart than this (a frame that recurs a day later confirms nothing).
REPEAT_WINDOW_S = 30 * 60.0
# The same frame this close in time to one already sent is the same reception replayed.
DUPLICATE_WINDOW_S = 10.0


def ensure_columns(conn: sqlite3.Connection) -> None:
    """Add the upload-tracking columns to an existing ``telemetry_log`` table."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(telemetry_log)")}
    if "satnogs_uploaded_at" not in columns:
        conn.execute("ALTER TABLE telemetry_log ADD COLUMN satnogs_uploaded_at TEXT")
    if "time_reliable" not in columns:
        conn.execute(
            "ALTER TABLE telemetry_log ADD COLUMN time_reliable INTEGER NOT NULL DEFAULT 1"
        )
    conn.commit()


@dataclass(frozen=True)
class LoggedFrame:
    """One received CW frame as stored in ``telemetry_log``."""

    id: int
    received_at: datetime
    raw_hex: str
    uploaded: bool
    reliable: bool


@dataclass
class SendReport:
    """What a send attempt did (counts of frames)."""

    queued: int = 0
    duplicates: int = 0  # already sent / replayed reception
    unreliable: int = 0  # time was a placeholder
    pending: int = 0  # already handed to the uploader, no answer yet
    waiting: int = 0  # not yet confirmed by a second reception
    unsupported: int = 0  # no SatNOGS format for the frame
    blocker: str | None = None  # a missing prerequisite, see upload_blocker()


def _parse_time(value: str) -> datetime:
    when = datetime.fromisoformat(value)
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def logged_frames(conn: sqlite3.Connection, norad: int) -> list[LoggedFrame]:
    """All frames logged for *norad*, oldest first."""
    rows = conn.execute(
        "SELECT id, received_at, raw_hex, satnogs_uploaded_at, time_reliable "
        "FROM telemetry_log WHERE norad_cat_id = ? ORDER BY received_at, id",
        (norad,),
    ).fetchall()
    return [
        LoggedFrame(
            id=int(r[0]),
            received_at=_parse_time(str(r[1])),
            raw_hex=str(r[2]),
            uploaded=r[3] is not None,
            reliable=bool(r[4]),
        )
        for r in rows
    ]


def _gap(a: LoggedFrame, b: LoggedFrame) -> float:
    return abs((a.received_at - b.received_at).total_seconds())


def confirmed_by_repeat(frame: LoggedFrame, frames: list[LoggedFrame]) -> bool:
    """True if the same frame was received again in a different transmission."""
    return any(
        other.id != frame.id
        and other.raw_hex == frame.raw_hex
        and MIN_REPEAT_GAP_S <= _gap(frame, other) <= REPEAT_WINDOW_S
        for other in frames
    )


def is_duplicate(frame: LoggedFrame, frames: list[LoggedFrame]) -> bool:
    """True if this frame (or the same reception, replayed) was already sent."""
    return any(
        other.id != frame.id
        and other.uploaded
        and other.raw_hex == frame.raw_hex
        and _gap(frame, other) <= DUPLICATE_WINDOW_S
        for other in frames
    )


def eligible_unsent(conn: sqlite3.Connection, norad: int) -> list[LoggedFrame]:
    """Unsent frames of *norad* that may be sent: reliable time, confirmed by a
    repeat, not a duplicate of one already sent."""
    frames = logged_frames(conn, norad)
    return [
        f
        for f in frames
        if not f.uploaded
        and f.reliable
        and confirmed_by_repeat(f, frames)
        and not is_duplicate(f, frames)
        and build_satnogs_frame(norad, f.raw_hex) is not None
    ]


def mark_uploaded(conn: sqlite3.Connection, frame_id: int) -> None:
    """Record that SatNOGS accepted frame *frame_id*."""
    conn.execute(
        "UPDATE telemetry_log SET satnogs_uploaded_at = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(), frame_id),
    )
    conn.commit()


def reset_unconfirmed_marks(conn: sqlite3.Connection) -> None:
    """One-time repair: forget "sent" marks written when a frame was merely queued.

    The first version marked a frame as sent as soon as it was handed to the
    uploader, so a rejected upload (e.g. HTTP 401, wrong API key) still left it
    marked. No upload was ever confirmed then, so every mark is cleared once.
    """
    key = "cw_upload_marks_v2"
    try:
        if conn.execute("SELECT 1 FROM app_settings WHERE key = ?", (key,)).fetchone():
            return
        conn.execute(
            "UPDATE telemetry_log SET satnogs_uploaded_at = NULL "
            "WHERE satnogs_uploaded_at IS NOT NULL"
        )
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, '1')", (key,))
        conn.commit()
    except sqlite3.OperationalError:
        return  # no app_settings / telemetry_log yet: nothing to repair


def send_frames(
    conn: sqlite3.Connection,
    uploader: SatnogsUploader,
    norad: int,
    ids: list[int],
    *,
    force: bool,
    require_repeat: bool,
    on_result: FrameResultFn | None = None,
    pending: set[int] | None = None,
) -> SendReport:
    """Queue the logged frames *ids* for the SatNOGS DB, honouring the rules above.

    *force* sends although the automatic-upload switch is off (an explicit
    user action); the API key, callsign and location are still required.
    Nothing is marked as sent here: SatNOGS's answer arrives later through
    *on_result*, and the caller calls mark_uploaded() when it was accepted.
    *pending* holds the ids handed to the uploader that have no answer yet; they
    are not queued twice, and each queued id is added to it (the caller removes
    it when the answer arrives).
    """
    report = SendReport(blocker=upload_blocker(conn, force))
    if report.blocker is not None:
        return report
    frames = logged_frames(conn, norad)
    wanted = set(ids)
    for frame in frames:
        if frame.id not in wanted or frame.uploaded:
            continue
        if pending is not None and frame.id in pending:
            report.pending += 1
        elif not frame.reliable:
            report.unreliable += 1
        elif is_duplicate(frame, frames):
            report.duplicates += 1
        elif require_repeat and not confirmed_by_repeat(frame, frames):
            report.waiting += 1
        else:
            wire = build_satnogs_frame(norad, frame.raw_hex)
            if wire is None:
                report.unsupported += 1
                continue
            callback = _frame_callback(frame.id, on_result)
            if uploader.submit(
                conn, wire, norad, frame.received_at, force=force, on_result=callback
            ):
                if pending is not None:
                    pending.add(frame.id)
                report.queued += 1
    return report


def _frame_callback(
    frame_id: int, on_result: FrameResultFn | None
) -> Callable[[bool, int, str], None] | None:
    if on_result is None:
        return None

    def report(accepted: bool, status: int, body: str) -> None:
        on_result(frame_id, accepted, status, body)

    return report


def auto_send(
    conn: sqlite3.Connection,
    uploader: SatnogsUploader,
    norad: int,
    frame_id: int,
    on_result: FrameResultFn | None = None,
    pending: set[int] | None = None,
) -> SendReport:
    """Automatic upload after frame *frame_id* was logged (footer switch on).

    A frame is only sent once a repeat confirms it; receiving the repeat also
    releases the earlier reading(s) of the same frame that were waiting.
    """
    frames = logged_frames(conn, norad)
    this = next((f for f in frames if f.id == frame_id), None)
    if this is None:
        return SendReport()
    group = [f.id for f in frames if f.raw_hex == this.raw_hex and not f.uploaded]
    return send_frames(
        conn,
        uploader,
        norad,
        group,
        force=False,
        require_repeat=True,
        on_result=on_result,
        pending=pending,
    )
