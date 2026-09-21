"""Windows DLL lookup for the RTL-SDR / HackRF ctypes bypass (no hardware).

Regression for the 2026-09-21 dev-checkout failure: from a source checkout the
HackRF finder never looked in the installed build's ``_internal`` and its
``hackrf*.dll`` glob matched the SoapySDR plugin ``HackRFSupport.dll``, which has
no ``hackrf_init`` ("function 'hackrf_init' not found").
"""

from __future__ import annotations

import ctypes.util
from pathlib import Path

import pytest

import sdr.device as device_mod


@pytest.fixture
def win_source_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pretend to be a Windows source checkout with an installed build present."""
    program_files = tmp_path / "Program Files"
    internal = program_files / "FBSAT59" / "_internal"
    internal.mkdir(parents=True)
    monkeypatch.setattr(device_mod.sys, "platform", "win32")
    monkeypatch.delattr(device_mod.sys, "frozen", raising=False)
    monkeypatch.setenv("PROGRAMFILES", str(program_files))
    monkeypatch.delenv("SOAPY_SDR_ROOT", raising=False)
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: None)
    return internal


@pytest.fixture
def plugin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A filtered soapy_modules dir (as main.py builds) holding only plugin DLLs."""
    cache = tmp_path / "Cache" / "soapy_modules_no_remote"
    cache.mkdir(parents=True)
    (cache / "HackRFSupport.dll").write_bytes(b"")
    (cache / "rtlsdrSupport.dll").write_bytes(b"")
    monkeypatch.setenv("SOAPY_SDR_PLUGIN_PATH", str(cache))
    return cache


def test_hackrf_finder_uses_installed_internal_not_plugin(
    win_source_checkout: Path, plugin_dir: Path
) -> None:
    (win_source_checkout / "hackrf.dll").write_bytes(b"")

    found = device_mod._find_hackrf_dll()

    assert found == str(win_source_checkout / "hackrf.dll")


def test_hackrf_finder_never_returns_soapy_plugin(
    win_source_checkout: Path, plugin_dir: Path
) -> None:
    # Installed build has no hackrf.dll: the plugin must not be picked as a fallback.
    assert device_mod._find_hackrf_dll() is None


def test_rtlsdr_finder_uses_installed_internal(win_source_checkout: Path, plugin_dir: Path) -> None:
    (win_source_checkout / "rtlsdr.dll").write_bytes(b"")

    found = device_mod._find_rtlsdr_dll()

    assert found == str(win_source_checkout / "rtlsdr.dll")


def test_installed_bundle_dir_absent_without_installed_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(device_mod.sys, "platform", "win32")
    monkeypatch.delattr(device_mod.sys, "frozen", raising=False)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "nowhere"))

    assert device_mod._installed_bundle_dir() is None


def test_installed_bundle_dir_ignored_when_frozen_or_not_windows(
    win_source_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(device_mod.sys, "platform", "linux")
    assert device_mod._installed_bundle_dir() is None

    monkeypatch.setattr(device_mod.sys, "platform", "win32")
    monkeypatch.setattr(device_mod.sys, "frozen", True, raising=False)
    assert device_mod._installed_bundle_dir() is None
