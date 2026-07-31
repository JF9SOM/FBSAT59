"""Bundled gr-satellites (GNU Radio conda-forge env) install management.

gr-satellites depends on the full GNU Radio runtime, which cannot be
statically linked or trivially bundled the way ft8lib/libq65/libft4wsjt are
(those are small, dependency-light C libraries — a single .so/.dylib/.dll).
Instead, CI packs a headless conda-forge environment (gnuradio-core +
gnuradio-satellites, deliberately excluding gnuradio-qtgui/grc/uhd/iio so no
Qt/PyQt/GUI toolkit is pulled in) with conda-pack, and this module manages
locating/uninstalling that environment on the user's machine. Downloading and
extracting it is handled by ui/gr_satellites_dialog.py's install worker,
mirroring the ft8lib/Direwolf "Download & Install" pattern.

Detection priority:
  1. User-installed bundle   ~/.local/share/fbsat59/gr-satellites-env/
  2. System install          shutil.which("gr_satellites")  (apt/conda/etc.)

The bundle is a full, independent Python environment; conda-pack's
``conda-unpack`` fixes up absolute paths baked into compiled files after
extraction. It does *not* touch the ``bin/gr_satellites`` entry-point
script's shebang, though — CI logs (2026-07-31) confirmed that script uses
``#!/usr/bin/env python`` (no absolute path for conda-unpack to rewrite),
so running it directly depends on whatever ``python`` happens to be first
on the *caller's* PATH at that moment, which has nothing to do with the
bundled environment. resolve_gr_satellites_command() below works around
this by invoking the bundled python explicitly rather than relying on the
script's own shebang.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def user_gr_satellites_dir() -> Path:
    """Return the platform-specific user install directory for the bundled env."""
    from platformdirs import user_data_dir

    return Path(user_data_dir("fbsat59")) / "gr-satellites-env"


def _bundled_executable_path() -> Path:
    """Path to the gr_satellites entry-point script inside the bundled env.

    On Windows this is *not* under Scripts/ (that directory holds pure-
    Python console-script entry points only). gnuradio-satellites is a GNU
    Radio OOT module — a C/C++-oriented package by conda-forge convention —
    and its executables live under Library/bin/ instead, alongside the
    compiled gnuradio-satellites.dll. Confirmed by downloading and
    inspecting the actual win-64 .conda package directly (2026-07-31):
    Library/bin/gr_satellites.py (plain script, used here) and
    Library/bin/gr_satellites.exe (a launcher stub, deliberately not used —
    see resolve_gr_satellites_command()'s docstring for why bundled
    launchers/shebangs can't be trusted without per-platform verification).
    """
    if sys.platform == "win32":
        return user_gr_satellites_dir() / "Library" / "bin" / "gr_satellites.py"
    return user_gr_satellites_dir() / "bin" / "gr_satellites"


def _bundled_python_path() -> Path:
    """Path to the Python interpreter inside the bundled env."""
    if sys.platform == "win32":
        return user_gr_satellites_dir() / "python.exe"
    return user_gr_satellites_dir() / "bin" / "python"


def is_bundle_installed() -> bool:
    """True if a bundled gr-satellites environment is present."""
    return _bundled_executable_path().exists()


def find_gr_satellites_executable() -> tuple[Path, bool] | None:
    """Resolve the gr_satellites executable's path, for display purposes.

    Returns ``(path, is_bundled)``, or ``None`` if neither the bundled
    environment nor a system install can be found. The bundle is preferred
    when present, since it is self-contained (no NumPy version conflicts,
    guaranteed GNU Radio + gr-satellites versions).

    To actually *run* gr_satellites, use resolve_gr_satellites_command()
    instead — this path alone is not safe to exec directly for the bundled
    case (see module docstring: its shebang can't be trusted).
    """
    bundled = _bundled_executable_path()
    if bundled.exists():
        return bundled, True
    system = shutil.which("gr_satellites")
    if system:
        return Path(system), False
    return None


def resolve_gr_satellites_command() -> tuple[list[str], bool] | None:
    """Resolve the argv prefix to invoke gr_satellites, and whether it's bundled.

    For the bundled environment this returns
    ``[bundled_python, bundled_gr_satellites_script]`` rather than the
    script path alone, since its shebang cannot be relied on to select the
    bundled interpreter (see module docstring). For a system install, the
    script resolved from PATH is returned as-is — its shebang was written
    by whatever installed it there (apt/conda/etc.) and is not our concern.
    """
    bundled = _bundled_executable_path()
    if bundled.exists():
        return [str(_bundled_python_path()), str(bundled)], True
    system = shutil.which("gr_satellites")
    if system:
        return [system], False
    return None


def bundled_satyaml_dir() -> Path | None:
    """Locate the satyaml definitions directory inside the bundled env, if installed.

    conda-pack preserves the normal Python install layout, so this is under
    ``lib/pythonX.Y/site-packages/`` on Linux/macOS or ``Lib/site-packages/``
    on Windows — the exact minor Python version is whatever CI picked, so it
    is discovered with a glob rather than hardcoded.
    """
    bundle_dir = user_gr_satellites_dir()
    if not bundle_dir.exists():
        return None
    for candidate in (
        *bundle_dir.glob("lib/python*/site-packages/satellites/satyaml"),
        bundle_dir / "Lib" / "site-packages" / "satellites" / "satyaml",
    ):
        if candidate.exists():
            return candidate
    return None


def uninstall_bundle() -> None:
    """Remove the user-installed bundled environment, if present."""
    shutil.rmtree(user_gr_satellites_dir(), ignore_errors=True)


def bundled_version() -> str | None:
    """Return the installed gnuradio-satellites version, if the bundle is present.

    Conda environments record each installed package as
    ``conda-meta/<name>-<version>-<build>.json``; reading the filename is a
    cheap, reliable way to get the version without spawning the bundled
    interpreter.
    """
    meta_dir = user_gr_satellites_dir() / "conda-meta"
    if not meta_dir.exists():
        return None
    matches = sorted(meta_dir.glob("gnuradio-satellites-*.json"))
    if not matches:
        return None
    stem = matches[-1].stem
    rest = stem.removeprefix("gnuradio-satellites-")
    parts = rest.split("-")
    return parts[0] if parts else None
