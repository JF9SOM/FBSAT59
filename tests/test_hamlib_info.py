"""
Tests for Hamlib bundle asset naming and version helpers.

Deliberately Qt-free: the update dialog delegates all asset selection to
core.hamlib_info, so the logic that decides "is an update available, and which
file do we fetch" can be covered without a QApplication.
"""

from __future__ import annotations

import platform

import pytest

from core import hamlib_info


@pytest.fixture(autouse=True)
def _pin_python_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the Python tag so tests do not depend on the interpreter running them."""
    monkeypatch.setattr(hamlib_info, "_PYVER_TAG", "py311")


@pytest.fixture
def linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")


@pytest.fixture
def macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")


def _asset(name: str) -> dict[str, object]:
    return {
        "name": name,
        "browser_download_url": f"https://example.invalid/{name}",
    }


class TestVersionKey:
    def test_orders_numerically_not_lexically(self) -> None:
        assert hamlib_info.version_key("4.7.2") < hamlib_info.version_key("4.7.10")

    def test_patch_release_outranks_its_base(self) -> None:
        assert hamlib_info.version_key("4.7.1") < hamlib_info.version_key("4.7.2")

    def test_non_numeric_component_sorts_as_zero(self) -> None:
        assert hamlib_info.version_key("abc") == (0,)
        assert hamlib_info.version_key("4.7.2") > hamlib_info.version_key("abc")


class TestGetHamlibVersionNumber:
    def test_strips_the_display_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Hamlib.hamlib_version is 'Hamlib 4.7.2'; release assets say '4.7.2'.
        # Comparing the raw forms never matches, which is what this normalises.
        monkeypatch.setattr(hamlib_info, "get_hamlib_version", lambda: "Hamlib 4.7.2")
        assert hamlib_info.get_hamlib_version_number() == "4.7.2"

    def test_falls_back_to_raw_string_when_no_number_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(hamlib_info, "get_hamlib_version", lambda: "unknown")
        assert hamlib_info.get_hamlib_version_number() == "unknown"


class TestAssetNaming:
    def test_linux_name_round_trips(self, linux: None) -> None:
        name = hamlib_info.asset_name("4.7.2")
        assert name == "hamlib-linux-x86_64-py311-4.7.2.tar.gz"
        assert hamlib_info.parse_asset_version(name or "") == "4.7.2"

    def test_macos_name_round_trips(self, macos: None) -> None:
        name = hamlib_info.asset_name("4.7.2")
        assert name == "hamlib-macos-arm64-py311-4.7.2.tar.gz"
        assert hamlib_info.parse_asset_version(name or "") == "4.7.2"

    def test_rejects_other_platform(self, linux: None) -> None:
        assert hamlib_info.parse_asset_version("hamlib-macos-arm64-py311-4.7.2.tar.gz") is None

    def test_rejects_other_python_version(self, linux: None) -> None:
        assert hamlib_info.parse_asset_version("hamlib-linux-x86_64-py312-4.7.2.tar.gz") is None

    def test_rejects_upstream_source_tarball(self, linux: None) -> None:
        # Upstream Hamlib/Hamlib ships these; they are not usable bundles.
        assert hamlib_info.parse_asset_version("hamlib-4.7.2.tar.gz") is None
        assert hamlib_info.parse_asset_version("hamlib-w64-4.7.2.zip") is None

    def test_rejects_empty_version(self, linux: None) -> None:
        assert hamlib_info.parse_asset_version("hamlib-linux-x86_64-py311-.tar.gz") is None

    def test_unsupported_platform_yields_no_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform, "system", lambda: "FreeBSD")
        assert hamlib_info.asset_name("4.7.2") is None
        assert hamlib_info.parse_asset_version("hamlib-linux-x86_64-py311-4.7.2.tar.gz") is None


class TestSelectNewestAsset:
    def test_picks_highest_version_regardless_of_list_order(self, linux: None) -> None:
        # The bundle release accumulates versions: --clobber only replaces
        # same-named files, so 4.7.1 lingers after 4.7.2 is uploaded.
        assets = [
            _asset("hamlib-linux-x86_64-py311-4.7.2.tar.gz"),
            _asset("hamlib-linux-x86_64-py311-4.7.1.tar.gz"),
        ]
        version, url = hamlib_info.select_newest_asset(assets)
        assert version == "4.7.2"
        assert url.endswith("hamlib-linux-x86_64-py311-4.7.2.tar.gz")

        version, url = hamlib_info.select_newest_asset(list(reversed(assets)))
        assert version == "4.7.2"
        assert url.endswith("hamlib-linux-x86_64-py311-4.7.2.tar.gz")

    def test_ignores_assets_for_other_platforms(self, linux: None) -> None:
        assets = [
            _asset("hamlib-macos-arm64-py311-4.7.2.tar.gz"),
            _asset("hamlib-windows-x86_64-py311-4.7.2.zip"),
            _asset("hamlib-linux-x86_64-py311-4.7.1.tar.gz"),
        ]
        version, url = hamlib_info.select_newest_asset(assets)
        assert version == "4.7.1"
        assert url.endswith("hamlib-linux-x86_64-py311-4.7.1.tar.gz")

    def test_upstream_release_assets_yield_nothing(self, linux: None) -> None:
        # Regression guard: the updater used to query Hamlib/Hamlib's own
        # release, whose assets never match our naming, so "Download & Install"
        # could never appear.
        assets = [
            _asset("hamlib-4.7.2.tar.gz"),
            _asset("hamlib-w64-4.7.2.zip"),
            _asset("SHA256SUM-4.7.2"),
        ]
        assert hamlib_info.select_newest_asset(assets) == ("", "")

    def test_empty_release_yields_nothing(self, linux: None) -> None:
        assert hamlib_info.select_newest_asset([]) == ("", "")


class TestBundleEndpoint:
    def test_api_points_at_this_projects_bundle_release(self) -> None:
        # Not upstream Hamlib/Hamlib — that release has no bundles for us.
        assert hamlib_info.HAMLIB_BUNDLE_REPO == "JF9SOM/fbsat59"
        assert hamlib_info.HAMLIB_BUNDLE_TAG == "hamlib-bundle"
        assert hamlib_info.HAMLIB_GITHUB_API == (
            "https://api.github.com/repos/JF9SOM/fbsat59/releases/tags/hamlib-bundle"
        )
