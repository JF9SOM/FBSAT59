#!/usr/bin/env python3
"""Diagnostic tool: does resending the P command with an unchanged azimuth
but a changing elevation cause the rotator to stutter (stop briefly on
every resend) instead of slewing azimuth smoothly to its target?

Context: HamlibRotatorController._try_fast_pass_intercept() (see
docs/hamlib.md "高仰角（天頂通過）パスの『迎撃』先読み") sends a single P
command and then only polls position until azimuth arrives, freezing
elevation at whatever value the intercept point happened to have. Real-world
logs (2026-09-25 KOSEN-2R, 2026-09-29 BY70-4) show this leaves elevation
pinned tens of degrees away from the satellite's real elevation for the
whole azimuth transit. The proposed fix is to keep resending P every cycle
with the SAME azimuth target but a live-tracking elevation. Before writing
that into the app, this script measures on real hardware whether repeating
the P command (even with an unchanged azimuth) makes the SkyWatcher
backend's azimuth motion stall or restart each time, which would rule the
fix out.

Reads rotator connection settings (port/baud/model) from the app's own
SQLite DB (same rotator_settings key MainWindow uses) so this exercises the
exact same Hamlib backend HamlibRotatorController uses — no protocol
reimplementation, no changes to the app itself.

SAFETY: this physically moves the connected rotator. Run it only while
watching the antenna, with a clear azimuth range in both directions. It
moves --distance-deg away from the rotator's current position, then all
the way back, twice (once per phase). Ctrl+C at any time sends a stop and
exits; the rotator does not return to its start position automatically
if interrupted.

Usage:
    python3 scripts/test_rotator_resend_stutter.py --distance-deg 40 --direction 1

Output: a CSV of timestamped (phase, event, az, el) samples printed to
stdout and saved under /tmp, plus a plain-language summary of the
per-interval azimuth slew rate for phase A (single P, no resends) vs phase
B (resent every cycle) so a stutter would show up as near-zero movement in
the seconds right after each resend in phase B but not in phase A.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from rig.controller import HamlibRotatorController  # noqa: E402

DB_CANDIDATES = [
    Path.home() / "Library" / "Application Support" / "FBSAT59" / "fbsat59.db",
    Path.home() / "Library" / "Application Support" / "fbsat59" / "fbsat59.db",
    Path.home() / ".local" / "share" / "fbsat59" / "fbsat59.db",
]


def _load_rotator_settings() -> dict:
    for db_path in DB_CANDIDATES:
        if not db_path.exists():
            continue
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = 'rotator_settings'"
            ).fetchone()
        finally:
            conn.close()
        if row:
            return json.loads(row[0])
    raise SystemExit(f"rotator_settings not found in any of: {DB_CANDIDATES}")


def _make_controller(settings: dict) -> HamlibRotatorController:
    net_mode = settings.get("mode") == "net"
    return HamlibRotatorController(
        model_id=int(settings["model_id"]),
        port=settings.get("port", "/dev/ttyUSB0"),
        baud_rate=int(settings.get("baud_rate", 9600)),
        net_mode=net_mode,
        net_host=settings.get("host", "localhost"),
        net_port=int(settings.get("net_port", 4533)),
    )


def _wrap360(deg: float) -> float:
    return deg % 360.0


def _poll(ctrl: HamlibRotatorController, phase: str, event: str, rows: list, t0: float) -> None:
    pos = ctrl.get_position()
    rows.append(
        {
            "t": round(time.monotonic() - t0, 2),
            "phase": phase,
            "event": event,
            "az": pos.azimuth_deg,
            "el": pos.elevation_deg,
        }
    )
    t, az, el = rows[-1]["t"], pos.azimuth_deg, pos.elevation_deg
    print(f"  t={t:6.2f}s  {event:14s}  az={az:6.1f}  el={el:5.1f}")


def run_phase_a(
    ctrl: HamlibRotatorController, target_az: float, el: float, poll_s: float, rows: list, t0: float
) -> None:
    """Baseline: exactly what _try_fast_pass_intercept() does today — one P
    command, then only poll (never resend) until the caller decides arrival."""
    print(f"\n--- Phase A (single P, no resend): az -> {target_az:.1f} el={el:.1f} ---")
    ctrl._send_p(target_az, el)  # noqa: SLF001 - intentionally exercising the low-level primitive
    _poll(ctrl, "A", "sent", rows, t0)
    last_az = None
    stall_ticks = 0
    while True:
        time.sleep(poll_s)
        _poll(ctrl, "A", "poll", rows, t0)
        az = rows[-1]["az"]
        if last_az is not None and abs(_ang_diff(az, last_az)) < 0.15:
            stall_ticks += 1
        else:
            stall_ticks = 0
        last_az = az
        if abs(_ang_diff(az, target_az)) <= 3.0:
            print("  arrived.")
            break
        if stall_ticks * poll_s > 15.0:
            print("  WARNING: no motion for 15s, aborting phase A.")
            break


def run_phase_b(
    ctrl: HamlibRotatorController,
    target_az: float,
    el_start: float,
    el_end: float,
    resend_interval_s: float,
    poll_s: float,
    rows: list,
    t0: float,
) -> None:
    """Candidate fix: resend P every resend_interval_s with the SAME target
    azimuth but a stepped-down elevation (simulating live tracking), polling
    frequently in between to see whether azimuth motion pauses right after
    each resend."""
    print(
        f"\n--- Phase B (resend every {resend_interval_s:.1f}s, same az, live el): "
        f"az -> {target_az:.1f} el {el_start:.1f}->{el_end:.1f} ---"
    )
    n_steps = max(1, int(30.0 / resend_interval_s))
    el_step = (el_end - el_start) / n_steps
    el = el_start
    ctrl._send_p(target_az, el)  # noqa: SLF001
    _poll(ctrl, "B", "sent", rows, t0)
    last_az = None
    stall_ticks = 0
    elapsed = 0.0
    while True:
        next_resend = resend_interval_s
        step_start = elapsed
        while elapsed - step_start < next_resend:
            time.sleep(poll_s)
            elapsed += poll_s
            _poll(ctrl, "B", "poll", rows, t0)
            az = rows[-1]["az"]
            if last_az is not None and abs(_ang_diff(az, last_az)) < 0.15:
                stall_ticks += 1
            else:
                stall_ticks = 0
            last_az = az
            if abs(_ang_diff(az, target_az)) <= 3.0:
                print("  arrived.")
                return
            if stall_ticks * poll_s > 15.0:
                print("  WARNING: no motion for 15s, aborting phase B.")
                return
        el = max(min(el + el_step, 90.0), 0.0)
        ctrl._send_p(target_az, el)  # noqa: SLF001
        _poll(ctrl, "B", "resend", rows, t0)


def _ang_diff(a: float, b: float) -> float:
    d = (a - b + 180.0) % 360.0 - 180.0
    return d


def summarize(rows: list) -> None:
    print("\n=== Summary: azimuth movement per poll interval right after each event ===")
    for phase in ("A", "B"):
        prows = [r for r in rows if r["phase"] == phase]
        if len(prows) < 2:
            continue
        print(f"\nPhase {phase}:")
        for i in range(1, len(prows)):
            dt = prows[i]["t"] - prows[i - 1]["t"]
            if dt <= 0:
                continue
            d_az = abs(_ang_diff(prows[i]["az"], prows[i - 1]["az"]))
            rate = d_az / dt
            marker = (
                "  <-- right after resend" if prows[i - 1]["event"] in ("sent", "resend") else ""
            )
            print(
                f"  {prows[i - 1]['event']:8s}->{prows[i]['event']:8s}  {rate:5.2f} deg/s{marker}"
            )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--distance-deg", type=float, default=40.0, help="how far to move each phase")
    ap.add_argument("--direction", type=int, choices=(1, -1), default=1, help="+1 or -1")
    ap.add_argument("--el-start", type=float, default=60.0)
    ap.add_argument("--el-end", type=float, default=20.0)
    ap.add_argument(
        "--resend-interval", type=float, default=4.0, help="seconds, matches rotator_cycle_ms"
    )
    ap.add_argument("--poll-interval", type=float, default=0.5)
    args = ap.parse_args()

    settings = _load_rotator_settings()
    print(f"Rotator settings from DB: {settings}")
    ctrl = _make_controller(settings)
    if not ctrl.connect():
        raise SystemExit("Could not open rotator connection.")

    rows: list = []
    t0 = time.monotonic()
    try:
        origin = ctrl.get_position()
        print(f"Starting position: az={origin.azimuth_deg:.1f} el={origin.elevation_deg:.1f}")
        target_a = _wrap360(origin.azimuth_deg + args.direction * args.distance_deg)
        run_phase_a(ctrl, target_a, args.el_start, args.poll_interval, rows, t0)

        target_b = _wrap360(origin.azimuth_deg)  # phase B returns toward the start
        run_phase_b(
            ctrl,
            target_b,
            args.el_start,
            args.el_end,
            args.resend_interval,
            args.poll_interval,
            rows,
            t0,
        )
    except KeyboardInterrupt:
        print("\nInterrupted — stopping rotator.")
        ctrl.stop()
    finally:
        summarize(rows)
        out_path = Path("/tmp/rotator_resend_stutter_test.csv")
        with out_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["t", "phase", "event", "az", "el"])
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nRaw samples written to {out_path}")
        ctrl.disconnect()


if __name__ == "__main__":
    main()
