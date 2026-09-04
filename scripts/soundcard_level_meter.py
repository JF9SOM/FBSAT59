#!/usr/bin/env python3
"""Live input level meter for diagnosing the FT4 sound card capture path.

Run this while the rig speaker is producing audio (e.g. during an RS-44
FT4 pass) to see in real time whether any signal reaches the PC via the
selected input device. Ctrl+C to stop.

Usage:
    .venv/bin/python scripts/soundcard_level_meter.py [device_index]

With no argument, uses the same input_device_index currently saved in
FBSAT59's soundcard_settings (read from the app's SQLite DB).
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

try:
    from platformdirs import user_data_dir

    _DB_PATH = Path(user_data_dir("fbsat59")) / "fbsat59.db"
except ImportError:
    _DB_PATH = Path.home() / ".local/share/fbsat59/fbsat59.db"


def _default_device() -> int | None:
    if not _DB_PATH.exists():
        return None
    try:
        import json

        conn = sqlite3.connect(str(_DB_PATH))
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'soundcard_settings'"
        ).fetchone()
        conn.close()
        if row:
            data = json.loads(row[0])
            return data.get("input_device_index")
    except Exception:
        pass
    return None


def main() -> None:
    device = int(sys.argv[1]) if len(sys.argv) > 1 else _default_device()
    info = (
        sd.query_devices(device, "input") if device is not None else sd.query_devices(kind="input")
    )
    print(f"Using input device: {device} -> {info['name']}")
    print("Listening... (Ctrl+C to stop). Bar should move when rig audio plays.\n")

    def callback(indata: np.ndarray, frames: int, time_info: object, status: object) -> None:
        if status:
            print(status, file=sys.stderr)
        rms = float(np.sqrt(np.mean(indata[:, 0].astype(np.float64) ** 2)))
        db = 20 * np.log10(rms + 1e-12)
        bar_len = max(0, min(60, int((db + 60) * 1.0)))
        bar = "#" * bar_len
        print(f"\r{db:6.1f} dBFS |{bar:<60}|", end="", flush=True)

    with sd.InputStream(
        device=device,
        channels=1,
        samplerate=48000,
        dtype="float32",
        callback=callback,
    ):
        try:
            while True:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
