"""
FBSAT59 Remote SDR verbose-log diagnostic (GitHub Issue #12).

The previous thread-comparison test (check_soapy_remote_thread_test.py)
ruled out threading: SoapySDR.Device({"driver": "remote", ...}) fails with
"no match" identically on the main thread and a background thread, in a
plain Python script with no FBSAT59/Qt code involved at all.

"no match" specifically means SoapySDR's internal Registry has no
"remote" driver key registered at all when Device::make() looks for one
- but SoapySDR only logs an error for a module if loading it actually
produced one (a "duplicate entry" or "failed ABI check" message). A
clean load with nothing wrong produces no log line at any of the default
log levels, so we've had no visibility into what's actually happening
inside remoteSupport.dll's own connection attempt.

This script does two things differently:
  1. Sets SOAPY_SDR_LOG_LEVEL=TRACE (the most verbose level) *before*
     importing SoapySDR, and registers a Python callback that captures
     literally every log message SoapySDR produces, including the
     DEBUG-level lines SoapyRemote's client code writes for every
     connection attempt (e.g. "SoapyClient querying devices for ...",
     "SoapyRemote::find() -- connect(...) FAIL: ...") that were
     completely invisible before (SoapySDR's default level is INFO,
     which silently drops anything at DEBUG or below).
  2. After the attempt, checks whether ANY captured line mentions
     "SoapyClient querying devices" or "SoapyRemote::find" - if that
     never appears, remoteSupport.dll's "remote" driver was never even
     queried, which means it isn't registered in SoapySDR's driver
     registry in this process at all (a registration/build problem, not
     a network problem). If it *does* appear, the driver clearly is
     registered, and whatever specific reason logged next to it is the
     real cause of the connection failure.

Usage:
    python check_soapy_remote_verbose_log.py <host> [port]

Requires:
    - Python 3.11.x (matching FBSAT59's bundled _SoapySDR.pyd)
    - FBSAT59 already installed (reuses its bundled SoapySDR files)

Writes a plain-text report to your Desktop.
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


def find_install_dir() -> Path | None:
    candidates: list[Path] = []
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"Software\FBSAT59") as key:
            val, _ = winreg.QueryValueEx(key, "InstallDir")
            candidates.append(Path(val))
    except OSError:
        pass
    candidates.append(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "FBSAT59")
    for c in candidates:
        if (c / "_internal" / "SoapySDR.dll").exists():
            return c
    return None


def main() -> int:
    lines: list[str] = []

    def log(msg: str = "") -> None:
        print(msg)
        lines.append(msg)

    log("===== FBSAT59 Remote SDR verbose-log diagnostic =====")
    log(f"Generated: {datetime.now()}")
    log(f"Python:    {sys.version}")
    log("")

    if len(sys.argv) < 2:
        log("Usage: python check_soapy_remote_verbose_log.py <host> [port]")
        log("Example: python check_soapy_remote_verbose_log.py 192.168.1.81 55132")
        _save(lines)
        return 1

    host = sys.argv[1]
    port = sys.argv[2] if len(sys.argv) > 2 else "55132"

    install_dir = find_install_dir()
    log("----- FBSAT59 install location -----")
    if install_dir is None:
        log("Could not find FBSAT59 install (checked registry and Program Files).")
        log("Make sure FBSAT59 is installed, then try again.")
        _save(lines)
        return 1
    log(f"Found: {install_dir}")
    log("")

    internal = install_dir / "_internal"
    modules = internal / "soapy_modules"

    os.environ["SOAPY_SDR_PLUGIN_PATH"] = str(modules)
    # Most verbose level, read once by SoapySDR at DLL-load time - must be
    # set before "import SoapySDR" triggers that load.
    os.environ["SOAPY_SDR_LOG_LEVEL"] = "TRACE"
    os.add_dll_directory(str(internal))
    sys.path.insert(0, str(internal))

    log("----- Environment -----")
    log(f"SOAPY_SDR_PLUGIN_PATH = {os.environ['SOAPY_SDR_PLUGIN_PATH']}")
    log(f"SOAPY_SDR_LOG_LEVEL   = {os.environ['SOAPY_SDR_LOG_LEVEL']}")
    log(f"DLL search dir added  = {internal}")
    log("")

    try:
        import SoapySDR
    except Exception:
        log("FAILED to import SoapySDR:")
        log(traceback.format_exc())
        log("")
        log("This usually means the installed Python version doesn't match")
        log("the one FBSAT59 was built with (Python 3.11). Please install a")
        log("64-bit Python 3.11.x from https://www.python.org/downloads/ and")
        log("try again with that version.")
        _save(lines)
        return 1

    log(f"SoapySDR imported OK. API version: {SoapySDR.getAPIVersion()}")
    log("")

    captured: list[str] = []
    _LEVEL_NAMES = {
        1: "FATAL",
        2: "CRITICAL",
        3: "ERROR",
        4: "WARNING",
        5: "NOTICE",
        6: "INFO",
        7: "DEBUG",
        8: "TRACE",
        9: "SSI",
    }

    def on_log(level: int, message: str) -> None:
        name = _LEVEL_NAMES.get(level, str(level))
        captured.append(f"  [{name}] {message}")

    SoapySDR.registerLogHandler(on_log)

    args = {"driver": "remote", "remote": f"{host}:{port}"}
    log(f"----- Attempting SoapySDR.Device({args}) -----")
    log("(capturing every SoapySDR log message, including ones normally hidden)")
    log("")

    ok = False
    try:
        dev = SoapySDR.Device(args)
        ok = True
        try:
            SoapySDR.Device.unmake(dev)
        except Exception:
            pass
    except Exception as exc:
        log(f"RESULT: FAILED - {exc!r}")

    if ok:
        log("RESULT: SUCCESS")
    log("")

    log("----- Captured SoapySDR log output -----")
    if captured:
        for line in captured:
            log(line)
    else:
        log("  (no log messages were captured at all)")
    log("")

    log("----- Automated verdict -----")
    queried = any(
        "SoapyClient querying devices" in c or "SoapyRemote::find" in c for c in captured
    )
    if queried:
        log("The 'remote' driver WAS queried (a SoapyClient/SoapyRemote log line")
        log("appears above) - it is registered correctly. Whatever reason is")
        log("logged next to that line above is the actual cause of the failure.")
    else:
        log("The 'remote' driver was NEVER queried - no 'SoapyClient querying")
        log("devices' or 'SoapyRemote::find' line appears anywhere above, even")
        log("at TRACE level. This means remoteSupport.dll's driver registration")
        log("never actually ran in this process, despite the DLL loading with")
        log("no error. This points to something in how remoteSupport.dll itself")
        log("was built (its self-registering static initializer never executing)")
        log("rather than a network or server-side problem.")

    _save(lines)
    return 0


def _save(lines: list[str]) -> None:
    desktop = Path(os.environ.get("USERPROFILE", ".")) / "Desktop"
    out = desktop / f"fbsat59_verbose_log_{os.getpid()}.txt"
    try:
        out.write_text("\n".join(lines), encoding="utf-8")
        print()
        print(f"Saved report to: {out}")
    except Exception as exc:
        print(f"(could not save report: {exc})")


if __name__ == "__main__":
    sys.exit(main())
