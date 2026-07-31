"""Tests for comms/telemetry/gr_satellites_install.py — bundled env path resolution.

No GNU Radio / conda / network required — these only exercise the pure
filesystem-path logic, with user_gr_satellites_dir() monkeypatched to a
tmp_path so nothing touches the real user data directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import comms.telemetry.gr_satellites_install as gsi


@pytest.fixture(autouse=True)
def _fake_bundle_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bundle_dir = tmp_path / "gr-satellites-env"
    monkeypatch.setattr(gsi, "user_gr_satellites_dir", lambda: bundle_dir)
    return bundle_dir


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")


class TestFindExecutable:
    def test_returns_none_when_nothing_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gsi.shutil, "which", lambda name: None)
        assert gsi.find_gr_satellites_executable() is None

    def test_prefers_bundle_over_system(
        self, _fake_bundle_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bin_dir = "Scripts" if sys.platform == "win32" else "bin"
        exe_name = "gr_satellites.exe" if sys.platform == "win32" else "gr_satellites"
        _make_executable(_fake_bundle_dir / bin_dir / exe_name)
        monkeypatch.setattr(gsi.shutil, "which", lambda name: "/usr/bin/gr_satellites")

        result = gsi.find_gr_satellites_executable()

        assert result is not None
        path, is_bundled = result
        assert is_bundled is True
        assert path == _fake_bundle_dir / bin_dir / exe_name

    def test_falls_back_to_system_when_no_bundle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gsi.shutil, "which", lambda name: "/usr/bin/gr_satellites")

        result = gsi.find_gr_satellites_executable()

        assert result == (Path("/usr/bin/gr_satellites"), False)

    def test_is_bundle_installed(self, _fake_bundle_dir: Path) -> None:
        assert gsi.is_bundle_installed() is False
        bin_dir = "Scripts" if sys.platform == "win32" else "bin"
        exe_name = "gr_satellites.exe" if sys.platform == "win32" else "gr_satellites"
        _make_executable(_fake_bundle_dir / bin_dir / exe_name)
        assert gsi.is_bundle_installed() is True


class TestSatyamlDir:
    def test_none_when_no_bundle(self) -> None:
        assert gsi.bundled_satyaml_dir() is None

    def test_finds_posix_layout(self, _fake_bundle_dir: Path) -> None:
        satyaml = (
            _fake_bundle_dir / "lib" / "python3.11" / "site-packages" / "satellites" / "satyaml"
        )
        satyaml.mkdir(parents=True)
        (satyaml / "iss.yml").write_text("norad: 25544\n")

        found = gsi.bundled_satyaml_dir()

        assert found == satyaml

    def test_finds_windows_layout(self, _fake_bundle_dir: Path) -> None:
        satyaml = _fake_bundle_dir / "Lib" / "site-packages" / "satellites" / "satyaml"
        satyaml.mkdir(parents=True)

        found = gsi.bundled_satyaml_dir()

        assert found == satyaml

    def test_none_when_bundle_dir_exists_but_no_satyaml(self, _fake_bundle_dir: Path) -> None:
        _fake_bundle_dir.mkdir(parents=True)
        assert gsi.bundled_satyaml_dir() is None


class TestUninstall:
    def test_removes_bundle_dir(self, _fake_bundle_dir: Path) -> None:
        (_fake_bundle_dir / "bin").mkdir(parents=True)
        assert _fake_bundle_dir.exists()

        gsi.uninstall_bundle()

        assert not _fake_bundle_dir.exists()

    def test_no_error_when_nothing_installed(self) -> None:
        gsi.uninstall_bundle()  # should not raise


class TestBundledVersion:
    def test_none_when_no_conda_meta(self) -> None:
        assert gsi.bundled_version() is None

    def test_parses_version_from_conda_meta_filename(self, _fake_bundle_dir: Path) -> None:
        meta_dir = _fake_bundle_dir / "conda-meta"
        meta_dir.mkdir(parents=True)
        (meta_dir / "gnuradio-satellites-5.10.0-py311h1234567_0.json").write_text("{}")
        (meta_dir / "gnuradio-core-3.10.12.0-py311_0.json").write_text("{}")

        assert gsi.bundled_version() == "5.10.0"
