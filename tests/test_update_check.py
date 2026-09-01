"""Tests for core.update_check pure logic (no Qt / no network)."""

from __future__ import annotations

import pytest

from core.update_check import (
    UpdateLevel,
    UpdateManifest,
    evaluate,
    is_release_version,
    parse_manifest,
)


def _manifest(
    *,
    latest: str = "0.3.50",
    minimum: str = "0.3.49",
    critical: bool = False,
) -> UpdateManifest:
    return UpdateManifest(
        latest_version=latest,
        minimum_supported_version=minimum,
        critical=critical,
        message_ja="重要な更新",
        message_en="critical update",
    )


class TestIsReleaseVersion:
    @pytest.mark.parametrize("v", ["0.3.49", "1.0.0", "0.3.49.post1"])
    def test_clean_release(self, v: str) -> None:
        assert is_release_version(v) is True

    @pytest.mark.parametrize(
        "v",
        ["0.3.49.dev6", "0.0.0", "0.0.0-dev", "0.0.0.dev0", "", "not-a-version"],
    )
    def test_dev_or_placeholder(self, v: str) -> None:
        assert is_release_version(v) is False


class TestParseManifest:
    def test_full(self) -> None:
        m = parse_manifest(
            {
                "latest_version": "0.3.50",
                "minimum_supported_version": "0.3.49",
                "critical": True,
                "message_ja": "x",
                "message_en": "y",
            }
        )
        assert m == UpdateManifest("0.3.50", "0.3.49", True, "x", "y")

    def test_minimum_defaults_to_latest(self) -> None:
        m = parse_manifest({"latest_version": "0.3.50"})
        assert m is not None
        assert m.minimum_supported_version == "0.3.50"
        assert m.critical is False

    @pytest.mark.parametrize("data", [{}, {"latest_version": ""}, [], "nope", None])
    def test_malformed_returns_none(self, data: object) -> None:
        assert parse_manifest(data) is None


class TestEvaluate:
    def test_up_to_date(self) -> None:
        r = evaluate("0.3.50", _manifest(), skipped_version=None, lang="en")
        assert r.level is UpdateLevel.UP_TO_DATE

    def test_newer_than_latest_is_up_to_date(self) -> None:
        r = evaluate("0.3.51", _manifest(), skipped_version=None, lang="en")
        assert r.level is UpdateLevel.UP_TO_DATE

    def test_new_version(self) -> None:
        r = evaluate("0.3.49", _manifest(latest="0.3.50"), skipped_version=None, lang="en")
        assert r.level is UpdateLevel.NEW_VERSION
        assert r.latest_version == "0.3.50"

    def test_dev_build_just_after_tag_is_below_latest(self) -> None:
        # git-describe dev version sorts *below* the tag it followed.
        r = evaluate("0.3.50.dev3", _manifest(latest="0.3.50"), skipped_version=None, lang="en")
        assert r.level is UpdateLevel.NEW_VERSION

    def test_skip_suppresses_new_version(self) -> None:
        r = evaluate("0.3.49", _manifest(latest="0.3.50"), skipped_version="0.3.50", lang="en")
        assert r.level is UpdateLevel.UP_TO_DATE

    def test_skip_of_older_version_does_not_suppress(self) -> None:
        r = evaluate("0.3.49", _manifest(latest="0.3.50"), skipped_version="0.3.48", lang="en")
        assert r.level is UpdateLevel.NEW_VERSION

    def test_critical_below_floor(self) -> None:
        r = evaluate(
            "0.3.48",
            _manifest(latest="0.3.50", minimum="0.3.49", critical=True),
            skipped_version=None,
            lang="ja",
        )
        assert r.level is UpdateLevel.CRITICAL
        assert r.message == "重要な更新"

    def test_critical_ignores_skip(self) -> None:
        r = evaluate(
            "0.3.48",
            _manifest(latest="0.3.50", minimum="0.3.49", critical=True),
            skipped_version="0.3.50",
            lang="en",
        )
        assert r.level is UpdateLevel.CRITICAL

    def test_critical_flag_but_at_or_above_floor_is_not_critical(self) -> None:
        r = evaluate(
            "0.3.49",
            _manifest(latest="0.3.50", minimum="0.3.49", critical=True),
            skipped_version=None,
            lang="en",
        )
        assert r.level is UpdateLevel.NEW_VERSION

    def test_unparseable_current_only_triggers_critical(self) -> None:
        # Not critical -> nothing we can compare -> UP_TO_DATE.
        r = evaluate("garbage", _manifest(latest="0.3.50"), skipped_version=None, lang="en")
        assert r.level is UpdateLevel.UP_TO_DATE
        # Critical -> surfaced despite the unparseable version.
        r2 = evaluate(
            "garbage",
            _manifest(latest="0.3.50", minimum="0.3.49", critical=True),
            skipped_version=None,
            lang="en",
        )
        assert r2.level is UpdateLevel.CRITICAL

    def test_message_language_fallback(self) -> None:
        m = UpdateManifest("0.3.50", "0.3.49", True, "", "english only")
        r = evaluate("0.1.0", m, skipped_version=None, lang="ja")
        assert r.message == "english only"
