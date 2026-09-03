"""Tests for core.app_restart.restart_application().

Used by the language switch: changing the UI language relaunches the app
so every already-built widget picks up the new catalog.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from core.app_restart import RESTART_ENV_VAR, restart_application


@pytest.fixture(autouse=True)
def _fake_qapp() -> MagicMock:
    """QApplication.instance() is None outside a running Qt event loop in
    these tests -- provide a fake so restart_application()'s app.quit()
    branch is exercised by default."""
    fake_app = MagicMock()
    with patch("core.app_restart.QApplication") as mock_cls:
        mock_cls.instance.return_value = fake_app
        yield fake_app


class TestRestartApplication:
    def test_dev_mode_relaunches_with_python_executable_and_full_argv(
        self, _fake_qapp: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "argv", ["src/main.py", "--some-flag"])

        with patch("core.app_restart.subprocess.Popen") as mock_popen:
            restart_application()

        args, _kwargs = mock_popen.call_args
        launched_args = args[0]
        assert launched_args[0] == sys.executable
        assert launched_args[1:] == ["src/main.py", "--some-flag"]

    def test_frozen_mode_drops_argv0_duplicate(
        self, _fake_qapp: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "argv", ["/Applications/FBSAT59.app/Contents/MacOS/FBSAT59"])

        with patch("core.app_restart.subprocess.Popen") as mock_popen:
            restart_application()

        args, _kwargs = mock_popen.call_args
        launched_args = args[0]
        assert launched_args == [sys.executable]

    def test_sets_restart_env_var(self, _fake_qapp: MagicMock) -> None:
        with patch("core.app_restart.subprocess.Popen") as mock_popen:
            restart_application()

        _args, kwargs = mock_popen.call_args
        assert kwargs["env"][RESTART_ENV_VAR] == "1"

    def test_detaches_the_relaunched_process(
        self, _fake_qapp: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The child must not share the parent's terminal session / stdio,
        or it is SIGHUP'd when the dev launcher's shell tears down."""
        monkeypatch.setattr(sys, "platform", "darwin")
        with patch("core.app_restart.subprocess.Popen") as mock_popen:
            restart_application()

        _args, kwargs = mock_popen.call_args
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL

    def test_quits_the_current_application(self, _fake_qapp: MagicMock) -> None:
        with patch("core.app_restart.subprocess.Popen"):
            restart_application()

        _fake_qapp.quit.assert_called_once()

    def test_no_app_instance_does_not_crash(self) -> None:
        """If called before a QApplication exists, quitting is simply
        skipped rather than raising."""
        with (
            patch("core.app_restart.QApplication") as mock_cls,
            patch("core.app_restart.subprocess.Popen"),
        ):
            mock_cls.instance.return_value = None
            restart_application()  # must not raise
