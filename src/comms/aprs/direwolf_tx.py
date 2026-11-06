"""APRS transmit through Direwolf itself (a TX Direwolf process, kept open or one-shot).

Why: the audio of FBSAT59's own AFSK/G3RUH generator is, sample for sample, nearly the
same as Direwolf's (frame bytes, tone levels and spectrum were compared), yet a
UZ7HO SoundModem receiver decoded SoundModem/UISS transmissions but not ours (GitHub
issue #35). Direwolf is a proven transmitter, so here it makes and plays the audio.

How: the RX Direwolf (``ADEVICE stdin null``) cannot transmit, because with a ``stdin``
input it never opens a real output device (verified on macOS: the TX is logged but no
audio comes out). So a second Direwolf is started with real input and
output devices, the PTT is keyed through the rig (Hamlib, as before), the frame goes in
over KISS, and Direwolf plays it (its own TXDELAY / TXTAIL flags around the frame).

Direwolf has no output gain setting, so there is no TX level control; set the level
with the radio's data input gain and the computer's output volume.
There is no other transmit path: the earlier built-in generator was removed because
it never produced a signal the SoundModem receiver accepted. Failures are reported
through the ``error`` signal.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import random
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from comms.aprs.direwolf import _MODEM_LINES, _kiss_encode, find_direwolf
from comms.aprs.line_process import LineProcess, start_line_process

logger = logging.getLogger(__name__)

_READY_TIMEOUT_S = 10.0
_RATE = 48_000  # Hz; Direwolf's TX audio sample rate
# Direwolf starts the audio about 0.3 s after the KISS frame; allow a lot more.
_TX_START_TIMEOUT_S = 6.0
# Extra time allowed for the end of the transmission beyond the frame's length.
_TX_END_MARGIN_S = 3.0
# Grace period before the TX Direwolf is stopped, after the PTT is released. Measured on
# macOS (4800 baud, loopback recording): the audio ends before the PTT is released, and
# the decode result did not depend on a wait of 0..0.8 s (the recording path itself is
# noisy), so this is only a small safety margin; it never delays the PTT or reception.
_DRAIN_S = 0.2
# Windows (WinMM) device names are cut to this many characters.
_WIN_NAME_MAX = 31


class DirewolfStartError(RuntimeError):
    """Direwolf could not be started or could not open the audio devices."""


def _device_names(in_device: int | None, out_device: int | None) -> tuple[str, str]:
    """PortAudio names of the input and output devices (input defaults to the output)."""
    import sounddevice as sd  # optional dependency

    out_name = str(sd.query_devices(out_device, "output")["name"])
    if in_device is None:
        return out_name, out_name
    return str(sd.query_devices(in_device, "input")["name"]), out_name


def _config_encoding() -> str:
    """Encoding Direwolf reads its config with.

    Windows Direwolf compares device names byte-wise with the ANSI (code page)
    names of WinMM, e.g. cp932 on Japanese Windows; UTF-8 would never match a
    name that contains non-ASCII characters.
    """
    if sys.platform == "win32":
        import locale

        return locale.getpreferredencoding(False)
    return "utf-8"


def _name_variants(name: str) -> list[str]:
    """Names to try for Direwolf: as is, and (Windows) cut to the WinMM length."""
    variants = [name]
    if sys.platform == "win32" and len(name) > _WIN_NAME_MAX:
        variants.append(name[:_WIN_NAME_MAX])
    return variants


def build_config(
    in_name: str,
    out_name: str,
    modem: str,
    txdelay_ms: int,
    txtail_ms: int,
    kiss_port: int,
) -> str:
    """direwolf.conf for a transmit-only run (no PTT, no CSMA wait, KISS only)."""
    modem_line = _MODEM_LINES.get(modem, _MODEM_LINES["1200"])
    return (
        "MYCALL N0CALL\n"
        f'ADEVICE "{in_name}" "{out_name}"\n'
        f"ARATE {_RATE}\n"
        "ACHANNELS 1\n"
        "CHANNEL 0\n"
        f"{modem_line}\n"
        # Direwolf counts TXDELAY / TXTAIL in 10 ms units.
        f"TXDELAY {max(0, round(txdelay_ms / 10))}\n"
        f"TXTAIL {max(0, round(txtail_ms / 10))}\n"
        # Transmit at once: the rig is already keyed, no listen-before-talk.
        "DWAIT 0\n"
        "SLOTTIME 5\n"
        "PERSIST 250\n"
        "AGWPORT 0\n"
        # "KISSPORT 0" turns off the default port 8001 (the RX Direwolf uses it); Direwolf
        # only accepts ports 1000..10000.
        "KISSPORT 0\n"
        f"KISSPORT {kiss_port}\n"
    )


def _free_port() -> int:
    """A free local TCP port in the range Direwolf accepts for KISSPORT (1000..10000)."""
    for port in random.sample(range(8200, 9800), 50):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise DirewolfStartError("no free KISS port")


_BAUD = {"1200": 1200, "4800": 4800, "9600": 9600}


def _estimate_seconds(frame_len: int, modem: str, txdelay_ms: int, txtail_ms: int) -> float:
    """Rough transmit time: flags + frame with FCS and bit stuffing (a generous upper bound)."""
    baud = _BAUD.get(modem, 1200)
    bits = (frame_len + 2) * 8 * 1.25 + 16
    return (txdelay_ms + txtail_ms) / 1000.0 + bits / baud


def _connect_retry(port: int, timeout_s: float = 5.0) -> socket.socket:
    """Connect to Direwolf's KISS port; it listens a moment after it says "Ready"."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=2)
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


class OutputBusyError(RuntimeError):
    """The sound card output is held by another owner (e.g. another tab)."""

    def __init__(self, other: str) -> None:
        super().__init__(other)
        self.other = other


class DirewolfTxSession:
    """A TX Direwolf process and its KISS connection.

    Direwolf needs about a second after it starts before it sends its first frame
    (a frame sent 3 s after the start went out within 0.05 s; one sent at once took
    1 s), so the APRS tab keeps one session open while the tab is open and every
    frame then leaves at once. A session also works one-shot (open, send, close).
    While it is open it holds the sound card output (``acquire_output``).
    """

    def __init__(
        self,
        owner: str,
        out_device: int | None,
        in_device: int | None,
        modem: str,
        txdelay_ms: int,
        txtail_ms: int,
    ) -> None:
        self._owner = owner
        self._out_device = out_device
        self._in_device = in_device
        self._modem = modem
        self._txdelay_ms = txdelay_ms
        self._txtail_ms = txtail_ms
        self._lock = threading.RLock()
        self._proc: LineProcess | None = None
        self._sock: socket.socket | None = None
        self._tmp: Path | None = None
        self._holds_output = False
        self._lines: queue.Queue[str | None] = queue.Queue()

    @property
    def config_key(self) -> tuple[Any, ...]:
        """Everything that is fixed when Direwolf starts; a change needs a new session."""
        return (
            self._out_device,
            self._in_device,
            self._modem,
            self._txdelay_ms,
            self._txtail_ms,
        )

    @property
    def is_open(self) -> bool:
        """True while the Direwolf process is alive and the KISS port is connected."""
        with self._lock:
            return self._proc is not None and self._proc.poll() is None and self._sock is not None

    def open(self) -> None:
        """Start Direwolf and connect to it; no-op when already open.

        Raises OutputBusyError or DirewolfStartError.
        """
        with self._lock:
            if self.is_open:
                return
            self._teardown()
            exe = find_direwolf()
            if exe is None:
                raise DirewolfStartError("direwolf not found")
            from comms.audio_device_manager import get_audio_device_manager

            mgr = get_audio_device_manager()
            if not mgr.acquire_output(self._owner, self._out_device):
                raise OutputBusyError(mgr.output_owner(self._out_device) or "")
            self._holds_output = True
            try:
                tmp = Path(tempfile.mkdtemp(prefix="fbsat59_dwtx_"))
                self._tmp = tmp
                proc, port = self._start_direwolf(exe, tmp)
                self._proc = proc
                self._lines = proc.lines
                self._sock = _connect_retry(port)
                logger.info("TX[%s]: Direwolf TX ready (port %d)", self._owner, port)
            except BaseException:
                self._teardown()
                raise

    def close(self, drain: bool = False) -> None:
        """Stop Direwolf and release the sound card output.

        *drain* waits a moment first so the end of a frame that was just played is not
        cut off; it never delays the PTT or reception.
        """
        with self._lock:
            if drain and self._proc is not None:
                time.sleep(_DRAIN_S)
            self._teardown()

    def _teardown(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            with contextlib.suppress(Exception):
                sock.close()
        proc, self._proc = self._proc, None
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.terminate()
                proc.wait(timeout=3)
        if self._holds_output:
            self._holds_output = False
            from comms.audio_device_manager import get_audio_device_manager

            with contextlib.suppress(Exception):
                get_audio_device_manager().release_output(self._owner, self._out_device)
        tmp, self._tmp = self._tmp, None
        if tmp is not None:
            # Direwolf may still hold its working directory for a moment (Windows).
            shutil.rmtree(tmp, ignore_errors=True)

    def send(self, frame: bytes, rig: Any) -> bool:
        """Transmit one frame, keying the rig on Direwolf's own PTT edges.

        Returns False when the PTT command failed. Raises DirewolfStartError when
        Direwolf is not running, the connection is lost or it never starts sending.
        """
        with self._lock:
            if not self.is_open or self._sock is None:
                raise DirewolfStartError("Direwolf is not running")
            self._drain_lines()
            frame_s = _estimate_seconds(len(frame), self._modem, self._txdelay_ms, self._txtail_ms)
            keyed = False
            try:
                try:
                    self._sock.sendall(_kiss_encode(frame))
                except OSError as exc:
                    raise DirewolfStartError(f"Direwolf connection lost: {exc}") from exc
                logger.info("TX[%s]: KISS frame sent to Direwolf", self._owner)
                # Direwolf reports its own PTT edges ("PTT 0 = 1" when the audio
                # starts, "PTT 0 = 0" when it ends); the rig is keyed and released
                # exactly on them, so there is no unmodulated carrier around the
                # frame (the flags of TXDELAY / TXTAIL cover the rig's key-up).
                if not self._wait_line("PTT 0 = 1", _TX_START_TIMEOUT_S):
                    raise DirewolfStartError("Direwolf did not start transmitting")
                keyed = bool(rig.set_ptt(True))
                logger.info("TX[%s]: PTT on -> %s", self._owner, keyed)
                if not keyed:
                    return False
                if not self._wait_line("PTT 0 = 0", frame_s + _TX_END_MARGIN_S):
                    logger.warning("TX[%s]: Direwolf PTT-off line never came", self._owner)
                unkeyed = rig.set_ptt(False)
                keyed = False
                logger.info("TX[%s]: playback finished, PTT off -> %s", self._owner, unkeyed)
                self._log_direwolf_output()
                return True
            finally:
                if keyed:
                    with contextlib.suppress(Exception):
                        rig.set_ptt(False)

    # ------------------------------------------------------------------ #

    def _drain_lines(self) -> None:
        """Drop console lines of earlier frames so only this frame's PTT edges count."""
        with contextlib.suppress(queue.Empty):
            while True:
                self._lines.get_nowait()

    def _start_direwolf(self, exe: Path, tmp: Path) -> tuple[LineProcess, int]:
        """Start Direwolf, trying the device-name variants; returns (process, kiss_port)."""
        try:
            in_name, out_name = _device_names(self._in_device, self._out_device)
        except Exception as exc:
            raise DirewolfStartError(f"audio device lookup failed: {exc}") from exc
        last = "no output"
        for in_v in _name_variants(in_name):
            for out_v in _name_variants(out_name):
                port = _free_port()
                conf = tmp / "tx.conf"
                conf.write_text(
                    build_config(in_v, out_v, self._modem, self._txdelay_ms, self._txtail_ms, port),
                    encoding=_config_encoding(),
                    errors="replace",
                )
                proc = start_line_process(
                    [str(exe), "-c", conf.name, "-t", "0", "-d", "o"],  # -d o: log PTT edges
                    tmp,
                )
                ok, last = self._wait_ready(proc, port)
                if ok:
                    logger.info(
                        "TX[%s]: Direwolf TX devices in=%r out=%r", self._owner, in_v, out_v
                    )
                    return proc, port
                with contextlib.suppress(Exception):
                    proc.kill()
        raise DirewolfStartError(last)

    def _wait_ready(self, proc: LineProcess, kiss_port: int) -> tuple[bool, str]:
        """Wait until Direwolf accepts KISS clients; (False, reason) if it cannot.

        Readiness is judged by the KISS port itself (not only by the console
        line), so a program whose output is delayed still counts as started.
        """
        deadline = time.monotonic() + _READY_TIMEOUT_S
        last = "timeout"
        while time.monotonic() < deadline:
            try:
                while True:
                    line = proc.lines.get_nowait()
                    if line is None:
                        return False, last
                    if line.strip():
                        last = line.strip()
                    if "not found" in line or "Pointless" in line:
                        return False, last
            except queue.Empty:
                pass
            if proc.poll() is not None:
                return False, last
            try:
                socket.create_connection(("127.0.0.1", kiss_port), timeout=0.3).close()
                return True, last
            except OSError:
                time.sleep(0.2)
        return False, last

    def _wait_line(self, text: str, timeout_s: float) -> bool:
        """Wait for a Direwolf output line containing *text*; False on timeout or exit."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                line = self._lines.get(timeout=0.05)
            except queue.Empty:
                continue
            if line is None:
                return False
            if line.startswith("[0"):
                logger.info("TX[%s]: Direwolf: %s", self._owner, line)
            if text in line:
                return True
        return False

    def _log_direwolf_output(self) -> None:
        """Log what Direwolf printed (its '[0L] ...' line confirms the transmission)."""
        try:
            while True:
                line = self._lines.get_nowait()
                if line and line.startswith("["):
                    logger.info("TX[%s]: Direwolf: %s", self._owner, line)
        except queue.Empty:
            pass


class DirewolfTxWorker(QObject):
    """Keys the PTT and has Direwolf transmit one AX.25 frame.

    Exactly one of ``finished`` or ``error`` is emitted per ``run()``. With a *session*
    (the APRS tab's kept-open Direwolf) the frame goes out through it and the session
    stays open; without one a short-lived Direwolf is started for this frame.
    *close_first* is an out-of-date session to close before sending.
    """

    finished: Signal = Signal()
    error: Signal = Signal(str)

    def __init__(
        self,
        owner: str,
        out_device: int | None,
        rig: Any,
        *,
        frame: bytes,
        modem: str,
        txdelay_ms: int,
        txtail_ms: int,
        in_device: int | None = None,
        parent: Any = None,
        session: DirewolfTxSession | None = None,
        close_first: DirewolfTxSession | None = None,
    ) -> None:
        super().__init__(parent)
        self._owner = owner
        self._out_device = out_device
        self._rig = rig
        self._frame = frame
        self._modem = modem
        self._txdelay_ms = txdelay_ms
        self._txtail_ms = txtail_ms
        self._in_device = in_device
        self._session = session
        self._close_first = close_first

    def run(self) -> None:
        """Transmit with Direwolf; report any problem through ``error``."""
        from i18n import _

        try:
            self._run_direwolf()
        except OutputBusyError as exc:
            self.error.emit(
                _("Sound card output is in use by {other}").format(
                    other=exc.other or _("another tab")
                )
            )
        except DirewolfStartError as exc:
            logger.error("TX[%s]: Direwolf TX failed to start: %s", self._owner, exc)
            self.error.emit(_("Direwolf could not transmit: {why}").format(why=exc))
        except Exception as exc:
            logger.exception("TX[%s]: Direwolf TX failed", self._owner)
            self.error.emit(str(exc))

    def _run_direwolf(self) -> None:
        from i18n import _

        own = self._session is None
        session = self._session or DirewolfTxSession(
            self._owner,
            self._out_device,
            self._in_device,
            self._modem,
            self._txdelay_ms,
            self._txtail_ms,
        )
        if self._close_first is not None:
            self._close_first.close()
        try:
            sent = self._send(session, retry=not own)
            if not sent:
                self.error.emit(_("PTT command failed — check Rig 1 connection"))
                return
            self.finished.emit()
        except BaseException:
            if not own:
                # A kept-open session that failed is not trusted any more.
                session.close()
            raise
        finally:
            if own:
                session.close(drain=True)

    def _send(self, session: DirewolfTxSession, retry: bool) -> bool:
        """Open the session if needed and send; a kept-open one is reopened once on failure."""
        for attempt in (1, 2):
            try:
                if not session.is_open:
                    session.open()
                return session.send(self._frame, self._rig)
            except DirewolfStartError:
                if not retry or attempt == 2:
                    raise
                logger.warning("TX[%s]: kept-open Direwolf failed, restarting it", self._owner)
                session.close()
        return False
