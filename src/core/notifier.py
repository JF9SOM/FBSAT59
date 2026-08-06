"""
AOS/LOS desktop notification engine.

PassNotifier — checks upcoming passes every second and fires OS desktop
notifications before AOS and (optionally) before LOS.

Supported platforms:
  Linux  — notify-send (libnotify, available on all major distros)
  macOS  — osascript (built-in)
  Windows — win10toast-click via plyer fallback; falls back to QSystemTrayIcon

Usage::

    notifier = PassNotifier(conn)
    notifier.check(passes, sat_name="FO-29")   # call every second from _on_tick
"""

from __future__ import annotations

import platform
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.engine import PassInfo

# ---------------------------------------------------------------------------
# Low-level OS notification helpers
# ---------------------------------------------------------------------------

_SYSTEM = platform.system()  # "Linux" | "Darwin" | "Windows"


def _send_notification(title: str, body: str) -> None:
    """Send a desktop notification using the platform-native mechanism.

    Tries platform-native tools first; falls back to plyer if available.
    Never raises — notification failure is non-fatal.
    """
    try:
        if _SYSTEM == "Linux":
            subprocess.Popen(
                ["notify-send", "-i", "dialog-information", "-t", "8000", title, body],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif _SYSTEM == "Darwin":
            script = f'display notification "{body}" with title "{title}" sound name "Glass"'
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif _SYSTEM == "Windows":
            _send_windows_notification(title, body)
        else:
            _send_plyer(title, body)
    except Exception:  # noqa: BLE001
        _send_plyer(title, body)


def _send_windows_notification(title: str, body: str) -> None:
    """Send a Windows 10/11 toast notification via plyer or PowerShell fallback."""
    try:
        from plyer import notification  # noqa: PLC0415

        notification.notify(
            title=title,
            message=body,
            app_name="FBSAT59",
            timeout=8,
        )
    except Exception:  # noqa: BLE001
        # PowerShell fallback (Windows 10+)
        try:
            ps_script = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                "ContentType = WindowsRuntime] > $null;"
                f"$t = [Windows.UI.Notifications.ToastTemplateType]::ToastText02;"
                f"$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($t);"  # noqa: E501
                f'$xml.GetElementsByTagName("text")[0].AppendChild($xml.CreateTextNode("{title}")) > $null;'  # noqa: E501
                f'$xml.GetElementsByTagName("text")[1].AppendChild($xml.CreateTextNode("{body}")) > $null;'  # noqa: E501
                f"$n = [Windows.UI.Notifications.ToastNotification]::new($xml);"
                f'[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("GPredict").Show($n);'
            )
            subprocess.Popen(
                ["powershell", "-Command", ps_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Without this, launching powershell.exe from this windowed
                # (console-less) PyInstaller build briefly flashes a blue
                # console window on screen every time this fallback fires.
                creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
            )
        except Exception:  # noqa: BLE001
            pass


def _send_plyer(title: str, body: str) -> None:
    """Universal fallback via plyer (optional dependency)."""
    try:
        from plyer import notification  # noqa: PLC0415

        notification.notify(title=title, message=body, app_name="FBSAT59", timeout=8)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

_DEFAULT_WARN_MINUTES = 5
_DEFAULT_LOS_WARN_ENABLED = False
_DEFAULT_LOS_WARN_MINUTES = 2
_DEFAULT_ENABLED = True


NotificationSettings = dict[str, int | bool]


def load_notification_settings(conn: sqlite3.Connection) -> NotificationSettings:
    """Load notification preferences from app_settings.

    Returns a dict with keys:
      enabled (bool), warn_minutes (int),
      los_enabled (bool), los_warn_minutes (int)
    """
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'notification_settings'"
    ).fetchone()
    import json  # noqa: PLC0415

    defaults: NotificationSettings = {
        "enabled": _DEFAULT_ENABLED,
        "warn_minutes": _DEFAULT_WARN_MINUTES,
        "los_enabled": _DEFAULT_LOS_WARN_ENABLED,
        "los_warn_minutes": _DEFAULT_LOS_WARN_MINUTES,
    }
    if row and row["value"]:
        try:
            stored: dict[str, int | bool] = json.loads(str(row["value"]))
            defaults.update(stored)
        except Exception:  # noqa: BLE001
            pass
    return defaults


def save_notification_settings(conn: sqlite3.Connection, settings: NotificationSettings) -> None:
    """Persist notification preferences to app_settings."""
    import json  # noqa: PLC0415

    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at)"
        " VALUES ('notification_settings', ?, CURRENT_TIMESTAMP)",
        (json.dumps(settings),),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# PassNotifier
# ---------------------------------------------------------------------------


class PassNotifier:
    """Check upcoming passes and fire desktop notifications.

    Call ``check()`` once per second from the main timer tick.
    The notifier tracks which (satellite, AOS) pairs have already been
    notified to avoid duplicate alerts.

    Args:
        conn: SQLite connection used to read notification settings.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._notified_aos: set[str] = set()  # "{norad}_{aos_iso}"
        self._notified_los: set[str] = set()  # "{norad}_{los_iso}"
        self._settings: NotificationSettings = load_notification_settings(conn)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def reload_settings(self) -> None:
        """Re-read settings from DB (call after Settings dialog OK)."""
        self._settings = load_notification_settings(self._conn)

    def check(self, passes: list[PassInfo], sat_name: str) -> None:
        """Check passes for imminent AOS/LOS and fire notifications as needed.

        Args:
            passes:   upcoming passes for one satellite (from _current_passes)
            sat_name: human-readable satellite name for the notification title
        """
        if not self._settings["enabled"]:
            return

        now = datetime.now(UTC)
        warn_min = int(self._settings["warn_minutes"])
        warn_delta = timedelta(minutes=warn_min)

        los_enabled = bool(self._settings["los_enabled"])
        los_min = int(self._settings["los_warn_minutes"])
        los_delta = timedelta(minutes=los_min)

        for p in passes:
            aos_key = f"{sat_name}_{p.aos.isoformat()}"
            los_key = f"{sat_name}_{p.los.isoformat()}"

            # AOS notification: fire when within warn_delta of AOS and not yet fired
            time_to_aos = p.aos - now
            if aos_key not in self._notified_aos and timedelta(0) <= time_to_aos <= warn_delta:
                self._notified_aos.add(aos_key)
                self._fire_aos(sat_name, p, warn_min, time_to_aos)

            # LOS notification
            if los_enabled:
                time_to_los = p.los - now
                if (
                    los_key not in self._notified_los
                    and timedelta(0) <= time_to_los <= los_delta
                    and p.aos <= now  # only notify during a pass
                ):
                    self._notified_los.add(los_key)
                    self._fire_los(sat_name, p, time_to_los)

    def check_group(self, group_results: list[object], use_utc: bool = False) -> None:
        """Check all satellites from group search results for AOS/LOS.

        Args:
            group_results: list[GroupPassResult] from PassPanel
            use_utc:       True → times shown in UTC, False → local time
        """
        if not self._settings["enabled"]:
            return

        now = datetime.now(UTC)
        warn_min = int(self._settings["warn_minutes"])
        warn_delta = timedelta(minutes=warn_min)

        los_enabled = bool(self._settings["los_enabled"])
        los_min = int(self._settings["los_warn_minutes"])
        los_delta = timedelta(minutes=los_min)

        for r in group_results:
            sat_name: str = r.sat_name  # type: ignore[attr-defined]
            p = r.pass_info  # type: ignore[attr-defined]

            aos_key = f"{sat_name}_{p.aos.isoformat()}"
            los_key = f"{sat_name}_{p.los.isoformat()}"

            time_to_aos = p.aos - now
            if aos_key not in self._notified_aos and timedelta(0) <= time_to_aos <= warn_delta:
                self._notified_aos.add(aos_key)
                self._fire_aos(sat_name, p, warn_min, time_to_aos)

            if los_enabled:
                time_to_los = p.los - now
                if (
                    los_key not in self._notified_los
                    and timedelta(0) <= time_to_los <= los_delta
                    and p.aos <= now
                ):
                    self._notified_los.add(los_key)
                    self._fire_los(sat_name, p, time_to_los)

    def clear(self) -> None:
        """Reset the notified-pass history (e.g. on satellite change)."""
        self._notified_aos.clear()
        self._notified_los.clear()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _fmt_time(dt: datetime) -> str:
        """Format a UTC datetime as local HH:MM for notification body."""
        return dt.astimezone().strftime("%H:%M")

    def _fire_aos(
        self,
        sat_name: str,
        p: PassInfo,
        warn_min: int,
        time_to_aos: timedelta,
    ) -> None:
        """Build and send the AOS notification."""
        mins_left = int(time_to_aos.total_seconds() / 60)
        if mins_left <= 0:
            title = f"🛰 {sat_name} is rising now"
        else:
            title = f"🛰 {sat_name} rising in {mins_left} min"
        body = (
            f"Max El: {p.max_elevation_deg:.0f}°  "
            f"AOS {self._fmt_time(p.aos)}  "
            f"LOS {self._fmt_time(p.los)}"
        )
        _send_notification(title, body)

    def _fire_los(self, sat_name: str, p: PassInfo, time_to_los: timedelta) -> None:
        """Build and send the LOS notification."""
        mins_left = int(time_to_los.total_seconds() / 60)
        if mins_left <= 0:
            title = f"🛰 {sat_name} is setting now"
        else:
            title = f"🛰 {sat_name} setting in {mins_left} min"
        body = f"LOS at {self._fmt_time(p.los)}"
        _send_notification(title, body)
