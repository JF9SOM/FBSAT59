"""Tests for core.terminal_launcher — cross-platform 'open terminal and run' helper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.terminal_launcher import open_terminal_and_run


class TestMacOS:
    def test_calls_osascript_with_do_script(self) -> None:
        with (
            patch("core.terminal_launcher.sys.platform", "darwin"),
            patch("core.terminal_launcher.subprocess.Popen") as mock_popen,
        ):
            success, error = open_terminal_and_run("brew install foo")

        assert success is True
        assert error == ""
        args = mock_popen.call_args[0][0]
        assert args[0] == "osascript"
        assert args[1] == "-e"
        assert "brew install foo" in args[2]
        assert 'tell application "Terminal"' in args[2]

    def test_escapes_double_quotes_and_backslashes(self) -> None:
        with (
            patch("core.terminal_launcher.sys.platform", "darwin"),
            patch("core.terminal_launcher.subprocess.Popen") as mock_popen,
        ):
            open_terminal_and_run('echo "hi\\there"')

        script = mock_popen.call_args[0][0][2]
        # The AppleScript string literal must not contain an unescaped quote
        # or backslash that would break out of the do script "..." literal.
        inner = script.split('do script "', 1)[1].rsplit('"\nend tell', 1)[0]
        assert inner.count('\\"') >= 1
        assert "\\\\" in inner

    def test_popen_failure_reports_error(self) -> None:
        with (
            patch("core.terminal_launcher.sys.platform", "darwin"),
            patch("core.terminal_launcher.subprocess.Popen", side_effect=OSError("nope")),
        ):
            success, error = open_terminal_and_run("brew install foo")

        assert success is False
        assert "nope" in error


class TestWindows:
    def test_calls_cmd_start_k(self) -> None:
        with (
            patch("core.terminal_launcher.sys.platform", "win32"),
            patch("core.terminal_launcher.subprocess.Popen") as mock_popen,
        ):
            success, error = open_terminal_and_run("choco install foo")

        assert success is True
        assert error == ""
        args = mock_popen.call_args[0][0]
        assert args == ["cmd", "/c", "start", "cmd", "/k", "choco install foo"]

    def test_popen_failure_reports_error(self) -> None:
        with (
            patch("core.terminal_launcher.sys.platform", "win32"),
            patch("core.terminal_launcher.subprocess.Popen", side_effect=OSError("nope")),
        ):
            success, error = open_terminal_and_run("choco install foo")

        assert success is False
        assert "nope" in error


class TestLinux:
    def test_uses_first_available_terminal(self) -> None:
        def fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}" if name == "konsole" else None

        with (
            patch("core.terminal_launcher.sys.platform", "linux"),
            patch("core.terminal_launcher.shutil.which", side_effect=fake_which),
            patch("core.terminal_launcher.subprocess.Popen") as mock_popen,
        ):
            success, error = open_terminal_and_run("sudo apt install foo")

        assert success is True
        assert error == ""
        args = mock_popen.call_args[0][0]
        assert args[0] == "/usr/bin/konsole"
        assert args[1:4] == ["-e", "bash", "-c"]
        assert "sudo apt install foo" in args[4]

    def test_prefers_gnome_terminal_over_others(self) -> None:
        with (
            patch("core.terminal_launcher.sys.platform", "linux"),
            patch("core.terminal_launcher.shutil.which", return_value="/usr/bin/x"),
            patch("core.terminal_launcher.subprocess.Popen") as mock_popen,
        ):
            open_terminal_and_run("echo hi")

        args = mock_popen.call_args[0][0]
        assert args[1:3] == ["--", "bash"]

    def test_no_terminal_found_returns_failure(self) -> None:
        with (
            patch("core.terminal_launcher.sys.platform", "linux"),
            patch("core.terminal_launcher.shutil.which", return_value=None),
        ):
            success, error = open_terminal_and_run("echo hi")

        assert success is False
        assert error != ""

    def test_falls_back_when_popen_raises(self) -> None:
        calls: list[str] = []

        def fake_popen(argv: list[str]) -> MagicMock:
            calls.append(argv[0])
            if "gnome-terminal" in argv[0]:
                raise OSError("boom")
            return MagicMock()

        with (
            patch("core.terminal_launcher.sys.platform", "linux"),
            patch(
                "core.terminal_launcher.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"
            ),
            patch("core.terminal_launcher.subprocess.Popen", side_effect=fake_popen),
        ):
            success, error = open_terminal_and_run("echo hi")

        assert success is True
        assert error == ""
        assert len(calls) == 2  # gnome-terminal failed, konsole succeeded
