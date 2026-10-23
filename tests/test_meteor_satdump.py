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


def _write_iq_wav(path: Path, samples: int = 1000, rate: int = 250_000, fmt_tag: int = 3) -> bytes:
    """Write a float32-stereo WAV like sdr.recorder does; return the raw IQ payload."""
    import struct

    import numpy as np

    payload = np.arange(samples * 2, dtype=np.float32).tobytes()
    fmt = struct.pack("<HHIIHH", fmt_tag, 2, rate, rate * 8, 8, 32)
    header = (
        b"RIFF"
        + struct.pack("<I", 0)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", 16)
        + fmt
        + b"fact"
        + struct.pack("<I", 4)
        + struct.pack("<I", samples)
        + b"data"
        + struct.pack("<I", 0)  # bogus size: the length must come from the file size
    )
    path.write_bytes(header + payload)
    return payload


class TestParseIqWav:
    def test_returns_rate_offset_and_length_from_file_size(self, tmp_path: Path) -> None:
        from comms.meteor.satdump import parse_iq_wav

        wav = tmp_path / "a.iq.wav"
        payload = _write_iq_wav(wav)
        rate, offset, nbytes = parse_iq_wav(wav)
        assert rate == 250_000
        assert offset == 56
        assert nbytes == len(payload)

    def test_partial_trailing_sample_is_dropped(self, tmp_path: Path) -> None:
        from comms.meteor.satdump import parse_iq_wav

        wav = tmp_path / "a.iq.wav"
        _write_iq_wav(wav)
        with wav.open("ab") as f:
            f.write(b"\x00" * 3)
        assert parse_iq_wav(wav)[2] % 8 == 0

    def test_rejects_non_float_wav(self, tmp_path: Path) -> None:
        from comms.meteor.satdump import IqWavError, parse_iq_wav

        wav = tmp_path / "a.wav"
        _write_iq_wav(wav, fmt_tag=1)
        with pytest.raises(IqWavError):
            parse_iq_wav(wav)

    def test_rejects_non_wav(self, tmp_path: Path) -> None:
        from comms.meteor.satdump import IqWavError, parse_iq_wav

        bad = tmp_path / "x.wav"
        bad.write_bytes(b"not a wav file at all")
        with pytest.raises(IqWavError):
            parse_iq_wav(bad)


class TestOfflineDecodeCommand:
    def _proc(self, tmp_path: Path, qtbot: QtBot, fast: bool) -> Any:
        from comms.meteor.satdump import SatDumpProcess

        wav = tmp_path / "a.iq.wav"
        _write_iq_wav(wav)
        return SatDumpProcess(
            pipeline="meteor_m2-x_lrpt",
            source="",
            frequency=137_900_000,
            samplerate=250_000,
            output_dir=tmp_path / "out",
            input_wav=wav,
            convert_to_cf32=fast,
        )

    def test_normal_mode_passes_the_wav_with_baseband_input_level(
        self, tmp_path: Path, qtbot: QtBot
    ) -> None:
        proc = self._proc(tmp_path, qtbot, fast=False)
        cmd = proc._build_file_cmd(Path("/x/satdump"))
        assert cmd is not None
        assert cmd[1:3] == ["meteor_m2-x_lrpt", "baseband"]
        assert cmd[3] == str(tmp_path / "a.iq.wav")
        assert cmd[cmd.index("--samplerate") + 1] == "250000"
        assert cmd[cmd.index("--baseband_format") + 1] == "cf32"
        assert "--tle_override" in cmd  # skips the CelesTrak download
        assert "--pll_bw" not in cmd  # SatDump's default gave the cleanest image
        assert not any("doppler" in c.lower() for c in cmd)
        assert "live" not in cmd

    def test_fast_mode_converts_to_raw_cf32_without_the_header(
        self, tmp_path: Path, qtbot: QtBot
    ) -> None:
        proc = self._proc(tmp_path, qtbot, fast=True)
        cmd = proc._build_file_cmd(Path("/x/satdump"))
        assert cmd is not None
        raw = Path(cmd[3])
        assert raw.suffix == ".cf32"
        assert raw.read_bytes() == (tmp_path / "a.iq.wav").read_bytes()[56:]
        proc._cleanup_temp()
        assert not raw.exists()

    def test_too_low_sample_rate_is_rejected(self, tmp_path: Path, qtbot: QtBot) -> None:
        from comms.meteor.satdump import SatDumpProcess

        wav = tmp_path / "low.iq.wav"
        _write_iq_wav(wav, rate=48_000)
        proc = SatDumpProcess(
            pipeline="meteor_m2-x_lrpt",
            source="",
            frequency=137_900_000,
            samplerate=48_000,
            output_dir=tmp_path / "out",
            input_wav=wav,
        )
        errors: list[str] = []
        proc.finished_err.connect(errors.append)
        assert proc._build_file_cmd(Path("/x/satdump")) is None
        assert errors and "too low" in errors[0]

    def test_baseband_progress_line_is_parsed(self, tmp_path: Path, qtbot: QtBot) -> None:
        proc = self._proc(tmp_path, qtbot, fast=False)
        seen: list[int] = []
        proc.progress.connect(seen.append)
        proc._parse_line("(I) Progress 5.300000%, SNR : 0.000000dB, Peak SNR: 0.000000dB")
        assert seen == [5]
