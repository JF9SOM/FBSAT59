"""
FBSAT59 Remote SDR thread diagnostic (GitHub Issue #12).

Tests whether SoapySDR.Device({"driver": "remote", ...}) succeeds when
called from Python's main thread vs. from a background thread
(threading.Thread) - mirroring exactly how FBSAT59 itself calls it
(always from a background thread, so the Connect button in Rig Settings
doesn't freeze the rest of the UI while it connects).

If the connection succeeds on the main thread but fails on the
background thread, that confirms a threading-related cause specific to
how FBSAT59 calls into SoapySDR - not the network, not the remote
server, not the DLL build itself.

Usage:
    python check_soapy_remote_thread_test.py <host> [port]

Requires:
    - Python 3.11.x (matching the Python version FBSAT59's _SoapySDR.pyd
      was built against - a different minor version will fail to import)
    - FBSAT59 already installed. This script borrows FBSAT59's own
      bundled SoapySDR.dll / _SoapySDR.pyd / soapy_modules folder -
      nothing else needs to be installed or configured.

Writes a plain-text report to your Desktop and also prints it to the
console.
"""

from __future__ import annotations

import os
import sys
import threading
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

    log("===== FBSAT59 Remote SDR thread diagnostic =====")
    log(f"Generated: {datetime.now()}")
    log(f"Python:    {sys.version}")
    log("")

    if len(sys.argv) < 2:
        log("Usage: python check_soapy_remote_thread_test.py <host> [port]")
        log("Example: python check_soapy_remote_thread_test.py 192.168.1.81 55132")
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

    # Match src/main.py's own setup exactly: SOAPY_SDR_PLUGIN_PATH env var
    # plus a DLL search directory so the module DLLs can find SoapySDR.dll.
    os.environ["SOAPY_SDR_PLUGIN_PATH"] = str(modules)
    os.add_dll_directory(str(internal))
    sys.path.insert(0, str(internal))

    log("----- Environment -----")
    log(f"SOAPY_SDR_PLUGIN_PATH = {os.environ['SOAPY_SDR_PLUGIN_PATH']}")
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
        log("try again with that version (e.g. 'py -3.11 check_soapy_remote_thread_test.py ...').")
        _save(lines)
        return 1

    log(f"SoapySDR imported OK. API version: {SoapySDR.getAPIVersion()}")
    log("")

    args = {"driver": "remote", "remote": f"{host}:{port}"}

    def attempt(label: str) -> tuple[bool, str]:
        try:
            dev = SoapySDR.Device(args)
        except Exception as exc:
            return False, f"{label}: FAILED - {exc!r}"
        try:
            SoapySDR.Device.unmake(dev)
        except Exception:
            pass  # cleanup failure doesn't change the pass/fail verdict
        return True, f"{label}: SUCCESS"

    log("----- Test 1: SoapySDR.Device() called from the MAIN thread -----")
    ok_main, msg_main = attempt("Main thread")
    log(msg_main)
    log("")

    log("----- Test 2: SoapySDR.Device() called from a BACKGROUND thread -----")
    log("(this mirrors exactly how FBSAT59's own Connect button does it)")
    result: dict[str, tuple[bool, str]] = {}

    def worker() -> None:
        result["bg"] = attempt("Background thread")

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=30)
    if "bg" in result:
        ok_bg, msg_bg = result["bg"]
        log(msg_bg)
    else:
        ok_bg = False
        msg_bg = "Background thread: TIMED OUT (still running after 30s - possible deadlock)"
        log(msg_bg)
    log("")

    log("----- Summary -----")
    log(f"Main thread:       {'OK' if ok_main else 'FAILED'}")
    log(f"Background thread: {'OK' if ok_bg else 'FAILED'}")
    if ok_main and not ok_bg:
        log("")
        log("=> CONFIRMS the thread hypothesis: this connects fine on the main")
        log("   thread but fails specifically when called from a background")
        log("   thread, exactly like FBSAT59's Connect button does.")
    elif ok_main and ok_bg:
        log("")
        log("=> Both succeeded here - the thread hypothesis does NOT explain")
        log("   the FBSAT59 failure by itself. Something else differs.")
    elif not ok_main and not ok_bg:
        log("")
        log("=> Both failed the same way here too - not a threading issue specifically.")
    else:
        log("")
        log("=> Unexpected: background succeeded but main thread failed.")

    _save(lines)
    return 0


def _save(lines: list[str]) -> None:
    desktop = Path(os.environ.get("USERPROFILE", ".")) / "Desktop"
    out = desktop / f"fbsat59_thread_test_{os.getpid()}.txt"
    try:
        out.write_text("\n".join(lines), encoding="utf-8")
        print()
        print(f"Saved report to: {out}")
    except Exception as exc:
        print(f"(could not save report: {exc})")


if __name__ == "__main__":
    sys.exit(main())
