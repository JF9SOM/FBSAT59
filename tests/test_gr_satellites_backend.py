"""Tests for comms/telemetry/gr_satellites_backend.py.

Covers the bundled-vs-system executable resolution branch in start() (the
PYTHONPATH NumPy-1.x workaround must apply only to system installs, not the
self-contained bundled conda-pack env) and the satyaml directory fallback
used by list_gr_satellites_norads()/list_gr_satellites_with_names()/
get_satellite_info(). No real gr_satellites/GNU Radio subprocess is spawned;
subprocess.Popen is monkeypatched.
"""

from __future__ import annotations

import socket
import subprocess
import time
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
        with patch.object(backend, "resolve_gr_satellites_command", return_value=None):
            ok, msg = b.start(25544, 48000, MagicMock())
        assert ok is False
        assert "not found" in msg

    def test_bundled_uses_explicit_python_and_skips_pythonpath_hack(self) -> None:
        b = backend.GrSatellitesBackend()
        bundled_python = "/home/user/.local/share/fbsat59/gr-satellites-env/bin/python"
        bundled_script = "/home/user/.local/share/fbsat59/gr-satellites-env/bin/gr_satellites"
        with (
            patch.object(
                backend,
                "resolve_gr_satellites_command",
                return_value=([bundled_python, bundled_script], True),
            ),
            patch.object(backend, "_supports_kiss_server", return_value=False),
            patch.object(backend.subprocess, "Popen", return_value=_FakeProc()) as mock_popen,
        ):
            ok, _msg = b.start(25544, 48000, MagicMock())

        assert ok is True
        cmd, kwargs = mock_popen.call_args
        # Must invoke via the bundled python explicitly, not the script's own
        # shebang (confirmed via CI: gr_satellites uses
        # "#!/usr/bin/env python", which has no absolute path for
        # conda-unpack to rewrite and would otherwise pick up whichever
        # "python" is first on the *caller's* PATH).
        assert cmd[0][0] == bundled_python
        assert cmd[0][1] == bundled_script
        # The bundled env is self-contained; the apt NumPy-1.x PYTHONPATH
        # hack must not be applied to it (checking the ambient PYTHONPATH,
        # if any, doesn't already contain it — rather than requiring the key
        # be entirely absent, since the test's own environment may set one).
        assert backend._GR_PYTHONPATH not in kwargs["env"].get("PYTHONPATH", "")
        b.stop()

    def test_system_executable_applies_pythonpath_hack(self) -> None:
        b = backend.GrSatellitesBackend()
        system_path = "/usr/bin/gr_satellites"
        with (
            patch.object(
                backend, "resolve_gr_satellites_command", return_value=([system_path], False)
            ),
            patch.object(backend, "_supports_kiss_server", return_value=False),
            patch.object(backend.subprocess, "Popen", return_value=_FakeProc()) as mock_popen,
        ):
            ok, _msg = b.start(25544, 48000, MagicMock())

        assert ok is True
        cmd, kwargs = mock_popen.call_args
        assert cmd[0][0] == system_path
        assert backend._GR_PYTHONPATH in kwargs["env"]["PYTHONPATH"]
        b.stop()

    def test_command_includes_norad_and_samp_rate(self) -> None:
        b = backend.GrSatellitesBackend()
        with (
            patch.object(
                backend,
                "resolve_gr_satellites_command",
                return_value=(["/usr/bin/gr_satellites"], False),
            ),
            patch.object(backend, "_supports_kiss_server", return_value=False),
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


# ---------------------------------------------------------------------------
# Phase 2: SatNOGS DB upload via --kiss_server
# ---------------------------------------------------------------------------


class TestSupportsKissServer:
    def setup_method(self) -> None:
        backend._kiss_server_supported_cache.clear()

    def test_detects_flag_in_help_output(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["gr_satellites", "--help"],
            returncode=1,
            stdout="",
            stderr="usage: ... [--kiss_server [PORT]] [--kiss_server_address ADDR] ...",
        )
        with patch.object(backend.subprocess, "run", return_value=completed) as mock_run:
            assert backend._supports_kiss_server(["gr_satellites"], {}) is True
            # Second call for the same argv_prefix must hit the cache rather
            # than re-running the ~0.3s --help probe.
            assert backend._supports_kiss_server(["gr_satellites"], {}) is True
        assert mock_run.call_count == 1

    def test_false_when_flag_absent(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["gr_satellites", "--help"],
            returncode=1,
            stdout="",
            stderr="usage: ... (older gr_satellites build, no kiss server flags) ...",
        )
        with patch.object(backend.subprocess, "run", return_value=completed):
            assert backend._supports_kiss_server(["old-gr-satellites"], {}) is False

    def test_false_on_oserror(self) -> None:
        with patch.object(backend.subprocess, "run", side_effect=OSError("not found")):
            assert backend._supports_kiss_server(["missing-binary"], {}) is False

    def test_false_on_timeout(self) -> None:
        with patch.object(
            backend.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="x", timeout=10),
        ):
            assert backend._supports_kiss_server(["slow-binary"], {}) is False

    def test_cache_is_keyed_by_argv_prefix(self) -> None:
        """A different resolved command (e.g. bundled vs. system) must be
        probed independently rather than sharing the first result."""
        with patch.object(
            backend.subprocess,
            "run",
            side_effect=[
                subprocess.CompletedProcess([], 1, "", "--kiss_server"),
                subprocess.CompletedProcess([], 1, "", "no such flag here"),
            ],
        ) as mock_run:
            assert backend._supports_kiss_server(["binary-a"], {}) is True
            assert backend._supports_kiss_server(["binary-b"], {}) is False
        assert mock_run.call_count == 2


class TestStartKissServerWiring:
    def test_adds_kiss_flags_and_starts_reader_when_supported(self) -> None:
        b = backend.GrSatellitesBackend()
        with (
            patch.object(
                backend,
                "resolve_gr_satellites_command",
                return_value=(["/usr/bin/gr_satellites"], False),
            ),
            patch.object(backend, "_supports_kiss_server", return_value=True),
            patch.object(backend, "find_free_port", return_value=54321),
            patch.object(backend.subprocess, "Popen", return_value=_FakeProc()) as mock_popen,
            patch.object(backend, "_KissFrameReader") as mock_reader_cls,
        ):
            ok, _msg = b.start(25544, 48000, MagicMock())

        assert ok is True
        cmd = mock_popen.call_args[0][0]
        assert "--kiss_server" in cmd
        assert "54321" in cmd
        assert "--kiss_server_address" in cmd
        assert "127.0.0.1" in cmd
        assert b.started_norad == 25544
        assert b.kiss_supported is True
        mock_reader_cls.assert_called_once_with(54321, b.raw_frame_received.emit)
        mock_reader_cls.return_value.start.assert_called_once()
        b.stop()
        mock_reader_cls.return_value.close.assert_called_once()

    def test_skips_kiss_flags_when_unsupported(self) -> None:
        b = backend.GrSatellitesBackend()
        with (
            patch.object(
                backend,
                "resolve_gr_satellites_command",
                return_value=(["/usr/bin/gr_satellites"], False),
            ),
            patch.object(backend, "_supports_kiss_server", return_value=False),
            patch.object(backend.subprocess, "Popen", return_value=_FakeProc()) as mock_popen,
        ):
            ok, _msg = b.start(25544, 48000, MagicMock())

        assert ok is True
        cmd = mock_popen.call_args[0][0]
        assert "--kiss_server" not in cmd
        assert b.kiss_supported is False
        b.stop()

    def test_status_mentions_unavailable_when_unsupported(self) -> None:
        b = backend.GrSatellitesBackend()
        statuses: list[str] = []
        b.status_changed.connect(statuses.append)
        with (
            patch.object(
                backend,
                "resolve_gr_satellites_command",
                return_value=(["/usr/bin/gr_satellites"], False),
            ),
            patch.object(backend, "_supports_kiss_server", return_value=False),
            patch.object(backend.subprocess, "Popen", return_value=_FakeProc()),
        ):
            b.start(25544, 48000, MagicMock())
        assert any("SatNOGS upload unavailable" in s for s in statuses)
        b.stop()


class TestKissFrameReader:
    def test_emits_decoded_frames_from_real_socket(self) -> None:
        """Binds a real TCP server, has the reader connect to it, then feeds
        raw KISS bytes (mirroring gr_satellites' --kiss_server output) and
        checks the deframed payload comes back via the callback."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        received: list[bytes] = []
        reader = backend._KissFrameReader(port, received.append)
        reader.start()
        try:
            conn, _addr = server.accept()
            try:
                # FEND, cmd=0x00 (data frame, port 0), payload, FEND
                conn.sendall(b"\xc0\x00hello\xc0")
                deadline = time.monotonic() + 3
                while not received and time.monotonic() < deadline:
                    time.sleep(0.05)
            finally:
                conn.close()
            assert received == [b"hello"]
        finally:
            reader.close()
            reader.join(timeout=3)
            server.close()

    def test_close_before_any_connection_stops_the_thread(self) -> None:
        """close() called immediately (server never listening / never
        connects) must still let run() return instead of hanging in the
        connect-retry loop for the full ~3s."""
        reader = backend._KissFrameReader(1, lambda _f: None)  # port 1: nothing listens there
        reader.start()
        reader.close()
        reader.join(timeout=3)
        assert not reader.is_alive()
