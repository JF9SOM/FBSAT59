"""Communications > FT4 tab.

Provides FT4 TX/RX for satellite QSOs using:
  - ft8_lib (ctypes) for message encode/decode
  - sounddevice for audio I/O
  - RigController for PTT (CAT)

Rig + Sound Card configuration (Rig Settings > Sound Card) is required.
SDR-only mode is not supported because FT4 requires TX capability.
If a second rig slot is an SDR, it can optionally be used for RX audio.

Tab is non-resident: opened via Communications > FT4 and closed with ×.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from comms.audio_device_manager import get_audio_device_manager, validate_output_device
from comms.ft4.codec import (
    FT4_PERIOD,
    SAMPLE_RATE,
    Ft4Codec,
    Ft4Message,
    get_user_ft8lib_dir,
)
from comms.ft4.decode_log import get_ft4_decode_logger
from comms.ft4.qso import Ft4QsoManager, QsoState, format_report
from comms.ft4.rx_capture import Ft4RxCaptureWorker
from comms.ft4.scheduler import Ft4Scheduler
from i18n import _
from ui.ft4_waterfall_dialog import Ft4WaterfallDialog

UTC = UTC

# Columns in the decoded-messages table
_COL_UTC = 0
_COL_DB = 1
_COL_DT = 2
_COL_FREQ = 3
_COL_MSG = 4
_COL_COUNT = 5

# Distinct from Qt.GlobalColor.yellow (used for decoded messages addressed to
# us) so an operator can tell "this is what I sent" apart from "this is what
# I was decoded to have received" at a glance (GitHub Issue #16).
_OWN_TX_ROW_COLOR = QColor("#4fc3f7")

# Text colour for decoded rows, by which slot parity the *other station*
# actually transmitted in (not the parity we'd reply in — see
# _display_decoded). Requested on GitHub Issue #16 so an operator can tell
# at a glance which slot a given decode came from, independent of the dB/DT/
# Hz columns. Kept as foreground colour rather than background so it layers
# cleanly under the "addressed to us" yellow highlight above.
_EVEN_SLOT_TEXT_COLOR = QColor("#1565c0")
_ODD_SLOT_TEXT_COLOR = QColor("#e65100")

_GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}$")


def _parse_cq_call_grid(words: list[str]) -> tuple[str, str]:
    """Extract (callsign, grid) from the words following "CQ" in a decoded message.

    Directed CQs insert a keyword before the callsign (e.g. "CQ WWA <call>
    <grid>", "CQ DX <call>", "CQ POTA <call> <grid>"), so the callsign is not
    always the first word — it is always the token immediately before the
    grid, or the last token when no grid is present.
    """
    if not words:
        return "", ""
    if len(words) >= 2 and _GRID_RE.match(words[-1]):
        return words[-2], words[-1]
    return words[-1], ""


_FT4_SETTINGS_KEY = "ft4_settings"
_DEFAULT_AUDIO_FREQ = 1500.0  # Hz — matches Q65's/WSJT-X's own default; see
# GitHub Issue #16: 1000 Hz reports of "decodes fine but never heard on
# PSKReporter" line up with narrow rig DATA/USB-D TX audio passbands that
# don't extend that low, whereas 1500 Hz (and the ~2300 Hz that *was*
# confirmed) sit comfortably inside them.
_AUDIO_OWNER = "FT4"
# 500ms @ 12000 Hz. Originally 240 (~20ms), sized only to bound the delay
# before a TX Level slider change takes effect mid-transmission (GitHub
# Issue #16). Raised for GitHub Issue #26: on IC-9700 satmode Direct,
# _tracking_through_tx()'s 1 Hz UL threshold makes controller.py's
# set_freq() calls (which hold the GIL for their whole blocking CAT
# round-trip -- Hamlib's Python binding is built without SWIG's -threads
# option) fire almost back-to-back throughout TX, each one taking
# ~200-400ms (measured; see "took=" lines added for this same issue).
# At the old 20ms blocksize, PortAudio only ever had a sliver of audio
# queued ahead of the hardware, so any single one of those stalls caused
# an audible "output underflow" and stretched a ~5s transmission out to
# 10-30+s. A much bigger block gives the hardware enough pre-queued
# audio to ride out one such stall without running dry, at the cost of
# the TX Level slider now taking up to ~500ms to visibly react instead
# of ~20ms -- imperceptible for a slider used to trim gain, not a
# real-time control.
_TX_BLOCK_SIZE = 6000
# GitHub Issue #26: a hard ceiling on how long a single transmission is
# allowed to hold PTT, independent of whatever _TxWorker's own audio
# stream thinks is going on. done.wait() below normally unblocks once our
# callback has pushed the last sample of the ~5.04s FT4 tone, but if the
# audio pipeline stalls (the underlying CAT-traffic/GIL contention this
# issue is about, or any other cause) that wait could in principle never
# return on its own, leaving the rig keyed indefinitely. One FT4 T/R
# period is FT4_PERIOD (7.5s); 7.0s gives it a full period's worth of
# margin over a normal transmission while still guaranteeing PTT comes
# back off before the *next* period would otherwise try to key up again.
_TX_WATCHDOG_S = 7.0


# ---------------------------------------------------------------------------
# Worker: TX audio output (runs in daemon thread to avoid blocking the UI)
# ---------------------------------------------------------------------------


class _TxWorker(QObject):
    """Plays FT4 audio through sounddevice and controls PTT.

    Streams audio via a sounddevice.OutputStream callback (rather than a
    single sd.play() call) so the TX Level gain can be re-read every block
    and take effect live during an active transmission, instead of only on
    the next transmission (GitHub Issue #16). The gain is linearly ramped
    across each block from the previous block's gain to the freshly-read
    value, so a mid-transmission slider move never produces an abrupt
    amplitude step (click) at a block boundary.

    Lives in a plain Python thread (not QThread) because we block on a
    threading.Event waiting for the stream to finish and do not need a Qt
    event loop inside the worker.
    """

    finished: Signal = Signal()
    error: Signal = Signal(str)

    def __init__(
        self,
        audio: NDArray[np.float32],
        out_device: int | None,
        rig: Any,
        get_gain: Callable[[], float],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._audio = audio
        self._out_device = out_device
        self._rig = rig
        self._get_gain = get_gain
        # Guards _active_stream, which is written from run()'s thread and
        # read from abort() (called from the Qt main thread by Halt TX /
        # closeEvent) — see abort()'s docstring.
        self._stream_lock = threading.Lock()
        self._active_stream: Any | None = None

    def abort(self) -> None:
        """Cut an in-progress transmission short (Halt TX / tab close).

        GitHub Issue #26: previously Halt TX only cleared the "keep
        transmitting" flag — a burst already playing ran to completion no
        matter what, which was barely noticeable back when a burst took its
        normal ~5s, but became a real "the button doesn't do anything"
        complaint once the still-unresolved underrun issue above started
        stretching some transmissions past 30s. Calling the stream's own
        abort() (rather than the module-level sd.stop(), which affects
        every stream process-wide) stops just this transmission immediately
        without waiting for its queued audio to drain. PortAudio still
        invokes finished_callback when a stream is aborted, so run()'s
        done.wait() unblocks and its normal PTT-off / lock-release cleanup
        still runs, just now within a fraction of a second instead of
        however long was left to play.
        """
        with self._stream_lock:
            stream = self._active_stream
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.abort()

    def run(self) -> None:
        """Emits exactly one of `error` or `finished` — never both, so a
        failure status is never clobbered by an immediately-following
        "TX done" (see finished/error handlers in Ft4Tab)."""
        mgr = get_audio_device_manager()
        if not mgr.acquire_output(_AUDIO_OWNER, self._out_device):
            other = mgr.output_owner(self._out_device) or _("another tab")
            self.error.emit(_("Sound card output is in use by {other}").format(other=other))
            return
        # GitHub Issue #26: pausing the shared RX input stream around each
        # TX burst (to avoid opening a TX stream concurrently with RX on
        # what turned out to be the same physical device, just enumerated
        # under a different Windows index) was tried and confirmed to
        # engage correctly, but made no difference to the freeze this was
        # meant to fix -- ruling out RX/TX concurrent-open as its cause.
        # Worse, that pause was a blocking real PortAudio stream.stop()
        # call with no timeout, and on this same flaky hardware it can
        # apparently hang indefinitely -- worse than the freeze it was
        # trying to prevent, since a stuck stop() there also never
        # released the TX lock, silently blocking every future
        # transmission for the rest of the session. Removed entirely; see
        # git history (this comment's commit) for the abandoned approach
        # if this needs revisiting with an actual timeout.
        log = get_ft4_decode_logger()
        try:
            import sounddevice as sd  # optional dep

            validate_output_device(self._out_device, SAMPLE_RATE, channels=1)

            if self._rig is not None:
                # freeze_doppler=False: an FT4 transmission is ~5 s, far too
                # long to hold the VFOs still -- doing so smears our signal
                # across the passband. Keep correcting right through TX
                # (GitHub Issue #16).
                t0 = time.monotonic()
                ptt_ok = self._rig.set_ptt(True, freeze_doppler=False)
                log.info("tx ptt_on ok=%s duration=%.3fs", ptt_ok, time.monotonic() - t0)
                if not ptt_ok:
                    self.error.emit(_("PTT command failed — check Rig 1 connection"))
                    return
                time.sleep(0.15)  # PTT lead time

            audio = self._audio
            n = len(audio)
            idx = 0
            last_gain = float(self._get_gain())
            done = threading.Event()

            # GitHub Issue #26: with pause_input()/resume_input() confirmed
            # engaging (rx_paused=True) and both fast, "tx playback_done"
            # still took 26-30s instead of the ~5s the audio itself is —
            # ruling out RX/TX concurrent-open as the cause of *this* span.
            # `status` (PortAudio's own under/overrun report for this
            # callback) was being silently discarded; cb_stats also tracks
            # how many times the callback actually ran and the longest gap
            # between two calls, to tell "many calls arriving late" apart
            # from "wrong call count" once the next freeze is logged.
            cb_stats: dict[str, Any] = {
                "n_calls": 0,
                "first_time": None,
                "max_gap": 0.0,
                "status_flags": set(),
            }

            def _callback(
                outdata: NDArray[np.float32], frames: int, _time: Any, status: Any
            ) -> None:
                nonlocal idx, last_gain
                now = time.monotonic()
                cb_stats["n_calls"] += 1
                if cb_stats["first_time"] is None:
                    cb_stats["first_time"] = now
                else:
                    gap = now - cb_stats["prev_time"]
                    if gap > cb_stats["max_gap"]:
                        cb_stats["max_gap"] = gap
                cb_stats["prev_time"] = now
                if status:
                    cb_stats["status_flags"].add(str(status))
                remaining = n - idx
                take = min(frames, remaining)
                if take > 0:
                    gain_now = float(self._get_gain())
                    ramp = np.linspace(last_gain, gain_now, take, dtype=np.float32)
                    outdata[:take, 0] = audio[idx : idx + take] * ramp
                    last_gain = gain_now
                    idx += take
                if take < frames:
                    outdata[take:, 0] = 0.0
                if remaining <= frames:
                    raise sd.CallbackStop()

            # GitHub Issue #26: a report with pause_input()/set_ptt() both
            # logging fine still ended in the whole app freezing solid right
            # after "tx ptt_on" -- with a *different*, unrelated background
            # thread (Doppler tracking) also going silent at the very same
            # instant, which points to a GIL-blocking native call somewhere
            # in opening/starting/playing this stream, not a lock deadlock
            # between our own Python objects. These three checkpoints (spans
            # not previously distinguished by any log line) narrow down
            # which of open/start/playback it's actually stuck in.
            # GitHub Issue #26: with the RX-pause dead end reverted,
            # "tx callback_stats" finally logged real data — PortAudio was
            # reporting output_underflow on every single transmission, with
            # the callback's *count* exactly right (252, matching the
            # audio length) but individual calls arriving up to ~300ms
            # late instead of the expected ~20ms apart, stretching ~5s of
            # audio out to 12-13s. Requesting latency="high" was tried next
            # to give this device's driver more buffer headroom, but the
            # next log round showed it made things measurably *worse* and
            # escalating within the same session (26.9s, then 32.1s, then
            # Halt TX unable to stop a transmission still running past
            # 34s) — this specific USB codec's driver apparently mishandles
            # PortAudio's "high" latency request rather than benefiting
            # from it. Reverted back to leaving `latency` unset (PortAudio's
            # own default) rather than guessing at another value with no
            # data to back it. The underlying underrun is still
            # unresolved — Halt TX (see abort(), below) now actually cuts
            # an in-progress burst short instead of only preventing future
            # ones, as a safety net until the root cause is found.
            t0 = time.monotonic()
            stream = sd.OutputStream(
                samplerate=SAMPLE_RATE,
                device=self._out_device,
                channels=1,
                dtype="float32",
                blocksize=_TX_BLOCK_SIZE,
                callback=_callback,
                finished_callback=done.set,
            )
            with self._stream_lock:
                self._active_stream = stream
            log.info("tx stream_open duration=%.3fs", time.monotonic() - t0)
            t0 = time.monotonic()
            with stream:
                stream_started_at = time.monotonic()
                log.info("tx stream_started duration=%.3fs", stream_started_at - t0)
                mgr.pin_active_output(_AUDIO_OWNER)
                t0 = time.monotonic()
                finished_naturally = done.wait(timeout=_TX_WATCHDOG_S)
                if not finished_naturally:
                    # The audio thread never signalled completion within
                    # the watchdog window -- cut it short exactly like an
                    # operator pressing Halt TX would (see abort()'s
                    # docstring: PortAudio still invokes finished_callback
                    # after abort(), so nothing further needs to be done
                    # here to unblock; the PTT-off below still runs
                    # unconditionally).
                    log.warning(
                        "tx watchdog FIRED after %.1fs — forcing PTT off "
                        "(audio pipeline did not signal completion)",
                        _TX_WATCHDOG_S,
                    )
                    with contextlib.suppress(Exception):
                        stream.abort()
                log.info("tx playback_done duration=%.3fs", time.monotonic() - t0)
                first_lag = (
                    cb_stats["first_time"] - stream_started_at
                    if cb_stats["first_time"] is not None
                    else -1.0
                )
                log.info(
                    "tx callback_stats n_calls=%d first_cb_lag=%.3fs max_gap=%.3fs status_flags=%s",
                    cb_stats["n_calls"],
                    first_lag,
                    cb_stats["max_gap"],
                    sorted(cb_stats["status_flags"]) or ["none"],
                )

            if self._rig is not None:
                time.sleep(0.10)  # PTT tail time
                t0 = time.monotonic()
                self._rig.set_ptt(False)
                log.info("tx ptt_off duration=%.3fs", time.monotonic() - t0)

            if finished_naturally:
                self.finished.emit()
            else:
                # Surfaced as an error (rather than finished) so the
                # operator sees clearly in the status bar / log that this
                # transmission was cut short by the watchdog rather than
                # completing normally -- PTT is already back off by now.
                self.error.emit(
                    _("TX watchdog: forced PTT off after {s:.0f}s (audio stalled)").format(
                        s=_TX_WATCHDOG_S
                    )
                )
        except Exception as exc:
            if self._rig is not None:
                with contextlib.suppress(Exception):
                    self._rig.set_ptt(False)
            self.error.emit(str(exc))
        finally:
            with self._stream_lock:
                self._active_stream = None
            mgr.release_output(_AUDIO_OWNER, self._out_device)


class _RxDecodeWorker(QObject):
    """Runs Ft4Codec.decode_audio() off the Qt main thread.

    libft4wsjt's full 3-pass subtract+BP/OSD decode over a crowded band can
    take a large fraction of a period (measured ~0.4s on a fast desktop
    for ~28 overlapping stations; a low-power field PC can take several
    times longer). Calling it directly from the scheduler's QTimer-driven
    slot blocked the Qt event loop for that whole time, which stalled the
    scheduler's own timer and desynced RX period boundaries from real UTC
    time for every period after the first slow decode — this is what let a
    single strong station decode while WSJT-X, running as a separate
    process, decoded many more from the same audio (2026-07-09).

    libft4wsjt's C bridge keeps its state in Fortran module-level `save`
    variables (see scripts/wsjtx_bridge/ft4wsjt_bridge.f90) and is not
    reentrant, so Ft4Tab must never have two of these running at once — see
    `_decode_busy` in `_on_capture_period()`.
    """

    done: Signal = Signal(object, object)  # (messages: list[Ft4Message], audio: NDArray)

    def __init__(
        self,
        codec: Ft4Codec,
        audio: NDArray[np.float32],
        my_call: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._codec = codec
        self._audio = audio
        self._my_call = my_call

    def run(self) -> None:
        t0 = time.monotonic()
        try:
            messages = self._codec.decode_audio(self._audio, my_call=self._my_call)
        except Exception:
            messages = []
        elapsed = time.monotonic() - t0
        logger = get_ft4_decode_logger()
        logger.info(
            "decode audio_len=%.2fs duration=%.2fs messages=%d",
            len(self._audio) / SAMPLE_RATE,
            elapsed,
            len(messages),
        )
        # Message content on its own lines (GitHub Issue #16: "include the
        # FT4 activity in the ft4_decode.log — so, any decodes are logged").
        # slot=EVEN/ODD is the parity the *sender* transmitted in -- the
        # period that was just captured and decoded, i.e. the one right
        # before "now" (see Ft4Tab._display_decoded for why "now" is
        # instead the correct *reply* parity, one slot later).
        reply_is_even, _pos = Ft4Scheduler.current_slot_info()
        sender_slot = "ODD" if reply_is_even else "EVEN"
        for msg in messages:
            logger.info(
                'decoded slot=%s snr=%+d dt=%+.1f freq=%.0f text="%s"',
                sender_slot,
                msg.snr_db,
                msg.dt_sec,
                msg.freq_hz,
                msg.text,
            )
        self.done.emit(messages, self._audio)


# ---------------------------------------------------------------------------
# FT4 Tab
# ---------------------------------------------------------------------------


class Ft4Tab(QWidget):
    """FT4 QSO tab.

    Requires a rig connected and Sound Card settings configured.
    Opens via Communications > FT4 or when a FT4 transponder is selected.
    """

    #: Emitted from the audio callback thread (soundcard or SDR) with the
    #: current chunk's peak level in dBFS; Qt auto-queues this to the UI
    #: thread. Lets the RX Level meter confirm audio is actually reaching
    #: this tab, independent of whether anything decodes.
    level_updated: Signal = Signal(float)

    #: Emitted from Ft4RxCaptureWorker's own thread (see _on_capture_period)
    #: when a period's audio was captured but decode was skipped (busy or
    #: unavailable) — Qt auto-queues this to the UI thread so the waterfall
    #: can still be updated safely (it does QPainter/QPixmap work, which
    #: must run on the Qt main thread).
    period_skipped: Signal = Signal(object)

    _LEVEL_MIN_INTERVAL_S = 0.05  # ~20fps UI update cap

    def __init__(
        self,
        conn: sqlite3.Connection,
        radio_control: Any,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._conn = conn
        self._radio_control = radio_control

        self._codec = Ft4Codec()
        self._scheduler = Ft4Scheduler(self)
        self._rx_capture = Ft4RxCaptureWorker(self._on_capture_period)
        self._qso: Ft4QsoManager | None = None  # created when callsign is known
        self._audio_active: bool = False  # soundcard RX subscribed via AudioDeviceManager
        self._tx_thread: threading.Thread | None = None
        self._tx_worker: _TxWorker | None = None
        self._tx_enabled: bool = False
        self._tx_in_progress: bool = False
        self._last_level_emit: float = 0.0
        self._waterfall_dialog: Ft4WaterfallDialog | None = None
        self._decode_busy: bool = False
        self._decode_thread: threading.Thread | None = None
        # Set (by _transmit_now(), on the Qt main thread) when we actually
        # start transmitting in what will become "the current period" from
        # Ft4RxCaptureWorker's point of view; consumed by _on_capture_period()
        # (background thread) when that same period's audio arrives one
        # period later. GitHub Issue #16: audio captured during our own TX
        # window is unreliable regardless of the physical cause (soundcard
        # TX/RX crosstalk, RF self-reception via the transponder, etc.) and
        # was producing corrupted decodes like "CQ EI4GNB -16" -- skip
        # decoding it rather than trying to explain away every possible
        # contamination path.
        self._tx_this_period_lock = threading.Lock()
        self._tx_this_period: bool = False

        self._my_call: str = ""
        self._my_grid: str = ""
        self._audio_freq: float = _DEFAULT_AUDIO_FREQ
        self._out_device: int | None = None
        self._in_device: int | None = None
        self._rx_source: str = "soundcard"  # "soundcard" or "sdr"
        self._tx_slot_mode: str = "auto"  # "auto", "even", or "odd"
        # Start a QSO by ourselves when called while idle. Off by default:
        # monitoring must never begin answering people on its own.
        self._auto_progress: bool = False
        self._sdr_connected: bool = False
        self._sdr_pipeline: Any | None = None
        self._tx_level_pct: float = 100.0  # % of full-scale TX audio amplitude

        self._load_settings()
        self._ensure_table()
        self._setup_ui()
        self.level_updated.connect(self._on_level_updated)
        self.period_skipped.connect(self._on_period_skipped)
        self._connect_rig_signals()
        self._connect_sdr_audio()
        self._refresh_codec_status()

        # Scheduler signals — Ft4Scheduler (QTimer-based) now only drives the
        # TX-turn decision and the countdown/TX-RX indicator display. RX
        # audio capture and decode triggering live entirely in
        # Ft4RxCaptureWorker (see _on_capture_period), whose own thread is
        # never blocked by whatever the Qt main thread is doing (2026-07-10).
        self._scheduler.period_tick.connect(self._on_period_tick)
        self._scheduler.period_changed.connect(self._on_period_changed)

        # Start listening immediately — decoding is a receive-only operation
        # and must not require pressing CQ / TX Enable first. TX itself
        # stays gated behind _tx_enabled, so this cannot transmit anything.
        self._start_scheduler(tx_even=True)
        if self._rx_source != "sdr":
            self._start_audio_capture()

    # ------------------------------------------------------------------ #
    # Setup                                                                #
    # ------------------------------------------------------------------ #

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(5)

        # -- Top: input/codec banners --
        self._input_banner = QLabel()
        self._input_banner.setStyleSheet("color: #f44336;")
        _ft4_help = QLabel(" ? ")
        _ft4_help.setStyleSheet(
            "color:white;background:#2980b9;border-radius:8px;font-weight:bold;padding:2px 6px;"
        )
        _ft4_help.setToolTip(
            "FT4 is available on:\n"
            "  • RS-44   (NORAD 44909)  DL 435.612 MHz / UL 145.993 MHz\n"
            "  • JO-97   (NORAD 43803)  DL 145.857 MHz / UL 435.118 MHz\n"
            "  • MO-122  (NORAD 60209)  DL 435.812 MHz / UL 145.938 MHz\n"
            "  • AO-73   (NORAD 39444)  DL 145.952 MHz / UL 435.148 MHz\n\n"
            "Select one of these satellites in Radio Control to get started."
        )
        _banner_row = QHBoxLayout()
        _banner_row.setSpacing(6)
        _banner_row.addWidget(self._input_banner)
        _banner_row.addWidget(_ft4_help)
        _banner_row.addStretch()
        root.addLayout(_banner_row)

        self._codec_banner = QLabel()
        self._codec_banner.setWordWrap(True)
        self._codec_banner.setStyleSheet("background:#e74c3c;color:white;padding:4px;")
        self._codec_banner.setVisible(False)
        root.addWidget(self._codec_banner)

        # -- Configuration row (single GroupBox, all settings inline) --
        cfg_grp = QGroupBox(_("Configuration"))
        cfg_lay = QHBoxLayout(cfg_grp)
        cfg_lay.setSpacing(6)

        cfg_lay.addWidget(QLabel(_("My Call:")))
        self._call_edit = QLineEdit(self._my_call)
        self._call_edit.setMinimumWidth(70)
        self._call_edit.setMaximumWidth(100)
        self._call_edit.textChanged.connect(self._on_settings_changed)
        cfg_lay.addWidget(self._call_edit)

        cfg_lay.addWidget(QLabel(_("Grid:")))
        self._grid_edit = QLineEdit(self._my_grid)
        self._grid_edit.setMinimumWidth(50)
        self._grid_edit.setMaximumWidth(70)
        self._grid_edit.textChanged.connect(self._on_settings_changed)
        cfg_lay.addWidget(self._grid_edit)

        cfg_lay.addWidget(QLabel(_("Audio Hz:")))
        self._audio_freq_edit = QLineEdit(str(int(self._audio_freq)))
        self._audio_freq_edit.setMinimumWidth(50)
        self._audio_freq_edit.setMaximumWidth(60)
        self._audio_freq_edit.textChanged.connect(self._on_settings_changed)
        cfg_lay.addWidget(self._audio_freq_edit)

        cfg_lay.addWidget(QLabel(_("RX:")))
        self._rx_src_combo = QComboBox()
        self._rx_src_combo.addItem(_("Rig Soundcard"), "soundcard")
        self._rx_src_combo.addItem(_("SDR"), "sdr")
        self._rx_src_combo.setMaximumWidth(100)
        self._rx_src_combo.currentIndexChanged.connect(self._on_rx_source_changed)
        cfg_lay.addWidget(self._rx_src_combo)

        # RX level meter — confirms audio is actually reaching this tab's
        # decode pipeline, independent of whether anything decodes.
        cfg_lay.addWidget(QLabel(_("RX Level:")))
        self._level_bar = QProgressBar()
        self._level_bar.setRange(0, 100)
        self._level_bar.setValue(0)
        self._level_bar.setTextVisible(False)
        self._level_bar.setFixedHeight(14)
        self._level_bar.setFixedWidth(80)
        self._level_bar.setToolTip(_("-- dBFS"))
        cfg_lay.addWidget(self._level_bar)

        self._waterfall_btn = QPushButton(_("Waterfall"))
        self._waterfall_btn.setToolTip(
            _(
                "Open a popup showing the last RX period's audio as a "
                "spectrogram, to check whether real FT4 signals are visible "
                "in the passband even when nothing decodes."
            )
        )
        self._waterfall_btn.clicked.connect(self._on_show_waterfall)
        cfg_lay.addWidget(self._waterfall_btn)

        cfg_lay.addStretch()
        root.addWidget(cfg_grp)

        # -- Period / TX status row (no GroupBox, same as Q65) --
        status_row = QHBoxLayout()

        self._period_label = QLabel("FT4")
        self._period_label.setStyleSheet("font-weight:bold;font-size:13px;")
        status_row.addWidget(self._period_label)

        self._countdown_label = QLabel(f"{FT4_PERIOD:.1f} s / {FT4_PERIOD:.1f}")
        self._countdown_label.setStyleSheet("font-size:13px;")
        status_row.addWidget(self._countdown_label)

        # Shows which parity we are actually set to transmit in right now.
        # With TX Slot: Auto this only becomes known once a CQ is called or
        # a station is answered, and until now there was no way to see what
        # it resolved to (GitHub Issue #16: "unclear which period is being
        # used for TX").
        self._tx_slot_indicator = QLabel("")
        self._tx_slot_indicator.setStyleSheet("font-size:12px;color:gray;")
        status_row.addWidget(self._tx_slot_indicator)

        status_row.addStretch()

        self._rx_indicator = QLabel(_("● RX"))
        self._rx_indicator.setStyleSheet("color:#00cc44;font-weight:bold;")
        status_row.addWidget(self._rx_indicator)

        self._tx_indicator = QLabel(_("● TX"))
        self._tx_indicator.setStyleSheet("color:gray;font-weight:bold;")
        status_row.addWidget(self._tx_indicator)

        root.addLayout(status_row)

        # -- Transmit GroupBox (buttons + TX line + QSO row) --
        tx_grp = QGroupBox(_("Transmit"))
        tx_lay = QVBoxLayout(tx_grp)
        tx_lay.setSpacing(4)

        # Quick buttons + TX Enable / Halt TX
        btn_row = QHBoxLayout()
        for label, slot, tip in [
            ("CQ", self._on_btn_cq, _("Call CQ")),
            (
                "MyGrid",
                self._on_btn_mygrid,
                _("Answer with our grid — the standard opening exchange"),
            ),
            ("RST", self._on_btn_rst, _("Answer with a signal report, skipping the grid")),
            ("R+RST", self._on_btn_rrst, _("Acknowledge and report (R + report)")),
            ("RR73", self._on_btn_rr73, _("Confirm the exchange")),
            ("73", self._on_btn_73, _("Sign off")),
        ]:
            btn = QPushButton(label)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            btn_row.addWidget(btn)

        btn_row.addWidget(QLabel(_("TX Slot:")))
        self._tx_slot_combo = QComboBox()
        self._tx_slot_combo.addItem(_("Auto"), "auto")
        self._tx_slot_combo.addItem(_("Even"), "even")
        self._tx_slot_combo.addItem(_("Odd"), "odd")
        self._tx_slot_combo.setCurrentIndex(
            max(0, self._tx_slot_combo.findData(self._tx_slot_mode))
        )
        self._tx_slot_combo.currentIndexChanged.connect(self._on_tx_slot_mode_changed)
        self._tx_slot_combo.setToolTip(
            _(
                "Which slot to transmit in when calling CQ / enabling TX.\n"
                "Auto: whichever slot is current when you press CQ / TX Enable.\n"
                "Responding to a decoded CQ always uses the correct opposite\n"
                "slot regardless of this setting."
            )
        )
        btn_row.addWidget(self._tx_slot_combo)

        btn_row.addStretch()

        self._tx_enable_btn = QPushButton(_("TX Enable"))
        self._tx_enable_btn.setCheckable(True)
        self._tx_enable_btn.setStyleSheet(
            "QPushButton:checked{background:#006600;color:white;font-weight:bold;}"
        )
        self._tx_enable_btn.toggled.connect(self._on_tx_enable_toggled)
        btn_row.addWidget(self._tx_enable_btn)

        self._halt_btn = QPushButton(_("Halt TX"))
        self._halt_btn.clicked.connect(self._on_halt)
        self._halt_btn.setStyleSheet("QPushButton{color:#cc3300;}")
        btn_row.addWidget(self._halt_btn)

        tx_lay.addLayout(btn_row)

        # TX message line
        tx_msg_row = QHBoxLayout()
        tx_msg_row.addWidget(QLabel(_("TX:")))
        self._tx_edit = QLineEdit()
        self._tx_edit.setPlaceholderText(_("FT4 message (auto-filled by state machine)"))
        tx_msg_row.addWidget(self._tx_edit, stretch=1)
        tx_lay.addLayout(tx_msg_row)

        # QSO state row
        qso_row = QHBoxLayout()
        self._qso_label = QLabel(_("State: IDLE"))
        self._qso_label.setStyleSheet("font-weight:bold;")
        qso_row.addWidget(self._qso_label)

        qso_row.addStretch()

        qso_row.addWidget(QLabel(_("TX Level:")))
        self._tx_level_slider = QSlider(Qt.Orientation.Horizontal)
        self._tx_level_slider.setRange(1, 100)
        self._tx_level_slider.setValue(int(self._tx_level_pct))
        self._tx_level_slider.setFixedWidth(80)
        self._tx_level_slider.setToolTip(
            _(
                # "(percentage of" rather than "(% of", and no "100% here":
                # xgettext reads "% o"/"% h" as printf directives and marks
                # the whole string python-format, which then makes msgfmt
                # --check reject any translation whose own "%" is followed by
                # something that is not a conversion specifier. A "%" ending
                # a sentence ("100%.") is not misread, so it can stay.
                "TX audio output level (percentage of full scale).\n"
                "Lower this if the rig's ALC is triggered or the transmit\n"
                "audio sounds distorted — FT4 audio is generated at full\n"
                "scale and some rigs/sound cards need well under 100%."
            )
        )
        self._tx_level_label = QLabel(f"{int(self._tx_level_pct)}%")
        self._tx_level_label.setFixedWidth(34)
        self._tx_level_slider.valueChanged.connect(self._on_tx_level_changed)
        qso_row.addWidget(self._tx_level_slider)
        qso_row.addWidget(self._tx_level_label)

        self._clear_btn = QPushButton(_("Clear"))
        self._clear_btn.clicked.connect(self._on_clear_qso)
        qso_row.addWidget(self._clear_btn)

        self._log_btn = QPushButton(_("Log QSO"))
        self._log_btn.setEnabled(False)
        self._log_btn.clicked.connect(self._on_log_qso)
        qso_row.addWidget(self._log_btn)

        self._adif_btn = QPushButton(_("Export ADIF…"))
        self._adif_btn.clicked.connect(self._on_export_adif)
        qso_row.addWidget(self._adif_btn)

        qso_row.addStretch()
        qso_row.addWidget(QLabel(_("Auto-progress:")))
        self._auto_progress_combo = QComboBox()
        self._auto_progress_combo.addItem(_("No"), False)
        self._auto_progress_combo.addItem(_("Yes"), True)
        self._auto_progress_combo.setCurrentIndex(1 if self._auto_progress else 0)
        self._auto_progress_combo.setToolTip(
            _(
                "When another station calls us while no QSO is running, start\n"
                "one automatically and prepare the reply. First caller wins if\n"
                "several answer at once. Transmitting still requires TX Enable."
            )
        )
        self._auto_progress_combo.currentIndexChanged.connect(self._on_auto_progress_changed)
        qso_row.addWidget(self._auto_progress_combo)

        tx_lay.addLayout(qso_row)
        root.addWidget(tx_grp)

        # -- Separator --
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep)

        # -- Decoded messages table (expands to fill space) --
        decoded_header = QHBoxLayout()
        decoded_header.addWidget(QLabel(_("Decoded Messages")))
        even_word = _("even")
        odd_word = _("odd")
        even_color = _EVEN_SLOT_TEXT_COLOR.name()
        odd_color = _ODD_SLOT_TEXT_COLOR.name()
        slot_legend = QLabel(
            f'(<span style="color:{even_color};">{even_word}</span> / '
            f'<span style="color:{odd_color};">{odd_word}</span>)'
        )
        slot_legend.setStyleSheet("font-size:11px;font-style:italic;")
        slot_legend.setToolTip(
            _(
                "Blue text: the other station transmitted in an even-parity "
                "slot. Orange text: odd-parity slot. Our own TX rows keep "
                "their fixed highlight colour instead."
            )
        )
        decoded_header.addWidget(slot_legend)
        decoded_header.addStretch()
        root.addLayout(decoded_header)
        self._table = QTableWidget(0, _COL_COUNT)
        self._table.setHorizontalHeaderLabels([_("UTC"), _("dB"), _("DT"), _("Hz"), _("Message")])
        self._table.horizontalHeader().setStretchLastSection(True)
        # Column widths doubled alongside the font below (Courier is
        # monospace, so character width scales ~linearly with point size) —
        # the old 9pt/narrow-column pairing was reported as too small to
        # read comfortably on a Mac (2026-08-09).
        self._table.setColumnWidth(_COL_UTC, 140)
        self._table.setColumnWidth(_COL_DB, 92)
        self._table.setColumnWidth(_COL_DT, 92)
        self._table.setColumnWidth(_COL_FREQ, 112)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setFont(QFont("Courier New", 18))
        # The native style's default item padding (most noticeable on
        # macOS) left excess space above/below the text once the font grew
        # to 18pt, wasting row height without adding readability. Trimming
        # it here shrinks each row back down to roughly the font's own
        # line height (2026-08-09).
        self._table.setStyleSheet("QTableWidget::item { padding: 0px 4px; }")
        self._table.cellDoubleClicked.connect(self._on_message_double_clicked)
        root.addWidget(self._table, stretch=1)

        # -- Bottom row --
        bot_row = QHBoxLayout()
        _clr = QPushButton(_("Clear"))
        _clr.clicked.connect(self._table.clearContents)
        _clr.clicked.connect(lambda: self._table.setRowCount(0))
        bot_row.addWidget(_clr)
        self._log_count_label = QLabel("")
        bot_row.addWidget(self._log_count_label)
        bot_row.addStretch()
        self._status_label = QLabel("")
        # Without word wrap, a long error string (e.g. a sound-card
        # exception message) forces QLabel's minimumSizeHint to fit the
        # whole line, widening the window and blocking shrinking it back.
        self._status_label.setWordWrap(True)
        bot_row.addWidget(self._status_label)
        root.addLayout(bot_row)

        self._refresh_log_count()

    def _connect_rig_signals(self) -> None:
        rc = self._radio_control
        for sig_name in ("rig_connected", "rig1_connected"):
            sig = getattr(rc, sig_name, None)
            if sig is not None:
                sig.connect(self._on_rig_connected)
        for sig_name in ("rig_disconnected", "rig1_disconnected"):
            sig = getattr(rc, sig_name, None)
            if sig is not None:
                sig.connect(self._on_rig_disconnected)
        # Reflect current connection state at tab-open time
        rig = getattr(rc, "_rig1", None)
        already_connected = rig is not None and getattr(rig, "is_connected", False)
        self._refresh_input_source(already_connected)

    # ------------------------------------------------------------------ #
    # Settings persistence                                                 #
    # ------------------------------------------------------------------ #

    def _load_settings(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (_FT4_SETTINGS_KEY,)
        ).fetchone()
        if row:
            data = json.loads(row[0])
            self._my_call = data.get("my_call", "")
            self._my_grid = data.get("my_grid", "")
            self._audio_freq = float(data.get("audio_freq_hz", _DEFAULT_AUDIO_FREQ))
            self._rx_source = data.get("rx_source", "soundcard")
            self._tx_slot_mode = data.get("tx_slot_mode", "auto")
            self._auto_progress = bool(data.get("auto_progress", False))
            self._tx_level_pct = float(data.get("tx_level_pct", 100.0))
        # Fall back to global callsign / grid from Set QTH if not yet set per-tab
        if not self._my_call:
            r = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = 'callsign'"
            ).fetchone()
            self._my_call = str(r[0]) if r else ""
        if not self._my_grid:
            r = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = 'grid_locator'"
            ).fetchone()
            self._my_grid = str(r[0]) if r else ""
        # Load soundcard device indices from shared soundcard_settings
        row2 = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = 'soundcard_settings'"
        ).fetchone()
        if row2:
            sc = json.loads(row2[0])
            val_in = sc.get("input_device_index")
            val_out = sc.get("output_device_index")
            self._in_device = int(val_in) if val_in is not None else None
            self._out_device = int(val_out) if val_out is not None else None

    def _save_settings(self) -> None:
        data = json.dumps(
            {
                "my_call": self._my_call,
                "my_grid": self._my_grid,
                "audio_freq_hz": self._audio_freq,
                "rx_source": self._rx_source,
                "tx_slot_mode": self._tx_slot_mode,
                "auto_progress": self._auto_progress,
                "tx_level_pct": self._tx_level_pct,
            }
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (_FT4_SETTINGS_KEY, data),
        )
        self._conn.commit()

    def _ensure_table(self) -> None:
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS ft4_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                qso_date      TEXT    NOT NULL,
                time_on       TEXT    NOT NULL,
                time_off      TEXT,
                call          TEXT    NOT NULL,
                gridsquare    TEXT,
                rst_sent      TEXT,
                rst_rcvd      TEXT,
                freq_hz       INTEGER,
                norad_cat_id  INTEGER,
                sat_name      TEXT
            )"""
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Codec status                                                         #
    # ------------------------------------------------------------------ #

    def _refresh_codec_status(self) -> None:
        if not self._codec.is_available:
            lib_dir = _(
                "Use Help > ft8lib Installation… (Recommended), or build "
                "ft8_lib (github.com/kgoba/ft8_lib) manually and place the "
                "shared library in {dir}"
            ).format(dir=get_user_ft8lib_dir())
            self._codec_banner.setText(
                _("ft8lib is not installed — FT4 TX/RX is disabled.\n") + lib_dir
            )
            self._codec_banner.setStyleSheet("background:#e74c3c;color:white;padding:4px;")
            self._codec_banner.setVisible(True)
            self._tx_enable_btn.setEnabled(False)
        elif not self._codec.decode_available:
            self._codec_banner.setText(
                _(
                    "ft8lib found but decode API is unavailable — TX only.\n"
                    "Update ft8_lib to ≥ v0.4 for RX decode support."
                )
            )
            self._codec_banner.setStyleSheet("background:#f39c12;color:white;padding:4px;")
            self._codec_banner.setVisible(True)
        elif self._codec.decode_backend != "wsjtx":
            self._codec_banner.setText(
                _(
                    "Using lightweight FT4 decoder — may miss weak or overlapping "
                    "stations in a crowded pass.\n"
                    "Help > FT4 Enhanced Decoder Installation… for WSJT-X-grade decoding."
                )
            )
            self._codec_banner.setStyleSheet("background:#3498db;color:white;padding:4px;")
            self._codec_banner.setVisible(True)
        else:
            self._codec_banner.setVisible(False)

    # ------------------------------------------------------------------ #
    # QSO helpers                                                          #
    # ------------------------------------------------------------------ #

    def _get_qso_manager(self) -> Ft4QsoManager | None:
        call = self._my_call.strip()
        grid = self._my_grid.strip()
        if not call:
            self._status_label.setText(_("Set My Call before operating"))
            return None
        if self._qso is None or self._qso._my_call != call.upper():
            self._qso = Ft4QsoManager(call, grid)
        return self._qso

    def _update_qso_display(self) -> None:
        qso = self._qso
        if qso is None or qso.state == QsoState.IDLE:
            self._qso_label.setText(_("State: IDLE"))
            self._log_btn.setEnabled(False)
            return
        sess = qso.session
        state_str = qso.state.name
        self._qso_label.setText(
            f"{sess.their_call}  [{state_str}]  "
            f"Sent: {sess.rst_sent or '—'}  Rcvd: {sess.rst_rcvd or '—'}"
        )
        self._log_btn.setEnabled(qso.state == QsoState.LOGGED)

    # ------------------------------------------------------------------ #
    # Rig / audio                                                          #
    # ------------------------------------------------------------------ #

    def _rig1(self) -> Any:
        """Return the Rig 1 controller, or None."""
        return getattr(self._radio_control, "_rig1", None)

    # ------------------------------------------------------------------ #
    # SDR audio connection                                                #
    # ------------------------------------------------------------------ #

    def _connect_sdr_audio(self) -> None:
        """Connect to SDR pipeline audio_ready signal if available."""
        if self._radio_control is None:
            return
        try:
            sdr_ctrl = getattr(self._radio_control, "_sdr_control", None)
            if sdr_ctrl is None:
                return
            pipeline = getattr(sdr_ctrl, "_pipeline", None)
            if pipeline is None:
                return
            pipeline.audio_ready.connect(self._on_sdr_audio_chunk)
            # Without this, the pipeline never actually demodulates/emits
            # audio_ready unless the user separately presses "Start Audio"
            # in SDR Control — an easy-to-miss, unrelated-looking button in
            # a different tab (GitHub Issue #12 follow-up).
            pipeline.request_audio(_AUDIO_OWNER)
            self._sdr_pipeline = pipeline
            self._sdr_connected = True
        except Exception:
            pass

    def _disconnect_sdr_audio(self) -> None:
        if self._sdr_pipeline is not None:
            with contextlib.suppress(Exception):
                self._sdr_pipeline.release_audio(_AUDIO_OWNER)
            with contextlib.suppress(Exception):
                self._sdr_pipeline.audio_ready.disconnect(self._on_sdr_audio_chunk)
            self._sdr_pipeline = None
        self._sdr_connected = False

    def refresh_sdr_pipeline(self, pipeline: Any) -> None:
        """Re-subscribe after MainWindow attaches a new SDR pipeline (reconnect).

        See CwTab.refresh_sdr_pipeline() for the full rationale (GitHub
        Issue #12 follow-up) — MainWindow creates a brand-new SDRPipeline
        on every Rig 1/2 (re)connect, and this tab's own reference
        (subscribed once at __init__) would otherwise silently stop
        receiving audio_ready after any later SDR reconnect. Safe to call
        unconditionally: _on_sdr_audio_chunk() already gates on
        self._rx_source == "sdr", so re-subscribing while on Soundcard
        input is harmless.
        """
        self._disconnect_sdr_audio()
        self._connect_sdr_audio()

    @Slot(object)
    def _on_sdr_audio_chunk(self, chunk: NDArray[np.float32]) -> None:
        if self._rx_source != "sdr":
            return
        chunk = chunk.astype(np.float32)
        self._rx_capture.push_audio(chunk)
        self._emit_level(chunk)

    # ------------------------------------------------------------------ #
    # Sounddevice audio capture                                            #
    # ------------------------------------------------------------------ #

    def _start_audio_capture(self) -> None:
        """Subscribe to shared soundcard RX audio for decode accumulation."""
        if self._audio_active:
            return
        try:
            import sounddevice as sd  # noqa: F401 — validate availability
        except ImportError:
            self._status_label.setText(_("sounddevice not installed — pip install sounddevice"))
            return
        if self._in_device is None:
            self._status_label.setText(
                _("Sound Card not configured — open Rig Settings > Sound Card")
            )
            return
        try:
            get_audio_device_manager().acquire_input(
                _AUDIO_OWNER, self._in_device, SAMPLE_RATE, self._audio_callback
            )
            self._audio_active = True
        except Exception as exc:
            self._status_label.setText("Audio Open Error")
            self._status_label.setToolTip(str(exc))
            self._audio_active = False

    def _stop_audio_capture(self) -> None:
        if self._audio_active:
            get_audio_device_manager().release_input(_AUDIO_OWNER, self._in_device)
            self._audio_active = False
            self._level_bar.setValue(0)
            self._level_bar.setToolTip(_("-- dBFS"))

    def _audio_callback(self, chunk: NDArray[np.float32]) -> None:
        self._rx_capture.push_audio(chunk)
        self._emit_level(chunk)

    def _emit_level(self, chunk: NDArray[np.float32]) -> None:
        """Runs on the audio callback thread (soundcard or SDR) — keep this fast."""
        if len(chunk) == 0:
            return
        now = time.monotonic()
        if now - self._last_level_emit < self._LEVEL_MIN_INTERVAL_S:
            return
        self._last_level_emit = now
        peak = float(np.max(np.abs(chunk)))
        dbfs = 20.0 * math.log10(max(peak, 1e-6))
        self.level_updated.emit(dbfs)

    @Slot(float)
    def _on_level_updated(self, dbfs: float) -> None:
        pct = max(0.0, min(100.0, (dbfs + 60.0) / 60.0 * 100.0))
        self._level_bar.setValue(int(pct))
        color = "#2ecc71" if dbfs < -12.0 else ("#f1c40f" if dbfs < -3.0 else "#e74c3c")
        self._level_bar.setStyleSheet(f"QProgressBar::chunk {{ background-color: {color}; }}")
        self._level_bar.setToolTip(_("{db:.0f} dBFS").format(db=dbfs))

    @Slot()
    def _on_show_waterfall(self) -> None:
        if self._waterfall_dialog is None:
            self._waterfall_dialog = Ft4WaterfallDialog(self)
        self._waterfall_dialog.set_tx_freq_hz(self._audio_freq)
        self._waterfall_dialog.show()
        self._waterfall_dialog.raise_()
        self._waterfall_dialog.activateWindow()

    # ------------------------------------------------------------------ #
    # Scheduler slots                                                      #
    # ------------------------------------------------------------------ #

    @Slot(bool, float)
    def _on_period_tick(self, is_tx: bool, seconds_remaining: float) -> None:
        self._countdown_label.setText(f"{seconds_remaining:.1f} s / {FT4_PERIOD:.1f}")
        if is_tx:
            self._rx_indicator.setStyleSheet("color:gray;font-weight:bold;")
            self._tx_indicator.setStyleSheet("color:#e74c3c;font-weight:bold;")
        else:
            self._rx_indicator.setStyleSheet("color:#00cc44;font-weight:bold;")
            self._tx_indicator.setStyleSheet("color:gray;font-weight:bold;")

    @Slot(bool)
    def _on_period_changed(self, is_tx: bool) -> None:
        """Only drives the TX-turn decision now — RX audio capture and
        decode triggering live entirely in Ft4RxCaptureWorker, which is not
        tied to TX/RX slot parity at all (every slot is captured and
        decoded regardless, see Ft4Scheduler's module docstring)."""
        if is_tx and self._tx_enabled and not self._tx_in_progress:
            self._transmit_now()

    def _on_capture_period(self, audio: NDArray[np.float32]) -> None:
        """Called from Ft4RxCaptureWorker's own background thread — NOT the
        Qt main thread — once per completed period. Must not touch Qt
        widgets directly; anything that does (the waterfall, in the skip
        cases below) goes through a Signal so Qt marshals it onto the main
        thread automatically. Decoding itself is further backgrounded onto
        its own thread (_RxDecodeWorker) since it can take a large fraction
        of a period and this thread must stay free to keep waking up
        exactly on time for the next period (2026-07-10).
        """
        with self._tx_this_period_lock:
            was_tx_period = self._tx_this_period
            self._tx_this_period = False
        if was_tx_period:
            # GitHub Issue #16: this period's audio covers a window during
            # which we ourselves were transmitting -- reporter evidence
            # (a passive WSJT-X instance decoding nothing on the same Main
            # VFO audio during our TX) argues against genuine transponder
            # self-reception, pointing instead at TX/RX crosstalk somewhere
            # in the local audio path. Either way this audio isn't a signal
            # worth decoding, so skip it outright (waterfall still updates,
            # same as the other skip cases below).
            get_ft4_decode_logger().info(
                "decode SKIPPED (own TX period) audio_len=%.2fs", len(audio) / SAMPLE_RATE
            )
            self.period_skipped.emit(audio)
            return
        if not self._codec.decode_available:
            self.period_skipped.emit(audio)
            return
        if self._decode_busy:
            # Previous period's decode hasn't finished yet. libft4wsjt's C
            # bridge is not reentrant, so this period's decode is dropped
            # rather than risking an overlapping call — but the waterfall
            # still updates so the gap is visible instead of silently stale.
            get_ft4_decode_logger().info(
                "decode SKIPPED (previous decode still running) audio_len=%.2fs",
                len(audio) / SAMPLE_RATE,
            )
            self.period_skipped.emit(audio)
            return
        self._decode_busy = True
        worker = _RxDecodeWorker(self._codec, audio, self._my_call)
        worker.done.connect(self._on_decode_done)
        thread = threading.Thread(target=worker.run, daemon=True)
        self._decode_thread = thread
        thread.start()

    @Slot(object)
    def _on_period_skipped(self, audio: NDArray[np.float32]) -> None:
        self._update_waterfall_only(audio, [])

    def _update_waterfall_only(
        self, audio: NDArray[np.float32], messages: list[Ft4Message]
    ) -> None:
        if self._waterfall_dialog is not None and self._waterfall_dialog.isVisible():
            self._waterfall_dialog.update_waterfall(audio, messages)

    @Slot(object, object)
    def _on_decode_done(self, messages: list[Ft4Message], audio: NDArray[np.float32]) -> None:
        self._decode_busy = False
        if messages:
            self._display_decoded(messages)
        self._update_waterfall_only(audio, messages)

    # ------------------------------------------------------------------ #
    # Transmit path                                                        #
    # ------------------------------------------------------------------ #

    def _transmit_now(self) -> None:
        """Start TX in a daemon thread."""
        if not self._codec.is_available:
            return
        msg = self._tx_edit.text().strip().upper()
        if not msg:
            qso = self._qso
            if qso is not None:
                msg = qso.pending_tx
        if not msg:
            return

        try:
            audio_freq = float(self._audio_freq_edit.text())
        except ValueError:
            audio_freq = _DEFAULT_AUDIO_FREQ

        audio = self._codec.encode_audio(msg, base_freq=audio_freq)
        if audio is None:
            self._status_label.setText(_("Invalid FT4 message: ") + msg)
            return

        # GitHub Issue #16: "include the FT4 activity in the ft4_decode.log
        # — so ... all TX messages are logged, with a timestamp for each."
        get_ft4_decode_logger().info(
            'tx slot=%s freq=%.0f text="%s"',
            "EVEN" if self._scheduler._tx_even else "ODD",
            audio_freq,
            msg,
        )
        self._display_own_tx(msg, audio_freq)
        with self._tx_this_period_lock:
            self._tx_this_period = True

        # TX audio is synthesized at full scale (±1.0); _TxWorker applies the
        # TX Level slider's gain live, block by block, so operators can trim
        # output level (and hear the effect immediately) even while actively
        # transmitting, to avoid rig ALC action / distortion (Issue #16).
        rig = self._rig1()
        worker = _TxWorker(
            audio, self._out_device, rig, get_gain=lambda: self._tx_level_pct / 100.0
        )
        worker.finished.connect(self._on_tx_finished)
        worker.error.connect(self._on_tx_error)

        self._tx_worker = worker
        self._tx_in_progress = True
        t = threading.Thread(target=worker.run, daemon=True)
        self._tx_thread = t
        t.start()
        self._status_label.setText(_("TX: ") + msg)

    @Slot()
    def _on_tx_finished(self) -> None:
        self._tx_in_progress = False
        self._tx_worker = None
        self._status_label.setText(_("TX done — waiting for next period"))

    @Slot(str)
    def _on_tx_error(self, msg: str) -> None:
        self._tx_in_progress = False
        self._tx_worker = None
        self._status_label.setText(_("TX error: ") + msg)

    # ------------------------------------------------------------------ #
    # Decoded messages display                                             #
    # ------------------------------------------------------------------ #

    def _display_own_tx(self, msg: str, audio_freq: float) -> None:
        """Append the just-started outgoing message to the Decoded Messages table.

        dB/DT have no meaning for our own transmission (nothing was decoded),
        so they show "N/A"; Freq shows the actual audio tone frequency used.
        This is the only row _on_capture_period() will add for this period —
        it skips decoding this period's captured audio entirely (see the
        "own TX period" branch there), so this row can't be duplicated or
        contradicted by a (possibly corrupted) decode of our own TX window.
        """
        utc_str = datetime.now(UTC).strftime("%H%M")
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setItem(row, _COL_UTC, QTableWidgetItem(utc_str))
        self._table.setItem(row, _COL_DB, QTableWidgetItem("N/A"))
        self._table.setItem(row, _COL_DT, QTableWidgetItem("N/A"))
        self._table.setItem(row, _COL_FREQ, QTableWidgetItem(f"{audio_freq:.0f}"))
        self._table.setItem(row, _COL_MSG, QTableWidgetItem(msg))
        for c in range(_COL_COUNT):
            item = self._table.item(row, c)
            if item is not None:
                item.setBackground(_OWN_TX_ROW_COLOR)
        self._table.scrollToBottom()

    def _display_decoded(self, messages: list[Ft4Message]) -> None:
        # The period we are in *right now* is already the one right after
        # the sender's, i.e. the opposite parity -- decoding takes well
        # under a second (ft4_decode.log: 0.05-0.2s), so we land here before
        # the next period boundary. That makes this the correct slot to
        # reply in, with no further flip. Stamped onto each row so a later
        # double-click answers based on when the message actually arrived,
        # not whatever period happens to be current when the operator gets
        # around to clicking (GitHub Issue #16: replies were going out on
        # the same slot as the calling station).
        reply_is_even, _pos = Ft4Scheduler.current_slot_info()
        utc_str = datetime.now(UTC).strftime("%H%M")
        for msg in messages:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setItem(row, _COL_UTC, QTableWidgetItem(utc_str))
            self._table.setItem(row, _COL_DB, QTableWidgetItem(f"{msg.snr_db:+.0f}"))
            self._table.setItem(row, _COL_DT, QTableWidgetItem(f"{msg.dt_sec:+.1f}"))
            self._table.setItem(row, _COL_FREQ, QTableWidgetItem(f"{msg.freq_hz:.0f}"))
            self._table.setItem(row, _COL_MSG, QTableWidgetItem(msg.text))
            utc_item = self._table.item(row, _COL_UTC)
            if utc_item is not None:
                utc_item.setData(Qt.ItemDataRole.UserRole, reply_is_even)
            # Colour by the slot the *other station* transmitted in (the
            # parity opposite our reply slot) so it is visible at a glance
            # which period a decode came from (GitHub Issue #16).
            sender_color = _ODD_SLOT_TEXT_COLOR if reply_is_even else _EVEN_SLOT_TEXT_COLOR
            for c in range(_COL_COUNT):
                item = self._table.item(row, c)
                if item is not None:
                    item.setForeground(sender_color)
            # Highlight if message addressed to us
            if self._my_call and self._my_call.upper() in msg.text.upper():
                for c in range(_COL_COUNT):
                    item = self._table.item(row, c)
                    if item is not None:
                        item.setBackground(Qt.GlobalColor.yellow)
            self._table.scrollToBottom()

        self._auto_advance_qso(messages, reply_is_even)

    def _auto_advance_qso(self, messages: list[Ft4Message], reply_is_even: bool) -> None:
        """Feed decodes to the state machine and follow it.

        Once a QSO is under way it always advances by itself. Starting one
        from IDLE because somebody called us is opt-in (the Auto-progress
        selector) so plain monitoring never answers on its own. If several
        stations call in the same period the first decode wins -- the state
        machine stops matching the rest as soon as it has a partner
        (GitHub Issue #16).
        """
        qso = self._qso
        if qso is None:
            return
        # Snapshot the state: comparing qso.state directly would let mypy
        # narrow the property and then reject the post-advance() re-read
        # below, which is the whole point (advance() mutates it).
        state_before = qso.state
        if state_before == QsoState.LOGGED:
            return
        if state_before == QsoState.IDLE and not self._auto_progress:
            return

        was_idle = state_before == QsoState.IDLE
        for msg in messages:
            next_tx = qso.advance(
                msg.text, their_snr=msg.snr_db, allow_auto_start=self._auto_progress
            )
            if next_tx is None:
                continue
            self._tx_edit.setText(next_tx)
            self._update_qso_display()
            if was_idle:
                # They called us in the period that just decoded; that
                # period's parity (see _display_decoded) is already the
                # correct one to answer in -- no flip.
                self._start_scheduler(tx_even=reply_is_even)
                self._status_label.setText(
                    _("Auto-answering {call}").format(call=qso.session.their_call)
                )
            break

        if qso.state == QsoState.LOGGED:
            self._on_qso_complete()

    def _on_qso_complete(self) -> None:
        """Stop transmitting once the exchange is over.

        Without this the last message stays in the TX field and goes out
        again every single TX slot until the operator notices and presses
        Halt -- _transmit_now() reads that field, and nothing was clearing
        it (GitHub Issue #16).
        """
        self._tx_edit.clear()
        if self._tx_enabled:
            self._tx_enabled = False
            self._tx_enable_btn.setChecked(False)
        self._update_qso_display()
        call = self._qso.session.their_call if self._qso is not None else ""
        self._status_label.setText(
            _("QSO with {call} complete — TX stopped, press Log QSO to save").format(call=call)
        )

    def _row_snr_db(self, row: int) -> float | None:
        """Measured SNR of a decoded row, or None for our own TX rows."""
        item = self._table.item(row, _COL_DB)
        if item is None:
            return None
        try:
            return float(item.text())
        except ValueError:
            return None  # our own TX rows show "N/A"

    @Slot(int, int)
    def _on_message_double_clicked(self, row: int, _col: int) -> None:
        """Pick up the other station from any decoded row and answer them.

        Not just CQs: a station signing "73" with someone else is about to be
        free, and is exactly who you want to call next on a short satellite
        pass. FT4 directed messages are "<TO> <FROM> <payload>", so the
        station to work is the sender -- unless we are the sender, in which
        case it is the addressee (GitHub Issue #16).

        The default answer is our grid, the standard opening exchange. Press
        RST afterwards to switch to a report instead.
        """
        item = self._table.item(row, _COL_MSG)
        if item is None:
            return
        words = item.text().upper().split()
        if not words:
            return
        qso = self._get_qso_manager()
        if qso is None:
            return

        if words[0] == "CQ":
            their_call, their_grid = _parse_cq_call_grid(words[1:])
        elif len(words) >= 2:
            to_call, from_call = words[0], words[1]
            their_call = to_call if from_call == self._my_call.upper() else from_call
            their_grid = words[2] if len(words) >= 3 and _GRID_RE.match(words[2]) else ""
        else:
            return
        if not their_call or their_call == self._my_call.upper():
            return

        reply = qso.respond_with_grid(their_call, their_grid, self._row_snr_db(row))
        self._tx_edit.setText(reply)
        self._update_qso_display()
        # Use the slot parity stamped on this row at decode time (see
        # _display_decoded), not whatever period happens to be current now.
        # The operator may click well after the message arrived, and by then
        # the period may have flipped one or more times; the reply parity
        # must stay fixed to when the call was actually heard, not to click
        # timing (GitHub Issue #16).
        utc_item = self._table.item(row, _COL_UTC)
        stored_even = utc_item.data(Qt.ItemDataRole.UserRole) if utc_item is not None else None
        if stored_even is None:
            stored_even, _pos = Ft4Scheduler.current_slot_info()
        self._start_scheduler(tx_even=bool(stored_even))

    # ------------------------------------------------------------------ #
    # TX quick buttons                                                     #
    # ------------------------------------------------------------------ #

    def _on_btn_cq(self) -> None:
        qso = self._get_qso_manager()
        if qso is None:
            return
        msg = qso.start_cq()
        self._tx_edit.setText(msg)
        self._update_qso_display()
        is_even, _pos = Ft4Scheduler.current_slot_info()
        self._start_scheduler(tx_even=self._resolve_tx_even(is_even))

    def _active_qso_for_button(self) -> Ft4QsoManager | None:
        """QSO manager for the message buttons, or None with a reason shown.

        These buttons all need a station to address. They used to read
        self._qso directly and silently do nothing when it was None or had no
        callsign yet, which looked exactly like a dead button (GitHub Issue
        #16) -- say what is missing instead.
        """
        qso = self._get_qso_manager()  # also reports a missing My Call
        if qso is None:
            return None
        if not qso.session.their_call:
            self._status_label.setText(_("Double-click a decoded station first"))
            return None
        return qso

    def _apply_button_message(self, msg: str, state: QsoState) -> None:
        """Put a hand-picked message on the air and move the QSO to match.

        The buttons used to write straight into the TX field, leaving the
        state machine behind -- so the next decode would answer as if the
        button had never been pressed.
        """
        qso = self._qso
        if qso is not None:
            qso.pending_tx = msg
            qso.set_state(state)
        self._tx_edit.setText(msg)
        self._update_qso_display()

    def _on_btn_mygrid(self) -> None:
        qso = self._active_qso_for_button()
        if qso is None:
            return
        sess = qso.session
        msg = qso.respond_with_grid(sess.their_call, sess.their_grid, sess.their_snr_db)
        self._tx_edit.setText(msg)
        self._update_qso_display()

    def _on_btn_rst(self) -> None:
        qso = self._active_qso_for_button()
        if qso is None:
            return
        sess = qso.session
        msg = qso.respond_with_report(sess.their_call, sess.their_grid, sess.their_snr_db)
        self._tx_edit.setText(msg)
        self._update_qso_display()

    def _on_btn_rrst(self) -> None:
        qso = self._active_qso_for_button()
        if qso is None:
            return
        sess = qso.session
        report = format_report(sess.their_snr_db) if sess.their_snr_db is not None else "-05"
        self._apply_button_message(
            f"{sess.their_call} {self._my_call.upper()} R{report}", QsoState.RREPORT_SENT
        )

    def _on_btn_rr73(self) -> None:
        qso = self._active_qso_for_button()
        if qso is None:
            return
        self._apply_button_message(
            f"{qso.session.their_call} {self._my_call.upper()} RR73", QsoState.CONFIRM
        )

    def _on_btn_73(self) -> None:
        qso = self._active_qso_for_button()
        if qso is None:
            return
        # Deliberately not _on_qso_complete(): the operator still has to send
        # this 73, so TX stays enabled and the message stays in the field.
        self._apply_button_message(
            f"{qso.session.their_call} {self._my_call.upper()} 73", QsoState.LOGGED
        )

    # ------------------------------------------------------------------ #
    # QSO log / clear                                                      #
    # ------------------------------------------------------------------ #

    @Slot()
    def _on_log_qso(self) -> None:
        qso = self._qso
        if qso is None:
            return
        # Attach satellite info from radio control
        norad_text = getattr(self._radio_control, "_norad_label", None)
        sat_text = getattr(self._radio_control, "_sat_name_label", None)
        try:
            qso.session.norad_cat_id = int(norad_text.text()) if norad_text else None
        except (ValueError, AttributeError):
            qso.session.norad_cat_id = None
        try:
            qso.session.sat_name = sat_text.text() if sat_text else ""
        except AttributeError:
            qso.session.sat_name = ""
        qso.log_qso(self._conn)
        self._refresh_log_count()
        self._on_clear_qso()

    @Slot()
    def _on_clear_qso(self) -> None:
        if self._qso is not None:
            self._qso.clear()
        self._tx_edit.clear()
        self._update_qso_display()

    # ------------------------------------------------------------------ #
    # Settings change handlers                                             #
    # ------------------------------------------------------------------ #

    @Slot()
    def _on_settings_changed(self) -> None:
        self._my_call = self._call_edit.text().upper().strip()
        self._my_grid = self._grid_edit.text().upper().strip()
        with contextlib.suppress(ValueError):
            self._audio_freq = float(self._audio_freq_edit.text())
            if self._waterfall_dialog is not None:
                self._waterfall_dialog.set_tx_freq_hz(self._audio_freq)
        self._qso = None  # reset manager so it picks up new callsign
        self._save_settings()

    @Slot(int)
    def _on_rx_source_changed(self, _idx: int) -> None:
        self._rx_source = self._rx_src_combo.currentData()
        if self._rx_source != "sdr":
            self._start_audio_capture()
        self._save_settings()

    @Slot(int)
    def _on_tx_slot_mode_changed(self, _idx: int) -> None:
        """Apply a new TX Slot choice immediately, not just on the next CQ.

        Previously this only recorded the setting; nothing re-read it until
        the operator pressed CQ or toggled TX Enable again, so the dropdown
        and the "TX: EVEN/ODD" indicator appeared to do nothing when
        changed on their own -- reported as slot switching looking
        "intermittent" (GitHub Issue #16). Skipped while actively replying
        to a specific station (GRID_SENT/EXCHANGE/RREPORT_SENT/CONFIRM):
        that slot is dictated by the protocol, not this setting, and must
        not be disturbed mid-exchange.
        """
        self._tx_slot_mode = self._tx_slot_combo.currentData()
        self._save_settings()
        if self._qso is None or self._qso.state in (
            QsoState.IDLE,
            QsoState.CALLING,
            QsoState.LOGGED,
        ):
            is_even, _pos = Ft4Scheduler.current_slot_info()
            self._start_scheduler(tx_even=self._resolve_tx_even(is_even))

    @Slot(int)
    def _on_auto_progress_changed(self, _idx: int) -> None:
        self._auto_progress = bool(self._auto_progress_combo.currentData())
        self._save_settings()

    @Slot(int)
    def _on_tx_level_changed(self, value: int) -> None:
        self._tx_level_pct = float(value)
        self._tx_level_label.setText(f"{value}%")
        self._save_settings()

    def _resolve_tx_even(self, auto_is_even: bool) -> bool:
        """Apply the user's manual TX Slot choice, if any, over the current slot.

        Only used when *we* initiate a CQ / TX Enable — responding to a
        decoded CQ always transmits in the opposite slot from the caller
        regardless of this setting, since that is dictated by the protocol.
        """
        if self._tx_slot_mode == "even":
            return True
        if self._tx_slot_mode == "odd":
            return False
        return auto_is_even

    # ------------------------------------------------------------------ #
    # TX Enable / Halt                                                     #
    # ------------------------------------------------------------------ #

    @Slot(bool)
    def _on_tx_enable_toggled(self, checked: bool) -> None:
        self._tx_enabled = checked
        if checked:
            if not self._codec.is_available:
                self._tx_enable_btn.setChecked(False)
                return
            if not self._my_call.strip():
                self._status_label.setText(_("Set My Call before enabling TX"))
                self._tx_enable_btn.setChecked(False)
                return
            if not self._scheduler._running:
                is_even, _pos = Ft4Scheduler.current_slot_info()
                self._start_scheduler(tx_even=self._resolve_tx_even(is_even))
            self._status_label.setText(_("TX enabled — waiting for next period"))
        else:
            self._status_label.setText(_("TX disabled"))

    @Slot()
    def _on_halt(self) -> None:
        self._tx_enabled = False
        self._tx_enable_btn.setChecked(False)
        if self._tx_in_progress and self._tx_worker is not None:
            # GitHub Issue #26: this used to only stop *future* bursts —
            # one already playing ran to completion regardless. See
            # _TxWorker.abort()'s docstring.
            self._tx_worker.abort()
            self._status_label.setText(_("TX halted — stopping current transmission"))
        else:
            self._status_label.setText(_("TX halted"))

    # ------------------------------------------------------------------ #
    # Rig connected/disconnected                                           #
    # ------------------------------------------------------------------ #

    @Slot()
    def _on_rig_connected(self) -> None:
        self._refresh_input_source(connected=True)
        self._status_label.setText(_("Rig connected — ready"))
        # Re-read soundcard settings in case they were updated
        self._load_settings()

    @Slot()
    def _on_rig_disconnected(self) -> None:
        self._on_halt()
        self._stop_audio_capture()
        self._scheduler.stop()
        self._rx_capture.stop()
        self._refresh_input_source(connected=False)
        self._status_label.setText(_("Rig disconnected"))

    def _refresh_input_source(self, connected: bool) -> None:
        """Update the input-source label text and colour (matches APRS/SSTV style).

        This reflects Rig 1's CAT connection only — soundcard RX capture does
        not depend on it (see _start_audio_capture), so the label must not
        claim audio is unavailable when it may in fact be flowing fine.
        """
        if connected:
            self._input_banner.setText(_("Input: Rig connected"))
            self._input_banner.setStyleSheet("color: #4caf50;")
        else:
            self._input_banner.setText(_("Input: Rig not connected"))
            self._input_banner.setStyleSheet("color: #f44336;")

    # ------------------------------------------------------------------ #
    # Scheduler start helper                                               #
    # ------------------------------------------------------------------ #

    def _start_scheduler(self, tx_even: bool) -> None:
        if not self._scheduler._running:
            self._scheduler.start(tx_even=tx_even)
        else:
            self._scheduler.set_tx_even(tx_even)
        # RX capture's lifecycle is tied to the scheduler's — both should be
        # running or stopped together (see _on_rig_disconnected/closeEvent).
        # start() is a no-op if already running.
        self._rx_capture.start()
        self._update_tx_slot_indicator(tx_even)

    def _update_tx_slot_indicator(self, tx_even: bool) -> None:
        text = _("TX: EVEN") if tx_even else _("TX: ODD")
        color = _EVEN_SLOT_TEXT_COLOR.name() if tx_even else _ODD_SLOT_TEXT_COLOR.name()
        self._tx_slot_indicator.setText(text)
        self._tx_slot_indicator.setStyleSheet(f"font-size:12px;font-weight:bold;color:{color};")

    # ------------------------------------------------------------------ #
    # Log count / ADIF export                                              #
    # ------------------------------------------------------------------ #

    def _refresh_log_count(self) -> None:
        row = self._conn.execute("SELECT COUNT(*) FROM ft4_log").fetchone()
        n = row[0] if row else 0
        self._log_count_label.setText(_("QSOs logged: ") + str(n))

    @Slot()
    def _on_export_adif(self) -> None:
        from ui.log_export_dialog import LogExportDialog

        dlg = LogExportDialog(self._conn, parent=self)
        dlg.exec()

    # ------------------------------------------------------------------ #
    # Cleanup on tab close                                                 #
    # ------------------------------------------------------------------ #

    def closeEvent(self, event: Any) -> None:
        # _on_halt() already calls _tx_worker.abort() below if a
        # transmission is in progress, so no separate stop-the-audio step
        # is needed here — just wait for the worker thread to actually
        # finish its PTT-off / lock-release cleanup afterward.
        self._on_halt()
        if self._tx_in_progress and self._tx_thread is not None:
            self._tx_thread.join(timeout=2.0)
            if self._tx_in_progress:
                # Thread still didn't finish — force PTT off directly as a
                # last-resort safety net so the rig can't stay keyed.
                rig = self._rig1()
                if rig is not None:
                    with contextlib.suppress(Exception):
                        rig.set_ptt(False)
        self._stop_audio_capture()
        self._disconnect_sdr_audio()
        self._scheduler.stop()
        self._rx_capture.stop()
        if self._waterfall_dialog is not None:
            self._waterfall_dialog.close()
        super().closeEvent(event)
