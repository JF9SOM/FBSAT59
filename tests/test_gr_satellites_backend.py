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
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from PySide6.QtCore import Qt

import comms.telemetry.gr_satellites_backend as backend

# The real probe, kept before the fixture below replaces it.
_REAL_SUPPORTS_UDP_RAW = backend._supports_udp_raw


@pytest.fixture(autouse=True)
def _no_udp_raw_probe() -> Iterator[None]:
    """start() probes ``gr_satellites --help`` for --udp_raw; most tests here replace
    subprocess.Popen wholesale, which that probe would run into. Fix its answer
    (False); a test that wants another answer patches it itself."""
    with patch.object(backend, "_supports_udp_raw", return_value=False):
        yield


class _FakeProc:
    def __init__(self) -> None:
        self.stdout = iter([])
        self.stderr = iter([])
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

    def test_catalog_norad_overrides_launch_id_but_not_attribution(self) -> None:
        """gr-satellites knows Foresail-1p as 98467 while the app tracks it as
        66778: the subprocess must get the catalog id, frames stay attributed
        to the app's id."""
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
            b.start(66778, 250000, MagicMock(), catalog_norad=98467)

        cmd = mock_popen.call_args[0][0]
        assert "98467" in cmd
        assert "66778" not in cmd
        assert b.started_norad == 66778
        b.stop()


class TestMapProvisionalToTracked:
    def test_maps_provisional_catalog_id_to_real_id_by_name(self) -> None:
        catalog = [(98467, "FORESAIL-1P")]
        tracked = [(66778, "Foresail-1p"), (25544, "ISS")]
        assert backend.map_provisional_to_tracked(catalog, tracked) == {98467: 66778}

    def test_name_match_ignores_case_and_punctuation(self) -> None:
        catalog = [(98647, "TEVEL2-1")]
        tracked = [(63217, "TEVEL 2_1")]
        assert backend.map_provisional_to_tracked(catalog, tracked) == {98647: 63217}

    def test_real_catalog_id_is_never_remapped(self) -> None:
        """Two unrelated spacecraft can share a name (IRIS 57315 vs 39197)."""
        assert backend.map_provisional_to_tracked([(57315, "IRIS")], [(39197, "IRIS")]) == {}

    def test_ambiguous_name_is_skipped(self) -> None:
        catalog = [(98500, "DUPE")]
        tracked = [(60001, "Dupe"), (60002, "DUPE")]
        assert backend.map_provisional_to_tracked(catalog, tracked) == {}

    def test_no_match_is_skipped(self) -> None:
        assert backend.map_provisional_to_tracked([(98500, "NOPE")], [(1, "Other")]) == {}


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

    def test_a_frame_after_a_silence_longer_than_the_connect_timeout_still_arrives(self) -> None:
        """Frames are sparse. The 1 s timeout of create_connection() used to stay on the
        socket, so after one quiet second recv() timed out, the reader quit and closed
        the connection (which also crashed gr_satellites on macOS) -- no later frame was
        ever read."""
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
                time.sleep(1.6)  # longer than the 1 s connect timeout: nothing decoded yet
                assert reader.is_alive()  # ... and the reader must not have given up
                conn.sendall(b"\xc0\x00late\xc0")
                deadline = time.monotonic() + 3
                while not received and time.monotonic() < deadline:
                    time.sleep(0.05)
            finally:
                conn.close()
            assert received == [b"late"]
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


class TestUdpIqForwarder:
    """IQ goes to gr_satellites in UDP datagrams; macOS drops (EMSGSIZE) any loopback
    datagram over 9216 bytes, and the forwarder swallows send errors, so an oversized
    datagram silently meant "no data at all"."""

    def test_a_pipeline_block_arrives_whole_over_loopback(self) -> None:
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(1.0)
        forwarder = backend._UdpIqForwarder(receiver.getsockname()[1])
        forwarder.start()
        try:
            block = (np.arange(16384) + 1j * np.arange(16384)).astype(np.complex64)
            forwarder.push_samples(block)

            received = bytearray()
            sizes: list[int] = []
            try:
                while len(received) < block.nbytes:
                    datagram = receiver.recv(65535)
                    sizes.append(len(datagram))
                    received.extend(datagram)
            except TimeoutError:
                pass
            assert bytes(received) == block.tobytes()
            assert max(sizes) <= 9216  # what macOS accepts by default
            assert all(n % 8 == 0 for n in sizes)  # never splits a complex64 sample
        finally:
            forwarder.close()
            receiver.close()


class TestSupportsUdpRaw:
    def setup_method(self) -> None:
        backend._udp_raw_supported_cache.clear()

    def test_detects_the_flag_and_caches(self) -> None:
        completed = subprocess.CompletedProcess(
            ["gr_satellites", "--help"], 1, "", "usage: ... [--udp_raw] ..."
        )
        with patch.object(backend.subprocess, "run", return_value=completed) as mock_run:
            assert _REAL_SUPPORTS_UDP_RAW(["gr_satellites"], {}) is True
            assert _REAL_SUPPORTS_UDP_RAW(["gr_satellites"], {}) is True
        assert mock_run.call_count == 1

    def test_false_for_a_build_without_the_flag_or_when_it_cannot_run(self) -> None:
        old = subprocess.CompletedProcess([], 1, "", "usage: ... no such flag ...")
        with patch.object(backend.subprocess, "run", return_value=old):
            assert _REAL_SUPPORTS_UDP_RAW(["old"], {}) is False
        with patch.object(backend.subprocess, "run", side_effect=OSError("nope")):
            assert _REAL_SUPPORTS_UDP_RAW(["missing"], {}) is False


class TestStartUdpRaw:
    """gr_satellites reads --udp as int16 unless --udp_raw is given (we send complex64)."""

    def test_the_flag_is_added_when_the_build_supports_it(self) -> None:
        b = backend.GrSatellitesBackend()
        with (
            patch.object(
                backend,
                "resolve_gr_satellites_command",
                return_value=(["/usr/bin/gr_satellites"], False),
            ),
            patch.object(backend, "_supports_kiss_server", return_value=False),
            patch.object(backend, "_supports_udp_raw", return_value=True),
            patch.object(backend.subprocess, "Popen", return_value=_FakeProc()) as mock_popen,
        ):
            b.start(25544, 250000, MagicMock())
        cmd = mock_popen.call_args[0][0]
        assert "--udp_raw" in cmd
        assert cmd.index("--udp") < cmd.index("--udp_raw")
        b.stop()

    def test_it_is_left_out_for_a_build_without_it(self) -> None:
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
            b.start(25544, 250000, MagicMock())
        assert "--udp_raw" not in mock_popen.call_args[0][0]
        b.stop()


def test_start_makes_the_subprocess_output_unbuffered() -> None:
    """A piped Python stdout is block-buffered, so a sparse stream of frames would not
    reach the table until ~8 KiB had accumulated."""
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
        b.start(25544, 250000, MagicMock())
    assert mock_popen.call_args.kwargs["env"]["PYTHONUNBUFFERED"] == "1"
    b.stop()


class TestReadStdoutBlocks:
    """Frame text from gr_satellites becomes one table block per frame."""

    def _run(self, lines: list[str], pause_after: float = 0.0) -> list[str]:
        b = backend.GrSatellitesBackend()
        blocks: list[str] = []
        b.telemetry_received.connect(blocks.append, Qt.ConnectionType.DirectConnection)
        b._proc = MagicMock()
        b._proc.stdout = iter(lines)
        with patch("comms.telemetry.gr_satellites_log.get_gr_satellites_logger"):
            b._read_stdout()
        if pause_after:
            time.sleep(pause_after)
        return blocks

    def test_blank_line_delimited_blocks(self) -> None:
        blocks = self._run(["-> Packet from A\n", "x = 1\n", "\n", "-> Packet from A\n", "x = 2\n"])
        assert blocks == ["-> Packet from A\nx = 1", "-> Packet from A\nx = 2"]

    def test_the_next_frames_heading_ends_the_block_when_there_is_no_blank_line(self) -> None:
        """The bundled gr_satellites prints no blank line between frames: with only blank
        lines as delimiters no frame reached the table until the process exited."""
        lines = [
            "-> Packet from 9k6 FSK downlink\n",
            "Container:\n",
            "    callsign = u'A'\n",
            "-> Packet from 9k6 FSK downlink\n",
            "Container:\n",
            "    callsign = u'B'\n",
        ]
        blocks = self._run(lines)
        assert blocks == [
            "-> Packet from 9k6 FSK downlink\nContainer:\n    callsign = u'A'",
            "-> Packet from 9k6 FSK downlink\nContainer:\n    callsign = u'B'",
        ]

    def test_the_last_block_is_sent_when_the_output_stops(self) -> None:
        b = backend.GrSatellitesBackend()
        blocks: list[str] = []
        b.telemetry_received.connect(blocks.append, Qt.ConnectionType.DirectConnection)
        gate = threading.Event()

        def stream() -> Iterator[str]:
            yield "-> Packet from A\n"
            yield "x = 1\n"
            gate.wait(3)  # the process is still running, nothing more printed for a while
            yield "-> Packet from A\n"

        b._proc = MagicMock()
        b._proc.stdout = stream()
        with patch("comms.telemetry.gr_satellites_log.get_gr_satellites_logger"):
            reader = threading.Thread(target=b._read_stdout, daemon=True)
            reader.start()
            deadline = time.monotonic() + 2.0
            while not blocks and time.monotonic() < deadline:
                time.sleep(0.05)
            assert blocks == ["-> Packet from A\nx = 1"]  # sent without waiting for a next frame
            gate.set()
            reader.join(timeout=3)

    def test_warnings_and_progress_messages_are_not_frames(self) -> None:
        lines = [
            "log :warning: `socket_pdu` has moved to gr-network\n",
            "udp_source :info: Listening for data on UDP port 7356.\n",
            "-> Packet from 9k6 FSK downlink\n",
            "Container:\n",
            "udp_source :warning: Insufficient block data.\n",
        ]
        blocks = self._run(lines)
        # the two start-up lines are dropped; the last line belongs to the frame's block
        assert len(blocks) == 1
        assert blocks[0].startswith("-> Packet from 9k6 FSK downlink")
