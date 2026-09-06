"""
FBSAT59 application entry point.

Startup sequence:
    1. Create QApplication
    2. Show splash screen (immediate feedback while the steps below run)
    3. Initialize SQLite DB
    4. Create TLEManager, LocationManager, SatelliteEngine, PassPredictor
    5. Create FastAPI app
    6. Show MainWindow (web server and scheduler start internally), close splash
    7. Run Qt event loop
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# In a PyInstaller bundle the Python SSL CA bundle is not present on the system
# path.  Point httpx (and the stdlib ssl module) at the certifi bundle that was
# collected into the frozen archive so that HTTPS requests succeed on all
# platforms (critical for TLE/SATNOGS downloads on macOS and Windows).
if getattr(sys, "frozen", False):
    try:
        import certifi

        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
        os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    except Exception:
        pass

# macOS: apps launched from Finder or a .app wrapper (as opposed to an
# interactive login shell) inherit a minimal PATH that omits Homebrew and
# MacPorts bin directories.  Any external tool the app shells out to --
# direwolf, rigctld, satdump -- then appears "not installed" even when it is
# on the PATH the user sees in their terminal.  Prepend the standard package-
# manager bin directories that actually exist so subprocess lookups
# (shutil.which) match the user's interactive shell.  Runs for both frozen and
# source launches since either can be started with a stripped-down PATH.
if sys.platform == "darwin":
    _pkg_mgr_bin_dirs = (
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/local/sbin",
        "/opt/local/bin",
        "/opt/local/sbin",
    )
    _cur_path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    _missing_path_dirs = [
        d for d in _pkg_mgr_bin_dirs if os.path.isdir(d) and d not in _cur_path_dirs
    ]
    if _missing_path_dirs:
        os.environ["PATH"] = os.pathsep.join(_missing_path_dirs + _cur_path_dirs)

# On the developer's Linux machine, ensure only Hamlib 4.7.x is loaded and not
# the older system package (4.5.5).  Loading both causes a "Hash collision"
# fatal error in Hamlib's rig registry.  This block is a no-op when running
# from a PyInstaller bundle or on Windows/macOS where /opt/hamlib does not exist.
# If the user installed a newer Hamlib via the in-app updater, load it first
# so it takes priority over the bundled version on all platforms.
try:
    from platformdirs import user_data_dir as _udd

    _hamlib_user_dir = Path(_udd("fbsat59")) / "hamlib"
except Exception:
    _hamlib_user_dir = Path.home() / ".local" / "share" / "fbsat59" / "hamlib"
if _hamlib_user_dir.exists():
    _hamlib_user_str = str(_hamlib_user_dir)
    if _hamlib_user_str not in sys.path:
        sys.path.insert(0, _hamlib_user_str)
    # Windows: register the directory for DLL loading so _Hamlib.pyd finds
    # hamlib.dll and its dependencies placed in the same flat directory.
    if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
        os.add_dll_directory(_hamlib_user_str)

# Windows frozen bundle: tell SoapySDR where to find device-module DLLs.
# Must be set before any 'import SoapySDR' occurs.
# Force-override any pre-existing SOAPY_SDR_PLUGIN_PATH (e.g. from PothosSDR
# or a system-level environment variable) so that only our bundled modules are
# loaded — a user's path may contain an unpatched rtlsdrSupport.dll that would
# cause Device::make() to register a second factory and fail.
if sys.platform == "win32" and getattr(sys, "frozen", False):
    _soapy_modules = Path(getattr(sys, "_MEIPASS", "")) / "soapy_modules"
    if _soapy_modules.exists():
        os.environ["SOAPY_SDR_PLUGIN_PATH"] = str(_soapy_modules)
    # Add _internal/ to DLL search path so rtlsdrSupport.dll (in soapy_modules/)
    # can find SoapySDR.dll and rtlsdr.dll at load time.
    _mei = Path(getattr(sys, "_MEIPASS", ""))
    if _mei.exists() and hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(_mei))

# Windows source checkout (NOT frozen): SoapySDR is not a pip package on
# Windows, so `import SoapySDR` normally fails when running from source and the
# whole SDR subsystem (device enumeration included) stays disabled.  If an
# installed FBSAT59 build is present, borrow its already-assembled SoapySDR
# bundle (core .pyd/.dll + soapy_modules/ + rtlsdr.dll/hackrf.dll), which is
# built for this same CPython 3.11 x86-64 ABI.  This block is the non-frozen
# counterpart of the frozen one above and never runs inside the shipped .exe
# (getattr(sys, "frozen", False) is True there) nor when SoapySDR is already
# importable (e.g. a dev venv that installed it by hand).
if (
    sys.platform == "win32"
    and not getattr(sys, "frozen", False)
    and importlib.util.find_spec("SoapySDR") is None
):
    _program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    _installed_internal = Path(_program_files) / "FBSAT59" / "_internal"
    if (_installed_internal / "SoapySDR.py").exists():
        if str(_installed_internal) not in sys.path:
            sys.path.append(str(_installed_internal))  # append: never shadow venv packages
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(_installed_internal))
        _borrowed_modules = _installed_internal / "soapy_modules"
        if _borrowed_modules.exists():
            os.environ.setdefault("SOAPY_SDR_PLUGIN_PATH", str(_borrowed_modules))

# Windows subprocess enumerate worker.
# SdrDevice.enumerate() on Windows spawns this process with --_gpredict_soapy_enum
# so that a C-level crash (e.g. RTL-SDR with libusbK driver) cannot kill the
# main Qt application.  Must run before any Qt or heavy library import.
if sys.platform == "win32" and len(sys.argv) >= 2 and sys.argv[1] == "--_gpredict_soapy_enum":
    import contextlib
    import json as _json

    # Frozen bundle only: _MEIPASS is unset when this worker runs from a source
    # checkout, and Path("") resolves to "." — os.add_dll_directory(".") raises
    # OSError, so guard on the string being non-empty.
    _mei_str = getattr(sys, "_MEIPASS", "")
    if _mei_str and Path(_mei_str).exists() and hasattr(os, "add_dll_directory"):
        os.add_dll_directory(_mei_str)
    try:
        import SoapySDR as _s_enum

        _enum_out: list[dict] = []
        for _kw in _s_enum.Device.enumerate():
            _d: dict = {}
            for _k in _kw:
                with contextlib.suppress(Exception):
                    _d[_k] = str(_kw[_k])
            _enum_out.append(_d)
        print(_json.dumps(_enum_out), flush=True)
    except Exception:
        print("[]", flush=True)
    sys.exit(0)

if sys.platform == "linux":
    _pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    _HAMLIB_SITE = f"/opt/hamlib/4.7/lib/python{_pyver}/site-packages"
    _HAMLIB_SYS = "/usr/lib/python3/dist-packages"
    # Only apply the sys.path surgery when the custom Hamlib build is present.
    # On standard installations /usr/lib/python3/dist-packages is the only
    # Hamlib source, so removing it would break both Hamlib and SoapySDR.
    if os.path.exists(_HAMLIB_SITE):
        # Pre-import SoapySDR before stripping _HAMLIB_SYS so it stays in
        # sys.modules (SoapySDR lives in the same dist-packages directory).
        import contextlib

        with contextlib.suppress(Exception):
            import SoapySDR as _soapy_preload  # noqa: F401
        with contextlib.suppress(Exception):
            import serial as _serial_preload  # noqa: F401
        if _HAMLIB_SYS in sys.path:
            sys.path.remove(_HAMLIB_SYS)
        if _HAMLIB_SITE not in sys.path:
            sys.path.insert(0, _HAMLIB_SITE)

# End-user Linux AppImage: expose the system's apt/zypper-installed Python
# packages (SoapySDR, its device modules, etc.) to the frozen interpreter.
# PyInstaller's bootloader only puts the bundled _MEIPASS libraries on
# sys.path — it does not automatically see the host's dist-packages/
# site-packages, so "apt install python3-soapysdr" alone is not enough
# (GitHub issue #11). Append (not insert) so bundled modules still win if a
# name collides; only import-misses fall through to these paths. This is
# independent of the /opt/hamlib block above, which is a developer-machine-
# only concern and never applies to a packaged AppImage.
if sys.platform == "linux" and getattr(sys, "frozen", False):
    _pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    for _sys_site in (
        "/usr/lib/python3/dist-packages",  # Debian/Ubuntu
        f"/usr/lib/python{_pyver}/dist-packages",
        "/usr/lib/python3/site-packages",  # openSUSE/Fedora
        f"/usr/lib/python{_pyver}/site-packages",
    ):
        if os.path.isdir(_sys_site) and _sys_site not in sys.path:
            sys.path.append(_sys_site)

# macOS frozen bundle: tell SoapySDR where to find device-module dylibs.
# Must be set before any 'import SoapySDR' occurs. SoapySDR.py/_SoapySDR*.so
# themselves need no sys.path change — PyInstaller already puts _MEIPASS
# (where the spec's darwin soapy_binaries block places them, flat at ".")
# on sys.path by default, same as the Windows block above. This bundle was
# extracted from conda-forge pinned to Python 3.11 (matching this app's
# bundled interpreter exactly) and rpath-fixed by dylibbundler in CI — see
# extract_soapy_conda_macos.py. Unlike Homebrew's own SoapySDR (built
# against whatever python3 Homebrew currently has, e.g. 3.14 — a different
# CPython ABI that produced a corrupt-looking SWIG std::map TypeError when
# loaded into this app's bundled 3.11 interpreter, diagnosed 2026-08-01),
# this bundle's Python ABI is guaranteed to match at build time.
if sys.platform == "darwin" and getattr(sys, "frozen", False):
    _soapy_modules_macos = Path(getattr(sys, "_MEIPASS", "")) / "soapy_modules"
    if _soapy_modules_macos.exists():
        os.environ["SOAPY_SDR_PLUGIN_PATH"] = str(_soapy_modules_macos)

# End-user macOS .app: expose Homebrew's Python site-packages (SoapySDR,
# its device modules, etc.) to the frozen interpreter as a last-resort
# fallback, in case the bundle above is missing (e.g. a build predating it,
# or the CI bundling step failed for some device). Same root cause as the
# Linux AppImage block above (GitHub issue #11) — PyInstaller's bootloader
# only puts the bundled _MEIPASS libraries on sys.path by default. Append
# (not insert) so the bundled SoapySDR above always wins if both are
# present — this fallback path is exactly the one that produced the CPython
# ABI mismatch noted above, so it is deliberately lowest priority, not a
# preferred source. The Homebrew prefix differs by architecture
# (/opt/homebrew on Apple Silicon, /usr/local on Intel) and Homebrew's own
# python3 version changes over time — glob for whatever
# "lib/pythonX.Y/site-packages" actually exists under either prefix rather
# than hardcoding a specific version or architecture.
if sys.platform == "darwin" and getattr(sys, "frozen", False):
    import glob as _glob

    for _brew_prefix in ("/opt/homebrew", "/usr/local"):
        for _sys_site in _glob.glob(f"{_brew_prefix}/lib/python3.*/site-packages"):
            if os.path.isdir(_sys_site) and _sys_site not in sys.path:
                sys.path.append(_sys_site)

# On Linux the AppImage does not bundle IBus/fcitx Qt IM plugins, so those
# input methods intercept keystrokes before Qt widgets receive them.  Falling
# back to XIM (the basic X11 input protocol) requires no extra plugins and
# restores normal keyboard input.  Skip if the user has already set the var.
if sys.platform.startswith("linux") and "QT_IM_MODULE" not in os.environ:
    os.environ["QT_IM_MODULE"] = "xim"

# Qt6 / libxkbcommon XKB fix: AppImages running on non-Ubuntu distros (e.g.
# openSUSE) with non-Latin keyboard layouts (Japanese etc.) log
# "qt.qpa.keymapper: no keyboard layouts with latin keys present" and drop all
# key input.  The bundled libxkbcommon cannot find the host XKB config files.
# Set both QT_XKB_CONFIG_ROOT (Qt6) and XKB_CONFIG_ROOT (libxkbcommon) to the
# host path before QApplication is created.  Try standard locations in order;
# use the first one that exists.  Skip if the user has already set the vars.
if sys.platform.startswith("linux"):
    for _xkb_path in ("/usr/share/X11/xkb", "/usr/share/xkb"):
        if os.path.isdir(_xkb_path):
            os.environ.setdefault("QT_XKB_CONFIG_ROOT", _xkb_path)
            os.environ.setdefault("XKB_CONFIG_ROOT", _xkb_path)
            break

from PySide6.QtCore import QLockFile, Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QMessageBox, QSplashScreen

from core.engine import PassPredictor, SatelliteEngine
from core.location import LocationManager, LocationSource
from data.database import init_database
from data.tle_manager import TLEManager
from ui.main_window import MainWindow
from ui.world_map import prefetch_land_data
from web.app import create_app
from web.rig_state import RigWebState


def _setup_logging() -> None:
    """Configure logging: always write to stderr and a log file."""
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    from platformdirs import user_log_dir

    log_dir = user_log_dir("fbsat59", "fbsat59")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "fbsat59.log")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(fmt))
    handlers: list[logging.Handler] = [logging.StreamHandler(), file_handler]
    print(f"[FBSAT59] Log file: {log_path}", file=sys.stderr)

    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


_setup_logging()
logger = logging.getLogger(__name__)


def _migrate_legacy_data() -> None:
    """Migrate data from the legacy GPredict-Improved directory to the current FBSAT59 directory.

    Called once at startup before init_database().  Safe to call on every
    launch: if the legacy directory does not exist, or if the new DB is
    already populated, the function returns immediately without touching
    anything.

    Platform paths
    --------------
    Linux  : ~/.local/share/<AppName>/
    macOS  : ~/Library/Application Support/<AppName>/
    Windows: %APPDATA%\\<AppName>\\
    """
    import shutil

    from platformdirs import user_data_dir

    new_data_dir = Path(user_data_dir("fbsat59", "fbsat59"))
    new_db = new_data_dir / "fbsat59.db"

    # Skip if the new DB already has content (> 64 KB means real data).
    if new_db.exists() and new_db.stat().st_size > 65536:
        return

    # Candidate legacy directory names tried in priority order.
    legacy_app_names = ["GPredict-Improved", "gpredict-improved"]
    legacy_dir: Path | None = None
    for name in legacy_app_names:
        candidate = Path(user_data_dir(name, name))
        if candidate.exists():
            legacy_dir = candidate
            break

    if legacy_dir is None:
        return

    logger.info("Legacy data directory found: %s — migrating to %s", legacy_dir, new_data_dir)
    new_data_dir.mkdir(parents=True, exist_ok=True)

    # Files/dirs to migrate.  DB file may have a different stem.
    migration_errors: list[str] = []

    # --- Database file (stem may be gpredict-improved or fbsat59) ---
    db_candidates = list(legacy_dir.glob("*.db"))
    for src_db in db_candidates:
        dst_db = new_data_dir / "fbsat59.db"
        if dst_db.exists() and dst_db.stat().st_size > src_db.stat().st_size:
            continue  # keep the larger (newer) file
        try:
            shutil.copy2(src_db, dst_db)
            logger.info("Migrated DB: %s → %s", src_db, dst_db)
            # Remove stale WAL/SHM files from the new location so SQLite
            # opens cleanly without partial journal from the old location.
            for ext in ("-wal", "-shm"):
                stale = dst_db.with_suffix(".db" + ext)
                if stale.exists():
                    stale.unlink(missing_ok=True)
        except OSError as exc:
            migration_errors.append(f"DB copy failed: {exc}")

    # --- Subdirectories (maps, ephemeris, hamlib, direwolf, ft8lib, …) ---
    for src_sub in legacy_dir.iterdir():
        if src_sub.suffix in (".db", ".db-wal", ".db-shm"):
            continue  # already handled above
        if src_sub.is_dir() and src_sub.name.lower() == "logs":
            # platformdirs nests user_log_dir() under user_data_dir() on
            # Windows (%LOCALAPPDATA%\<app>\<app>\Logs), so the legacy
            # app's old log directory shows up here as an ordinary
            # subdirectory of legacy_dir and was getting merged wholesale
            # into the new Logs directory -- e.g. a stale
            # gpredict-improved.log sitting right next to the current
            # fbsat59.log with no way to tell it's inert (confirmed
            # live, 2026-07-21). Old log files carry no functional value
            # (unlike the DB, maps, ephemeris, etc. below), so skip this
            # one directory entirely.
            continue
        dst_sub = new_data_dir / src_sub.name
        try:
            if src_sub.is_dir():
                if dst_sub.exists():
                    # Merge: copy only files that are missing in the destination.
                    for item in src_sub.rglob("*"):
                        rel = item.relative_to(src_sub)
                        target = dst_sub / rel
                        if item.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                        elif not target.exists():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(item, target)
                else:
                    shutil.copytree(src_sub, dst_sub)
                logger.info("Migrated directory: %s", src_sub.name)
            else:
                if not dst_sub.exists():
                    shutil.copy2(src_sub, dst_sub)
                    logger.info("Migrated file: %s", src_sub.name)
        except OSError as exc:
            migration_errors.append(f"{src_sub.name}: {exc}")

    if migration_errors:
        logger.warning("Migration completed with errors: %s", migration_errors)
        logger.warning("Legacy directory kept at %s — please review manually.", legacy_dir)
        return

    # Remove the legacy directory only when everything succeeded.
    try:
        shutil.rmtree(legacy_dir)
        logger.info("Legacy directory removed: %s", legacy_dir)
    except OSError as exc:
        logger.warning("Could not remove legacy directory %s: %s", legacy_dir, exc)


def _get_version() -> str:
    """Return the application version string.

    Tagged release  (v0.1.0)      → "0.1.0"
    Dev build after tag           → "0.1.0.dev3"  (last tag + commit count)
    Fallback (no metadata/git)    → "0.1.0"

    Priority:
      1. version.txt bundled by PyInstaller (written by CI before pyinstaller runs)
      2. git describe (works in dev environment)
      3. importlib.metadata (works when installed via pip)
      4. hardcoded fallback
    """
    # 1. PyInstaller frozen bundle: read version.txt written by CI at build time.
    # sys._MEIPASS is the temp dir where PyInstaller extracts bundled files.
    if getattr(sys, "frozen", False):
        try:
            version_file = Path(getattr(sys, "_MEIPASS", "")) / "version.txt"
            if version_file.exists():
                ver = version_file.read_text(encoding="utf-8").strip()
                if ver:
                    return ver
        except Exception:
            pass

    # 2. Git describe — accurate in dev environment.
    try:
        import subprocess

        result = subprocess.run(
            ["git", "describe", "--tags", "--long"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            # Output format: "v0.1.0-4-gf1dd166"
            parts = result.stdout.strip().split("-")
            tag = "-".join(parts[:-2]).lstrip("v")
            count = int(parts[-2])
            if count == 0:
                return tag
            return f"{tag}.dev{count}"
    except Exception:
        pass

    # 3. importlib.metadata (may lag by one commit when using -e install)
    try:
        from importlib.metadata import version as _meta_version

        ver = _meta_version("fbsat59")
        if ".dev" not in ver and "+" not in ver:
            return ver
        return ver.split("+")[0]
    except Exception:
        return "0.1.0"


APP_VERSION = _get_version()


def _show_splash(app: QApplication) -> QSplashScreen | None:
    """Show a splash screen immediately so the user gets feedback right away.

    Startup (DB init, TLE fetch, IP geolocation, MainWindow construction) can
    take several seconds — especially on Windows — during which the screen
    would otherwise stay blank after the user clicks the launcher icon. This
    is shown as the very first thing in main(), before any of that slow work
    begins, and closed once the main window is ready.
    """
    if getattr(sys, "frozen", False):
        icon_path = Path(getattr(sys, "_MEIPASS", "")) / "assets" / "icon_256.png"
    else:
        icon_path = Path(__file__).parent.parent / "assets" / "icon_256.png"

    if not icon_path.exists():
        return None

    pixmap = QPixmap(str(icon_path))
    if pixmap.isNull():
        return None
    pixmap = pixmap.scaled(
        200, 200, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
    )

    splash = QSplashScreen(pixmap, Qt.WindowType.WindowStaysOnTopHint)
    splash.showMessage(
        "Starting FBSAT59… / 起動中…",
        Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
        Qt.GlobalColor.white,
    )
    splash.show()
    app.processEvents()
    return splash


def _splash_message(splash: QSplashScreen | None, text: str) -> None:
    """Update the splash screen's status text and force it to repaint."""
    if splash is None:
        return
    splash.showMessage(
        text,
        Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
        Qt.GlobalColor.white,
    )
    QApplication.processEvents()


def _acquire_single_instance_lock() -> QLockFile | None:
    """Ensure only one FBSAT59 instance runs at a time.

    Startup (DB init, TLE fetch, map data, MainWindow construction) can take
    a while, so a user who double-clicks the launcher before the window
    appears will otherwise end up with two running instances.  QLockFile
    checks whether the process that holds the lock is still alive (not just
    whether the lock file exists), so a crashed instance never blocks a new
    launch.

    Returns the held QLockFile on success — the caller must keep a
    reference alive for the lifetime of the process, since the lock is
    released when the object is destroyed.  Returns None if another
    instance already holds the lock (after retrying for a few seconds when
    RESTART_ENV_VAR=1 is set — core.app_restart.restart_application()
    launches the new process before the old one has necessarily finished
    releasing the lock during its own shutdown).
    """
    import time

    from platformdirs import user_data_dir

    from core.app_restart import RESTART_ENV_VAR

    lock_dir = Path(user_data_dir("fbsat59", "fbsat59"))
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(lock_dir / "fbsat59.lock"))
    lock.setStaleLockTime(30000)

    is_restart = os.environ.get(RESTART_ENV_VAR) == "1"
    deadline = time.monotonic() + (8.0 if is_restart else 0.0)
    while True:
        if lock.tryLock(200):
            return lock
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def main() -> int:
    """Application main entry point."""
    # On macOS, use Qt's cross-platform "Fusion" style instead of the native
    # Cocoa style and keep the menu bar inside the window, so the UI matches
    # the Windows / Linux builds (native macOS metrics force wide pill buttons,
    # extra padding, and migrate the menu bar to the system-wide top bar).
    # Windows and Linux are left on their existing styles.
    if sys.platform == "darwin":
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeMenuBar, True)

    app = QApplication(sys.argv)
    app.setApplicationName("FBSAT59")
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("FBSAT59")

    if sys.platform == "darwin":
        app.setStyle("Fusion")

    # Show a splash screen right away so the user gets immediate feedback
    # instead of a blank screen while the slow startup work below runs.
    splash = _show_splash(app)

    # Bail out immediately if another instance is already running or still
    # starting up, before any of the slow startup work below begins.  Keep
    # `instance_lock` referenced for the rest of main() — QLockFile releases
    # the lock as soon as it is garbage-collected.
    instance_lock = _acquire_single_instance_lock()
    if instance_lock is None:
        logger.warning("Another FBSAT59 instance is already running or starting — exiting.")
        if splash is not None:
            splash.close()
        box = QMessageBox(
            QMessageBox.Icon.Information,
            "FBSAT59",
            "FBSAT59 はすでに起動中です。\nFBSAT59 is already running or starting up.",
        )
        QTimer.singleShot(1500, box.close)
        box.exec()
        return 0

    # Migrate data from legacy GPredict-Improved directory (runs only once,
    # when the old directory exists and the new DB is empty/missing).
    _migrate_legacy_data()

    # Prefetch Natural Earth map data (downloads on first run, uses cache thereafter)
    _splash_message(splash, "Loading map data… / 地図データ読み込み中…")
    prefetch_land_data()

    # Initialize SQLite DB
    _splash_message(splash, "Initializing database… / データベース初期化中…")
    conn = init_database()

    # Create core components
    tle_manager = TLEManager(conn)
    location_manager = LocationManager(conn)
    location = location_manager.load_saved()

    # Do not overwrite manually set or GPS location. Run IP geolocation only when unset or IP-based.
    _skip_ip = location is not None and location.source in (
        LocationSource.MANUAL,
        LocationSource.GPS,
    )
    if not _skip_ip:
        _splash_message(splash, "Detecting location… / 位置情報取得中…")
        logger.info("No saved QTH (or IP-based) — trying IP geolocation...")
        try:
            ip_loc = asyncio.run(location_manager.from_ip())
            if ip_loc:
                location = ip_loc
                logger.info(
                    "IP geolocation: %.4f°N %.4f°E (%s, %s)",
                    ip_loc.latitude_deg,
                    ip_loc.longitude_deg,
                    ip_loc.city,
                    ip_loc.country,
                )
        except Exception as exc:
            logger.warning("IP geolocation failed at startup: %s", exc)

    engine: SatelliteEngine | None = None
    pass_predictor: PassPredictor | None = None

    if location is not None:
        engine = SatelliteEngine(
            tle_manager,
            location.latitude_deg,
            location.longitude_deg,
            location.elevation_m,
        )
        pass_predictor = PassPredictor(
            tle_manager,
            location.latitude_deg,
            location.longitude_deg,
            location.elevation_m,
        )
        logger.info(
            "Engine initialized at %.4f°N %.4f°E",
            location.latitude_deg,
            location.longitude_deg,
        )
    else:
        logger.info("No saved location — engine not initialized. Set QTH from menu.")

    # Shared rig/rotator state (written by Qt UI, read by FastAPI)
    rig_state = RigWebState()

    # Create FastAPI app
    fastapi_app = create_app(
        conn=conn,
        tle_manager=tle_manager,
        pass_predictor=pass_predictor,
        engine=engine,
        start_time=datetime.now(UTC),
        location_manager=location_manager,
        rig_state=rig_state,
    )

    # Show main window (web server and scheduler also start internally)
    _splash_message(splash, "Building main window… / メイン画面構築中…")
    window = MainWindow(
        conn=conn,
        tle_manager=tle_manager,
        engine=engine,
        pass_predictor=pass_predictor,
        location_manager=location_manager,
        fastapi_app=fastapi_app,
        rig_state=rig_state,
    )
    window.show()
    if splash is not None:
        splash.finish(window)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
