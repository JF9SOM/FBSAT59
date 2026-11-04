"""User-adjustable APRS transmit timing, persisted in ``app_settings``.

Four values shape what goes on the air around one frame:

``ptt_lead_ms``  PTT up -> audio starts (unmodulated carrier)
``txdelay_ms``   flags sent before the frame (inside the audio)
``txtail_ms``    flags sent after the frame (inside the audio)
``ptt_tail_ms``  audio ends -> PTT released (unmodulated carrier)

The defaults reproduce the previously hard-coded behaviour (Direwolf's
TXDELAY 300 / TXTAIL 100 plus 200 ms PTT lead and tail).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from typing import Any

SETTING_KEY = "aprs_tx_timing"
MAX_MS = 2000


@dataclass(frozen=True)
class TxTiming:
    """Transmit timing in milliseconds."""

    ptt_lead_ms: int = 200
    txdelay_ms: int = 300
    txtail_ms: int = 100
    ptt_tail_ms: int = 200

    def clamped(self) -> TxTiming:
        """Copy with every value limited to 0..MAX_MS."""
        return TxTiming(
            **{f.name: min(MAX_MS, max(0, int(getattr(self, f.name)))) for f in fields(self)}
        )


def load_tx_timing(conn: Any) -> TxTiming:
    """Read the saved timing; missing or invalid data gives the defaults."""
    if not hasattr(conn, "execute"):
        return TxTiming()
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (SETTING_KEY,)
        ).fetchone()
        data = json.loads(row["value"]) if row and row["value"] else {}
        known = {f.name for f in fields(TxTiming)}
        return TxTiming(**{k: int(v) for k, v in data.items() if k in known}).clamped()
    except (json.JSONDecodeError, TypeError, ValueError, KeyError):
        return TxTiming()


def save_tx_timing(conn: Any, timing: TxTiming) -> None:
    """Persist *timing* (clamped)."""
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP)",
        (SETTING_KEY, json.dumps(asdict(timing.clamped()))),
    )
    conn.commit()
