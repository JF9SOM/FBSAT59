"""Saved Sound Card indices must follow the chosen device when PortAudio re-orders its list."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from comms.audio_device_manager import heal_soundcard_settings, resolve_device_index

APIS = ["Core Audio", "MME"]


def _dev(name: str, out: int = 2, inp: int = 0, api: int = 0) -> dict[str, Any]:
    return {"name": name, "max_output_channels": out, "max_input_channels": inp, "hostapi": api}


class _FakeSd:
    def __init__(self, devices: list[dict[str, Any]]) -> None:
        self._devices = devices

    def query_devices(self) -> list[dict[str, Any]]:
        return self._devices

    def query_hostapis(self) -> list[dict[str, Any]]:
        return [{"name": n} for n in APIS]


def _conn(data: dict[str, Any]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at DATETIME)"
    )
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('soundcard_settings', ?)",
        (json.dumps(data),),
    )
    return conn


def _saved(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM app_settings").fetchone()
    return json.loads(row[0])  # type: ignore[no-any-return]


def test_index_follows_name_when_list_is_reordered() -> None:
    devices = [_dev("Speakers"), _dev("USB Audio CODEC")]
    assert resolve_device_index(0, "USB Audio CODEC", None, "output", devices, APIS) == (1, True)


def test_unchanged_index_is_kept() -> None:
    devices = [_dev("USB Audio CODEC"), _dev("Speakers")]
    assert resolve_device_index(0, "USB Audio CODEC", None, "output", devices, APIS) == (0, True)


def test_missing_device_is_reported_and_index_kept() -> None:
    assert resolve_device_index(3, "Gone", None, "output", [_dev("Speakers")], APIS) == (3, False)


def test_host_api_disambiguates_same_name() -> None:
    devices = [_dev("USB Audio CODEC", api=1), _dev("USB Audio CODEC", api=0)]
    assert resolve_device_index(5, "USB Audio CODEC", "Core Audio", "output", devices, APIS) == (
        1,
        True,
    )


def test_input_only_device_is_not_an_output_candidate() -> None:
    devices = [_dev("Mic", out=0, inp=1)]
    assert resolve_device_index(0, "Mic", None, "output", devices, APIS) == (0, False)


def test_heal_rewrites_index_from_legacy_label() -> None:
    conn = _conn(
        {
            "configured": True,
            "output_device_index": 0,
            "output_device_label": "0: USB Audio CODEC",
        }
    )
    sd = _FakeSd([_dev("Speakers"), _dev("USB Audio CODEC")])
    assert heal_soundcard_settings(conn, sd) == []
    assert _saved(conn)["output_device_index"] == 1


def test_heal_reports_missing_device_and_leaves_setting() -> None:
    conn = _conn({"configured": True, "output_device_index": 4, "output_device_name": "Gone"})
    assert heal_soundcard_settings(conn, _FakeSd([_dev("Speakers")])) == ["output"]
    assert _saved(conn)["output_device_index"] == 4


def test_reinitialize_portaudio_repoints_saved_indices(monkeypatch: Any) -> None:
    """A rebuilt device list renumbers devices; the saved numbers must follow."""
    import sounddevice as sd

    from comms.audio_device_manager import AudioDeviceManager

    conn = _conn(
        {
            "configured": True,
            "output_device_index": 15,
            "output_device_name": "IC9700-TXIN",
            "output_device_hostapi": "MME",
        }
    )
    rebuilt = [_dev("Speakers"), _dev("x"), _dev("IC9700-TXIN", api=1)]
    fake = _FakeSd(rebuilt)
    monkeypatch.setattr(sd, "_terminate", lambda: None)
    monkeypatch.setattr(sd, "_initialize", lambda: None)
    monkeypatch.setattr(sd, "stop", lambda: None)
    monkeypatch.setattr(sd, "query_devices", fake.query_devices)
    monkeypatch.setattr(sd, "query_hostapis", fake.query_hostapis)

    mgr = AudioDeviceManager()
    mgr.set_heal_connection(conn)
    assert mgr.reinitialize_portaudio() is True

    row = conn.execute("SELECT value FROM app_settings").fetchone()
    assert json.loads(row[0])["output_device_index"] == 2
