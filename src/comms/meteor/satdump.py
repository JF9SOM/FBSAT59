"""SatDump subprocess manager for METEOR / HRPT reception.

Locates the ``satdump`` executable (system PATH or user-installed) and
manages the ``satdump live`` child process.  Progress lines emitted on
stdout/stderr are forwarded as Qt signals so the UI can display them.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import IO

from PySide6.QtCore import QThread, Signal

# ---------------------------------------------------------------------------
# Satellite / pipeline definitions
# ---------------------------------------------------------------------------

METEOR_PIPELINES: list[dict[str, str | int]] = [
    # --- LRPT (137 MHz, RTL-SDR compatible) ---
    {
        "label": "METEOR-M N2-3  LRPT  137.9 MHz",
        "pipeline": "meteor_m2-x_lrpt",
        "frequency": 137_900_000,
        "samplerate": 1_200_000,
        "norad": 57166,
        "xpdr_keyword": "LRPT",
        "xpdr_freq": 137_900_000,
    },
    {
        "label": "METEOR-M N2-4  LRPT  137.1 MHz",
        "pipeline": "meteor_m2-x_lrpt",
        "frequency": 137_100_000,
        "samplerate": 1_200_000,
        "norad": 59051,
        "xpdr_keyword": "LRPT",
        "xpdr_freq": 137_100_000,
    },
    # --- HRPT (1.7 GHz, dish + LNA required) ---
    {
        "label": "METEOR-M N2-3  HRPT  1700.0 MHz",
        "pipeline": "meteor_m2-x_hrpt",
        "frequency": 1_700_000_000,
        "samplerate": 3_000_000,
        "norad": 57166,
        "xpdr_keyword": "HRPT",
        "xpdr_freq": 1_700_000_000,
    },
    {
        "label": "METEOR-M N2-4  HRPT  1700.0 MHz",
        "pipeline": "meteor_m2-x_hrpt",
        "frequency": 1_700_000_000,
        "samplerate": 3_000_000,
        "norad": 59051,
        "xpdr_keyword": "HRPT",
        "xpdr_freq": 1_700_000_000,
    },
    {
        "label": "NOAA 18  HRPT  1707.0 MHz",
        "pipeline": "noaa_hrpt",
        "frequency": 1_707_000_000,
        "samplerate": 3_000_000,
        "norad": 28654,
        "xpdr_keyword": "HRPT",
        "xpdr_freq": 1_707_000_000,
    },
    {
        "label": "NOAA 19  HRPT  1698.0 MHz",
        "pipeline": "noaa_hrpt",
        "frequency": 1_698_000_000,
        "samplerate": 3_000_000,
        "norad": 33591,
        "xpdr_keyword": "HRPT",
        "xpdr_freq": 1_698_000_000,
    },
    {
        "label": "Metop-B  HRPT  1701.3 MHz",
        "pipeline": "metop_hrpt",
        "frequency": 1_701_300_000,
        "samplerate": 3_000_000,
        "norad": 38771,
        "xpdr_keyword": "HRPT",
        "xpdr_freq": 1_701_300_000,
    },
    {
        "label": "Metop-C  HRPT  1701.3 MHz",
        "pipeline": "metop_hrpt",
        "frequency": 1_701_300_000,
        "samplerate": 3_000_000,
        "norad": 43689,
        "xpdr_keyword": "HRPT",
        "xpdr_freq": 1_701_300_000,
    },
]

# NORAD IDs of all supported satellites (METEOR LRPT/HRPT + NOAA + Metop)
METEOR_NORAD_IDS: frozenset[int] = frozenset(
    {35865, 40069, 44387, 57166, 59051, 28654, 33591, 38771, 43689}
)


# ---------------------------------------------------------------------------
# SatDump discovery
# ---------------------------------------------------------------------------


def find_satdump() -> Path | None:
    """Return the path to the ``satdump`` executable, or None if not found."""
    # 1. User-installed
    user_dir = get_user_satdump_dir()
    exe_name = "satdump.exe" if sys.platform == "win32" else "satdump"
    user_exe = user_dir / exe_name
    if user_exe.is_file():
        return user_exe

    # 2. System PATH
    found = shutil.which("satdump")
    if found:
        return Path(found)

    # 3. Standard macOS .app bundle install (SatDump.dmg -> drag to /Applications)
    if sys.platform == "darwin":
        for app_dir in (Path("/Applications"), Path.home() / "Applications"):
            bundle_exe = app_dir / "SatDump.app" / "Contents" / "MacOS" / "satdump"
            if bundle_exe.is_file():
                return bundle_exe

    return None


def get_user_satdump_dir() -> Path:
    """Return the user-specific directory used for user-installed SatDump."""
    if sys.platform == "win32":
        base = Path.home() / "AppData" / "Roaming" / "fbsat59" / "satdump"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "fbsat59" / "satdump"
    else:
        base = Path.home() / ".local" / "share" / "fbsat59" / "satdump"
    return base


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------


class SatDumpProcess(QThread):
    """Runs ``satdump live`` in a background thread and forwards output.

    Signals
    -------
    log_line(str)
        A line of stdout / stderr output from the satdump process.
    progress(int)
        Estimated progress 0-100 parsed from satdump output (best-effort).
    lock_status(bool)
        True when a frame-lock line is detected in the output.
    finished_ok()
        Process exited with code 0 or was stopped cleanly.
    finished_err(str)
        Process exited with a non-zero code or failed to start.
    """

    log_line = Signal(str)
    progress = Signal(int)
    lock_status = Signal(bool)
    finished_ok = Signal()
    finished_err = Signal(str)

    def __init__(
        self,
        pipeline: str,
        source: str,
        frequency: int,
        samplerate: int,
        output_dir: Path,
        gain: int = 40,
        parent: object | None = None,
    ) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._pipeline = pipeline
        self._source = source
        self._frequency = frequency
        self._samplerate = samplerate
        self._output_dir = output_dir
        self._gain = gain
        self._proc: subprocess.Popen[str] | None = None

    # ------------------------------------------------------------------

    def run(self) -> None:
        satdump = find_satdump()
        if satdump is None:
            self.finished_err.emit(
                "satdump executable not found.\n"
                "Please install SatDump and make sure it is on PATH.\n"
                "See Help > SatDump… for instructions."
            )
            return

        self._output_dir.mkdir(parents=True, exist_ok=True)

        # SatDump's `live` CLI takes the output directory as a *positional*
        # argument (argv[3], right after the pipeline id) -- there is no
        # --output flag. Passing it as a flag shifts every subsequent
        # positional/flag pairing, so e.g. `parameters["source"]` ends up
        # unset and SatDump aborts with a JSON type error.
        cmd = [
            str(satdump),
            "live",
            self._pipeline,
            str(self._output_dir),
            "--source",
            self._source,
            "--samplerate",
            str(self._samplerate),
            "--frequency",
            str(self._frequency),
            "--gain",
            str(self._gain),
        ]

        self.log_line.emit("$ " + " ".join(cmd))

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self.finished_err.emit(f"Failed to start satdump: {exc}")
            return

        assert self._proc.stdout is not None
        stdout: IO[str] = self._proc.stdout
        for line in stdout:
            line = line.rstrip()
            self.log_line.emit(line)
            self._parse_line(line)
            if self.isInterruptionRequested():
                break

        self._proc.wait()
        rc = self._proc.returncode

        if rc == 0 or rc == -15:  # 0 = clean exit, -15 = SIGTERM from stop()
            self.finished_ok.emit()
        else:
            self.finished_err.emit(f"satdump exited with code {rc}")

    def stop(self, force: bool = False) -> None:
        """Request the satdump process to terminate.

        Pass ``force=True`` to send SIGKILL immediately instead of SIGTERM —
        used as a last resort when a graceful stop() didn't let run() return
        within a grace period (see MeteorTab.closeEvent()).
        """
        self.requestInterruption()
        if self._proc is not None and self._proc.poll() is None:
            if force:
                self._proc.kill()
            else:
                self._proc.terminate()

    # ------------------------------------------------------------------

    def _parse_line(self, line: str) -> None:
        """Extract progress / lock information from a satdump output line."""
        lower = line.lower()

        # Lock detection
        if "lock" in lower:
            locked = "locked" in lower or "lock!" in lower
            self.lock_status.emit(locked)

        # Progress: look for percentage patterns like "  45%"
        import re

        m = re.search(r"\b(\d{1,3})\s*%", line)
        if m:
            pct = int(m.group(1))
            if 0 <= pct <= 100:
                self.progress.emit(pct)
