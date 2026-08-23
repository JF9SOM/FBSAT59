"""SatDump subprocess manager for METEOR / HRPT reception.

Locates the ``satdump`` executable (system PATH or user-installed) and
manages the ``satdump live`` child process.  Progress lines emitted on
stdout/stderr are forwarded as Qt signals so the UI can display them.
"""

from __future__ import annotations

import re
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
    #
    # Previously offered 137.1 MHz and an 80k symbol rate as extra manual
    # choices per satellite (operators have swapped both in the past --
    # see usradioguy.com/meteor-satellite/ operational logs), but as of
    # 2026-08 neither N2-3 nor N2-4 actually uses them (confirmed with
    # experienced operators). Trimmed down to just the one combination
    # both satellites currently transmit on.
    {
        "label": "METEOR-M N2-3  LRPT  137.9 MHz (72k)",
        "pipeline": "meteor_m2-x_lrpt",
        "frequency": 137_900_000,
        "samplerate": 1_200_000,
        "norad": 57166,
        "xpdr_keyword": "LRPT",
        "xpdr_freq": 137_900_000,
    },
    {
        "label": "METEOR-M N2-4  LRPT  137.9 MHz (72k)",
        "pipeline": "meteor_m2-x_lrpt",
        "frequency": 137_900_000,
        "samplerate": 1_200_000,
        "norad": 59051,
        "xpdr_keyword": "LRPT",
        "xpdr_freq": 137_900_000,
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

# FFT size/rate for the optional --fft_enable waterfall feed (see
# fft_http_port below). Only meant to answer "is a signal present" -- not
# tuned for resolution, so kept small and cheap regardless of pipeline
# sample rate.
_FFT_SIZE = 512
_FFT_RATE = 2


# ---------------------------------------------------------------------------
# SatDump discovery
# ---------------------------------------------------------------------------


def find_satdump() -> Path | None:
    """Return the path to the ``satdump`` executable, or None if not found."""
    # 1. User-installed
    user_dir = _user_satdump_dir()
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


def _user_satdump_dir() -> Path:
    """Return the user-specific directory used for user-installed SatDump."""
    if sys.platform == "win32":
        base = Path.home() / "AppData" / "Roaming" / "fbsat59" / "satdump"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "fbsat59" / "satdump"
    else:
        base = Path.home() / ".local" / "share" / "fbsat59" / "satdump"
    return base


def _send_graceful_stop_windows(pid: int) -> bool:
    """Send CTRL_C_EVENT to *pid* via the console-attach workaround.

    A windowed (console-less) PyInstaller build has no console of its own,
    and Win32's GenerateConsoleCtrlEvent requires the calling process to be
    attached to the *same* console session as the target -- so a plain
    ``Popen.send_signal(signal.CTRL_C_EVENT)`` silently fails here (a known
    CPython limitation: https://github.com/python/cpython/issues/112190).

    This used to send CTRL_BREAK_EVENT instead, on the assumption that the
    Windows CRT wires ``signal(SIGINT, ...)`` up to both CTRL_C_EVENT and
    CTRL_BREAK_EVENT. Checking SatDump's own source
    (``src-cli/live.cpp``, upstream tag 1.2.2) shows it only ever registers
    ``signal(SIGINT, sig_handler_live)`` / ``signal(SIGTERM, ...)`` -- there
    is no CTRL_BREAK_EVENT (SIGBREAK) handler anywhere. Confirmed live on
    Windows 11 (GitHub Issue #27 follow-up, 2026-08-22): a strong, well-
    locked METEOR-M pass with Deframer SYNCED for several minutes still
    produced only a ``.cadu`` file and no image after pressing Stop, with
    the app reporting "satdump exited with code 3221225786" --
    STATUS_CONTROL_C_EXIT (0xC000013A), Windows' default termination code
    for a console-control event nobody handled. SatDump's own log had no
    "Signal Received. Stopping." line at all, confirming the CTRL_BREAK
    event never reached a handler and the OS just killed the process.

    CTRL_C_EVENT does reach ``sig_handler_live`` (it maps to SIGINT), but
    Microsoft's docs are explicit that CTRL+C signals are disabled for any
    process started with CREATE_NEW_PROCESS_GROUP -- so SatDumpProcess.run()
    no longer passes that flag on Windows (CREATE_NO_WINDOW alone still
    gives satdump.exe its own hidden console for AttachConsole() to join
    here).

    GenerateConsoleCtrlEvent broadcasts to *every* process sharing the
    attached console, including this one once AttachConsole() joins it.
    Unlike CTRL_BREAK_EVENT, CTRL_C_EVENT *can* be suppressed for this
    process via SetConsoleCtrlHandler -- registering a handler that returns
    TRUE marks the event handled so the OS's default handler (which would
    otherwise run ExitProcess() on *this* process too) never runs.

    Returns True if the event was raised, False if any step failed (in
    which case the caller should fall back to a hard kill).
    """
    import ctypes
    import ctypes.wintypes
    import time

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    _CTRL_C_EVENT = 0

    handler_routine_t = ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
        ctypes.wintypes.BOOL, ctypes.wintypes.DWORD
    )

    def _swallow_ctrl_event(_ctrl_type: int) -> bool:
        return True  # mark handled so the default handler doesn't ExitProcess() us

    # Keep a reference alive for the whole function -- CFUNCTYPE callback
    # objects are garbage-collected like anything else, and Windows would
    # be calling into freed memory if this one were.
    handler = handler_routine_t(_swallow_ctrl_event)

    kernel32.FreeConsole()  # detach from our own console, if we have one
    if not kernel32.AttachConsole(pid):
        return False
    try:
        if not kernel32.SetConsoleCtrlHandler(handler, True):
            return False
        ok = bool(kernel32.GenerateConsoleCtrlEvent(_CTRL_C_EVENT, 0))
        # Delivery happens asynchronously on a separate OS thread; give it
        # a moment to reach our handler before unregistering below, or an
        # event still in flight could fall through to the default one.
        time.sleep(0.2)
        return ok
    finally:
        kernel32.SetConsoleCtrlHandler(handler, False)
        kernel32.FreeConsole()


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
        True when the Deframer (or generic Lock field) reports SYNCED.
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
        ppm: int = 0,
        agc: bool = False,
        fft_http_port: int | None = None,
        parent: object | None = None,
    ) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._pipeline = pipeline
        self._source = source
        self._frequency = frequency
        self._samplerate = samplerate
        self._output_dir = output_dir
        self._gain = gain
        self._ppm = ppm
        self._agc = agc
        self._fft_http_port = fft_http_port
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
        ]
        if self._agc:
            # rtlsdr_sdr.cpp reads "agc" as a plain bool switch (default
            # false -- confirmed against a real run's "Set RTL-SDR AGC to 0"
            # log line when this flag was never sent). When AGC is enabled
            # the tuner's own hardware AGC drives gain, so a manual --gain
            # value would just be ignored -- omit it entirely rather than
            # send a number that has no effect.
            cmd += ["--agc", "true"]
        else:
            cmd += ["--gain", str(self._gain)]
        if self._ppm:
            # Corrects RTL-SDR local-oscillator drift (commonly tens of ppm
            # on cheap dongles). At 137.9 MHz an uncorrected 50 ppm drift is
            # ~6.9 kHz off-frequency -- easily enough to push the LRPT
            # carrier outside psk_demod's narrow PLL capture range
            # (pll_bw: 0.002) even though the same drift is imperceptible
            # for wideband FM broadcast reception.
            cmd += ["--ppm_correction", str(self._ppm)]
        # Pipelines like meteor_m2-x_lrpt have a "products" work stage
        # (e.g. meteor_msumr_lrpt) *after* the cadu-writing stage that
        # actually turns decoded frames into the MSU-MR PNG(s) -- but SatDump
        # only runs it on a graceful stop if --finish_processing is passed
        # (default false). Without this flag, `live` happily records a
        # perfectly locked, well-decoded .cadu file and then exits without
        # ever producing an image, no matter how the process is stopped.
        cmd += ["--finish_processing", "true"]
        # meteor_m2-x_lrpt's "products" stage can interpolate over black
        # lines caused by interference/signal drop-outs (writes a separate
        # "MSU-MR (Filled)" folder alongside the normal output). Defaults to
        # false upstream; there's no reason not to always ask for it here.
        cmd += ["--fill_missing", "true"]
        if self._fft_http_port is not None:
            # SatDump's own live-processing splitter can feed a local HTTP
            # API with periodic FFT snapshots without disturbing its
            # exclusive hold on the SDR -- see comms.meteor.fft_waterfall,
            # which polls this to drive the METEOR tab's Waterfall view.
            cmd += [
                "--fft_enable",
                "true",
                "--fft_size",
                str(_FFT_SIZE),
                "--fft_rate",
                str(_FFT_RATE),
                "--http_server",
                f"127.0.0.1:{self._fft_http_port}",
            ]

        self.log_line.emit("$ " + " ".join(cmd))

        # satdump.exe is a console-subsystem executable; launched from this
        # windowed/console-less PyInstaller build it would otherwise pop up
        # a visible console window. Closing that window manually then kills
        # the process with Windows' STATUS_CONTROL_C_EXIT code (3221225786
        # / 0xC000013A), which is neither 0 nor -15 and so gets reported as
        # an error below even though the process was just told to close.
        try:
            if sys.platform == "win32":
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    # CREATE_NO_WINDOW still gives satdump.exe (a console
                    # subsystem exe) its own hidden console, which stop()'s
                    # _send_graceful_stop_windows() attaches to in order to
                    # deliver CTRL_C_EVENT. Deliberately NOT combined with
                    # CREATE_NEW_PROCESS_GROUP: Microsoft's docs state that
                    # flag disables CTRL+C signals entirely for the new
                    # process group, and SatDump's own SIGINT handler is the
                    # only thing that triggers its graceful shutdown path
                    # (--finish_processing's image write) -- confirmed via
                    # SatDump's source (src-cli/live.cpp) and live on
                    # Windows 11 (GitHub Issue #27 follow-up, 2026-08-22).
                    creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
                )
            else:
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

        # Drain stdout until SatDump actually exits (the pipe closes), not
        # just until stop() is called. With --finish_processing, a graceful
        # stop keeps producing output (and can take real time) while the
        # image products are written -- bailing out of this loop early on
        # isInterruptionRequested() would both hide that output and, if
        # SatDump keeps writing to a pipe nobody is reading, risk it
        # blocking on its own stdout once the OS pipe buffer fills up.
        assert self._proc.stdout is not None
        stdout: IO[str] = self._proc.stdout
        for line in stdout:
            line = line.rstrip()
            self.log_line.emit(line)
            self._parse_line(line)

        self._proc.wait()
        rc = self._proc.returncode

        if rc == 0 or rc == -15:  # 0 = clean exit, -15 = SIGTERM from stop()
            self.finished_ok.emit()
        else:
            self.finished_err.emit(f"satdump exited with code {rc}")

    def stop(self, force: bool = False) -> None:
        """Request the satdump process to terminate.

        Pass ``force=True`` to send SIGKILL/TerminateProcess immediately —
        used as a last resort when a graceful stop() didn't let run() return
        within a grace period (see MeteorTab.closeEvent()).

        A graceful (non-force) stop lets SatDump run its own shutdown path
        (which, combined with --finish_processing, is what actually writes
        the received image out) instead of being killed mid-write.
        """
        self.requestInterruption()
        if self._proc is None or self._proc.poll() is not None:
            return
        if force:
            self._proc.kill()
            return
        if sys.platform == "win32":
            # Popen.terminate() calls TerminateProcess() on Windows -- an
            # unconditional hard kill, unlike the SIGTERM it sends on
            # POSIX. That gives SatDump no chance to reach its own
            # "Signal Received. Stopping." shutdown path, so
            # --finish_processing's image write never happens either.
            # Fall back to the hard kill only if the graceful path fails.
            if not _send_graceful_stop_windows(self._proc.pid):
                self._proc.terminate()
        else:
            self._proc.terminate()

    # ------------------------------------------------------------------

    def _parse_line(self, line: str) -> None:
        """Extract progress / lock information from a satdump output line."""
        lower = line.lower()

        # Lock detection: SatDump reports sync state as "Deframer : SYNCED"
        # / "Deframer : NOSYNC" on the LRPT/HRPT pipelines used here, or a
        # generic "Lock : SYNCED" / "Lock : NOSYNC" field on some other
        # pipelines. Anchor on the field name followed by a colon so this
        # never matches unrelated messages such as the RTL-SDR tuner's
        # startup "[R82XX] PLL not locked!" warning, which contains "lock"
        # but no "deframer:"/"lock:" field and is unrelated to signal sync.
        m = re.search(r"deframer\s*:\s*(\w+)", lower) or re.search(r"\block\s*:\s*(\w+)", lower)
        if m:
            self.lock_status.emit(m.group(1) == "synced")

        # Progress: look for percentage patterns like "  45%"
        m2 = re.search(r"\b(\d{1,3})\s*%", line)
        if m2:
            pct = int(m2.group(1))
            if 0 <= pct <= 100:
                self.progress.emit(pct)
