"""APRS transmit through Direwolf itself (one short-lived process per frame).

Why: the audio of FBSAT59's own AFSK/G3RUH generator is, sample for sample, nearly the
same as Direwolf's (frame bytes, tone levels and spectrum were compared), yet a
UZ7HO SoundModem receiver decoded SoundModem/UISS transmissions but not ours (GitHub
issue #35). Direwolf is a proven transmitter, so here it makes and plays the audio.

How: the RX Direwolf (``ADEVICE stdin null``) cannot transmit, because with a ``stdin``
input it never opens a real output device (verified on macOS: the TX is logged but no
audio comes out). So a second Direwolf is started per transmission with real input and
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
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from comms.aprs.direwolf import _MODEM_LINES, _kiss_encode, find_direwolf

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


class DirewolfTxWorker(QObject):
    """Keys the PTT and has Direwolf transmit one AX.25 frame.

    Exactly one of ``finished`` or ``error`` is emitted per ``run()``.
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
        ptt_lead_s: float = 0.0,
        ptt_tail_s: float = 0.0,
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
        self._ptt_lead_s = max(0.0, ptt_lead_s)
        self._ptt_tail_s = max(0.0, ptt_tail_s)
        self._dw_lines: queue.Queue[str | None] = queue.Queue()

    def run(self) -> None:
        """Transmit with Direwolf; report any problem through ``error``."""
        try:
            self._run_direwolf()
        except DirewolfStartError as exc:
            logger.error("TX[%s]: Direwolf TX failed to start: %s", self._owner, exc)
            from i18n import _

            self.error.emit(_("Direwolf could not transmit: {why}").format(why=exc))

    # ------------------------------------------------------------------ #

    def _start_direwolf(self, exe: Path, tmp: Path) -> tuple[subprocess.Popen[str], int]:
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
                    encoding="utf-8",
                )
                proc = subprocess.Popen(
                    [str(exe), "-c", conf.name, "-t", "0", "-d", "o"],  # -d o: log PTT edges
                    cwd=tmp,
                    # Kept open: Direwolf may quit when its stdin reaches EOF.
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
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

    def _wait_ready(self, proc: subprocess.Popen[str], kiss_port: int) -> tuple[bool, str]:
        """Wait until Direwolf accepts KISS clients; (False, reason) if it cannot."""
        lines: queue.Queue[str | None] = queue.Queue()

        def _pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.put(line.rstrip())
            lines.put(None)

        threading.Thread(target=_pump, daemon=True).start()
        self._dw_lines = lines
        deadline = time.monotonic() + _READY_TIMEOUT_S
        last = "timeout"
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                return False, last
            if line.strip():
                last = line.strip()
            if f"Ready to accept KISS TCP client application 0 on port {kiss_port}" in line:
                return True, last
            if "not found" in line or "Pointless" in line:
                return False, last
        return False, last

    def _run_direwolf(self) -> None:
        exe = find_direwolf()
        if exe is None:
            raise DirewolfStartError("direwolf not found")
        from comms.audio_device_manager import get_audio_device_manager
        from i18n import _

        mgr = get_audio_device_manager()
        if not mgr.acquire_output(self._owner, self._out_device):
            other = mgr.output_owner(self._out_device) or _("another tab")
            self.error.emit(_("Sound card output is in use by {other}").format(other=other))
            return
        proc: subprocess.Popen[str] | None = None
        keyed = False
        try:
            with tempfile.TemporaryDirectory(prefix="fbsat59_dwtx_") as td:
                proc, port = self._start_direwolf(exe, Path(td))
                frame_s = _estimate_seconds(
                    len(self._frame), self._modem, self._txdelay_ms, self._txtail_ms
                )
                logger.info(
                    "TX[%s]: Direwolf TX start (expected ~%.2f s, port %d)",
                    self._owner,
                    frame_s,
                    port,
                )
                with _connect_retry(port) as sock:
                    sock.sendall(_kiss_encode(self._frame))
                    logger.info("TX[%s]: KISS frame sent to Direwolf", self._owner)
                    # Direwolf reports its own PTT edges ("PTT 0 = 1" when the audio
                    # starts, "PTT 0 = 0" when it ends); the rig is keyed and released
                    # exactly on them, so there is no unmodulated carrier around the
                    # frame (the flags of TXDELAY / TXTAIL cover the rig's key-up).
                    if not self._wait_line("PTT 0 = 1", _TX_START_TIMEOUT_S):
                        raise DirewolfStartError("Direwolf did not start transmitting")
                    keyed = bool(self._rig.set_ptt(True))
                    logger.info("TX[%s]: PTT on -> %s", self._owner, keyed)
                    if not keyed:
                        from i18n import _ as tr

                        self.error.emit(tr("PTT command failed — check Rig 1 connection"))
                        return
                    ended = self._wait_line("PTT 0 = 0", frame_s + _TX_END_MARGIN_S)
                    if not ended:
                        logger.warning("TX[%s]: Direwolf PTT-off line never came", self._owner)
                time.sleep(self._ptt_tail_s)
                unkeyed = self._rig.set_ptt(False)
                keyed = False
                logger.info("TX[%s]: playback finished, PTT off -> %s", self._owner, unkeyed)
                self._log_direwolf_output()
                self.finished.emit()
        except DirewolfStartError:
            raise
        except Exception as exc:
            logger.exception("TX[%s]: Direwolf TX failed", self._owner)
            self.error.emit(str(exc))
        finally:
            if keyed:
                with contextlib.suppress(Exception):
                    self._rig.set_ptt(False)
            mgr.release_output(self._owner, self._out_device)
            if proc is not None:
                time.sleep(_DRAIN_S)
                with contextlib.suppress(Exception):
                    proc.terminate()
                    proc.wait(timeout=3)

    def _wait_line(self, text: str, timeout_s: float) -> bool:
        """Wait for a Direwolf output line containing *text*; False on timeout or exit."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                line = self._dw_lines.get(timeout=0.05)
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
        lines = self._dw_lines
        try:
            while True:
                line = lines.get_nowait()
                if line and line.startswith("["):
                    logger.info("TX[%s]: Direwolf: %s", self._owner, line)
        except queue.Empty:
            pass
