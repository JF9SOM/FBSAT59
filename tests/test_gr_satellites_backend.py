"""Tests for comms/telemetry/gr_satellites_backend.py.

Covers the bundled-vs-system executable resolution branch in start() (the
PYTHONPATH NumPy-1.x workaround must apply only to system installs, not the
self-contained bundled conda-pack env) and the satyaml directory fallback
used by list_gr_satellites_norads()/list_gr_satellites_with_names()/
get_satellite_info(). No real gr_satellites/GNU Radio subprocess is spawned;
subprocess.Popen is monkeypatched.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import comms.telemetry.gr_satellites_backend as backend


class _FakeProc:
    def __init__(self) -> None:
        self.stdout = iter([])
        self.returncode = None

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return 0


class TestStartExecutableResolution:
    def test_returns_error_when_not_found(self) -> None:
        b = backend.GrSatellitesBackend()
        with patch.object(backend, "find_gr_satellites_executable", return_value=None):
            ok, msg = b.start(25544, 48000, MagicMock())
        assert ok is False
        assert "not found" in msg

    def test_bundled_executable_skips_pythonpath_hack(self) -> None:
        b = backend.GrSatellitesBackend()
        bundled_path = Path("/home/user/.local/share/fbsat59/gr-satellites-env/bin/gr_satellites")
        with (
            patch.object(
                backend, "find_gr_satellites_executable", return_value=(bundled_path, True)
            ),
            patch.object(backend.subprocess, "Popen", return_value=_FakeProc()) as mock_popen,
        ):
            ok, _msg = b.start(25544, 48000, MagicMock())

        assert ok is True
        cmd, kwargs = mock_popen.call_args
        assert cmd[0][0] == str(bundled_path)
        # The bundled env is self-contained; the apt NumPy-1.x PYTHONPATH
        # hack must not be applied to it (checking the ambient PYTHONPATH,
        # if any, doesn't already contain it — rather than requiring the key
        # be entirely absent, since the test's own environment may set one).
        assert backend._GR_PYTHONPATH not in kwargs["env"].get("PYTHONPATH", "")
        b.stop()

    def test_system_executable_applies_pythonpath_hack(self) -> None:
        b = backend.GrSatellitesBackend()
        system_path = Path("/usr/bin/gr_satellites")
        with (
            patch.object(
                backend, "find_gr_satellites_executable", return_value=(system_path, False)
            ),
            patch.object(backend.subprocess, "Popen", return_value=_FakeProc()) as mock_popen,
        ):
            ok, _msg = b.start(25544, 48000, MagicMock())

        assert ok is True
        cmd, kwargs = mock_popen.call_args
        assert cmd[0][0] == str(system_path)
        assert backend._GR_PYTHONPATH in kwargs["env"]["PYTHONPATH"]
        b.stop()

    def test_command_includes_norad_and_samp_rate(self) -> None:
        b = backend.GrSatellitesBackend()
        with (
            patch.object(
                backend,
                "find_gr_satellites_executable",
                return_value=(Path("/usr/bin/gr_satellites"), False),
            ),
            patch.object(backend.subprocess, "Popen", return_value=_FakeProc()) as mock_popen,
        ):
            b.start(43803, 250000, MagicMock())

        cmd = mock_popen.call_args[0][0]
        assert "43803" in cmd
        assert "250000" in cmd
        b.stop()


class TestDetectGrSatellites:
    def test_true_when_resolvable(self) -> None:
        with patch.object(
            backend, "find_gr_satellites_executable", return_value=(Path("/usr/bin/x"), False)
        ):
            assert backend.detect_gr_satellites() is True

    def test_false_when_unresolvable(self) -> None:
        with patch.object(backend, "find_gr_satellites_executable", return_value=None):
            assert backend.detect_gr_satellites() is False


class TestSatyamlDirFallback:
    def test_prefers_bundled_dir(self, tmp_path: Path) -> None:
        bundled = tmp_path / "bundled_satyaml"
        bundled.mkdir()
        with patch.object(backend, "bundled_satyaml_dir", return_value=bundled):
            assert backend._satyaml_dir() == bundled

    def test_falls_back_to_system_dir_when_it_exists(self, tmp_path: Path) -> None:
        system_dir = tmp_path / "system_satyaml"
        system_dir.mkdir()
        with (
            patch.object(backend, "bundled_satyaml_dir", return_value=None),
            patch.object(backend, "_SYSTEM_SATYAML_DIR", system_dir),
        ):
            assert backend._satyaml_dir() == system_dir

    def test_none_when_neither_exists(self, tmp_path: Path) -> None:
        with (
            patch.object(backend, "bundled_satyaml_dir", return_value=None),
            patch.object(backend, "_SYSTEM_SATYAML_DIR", tmp_path / "does-not-exist"),
        ):
            assert backend._satyaml_dir() is None


class TestListSatellitesUsesResolvedDir:
    def test_list_norads_reads_from_resolved_dir(self, tmp_path: Path) -> None:
        satyaml = tmp_path / "satyaml"
        satyaml.mkdir()
        (satyaml / "iss.yml").write_text("norad: 25544\nname: ISS\n")
        (satyaml / "jo97.yml").write_text("norad: 43803\nname: JO-97\n")

        with patch.object(backend, "_satyaml_dir", return_value=satyaml):
            norads = backend.list_gr_satellites_norads()
            names = backend.list_gr_satellites_with_names()

        assert norads == {25544, 43803}
        assert ("JO-97", 43803) in [(n, r) for r, n in names]

    def test_get_satellite_info_reads_transmitters(self, tmp_path: Path) -> None:
        satyaml = tmp_path / "satyaml"
        satyaml.mkdir()
        (satyaml / "jo97.yml").write_text(
            "norad: 43803\nname: JO-97\ntransmitters:\n  Transmitter 1:\n    frequency: 145857000\n"
        )

        with patch.object(backend, "_satyaml_dir", return_value=satyaml):
            info = backend.get_satellite_info(43803)

        assert info is not None
        assert info["name"] == "JO-97"
        assert info["frequencies"] == [145857000]
