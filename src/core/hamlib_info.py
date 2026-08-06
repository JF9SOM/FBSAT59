"""
Hamlib version detection and user-installation path helpers.
"""

from __future__ import annotations

import platform
import re
import sys
from pathlib import Path


def get_hamlib_version() -> str:
    """Return the version string of the currently loaded Hamlib."""
    try:
        import Hamlib

        return str(Hamlib.hamlib_version)
    except Exception:
        return "unknown"


def get_hamlib_version_number() -> str:
    """
    Return only the numeric part of the loaded Hamlib version.

    ``Hamlib.hamlib_version`` is a display string such as ``'Hamlib 4.7.2'``,
    while release assets and ``version.txt`` carry the bare ``'4.7.2'``.
    Comparing the two forms directly never matches, so always normalise
    through here before checking whether an update is needed.
    """
    match = re.search(r"\d+(?:\.\d+)+", get_hamlib_version())
    return match.group(0) if match else get_hamlib_version()


def version_key(version: str) -> tuple[int, ...]:
    """
    Return a sortable key for a dotted version string.

    Non-numeric components sort as 0 so a malformed name can never outrank a
    well-formed one.
    """
    parts: list[int] = []
    for chunk in version.split("."):
        digits = re.match(r"\d+", chunk)
        parts.append(int(digits.group(0)) if digits else 0)
    return tuple(parts)


def get_user_hamlib_dir() -> Path:
    """Return the per-user Hamlib installation directory (flat layout)."""
    try:
        from platformdirs import user_data_dir

        return Path(user_data_dir("fbsat59")) / "hamlib"
    except Exception:
        if sys.platform == "win32":
            appdata = Path.home() / "AppData" / "Roaming"
            return appdata / "fbsat59" / "hamlib"
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "fbsat59" / "hamlib"
        return Path.home() / ".local" / "share" / "fbsat59" / "hamlib"


def get_user_hamlib_version() -> str | None:
    """Return the version stored in the user Hamlib dir, or None if not installed."""
    ver_file = get_user_hamlib_dir() / "version.txt"
    if ver_file.exists():
        try:
            return ver_file.read_text().strip()
        except Exception:
            pass
    return None


def is_user_hamlib_installed() -> bool:
    """Return True if a user-local Hamlib installation is present."""
    d = get_user_hamlib_dir()
    if not d.exists():
        return False
    # Look for Hamlib.py (present on all platforms in the flat layout)
    return (d / "Hamlib.py").exists()


# ---------------------------------------------------------------------------
# Asset naming — must match what the CI uploads to GitHub Releases
#
# The portable Hamlib packages are built by this project's own CI and uploaded
# to the 'hamlib-bundle' pre-release of this repository — NOT to upstream
# Hamlib/Hamlib, whose releases only carry source tarballs and Windows
# installers. Pointing the updater at upstream makes every asset lookup fail
# silently, so keep this aimed at our own release.
# ---------------------------------------------------------------------------
HAMLIB_BUNDLE_REPO = "JF9SOM/fbsat59"
HAMLIB_BUNDLE_TAG = "hamlib-bundle"
HAMLIB_GITHUB_API = (
    f"https://api.github.com/repos/{HAMLIB_BUNDLE_REPO}/releases/tags/{HAMLIB_BUNDLE_TAG}"
)
HAMLIB_GITHUB_RELEASES = "https://github.com/Hamlib/Hamlib/releases"

_PYVER_TAG = f"py{sys.version_info.major}{sys.version_info.minor}"


def _asset_pattern() -> tuple[str, str] | None:
    """
    Return the (prefix, suffix) bracketing the version in this platform's
    bundle asset name, or None on an unsupported platform.
    """
    os_name = platform.system()
    if os_name == "Linux":
        return f"hamlib-linux-x86_64-{_PYVER_TAG}-", ".tar.gz"
    if os_name == "Windows":
        # Custom CI asset, flat layout (DLLs + .pyd + Hamlib.py).
        return f"hamlib-windows-x86_64-{_PYVER_TAG}-", ".zip"
    if os_name == "Darwin":
        arch = platform.machine()  # 'arm64' on Apple Silicon, 'x86_64' on Intel
        return f"hamlib-macos-{arch}-{_PYVER_TAG}-", ".tar.gz"
    return None


def asset_name(version: str) -> str | None:
    """e.g. 'hamlib-linux-x86_64-py311-4.7.2.tar.gz' — None if unsupported."""
    pattern = _asset_pattern()
    if pattern is None:
        return None
    prefix, suffix = pattern
    return f"{prefix}{version}{suffix}"


def parse_asset_version(name: str) -> str | None:
    """
    Return the Hamlib version embedded in a bundle asset name, or None if the
    name targets another platform, architecture or Python version.

    The release accumulates assets across builds (``gh release upload
    --clobber`` only replaces same-named files), so the caller must match on
    the pattern rather than on one expected version.
    """
    pattern = _asset_pattern()
    if pattern is None:
        return None
    prefix, suffix = pattern
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    version = name[len(prefix) : len(name) - len(suffix)]
    return version or None


def is_update_available(latest: str, current: str) -> bool:
    """
    Return True only when `latest` is strictly newer than `current`.

    Equality means up to date, and an older release must never be offered:
    between tag pushes the bundled Hamlib can outrank whatever sits in the
    bundle release, and presenting that as an "update" would silently
    downgrade the user. An unreadable current version (Hamlib failed to
    import, so no digits to parse) sorts as 0 and therefore does invite an
    install, which is the desired behaviour.
    """
    if not latest:
        return False
    return version_key(latest) > version_key(current)


def select_newest_asset(assets: list[dict[str, object]]) -> tuple[str, str]:
    """
    Pick the highest-versioned bundle asset built for this platform from a
    GitHub release's asset list, returning (version, download_url).

    Returns ('', '') when the release carries nothing for this platform.

    The version comes from the asset name, not the release tag: the bundle
    release is a rolling pre-release tagged 'hamlib-bundle', so its tag says
    nothing about which Hamlib it holds. Old versions also linger there —
    ``gh release upload --clobber`` only replaces same-named files — hence the
    explicit max rather than "first match wins".
    """
    best_version = ""
    best_url = ""
    best_key: tuple[int, ...] = ()

    for asset in assets:
        version = parse_asset_version(str(asset.get("name", "")))
        if version is None:
            continue
        key = version_key(version)
        if key > best_key:
            best_key = key
            best_version = version
            best_url = str(asset.get("browser_download_url", ""))

    return best_version, best_url
