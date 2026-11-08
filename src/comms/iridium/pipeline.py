"""Subprocess chain that turns raw Iridium IQ into ACARS messages.

    raw IQ --stdin--> iridium-extractor --> iridium-parser.py --> reassembler.py
                                                                       |
                                                              one JSON line per
                                                              ACARS message

The chain is wired with OS pipes, so nothing but ACARS ever leaves the last
stage: the parser is told to pass IDA (data) frames only, and the reassembler
emits ACARS only.  No intermediate ``.bits`` / ``.parsed`` file is written.

The three stages are plain argv lists so tests can substitute stubs.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from comms.iridium.acars import IridiumAcarsMessage, parse_acars_json_line

logger = logging.getLogger(__name__)

# HackRF's native sample format (signed 8-bit I/Q), accepted by iridium-extractor.
HACKRF_SAMPLE_FORMAT = "ci8"
# Iridium downlink/simplex band centre (1618.0 - 1626.5 MHz).
DEFAULT_CENTER_HZ = 1_622_000_000
# 10 Msps covers the whole band; gr-iridium requires a multiple of 100 kHz.
DEFAULT_SAMPLE_RATE_HZ = 10_000_000
DEFAULT_DECIMATION = 4

# How long stop() waits for each process to exit before killing it.
_STOP_TIMEOUT_S = 3.0


def build_stage_commands(
    extractor: Sequence[str],
    toolkit_dir: Path,
    python: Sequence[str],
    *,
    center_hz: int = DEFAULT_CENTER_HZ,
    sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ,
    sample_format: str = HACKRF_SAMPLE_FORMAT,
    decimation: int = DEFAULT_DECIMATION,
    station: str | None = None,
) -> list[list[str]]:
    """Return the argv lists of the extractor, parser and reassembler stages."""
    extractor_cmd = [
        *extractor,
        "-f",
        sample_format,
        "-r",
        str(sample_rate_hz),
        "-c",
        str(center_hz),
        "-D",
        str(decimation),
    ]
    # --perfect drops frames that only parsed after error correction; --filter IDA
    # keeps data frames only (voice and signalling frames never go further).
    parser_cmd = [
        *python,
        str(toolkit_dir / "iridium-parser.py"),
        "--perfect",
        "--filter",
        "IDA",
        "-o",
        "line",
    ]
    reassembler_cmd = [
        *python,
        str(toolkit_dir / "reassembler.py"),
        "-m",
        "libacars",
        "-a",
        "json,nopings",
    ]
    if station:
        reassembler_cmd += ["--station", station]
    return [extractor_cmd, parser_cmd, reassembler_cmd]


class IridiumAcarsPipeline:
    """Runs the three-stage chain and reports ACARS messages through callbacks.

    Callbacks are invoked from background threads.
    """

    def __init__(
        self,
        stages: Sequence[Sequence[str]],
        on_message: Callable[[IridiumAcarsMessage], None],
        on_status: Callable[[str], None] | None = None,
        on_stopped: Callable[[int | None], None] | None = None,
        cwd: Path | None = None,
    ) -> None:
        if len(stages) != 3:
            raise ValueError("the Iridium chain has exactly three stages")
        self._stages = [list(s) for s in stages]
        self._on_message = on_message
        self._on_status = on_status
        self._on_stopped = on_stopped
        self._cwd = cwd
        self._procs: list[subprocess.Popen[bytes]] = []
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._write_lock = threading.Lock()

    @property
    def running(self) -> bool:
        """True while the chain is started and the extractor is alive."""
        return bool(self._procs) and self._procs[0].poll() is None

    def start(self) -> None:
        """Spawn the chain.  Raises OSError if an executable cannot be started."""
        if self._procs:
            raise RuntimeError("pipeline already started")
        self._stopping.clear()
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"  # the toolkit scripts must not hold output back
        # creationflags must stay 0 off Windows; CREATE_NO_WINDOW hides console windows.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            extractor = subprocess.Popen(
                self._stages[0],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._cwd,
                env=env,
                creationflags=flags,
            )
            self._procs.append(extractor)
            parser = subprocess.Popen(
                self._stages[1],
                stdin=extractor.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=self._cwd,
                env=env,
                creationflags=flags,
            )
            self._procs.append(parser)
            assert extractor.stdout is not None
            extractor.stdout.close()  # the parser owns the read end now
            reassembler = subprocess.Popen(
                self._stages[2],
                stdin=parser.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._cwd,
                env=env,
                creationflags=flags,
            )
            self._procs.append(reassembler)
            assert parser.stdout is not None
            parser.stdout.close()
        except OSError:
            self._kill_all()
            raise
        self._spawn(self._read_messages, reassembler, "iridium-acars")
        self._spawn(self._read_status, extractor, "iridium-status")
        self._spawn(self._drain_stderr, reassembler, "iridium-reasm-err")

    def write_iq(self, data: bytes) -> bool:
        """Feed raw IQ bytes to the extractor.  Returns False once the chain is gone."""
        if not self._procs or self._stopping.is_set():
            return False
        stdin = self._procs[0].stdin
        if stdin is None:
            return False
        try:
            with self._write_lock:
                stdin.write(data)
                stdin.flush()  # the stub/real extractor must see the bytes now
        except (BrokenPipeError, ValueError, OSError):
            return False
        return True

    def stop(self) -> None:
        """Stop the chain; safe to call more than once."""
        self._stopping.set()
        if self._procs:
            stdin = self._procs[0].stdin
            if stdin is not None:
                with contextlib.suppress(OSError, ValueError):
                    stdin.close()
        for proc in self._procs:
            try:
                proc.wait(timeout=_STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=_STOP_TIMEOUT_S)
        for t in self._threads:
            t.join(timeout=_STOP_TIMEOUT_S)
        self._procs = []
        self._threads = []

    # ---- internals -------------------------------------------------------------

    def _spawn(
        self,
        target: Callable[[subprocess.Popen[bytes]], None],
        proc: subprocess.Popen[bytes],
        name: str,
    ) -> None:
        t = threading.Thread(target=target, args=(proc,), name=name, daemon=True)
        self._threads.append(t)
        t.start()

    def _kill_all(self) -> None:
        for proc in self._procs:
            with contextlib.suppress(OSError):
                proc.kill()
        self._procs = []

    def _read_messages(self, reassembler: subprocess.Popen[bytes]) -> None:
        assert reassembler.stdout is not None
        for raw in reassembler.stdout:
            msg = parse_acars_json_line(raw.decode("utf-8", errors="replace"))
            if msg is not None:
                try:
                    self._on_message(msg)
                except Exception:  # noqa: BLE001 - a UI callback must not kill the reader
                    logger.exception("Iridium message callback failed")
        rc = reassembler.wait()
        if self._on_stopped is not None and not self._stopping.is_set():
            self._on_stopped(rc)

    def _read_status(self, extractor: subprocess.Popen[bytes]) -> None:
        """Forward the extractor's once-per-second status line (stderr)."""
        assert extractor.stderr is not None
        for raw in extractor.stderr:
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text and self._on_status is not None:
                self._on_status(text)

    def _drain_stderr(self, reassembler: subprocess.Popen[bytes]) -> None:
        """Log the reassembler's stderr (missing libacars etc.) and surface it."""
        assert reassembler.stderr is not None
        for raw in reassembler.stderr:
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.warning("iridium reassembler: %s", text)
                if self._on_status is not None:
                    self._on_status(text)
