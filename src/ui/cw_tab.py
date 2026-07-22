"""Communications > CW Decoder tab.

Decodes CW (Morse code) from audio using the DeepCW ONNX model
(e04/deepcw-engine).  Audio input can come from:
  - SDR pipeline (audio_ready signal) when an SDR is connected
  - Soundcard InputStream (sounddevice) for rig/external audio

No rig is required — CW decoding is receive-only.
The model requires 5–20 seconds of audio per decode call.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
from collections import deque
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from comms.audio_device_manager import get_audio_device_manager
from comms.cw.codec import MIN_AUDIO_SECONDS, SAMPLE_RATE, CwDecoder
from comms.cw.model_info import is_onnxruntime_available, is_ready
from i18n import _

# Rolling audio buffer: keep last N seconds (model max is 20 s)
_BUFFER_SECONDS = 20
# Decode every 5 s, but only when >= MIN_AUDIO_SECONDS of audio is buffered
_DECODE_INTERVAL_MS = 5_000


# ---------------------------------------------------------------------------
# Background decode worker
# ---------------------------------------------------------------------------


class _DecodeWorker(QThread):
    """Runs CwDecoder.decode() off the UI thread."""

    result_ready = Signal(str)

    def __init__(self, decoder: CwDecoder, audio: NDArray[np.float32], sample_rate: int) -> None:
        super().__init__()
        self._decoder = decoder
        self._audio = audio
        self._sample_rate = sample_rate

    def run(self) -> None:
        text = self._decoder.decode(self._audio, self._sample_rate)
        self.result_ready.emit(text)


# ---------------------------------------------------------------------------
# CW Decoder tab
# ---------------------------------------------------------------------------


class CwTab(QWidget):
    """Non-resident Communications > CW Decoder tab."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        radio_control: QWidget | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._conn = conn
        self._radio_control = radio_control

        # Decoder (loaded lazily on first Start)
        self._decoder: CwDecoder | None = None

        # Audio accumulation buffer (deque of float32 arrays)
        self._rx_buffer: deque[NDArray[np.float32]] = deque()
        self._rx_sample_rate: int = SAMPLE_RATE
        self._sdr_pipeline: Any = None
        self._sdr_connected: bool = False

        # Sounddevice (shared with other Communications tabs via AudioDeviceManager)
        self._audio_active: bool = False
        self._in_device: int | None = None

        # Decode worker
        self._worker: _DecodeWorker | None = None
        self._decoding: bool = False
        self._last_text: str = ""
        self._running: bool = False

        self._setup_ui()
        self._load_sound_card_device()

        self._decode_timer = QTimer(self)
        self._decode_timer.setInterval(_DECODE_INTERVAL_MS)
        self._decode_timer.timeout.connect(self._trigger_decode)

        self._level_timer = QTimer(self)
        self._level_timer.setInterval(200)
        self._level_timer.timeout.connect(self._update_level)

        self._refresh_model_status()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Banner (shown when model/runtime is missing)
        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._banner.setStyleSheet(
            "background:#c0392b; color:white; padding:6px; border-radius:4px;"
        )
        self._banner.setVisible(False)
        root.addWidget(self._banner)

        # Controls
        ctrl_box = QGroupBox(_("CW Decoder"))
        cl = QHBoxLayout(ctrl_box)

        cl.addWidget(QLabel(_("Input:")))
        self._rb_sdr = QRadioButton(_("SDR"))
        self._rb_sd = QRadioButton(_("Soundcard"))
        self._rb_sdr.setChecked(True)
        cl.addWidget(self._rb_sdr)
        cl.addWidget(self._rb_sd)
        self._rb_sdr.toggled.connect(self._on_source_changed)

        cl.addStretch()

        self._start_btn = QPushButton(_("▶ Start"))
        self._start_btn.setCheckable(True)
        self._start_btn.setFixedWidth(90)
        self._start_btn.toggled.connect(self._on_start_stop)
        cl.addWidget(self._start_btn)

        self._clear_btn = QPushButton(_("Clear"))
        self._clear_btn.setFixedWidth(72)
        self._clear_btn.clicked.connect(self._on_clear)
        cl.addWidget(self._clear_btn)

        root.addWidget(ctrl_box)

        # Status / level row
        stat_row = QHBoxLayout()
        self._status_label = QLabel(_("Ready"))
        self._status_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        stat_row.addWidget(self._status_label)
        stat_row.addWidget(QLabel(_("Level:")))
        self._level_label = QLabel("— dB")
        self._level_label.setFixedWidth(72)
        stat_row.addWidget(self._level_label)
        root.addLayout(stat_row)

        # Decoded text
        self._text_edit = QPlainTextEdit()
        self._text_edit.setReadOnly(True)
        font = QFont("Monospace")
        font.setPointSize(12)
        self._text_edit.setFont(font)
        self._text_edit.setPlaceholderText(
            _("Decoded CW text will appear here (requires ~5 s of audio)…")
        )
        root.addWidget(self._text_edit)

    # ------------------------------------------------------------------ #
    # Model status
    # ------------------------------------------------------------------ #

    def _refresh_model_status(self) -> None:
        if not is_onnxruntime_available():
            self._banner.setText(_("onnxruntime not installed — use Help > CW Model Installation…"))
            self._banner.setVisible(True)
            self._start_btn.setEnabled(False)
            return
        if not is_ready():
            self._banner.setText(_("CW model not found — use Help > CW Model Installation…"))
            self._banner.setVisible(True)
            self._start_btn.setEnabled(False)
            return
        self._banner.setVisible(False)
        self._start_btn.setEnabled(True)

    # ------------------------------------------------------------------ #
    # Start / Stop
    # ------------------------------------------------------------------ #

    @Slot(bool)
    def _on_start_stop(self, checked: bool) -> None:
        if checked:
            self._start()
        else:
            self._stop()

    def _start(self) -> None:
        if self._decoder is None:
            self._decoder = CwDecoder()
        if not self._decoder.is_ready:
            self._status_label.setText(_("Model not ready — use Help > CW Model Installation…"))
            self._start_btn.setChecked(False)
            return

        self._running = True
        self._rx_buffer.clear()
        self._last_text = ""
        self._start_btn.setText(_("■ Stop"))

        if self._rb_sdr.isChecked():
            self._connect_sdr_audio()
            if not self._sdr_connected:
                self._status_label.setText(_("SDR not connected — connect SDR first"))
        else:
            self._rx_sample_rate = 48_000
            self._start_audio_capture()

        self._decode_timer.start()
        self._level_timer.start()
        self._status_label.setText(
            _("Listening… (decoding starts after {n} s)").format(n=int(MIN_AUDIO_SECONDS))
        )

    def _stop(self) -> None:
        self._running = False
        self._decode_timer.stop()
        self._level_timer.stop()
        self._start_btn.setText(_("▶ Start"))
        self._disconnect_sdr_audio()
        self._stop_audio_capture()
        self._status_label.setText(_("Stopped"))
        self._level_label.setText("— dB")

    # ------------------------------------------------------------------ #
    # Source changes
    # ------------------------------------------------------------------ #

    @Slot()
    def _on_source_changed(self) -> None:
        if self._running:
            self._stop()
            self._start_btn.setChecked(False)

    @Slot()
    def _on_clear(self) -> None:
        self._text_edit.clear()
        self._last_text = ""

    # ------------------------------------------------------------------ #
    # SDR audio
    # ------------------------------------------------------------------ #

    def _connect_sdr_audio(self) -> None:
        if self._radio_control is None:
            return
        try:
            sdr_ctrl = getattr(self._radio_control, "_sdr_control", None)
            if sdr_ctrl is None:
                return
            pipeline = getattr(sdr_ctrl, "_pipeline", None)
            if pipeline is None:
                return
            self._sdr_pipeline = pipeline
            pipeline.audio_ready.connect(self._on_sdr_audio_chunk)
            self._sdr_connected = True
            self._rx_sample_rate = SAMPLE_RATE
        except Exception:
            pass

    def _disconnect_sdr_audio(self) -> None:
        if self._sdr_pipeline is not None:
            with contextlib.suppress(Exception):
                self._sdr_pipeline.audio_ready.disconnect(self._on_sdr_audio_chunk)
            self._sdr_pipeline = None
        self._sdr_connected = False

    @Slot(object)
    def _on_sdr_audio_chunk(self, chunk: NDArray[np.float32]) -> None:
        if not self._running or not self._rb_sdr.isChecked():
            return
        self._rx_buffer.append(chunk.astype(np.float32))
        self._trim_buffer()

    # ------------------------------------------------------------------ #
    # Soundcard audio
    # ------------------------------------------------------------------ #

    def _load_sound_card_device(self) -> None:
        try:
            import json

            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = 'soundcard_settings'"
            ).fetchone()
            if row:
                sc = json.loads(row[0])
                val = sc.get("input_device_index")
                if val is not None:
                    self._in_device = int(val)
        except Exception:
            pass

    _AUDIO_OWNER = "CW Decoder"

    def _start_audio_capture(self) -> None:
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
        self._rx_buffer.clear()
        try:
            get_audio_device_manager().acquire_input(
                self._AUDIO_OWNER, self._in_device, self._rx_sample_rate, self._audio_callback
            )
            self._audio_active = True
        except Exception as exc:
            self._status_label.setText(f"Audio open error: {exc}")
            self._audio_active = False

    def _stop_audio_capture(self) -> None:
        if self._audio_active:
            get_audio_device_manager().release_input(self._AUDIO_OWNER, self._in_device)
            self._audio_active = False

    def _audio_callback(self, chunk: NDArray[np.float32]) -> None:
        self._rx_buffer.append(chunk)
        self._trim_buffer()

    # ------------------------------------------------------------------ #
    # Buffer management
    # ------------------------------------------------------------------ #

    def _trim_buffer(self) -> None:
        max_samples = _BUFFER_SECONDS * self._rx_sample_rate
        total = sum(len(c) for c in self._rx_buffer)
        while total > max_samples and self._rx_buffer:
            removed = self._rx_buffer.popleft()
            total -= len(removed)

    def _get_audio_snapshot(self) -> NDArray[np.float32] | None:
        if not self._rx_buffer:
            return None
        return np.concatenate(list(self._rx_buffer), axis=0).astype(np.float32)

    # ------------------------------------------------------------------ #
    # Periodic decode
    # ------------------------------------------------------------------ #

    @Slot()
    def _trigger_decode(self) -> None:
        if self._decoding or not self._running:
            return
        if self._decoder is None or not self._decoder.is_ready:
            return
        audio = self._get_audio_snapshot()
        if audio is None:
            return
        duration = len(audio) / self._rx_sample_rate
        if duration < MIN_AUDIO_SECONDS:
            remaining = MIN_AUDIO_SECONDS - duration
            self._status_label.setText(_("Buffering… {n:.0f} s remaining").format(n=remaining))
            return

        self._decoding = True
        self._worker = _DecodeWorker(self._decoder, audio, self._rx_sample_rate)
        self._worker.result_ready.connect(self._on_decode_result)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    @Slot(str)
    def _on_decode_result(self, text: str) -> None:
        if not text:
            self._status_label.setText(_("Listening…"))
            return
        if text != self._last_text:
            self._last_text = text
            self._append_text(text)
        self._status_label.setText(_("Listening…"))

    @Slot()
    def _on_worker_finished(self) -> None:
        self._decoding = False
        self._worker = None

    def _append_text(self, text: str) -> None:
        cleaned = re.sub(r" {2,}", " ", text).strip()
        if cleaned:
            self._text_edit.appendPlainText(cleaned)
            sb = self._text_edit.verticalScrollBar()
            if sb is not None:
                sb.setValue(sb.maximum())

    # ------------------------------------------------------------------ #
    # Level meter
    # ------------------------------------------------------------------ #

    @Slot()
    def _update_level(self) -> None:
        audio = self._get_audio_snapshot()
        if audio is None or len(audio) == 0:
            self._level_label.setText("— dB")
            return
        rms = float(np.sqrt(np.mean(audio**2)))
        db = 20.0 * np.log10(max(rms, 1e-10))
        self._level_label.setText(f"{db:.1f} dB")

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def notify_rig1_connected(self) -> None:
        """Called when Rig 1 connects (no-op for CW — RX only)."""

    def notify_rig1_disconnected(self) -> None:
        """Called when Rig 1 disconnects (no-op for CW — RX only)."""

    def refresh_sdr_pipeline(self, pipeline: Any) -> None:
        """Re-subscribe to a newly (re)created SDR pipeline, or clear on disconnect.

        MainWindow creates a brand-new SDRPipeline every time Rig 1/2
        (re)connects as the SDR — this tab's own _sdr_pipeline reference,
        grabbed once when "Start" was pressed, would otherwise go silently
        stale and never receive audio_ready again after any later SDR
        reconnect while this tab was already running (GitHub Issue #12
        follow-up: the Level meter would stay stuck at "— dB" forever with
        no indication anything had gone wrong). Called by MainWindow
        whenever the SDR pipeline changes, whether or not this tab is open
        or running, so no explicit stop/start is required from the user.
        Supersedes the never-wired notify_sdr_connected()/
        notify_sdr_disconnected() this replaced.
        """
        if self._sdr_pipeline is not None:
            with contextlib.suppress(Exception):
                self._sdr_pipeline.audio_ready.disconnect(self._on_sdr_audio_chunk)
            self._sdr_pipeline = None
        self._sdr_connected = False
        self._rb_sdr.setEnabled(pipeline is not None)
        if pipeline is None:
            if self._running and self._rb_sdr.isChecked():
                self._stop()
                self._start_btn.setChecked(False)
                self._status_label.setText(_("SDR disconnected"))
            return
        if not self._running or not self._rb_sdr.isChecked():
            return
        self._sdr_pipeline = pipeline
        pipeline.audio_ready.connect(self._on_sdr_audio_chunk)
        self._sdr_connected = True
        self._rx_sample_rate = SAMPLE_RATE
        self._rx_buffer.clear()

    # ------------------------------------------------------------------ #
    # Cleanup
    # ------------------------------------------------------------------ #

    def closeEvent(self, event: Any) -> None:
        self._stop()
        super().closeEvent(event)
