"""Startup update check against a GitHub-hosted manifest.

The app already ships a manual updater (Help > Check for Updates…,
``ui.app_update_dialog``). This module adds an *automatic* startup check
plus a way to flag a release as **critical** so every existing user is
nudged to update even if they turned the routine check off.

Design:

* The manifest is a small JSON file committed at the repo root and served
  from ``raw.githubusercontent.com`` (CDN, no API rate limit). Bumping it
  is all that's needed to raise an advisory -- no rebuild, no release.
* The manifest carries version numbers, a ``critical`` flag and a
  localized message only. It never carries a download URL: the actual
  download still goes through ``app_update_dialog``'s own
  ``releases/latest`` asset resolution, so a bad/tampered manifest cannot
  point users at an arbitrary binary. The message string is display-only
  and is never executed or interpreted as a command.
* Any network/parse failure is swallowed -- startup must never depend on
  this, and a routine offline launch must not raise dialogs.
"""

from __future__ import annotations

import enum
import json
import logging
import urllib.request
from dataclasses import dataclass

from packaging.version import InvalidVersion, Version
from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)

#: Raw manifest URL. GitHub serves raw content case-insensitively for the
#: owner/repo path, matching ``ui.app_update_dialog``'s API host.
MANIFEST_URL = "https://raw.githubusercontent.com/JF9SOM/FBSAT59/main/update_manifest.json"


class UpdateLevel(enum.Enum):
    """Outcome of :func:`evaluate`."""

    UP_TO_DATE = "up_to_date"
    NEW_VERSION = "new_version"
    CRITICAL = "critical"


@dataclass(frozen=True)
class UpdateManifest:
    """Parsed contents of ``update_manifest.json``."""

    latest_version: str
    minimum_supported_version: str
    critical: bool
    message_ja: str
    message_en: str

    def message_for(self, lang: str) -> str:
        """Return the message for *lang* ('ja'/'en'), falling back to English."""
        if lang.startswith("ja") and self.message_ja:
            return self.message_ja
        return self.message_en or self.message_ja


@dataclass(frozen=True)
class UpdateCheckResult:
    """What the caller should surface to the user."""

    level: UpdateLevel
    latest_version: str
    message: str


def parse_manifest(data: object) -> UpdateManifest | None:
    """Turn a decoded JSON object into an :class:`UpdateManifest`.

    Returns ``None`` when the object is missing the required version
    fields, rather than raising -- a malformed manifest is treated the
    same as no manifest.
    """
    if not isinstance(data, dict):
        return None
    latest = str(data.get("latest_version", "")).strip()
    minimum = str(data.get("minimum_supported_version", "") or latest).strip()
    if not latest:
        return None
    return UpdateManifest(
        latest_version=latest,
        minimum_supported_version=minimum,
        critical=bool(data.get("critical", False)),
        message_ja=str(data.get("message_ja", "") or ""),
        message_en=str(data.get("message_en", "") or ""),
    )


def is_release_version(version: str) -> bool:
    """True for a clean tagged-release version string (e.g. ``0.3.49``).

    Excludes dev builds (``0.3.49.dev6``, from ``git describe``) and the
    ``0.0.0`` placeholder CI stamps on manual ``workflow_dispatch`` test
    builds -- neither should be pestered to "update" to an older tag.
    """
    try:
        v = Version(version)
    except InvalidVersion:
        return False
    if v.is_devrelease or v.is_prerelease:
        return False
    return v.base_version != "0.0.0"


def evaluate(
    current: str,
    manifest: UpdateManifest,
    *,
    skipped_version: str | None,
    lang: str,
) -> UpdateCheckResult:
    """Decide what to tell the user.

    * ``CRITICAL``  -- ``manifest.critical`` is set and *current* is below
      ``minimum_supported_version``. The caller must surface this
      regardless of the "check on startup" preference.
    * ``NEW_VERSION`` -- *current* is below ``latest_version`` and the user
      has not chosen to skip exactly that version.
    * ``UP_TO_DATE`` -- otherwise (including an unparseable *current*,
      which only ``CRITICAL`` can override).
    """
    try:
        cur = Version(current)
    except InvalidVersion:
        cur = None

    if manifest.critical:
        try:
            floor = Version(manifest.minimum_supported_version)
        except InvalidVersion:
            floor = None
        if floor is not None and (cur is None or cur < floor):
            return UpdateCheckResult(
                UpdateLevel.CRITICAL,
                manifest.latest_version,
                manifest.message_for(lang),
            )

    try:
        latest = Version(manifest.latest_version)
    except InvalidVersion:
        return UpdateCheckResult(UpdateLevel.UP_TO_DATE, manifest.latest_version, "")

    if cur is not None and cur < latest and skipped_version != manifest.latest_version:
        return UpdateCheckResult(
            UpdateLevel.NEW_VERSION,
            manifest.latest_version,
            manifest.message_for(lang),
        )

    return UpdateCheckResult(UpdateLevel.UP_TO_DATE, manifest.latest_version, "")


class UpdateCheckWorker(QThread):
    """Fetches and parses the manifest off the UI thread.

    Emits :attr:`checked` with an :class:`UpdateManifest` on success.
    Every failure path is silent (logged at debug only) and emits
    nothing -- the caller simply does nothing.
    """

    checked = Signal(object)  # UpdateManifest

    def run(self) -> None:
        try:
            req = urllib.request.Request(
                MANIFEST_URL,
                headers={"User-Agent": "fbsat59-update-check"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                raw = json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001 - startup check must never raise
            logger.debug("Update check skipped: %s", exc)
            return
        manifest = parse_manifest(raw)
        if manifest is None:
            logger.debug("Update check: manifest missing required fields")
            return
        self.checked.emit(manifest)
