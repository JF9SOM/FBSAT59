"""UTC time of the signal being decoded: the wall clock live, the recording's
own clock when an IQ recording is being played back.

A played-back recording is decoded long after it was received, so decoded data
must carry the time it was *received*, not the time of the replay. SdrFileDevice
knows the recording's start time (from the file name or entered by the user)
and its playback position; the two give the time of the data being played.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def replay_time(pipeline: object | None) -> tuple[datetime, bool] | None:
    """(UTC time of the newest data, start time confirmed?) for a played-back recording.

    None when *pipeline* is not playing a recording (a live SDR, a sound card, or
    no pipeline). "Confirmed" is False when the recording's start time is only
    the placeholder SDR Control shows for an unreadable file name.
    """
    device = getattr(pipeline, "_device", None)
    start = getattr(device, "start_time_utc", None)
    position = getattr(device, "position_s", None)
    if not isinstance(start, datetime) or not isinstance(position, int | float):
        return None
    confirmed = getattr(device, "start_time_confirmed", True)
    return start + timedelta(seconds=float(position)), confirmed is not False


def signal_time(pipeline: object | None) -> tuple[datetime, bool]:
    """(UTC time of the newest data, time trustworthy?) -- see replay_time().

    A live input is always trustworthy (the wall clock).
    """
    replay = replay_time(pipeline)
    if replay is not None:
        return replay
    return datetime.now(UTC), True
