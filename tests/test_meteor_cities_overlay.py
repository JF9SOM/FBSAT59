"""Tests for comms.meteor.cities_overlay.

Covers find_product_cbor() (pure filesystem logic) and CitiesOverlayProcess
(the QThread wrapper around SatDump's "project" CLI tool) with
find_satdump()/subprocess.run() mocked out -- no real SatDump binary or
product.cbor needed. See CLAUDE.md for how the underlying `satdump project`
command line was confirmed against a real product.cbor.
"""

from __future__ import annotations

import subprocess as _subprocess
from pathlib import Path
from typing import Any

import pytest
from pytestqt.qtbot import QtBot


class TestFindProductCbor:
    def test_finds_product_cbor_in_subfolder(self, tmp_path: Path) -> None:
        from comms.meteor.cities_overlay import find_product_cbor

        instrument_dir = tmp_path / "MSU-MR"
        instrument_dir.mkdir()
        cbor_path = instrument_dir / "product.cbor"
        cbor_path.write_bytes(b"\x00")

        result = find_product_cbor(tmp_path)

        assert result == cbor_path

    def test_finds_product_cbor_when_given_the_instrument_dir_directly(
        self, tmp_path: Path
    ) -> None:
        from comms.meteor.cities_overlay import find_product_cbor

        cbor_path = tmp_path / "product.cbor"
        cbor_path.write_bytes(b"\x00")

        result = find_product_cbor(tmp_path)

        assert result == cbor_path

    def test_returns_none_when_not_found(self, tmp_path: Path) -> None:
        from comms.meteor.cities_overlay import find_product_cbor

        (tmp_path / "some_image.png").write_bytes(b"\x00")

        assert find_product_cbor(tmp_path) is None


class TestCitiesOverlayProcess:
    def test_finished_err_when_satdump_not_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, qtbot: QtBot
    ) -> None:
        from comms.meteor import cities_overlay as mod

        monkeypatch.setattr(mod, "find_satdump", lambda: None)

        proc = mod.CitiesOverlayProcess(tmp_path / "product.cbor", tmp_path / "out.png")
        errors: list[str] = []
        oks: list[str] = []
        proc.finished_err.connect(errors.append)
        proc.finished_ok.connect(oks.append)

        proc.run()

        assert not oks
        assert len(errors) == 1
        assert "not found" in errors[0].lower()

    def test_finished_ok_when_subprocess_succeeds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, qtbot: QtBot
    ) -> None:
        from comms.meteor import cities_overlay as mod

        monkeypatch.setattr(mod, "find_satdump", lambda: Path("/fake/satdump"))
        output_path = tmp_path / "out.png"

        def _fake_run(cmd: list[str], **kwargs: Any) -> _subprocess.CompletedProcess[str]:
            # Simulate satdump actually writing the output file.
            output_path.write_bytes(b"\x89PNG\r\n")
            return _subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(_subprocess, "run", _fake_run)

        proc = mod.CitiesOverlayProcess(tmp_path / "product.cbor", output_path)
        oks: list[str] = []
        errors: list[str] = []
        proc.finished_ok.connect(oks.append)
        proc.finished_err.connect(errors.append)

        proc.run()

        assert not errors
        assert oks == [str(output_path)]

    def test_finished_err_when_subprocess_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, qtbot: QtBot
    ) -> None:
        from comms.meteor import cities_overlay as mod

        monkeypatch.setattr(mod, "find_satdump", lambda: Path("/fake/satdump"))

        def _fake_run(cmd: list[str], **kwargs: Any) -> _subprocess.CompletedProcess[str]:
            return _subprocess.CompletedProcess(
                cmd, returncode=1, stdout="", stderr="type must be string, but is number"
            )

        monkeypatch.setattr(_subprocess, "run", _fake_run)

        proc = mod.CitiesOverlayProcess(tmp_path / "product.cbor", tmp_path / "out.png")
        oks: list[str] = []
        errors: list[str] = []
        proc.finished_ok.connect(oks.append)
        proc.finished_err.connect(errors.append)

        proc.run()

        assert not oks
        assert len(errors) == 1
        assert "type must be string" in errors[0]

    def test_finished_err_when_output_file_missing_despite_zero_exit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, qtbot: QtBot
    ) -> None:
        """Belt-and-braces: even if satdump reports success, treat a missing
        output file as failure rather than telling the caller it worked."""
        from comms.meteor import cities_overlay as mod

        monkeypatch.setattr(mod, "find_satdump", lambda: Path("/fake/satdump"))

        def _fake_run(cmd: list[str], **kwargs: Any) -> _subprocess.CompletedProcess[str]:
            return _subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(_subprocess, "run", _fake_run)

        proc = mod.CitiesOverlayProcess(tmp_path / "product.cbor", tmp_path / "out.png")
        oks: list[str] = []
        errors: list[str] = []
        proc.finished_ok.connect(oks.append)
        proc.finished_err.connect(errors.append)

        proc.run()

        assert not oks
        assert len(errors) == 1

    def test_finished_err_on_timeout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, qtbot: QtBot
    ) -> None:
        from comms.meteor import cities_overlay as mod

        monkeypatch.setattr(mod, "find_satdump", lambda: Path("/fake/satdump"))

        def _fake_run(cmd: list[str], **kwargs: Any) -> _subprocess.CompletedProcess[str]:
            raise _subprocess.TimeoutExpired(cmd=cmd, timeout=120)

        monkeypatch.setattr(_subprocess, "run", _fake_run)

        proc = mod.CitiesOverlayProcess(tmp_path / "product.cbor", tmp_path / "out.png")
        errors: list[str] = []
        proc.finished_err.connect(errors.append)

        proc.run()

        assert len(errors) == 1
        assert "timed out" in errors[0].lower()

    def test_command_includes_expected_flags(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, qtbot: QtBot
    ) -> None:
        """Regression guard for the exact CLI shape confirmed manually
        against SatDump 1.2.2 (see CLAUDE.md)."""
        from comms.meteor import cities_overlay as mod

        monkeypatch.setattr(mod, "find_satdump", lambda: Path("/fake/satdump"))
        product_cbor = tmp_path / "product.cbor"
        output_path = tmp_path / "out.png"
        captured: dict[str, list[str]] = {}

        def _fake_run(cmd: list[str], **kwargs: Any) -> _subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            output_path.write_bytes(b"\x89PNG\r\n")
            return _subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(_subprocess, "run", _fake_run)

        proc = mod.CitiesOverlayProcess(product_cbor, output_path)
        proc.run()

        cmd = captured["cmd"]
        assert cmd[1] == "project"
        assert "-layer" in cmd
        assert "-target" in cmd
        assert str(product_cbor) in cmd
        assert str(output_path) in cmd
        assert mod.CITIES_OVERLAY_COMPOSITE in cmd
        assert "--draw_cities_overlay" in cmd
        assert "--auto_mode" in cmd
        assert "--auto_scale_mode" in cmd
