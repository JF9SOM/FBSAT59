"""Tests for comms.meteor.satdump — Windows graceful-stop mechanism.

GitHub Issue #27 follow-up (2026-08-22): a real Windows 11 test showed a
strong, well-locked METEOR-M pass (Deframer SYNCED for several minutes)
still producing only a .cadu file and no image, with the app reporting
"satdump exited with code 3221225786" (STATUS_CONTROL_C_EXIT). Checking
SatDump's own source (src-cli/live.cpp, tag 1.2.2) confirmed it only
registers signal(SIGINT, ...) / signal(SIGTERM, ...) -- there is no
CTRL_BREAK_EVENT (SIGBREAK) handler at all, so the CTRL_BREAK_EVENT this
code used to send fell through to the OS's default "just kill it"
behavior. The fix switches to CTRL_C_EVENT (which SatDump's SIGINT handler
does respond to) and drops CREATE_NEW_PROCESS_GROUP from the Popen flags,
since Microsoft's docs state that flag disables CTRL+C signals entirely
for the new process group.
"""

from __future__ import annotations

import ctypes
import subprocess as _subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pytestqt.qtbot import QtBot


class TestWindowsCreationFlags:
    """SatDumpProcess.run()'s Windows Popen call must not combine
    CREATE_NO_WINDOW with CREATE_NEW_PROCESS_GROUP."""

    def test_windows_popen_omits_new_process_group_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, qtbot: QtBot
    ) -> None:
        from comms.meteor import satdump as satdump_mod

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(_subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
        monkeypatch.setattr(_subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
        monkeypatch.setattr(satdump_mod, "find_satdump", lambda: Path("C:/fake/satdump.exe"))

        captured: dict[str, Any] = {}

        class _FakeProc:
            stdout: Iterator[str] = iter([])
            returncode = 0

            def wait(self) -> None:
                return None

        def _fake_popen(cmd: list[str], **kwargs: Any) -> _FakeProc:
            captured.update(kwargs)
            return _FakeProc()

        monkeypatch.setattr(_subprocess, "Popen", _fake_popen)

        proc = satdump_mod.SatDumpProcess(
            pipeline="meteor_m2-x_lrpt",
            source="rtlsdr",
            frequency=137_900_000,
            samplerate=1_200_000,
            output_dir=tmp_path,
        )
        proc.run()

        assert captured["creationflags"] == _subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]


class TestSendGracefulStopWindows:
    """_send_graceful_stop_windows() must send CTRL_C_EVENT (Win32 constant
    0), never CTRL_BREAK_EVENT (1) -- see module docstring above."""

    def _install_fake_kernel32(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        calls: dict[str, Any] = {}

        class _FakeKernel32:
            def FreeConsole(self) -> bool:
                calls["free_console_count"] = calls.get("free_console_count", 0) + 1
                return True

            def AttachConsole(self, pid: int) -> bool:
                calls["attach_console_pid"] = pid
                return True

            def SetConsoleCtrlHandler(self, handler: object, add: bool) -> bool:
                calls.setdefault("set_ctrl_handler_add", []).append(add)
                return True

            def GenerateConsoleCtrlEvent(self, event: int, group: int) -> bool:
                calls["generate_event"] = (event, group)
                return True

        fake_kernel32 = _FakeKernel32()
        monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: fake_kernel32, raising=False)
        monkeypatch.setattr(ctypes, "WINFUNCTYPE", lambda *a, **k: lambda fn: fn, raising=False)
        monkeypatch.setattr("time.sleep", lambda *_a: None)
        return calls

    def test_sends_ctrl_c_event_not_ctrl_break(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from comms.meteor.satdump import _send_graceful_stop_windows

        calls = self._install_fake_kernel32(monkeypatch)

        ok = _send_graceful_stop_windows(1234)

        assert ok is True
        assert calls["attach_console_pid"] == 1234
        event, group = calls["generate_event"]
        assert event == 0  # CTRL_C_EVENT -- NOT 1 (CTRL_BREAK_EVENT)
        assert group == 0
        # A handler is registered before raising the event and unregistered
        # afterward, so this process doesn't get killed by its own signal.
        assert calls["set_ctrl_handler_add"] == [True, False]
        assert calls["free_console_count"] == 2  # once up front, once in finally

    def test_returns_false_if_attach_console_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from comms.meteor.satdump import _send_graceful_stop_windows

        class _FakeKernel32:
            def FreeConsole(self) -> bool:
                return True

            def AttachConsole(self, pid: int) -> bool:
                return False

        monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: _FakeKernel32(), raising=False)
        monkeypatch.setattr(ctypes, "WINFUNCTYPE", lambda *a, **k: lambda fn: fn, raising=False)

        assert _send_graceful_stop_windows(1234) is False
