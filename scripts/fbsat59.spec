# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for FBSAT59.

Bundles:
  - src/main.py (entry point)
  - src/web/static/  (FastAPI static files)
  - locale/          (i18n .mo files)
  - src/data/community_transmitters.json

Hamlib note:
  Linux  : collected from /opt/hamlib/4.7 or system path.
  macOS  : collected from Homebrew (brew install hamlib).
  Windows: DLLs pre-downloaded by CI into hamlib-win64/; Python binding
           copied to site-packages before this spec runs.
"""

import os
import sys
from pathlib import Path

block_cipher = None

# Repository root (one level up from scripts/)
ROOT = Path(SPECPATH).parent  # noqa: F821  (SPECPATH is injected by PyInstaller)
SRC = ROOT / "src"

# --------------------------------------------------------------------------- #
# Platform-specific Hamlib binary collection
# --------------------------------------------------------------------------- #
hamlib_binaries: list[tuple[str, str]] = []
soapy_binaries: list[tuple[str, str]] = []

if sys.platform == "win32":
    # CI places hamlib DLLs in hamlib-win64/bin/ relative to repo root
    hamlib_bin_dir = ROOT / "hamlib-win64" / "bin"
    if hamlib_bin_dir.exists():
        for dll in hamlib_bin_dir.glob("*.dll"):
            hamlib_binaries.append((str(dll), "."))

    # SoapySDR: core DLLs + Python binding bundled flat in ".";
    # device-module DLLs in "soapy_modules" (SOAPY_SDR_PLUGIN_PATH target).
    # Extracted from conda-forge packages by CI before this spec runs.
    _soapy_dir = ROOT / "soapy-win64"
    if _soapy_dir.exists():
        for _dll in (_soapy_dir / "bin").glob("*.dll"):
            soapy_binaries.append((str(_dll), "."))
        for _f in (_soapy_dir / "python").iterdir():
            soapy_binaries.append((str(_f), "."))
        for _dll in (_soapy_dir / "modules").glob("*.dll"):
            soapy_binaries.append((str(_dll), "soapy_modules"))

elif sys.platform == "darwin":
    # Homebrew hamlib dylibs
    brew_lib = Path("/opt/homebrew/lib")
    if not brew_lib.exists():
        brew_lib = Path("/usr/local/lib")
    for dylib in brew_lib.glob("libhamlib*.dylib"):
        hamlib_binaries.append((str(dylib), "."))

elif sys.platform == "linux":
    # When built in CI (or locally) with /opt/hamlib/4.7, collect the shared
    # libraries explicitly so they are bundled and not required on the end-user's system.
    _hamlib_lib = Path("/opt/hamlib/4.7/lib")
    if _hamlib_lib.exists():
        for _so in _hamlib_lib.glob("libhamlib*.so*"):
            if not _so.is_symlink():
                hamlib_binaries.append((str(_so), "."))
        # Also collect the versioned symlink target (libhamlib.so.4 → libhamlib.so.4.x.y)
        for _so in _hamlib_lib.glob("libhamlib.so.*"):
            hamlib_binaries.append((str(_so), "."))

# --------------------------------------------------------------------------- #
# Direwolf bundle (downloaded from direwolf-bundle release by CI)
# Placed at _MEIPASS root so find_direwolf() finds it as _MEIPASS/direwolf
# --------------------------------------------------------------------------- #
direwolf_binaries: list[tuple[str, str]] = []
_direwolf_dir = ROOT / "direwolf-bundle"
if _direwolf_dir.exists():
    _dw_exe = _direwolf_dir / ("direwolf.exe" if sys.platform == "win32" else "direwolf")
    if _dw_exe.exists():
        direwolf_binaries.append((str(_dw_exe), "."))
    if sys.platform == "win32":
        for _dll in _direwolf_dir.glob("*.dll"):
            direwolf_binaries.append((str(_dll), "."))
    elif sys.platform == "darwin":
        for _dylib in _direwolf_dir.glob("*.dylib"):
            direwolf_binaries.append((str(_dylib), "."))

q65lib_binaries: list[tuple[str, str]] = []
_q65lib_dir = ROOT / "q65lib-bundle"
if _q65lib_dir.exists():
    if sys.platform == "win32":
        for _dll in _q65lib_dir.glob("*.dll"):
            q65lib_binaries.append((str(_dll), "."))
    elif sys.platform == "darwin":
        for _dylib in _q65lib_dir.glob("*.dylib"):
            q65lib_binaries.append((str(_dylib), "."))
    else:
        for _so in _q65lib_dir.glob("*.so"):
            q65lib_binaries.append((str(_so), "."))

ft8lib_binaries: list[tuple[str, str]] = []
_ft8lib_dir = ROOT / "ft8lib-bundle"
if _ft8lib_dir.exists():
    if sys.platform == "win32":
        for _dll in _ft8lib_dir.glob("*.dll"):
            ft8lib_binaries.append((str(_dll), "."))
    elif sys.platform == "darwin":
        for _dylib in _ft8lib_dir.glob("*.dylib"):
            ft8lib_binaries.append((str(_dylib), "."))
    else:
        for _so in _ft8lib_dir.glob("*.so"):
            ft8lib_binaries.append((str(_so), "."))

# --------------------------------------------------------------------------- #
# Collect binary-heavy packages that PyInstaller cannot auto-detect fully
# --------------------------------------------------------------------------- #
from PyInstaller.utils.hooks import collect_all  # noqa: E402

extra_datas: list[tuple[str, str]] = []
extra_binaries: list[tuple[str, str]] = []
extra_hidden: list[str] = []

for _pkg in ("scipy", "sounddevice", "lameenc", "onnxruntime"):
    try:
        _d, _b, _h = collect_all(_pkg)
        extra_datas += _d
        extra_binaries += _b
        extra_hidden += _h
    except Exception:
        pass

# --------------------------------------------------------------------------- #
# Data files
# --------------------------------------------------------------------------- #
datas = [
    # Web UI static files
    (str(SRC / "web" / "static"), "web/static"),
    # i18n compiled catalogs
    (str(ROOT / "locale"), "locale"),
    # Community frequency database
    (str(SRC / "data" / "community_transmitters.json"), "data"),
    # App icon PNGs (used by Qt window icon at runtime on all platforms)
    (str(ROOT / "assets"), "assets"),
    # Version file written by CI before pyinstaller runs (used by _get_version())
    (str(SRC / "version.txt"), "."),
]

# Collect certifi CA bundle (cacert.pem) so httpx HTTPS works in the bundle.
try:
    import certifi as _certifi

    datas.append((_certifi.where(), "certifi"))
except Exception:
    pass

# Collect Skyfield package data (built-in leap-second / time-scale tables).
try:
    import skyfield as _skyfield

    _sf_dir = Path(_skyfield.__file__).parent
    for _sf_data_dir in ["data"]:
        _sf_path = _sf_dir / _sf_data_dir
        if _sf_path.is_dir():
            datas.append((str(_sf_path), f"skyfield/{_sf_data_dir}"))
except Exception:
    pass

# --------------------------------------------------------------------------- #
# Hidden imports (dynamic loaders that PyInstaller cannot auto-detect)
# --------------------------------------------------------------------------- #
hidden_imports = [
    # uvicorn workers / reload
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    # FastAPI / Starlette
    "starlette.routing",
    "starlette.staticfiles",
    "starlette.middleware",
    "starlette.middleware.cors",
    # Skyfield
    "skyfield",
    "skyfield.api",
    "skyfield.data",
    "skyfield.iokit",
    # Pydantic v2 core
    "pydantic",
    "pydantic.deprecated",
    "pydantic_core",
    # APScheduler
    "apscheduler",
    "apscheduler.schedulers.background",
    "apscheduler.triggers.interval",
    # zeroconf
    "zeroconf",
    "zeroconf._services",
    # qrcode
    "qrcode",
    "qrcode.image.pil",
    # Shapely
    "shapely",
    "shapely.geometry",
    # BeautifulSoup
    "bs4",
    # PySide6 extras used at runtime
    "PySide6.QtCharts",
    "PySide6.QtSvg",
    "PySide6.QtPrintSupport",
    # Alembic (DB migrations)
    "alembic",
    "alembic.config",
    "alembic.runtime",
    "alembic.runtime.migration",
    # SSL CA bundle (required for httpx HTTPS on macOS/Windows bundles)
    "certifi",
]

# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
a = Analysis(
    [str(SRC / "main.py")],
    pathex=[str(SRC)],
    binaries=hamlib_binaries + soapy_binaries + direwolf_binaries + q65lib_binaries + ft8lib_binaries + extra_binaries,
    datas=datas + extra_datas,
    hiddenimports=hidden_imports + extra_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "IPython",
        "jupyter",
        "notebook",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

# --------------------------------------------------------------------------- #
# Executable
# --------------------------------------------------------------------------- #
# Platform-specific icon path
_icon_dir = ROOT / "assets"
if sys.platform == "win32":
    _exe_icon = str(_icon_dir / "icon.ico") if (_icon_dir / "icon.ico").exists() else None
elif sys.platform == "darwin":
    _exe_icon = str(_icon_dir / "icon.icns") if (_icon_dir / "icon.icns").exists() else None
else:
    _exe_icon = str(_icon_dir / "icon_256.png") if (_icon_dir / "icon_256.png").exists() else None

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="fbsat59",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # GUI app — no terminal window on Windows/macOS
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_exe_icon,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="fbsat59",
)

# macOS: also build an .app bundle
if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name="FBSAT59.app",
        icon=_exe_icon,
        bundle_identifier="org.fbsat59",
        info_plist={
            "NSPrincipalClass": "NSApplication",
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": "0.1.0",
        },
    )
