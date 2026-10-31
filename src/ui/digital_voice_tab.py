"""Digital Voice tab -- Communications > Digital Voice.

Receives CODEC2 voice messages from satellites that send them as raw FSK (HADES-SA:
800 baud 2FSK, CODEC2 700C). Input is an SDR (receive only); packets are gathered into
messages, which are listed, played, saved as MP3 and (optionally) recognised as text.
See comms.voice.hades_voice.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from comms.voice import codec2, recognize, translate
from comms.voice.hades_voice import (
    PACKET_SECONDS,
    FskVoiceFileDecoder,
    FskVoiceSdrDemod,
    PcmMessage,
    VoiceMessage,
    VoiceMessageAssembler,
    unmask,
)
from comms.voice.mp3 import LAMEENC_AVAILABLE, save_mp3
from i18n import _

logger = logging.getLogger(__name__)

_MODE_FSK = "2FSK 800/200bps"
# app_settings key of the Speed choice (Normal / Fast) for IQ recordings
_DECODE_SPEED_KEY = "voice_decode_speed"
# app_settings keys of the translation target language and the auto-translate checkbox
_TRANSLATE_TARGET_KEY = "voice_translate_target"
_TRANSLATE_AUTO_KEY = "voice_translate_auto"
_IDLE_CHECK_MS = 1000
_RAW_MAX_LINES = 5000


class _MessageItem(QListWidgetItem):
    """List item bound to one VoiceMessage (and its recognised text, once there is one)."""

    def __init__(self, message: VoiceMessage, stamp: str) -> None:
        super().__init__()
        self.message = message
        self.stamp = stamp
        self.recognized: tuple[str, str] | None = None  # (language, text)
        self.translations: dict[str, str] = {}  # target language -> translated text
        self.refresh()

    def refresh(self) -> None:
        """Update the text from the message's current state."""
        m = self.message
        if isinstance(m, PcmMessage):
            self.setText(f"{self.stamp}   {m.duration_s:.1f} s   " + _("(audio file)"))
            return
        text = f"{self.stamp}   {m.packet_count}/{m.length} packets   {m.duration_s:.1f} s"
        if m.missing:
            text += "   " + _("missing: {n}").format(n=len(m.missing))
        if not m.complete:
            text += "   " + _("(receiving…)")
        self.setText(text)


class _RecognizeWorker(QThread):
    """Runs the speech recognition off the GUI thread (the first run downloads the model)."""

    done: Signal = Signal(str, str)
    failed: Signal = Signal(str)

    def __init__(self, pcm: np.ndarray, parent: Any = None) -> None:
        super().__init__(parent)
        self._pcm = pcm

    def run(self) -> None:
        try:
            language, text = recognize.transcribe(self._pcm)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.done.emit(language, text)


class _TranslateWorker(QThread):
    """Translates a recognised text off the GUI thread (a network request)."""

    done: Signal = Signal(str, str)  # target language, translated text
    failed: Signal = Signal(str)

    def __init__(self, text: str, target: str, parent: Any = None) -> None:
        super().__init__(parent)
        self._text = text
        self._target = target

    def run(self) -> None:
        try:
            _detected, text = translate.translate(self._text, self._target)
        except translate.RateLimitedError:
            self.failed.emit(
                _("Google Translate is limiting requests; try again in a while (press Translate).")
            )
            return
        except Exception as exc:
            self.failed.emit(_("Could not translate: ") + str(exc))
            return
        self.done.emit(self._target, text)


class DigitalVoiceTab(QWidget):
    """Non-resident tab opened from Communications > Digital Voice."""

    def __init__(
        self, conn: Any, radio_control: QWidget | None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._conn = conn
        self._radio_control = radio_control
        self._assembler = VoiceMessageAssembler()
        self._items: dict[int, _MessageItem] = {}  # id(message) -> item
        self._demod: FskVoiceSdrDemod | None = None
        self._demod_pipeline: Any | None = None
        self._fast_job: FskVoiceFileDecoder | None = None  # IQ-file decode worker (Speed "Fast")
        self._player: Any | None = None  # AudioFilePlayer, created on first file playback
        self._worker: _RecognizeWorker | None = None
        self._translate_worker: _TranslateWorker | None = None
        self._playing = False

        self._setup_ui()
        self._wire_radio_signals()
        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(_IDLE_CHECK_MS)
        self._idle_timer.timeout.connect(self._on_idle_tick)
        self._idle_timer.start()
        self._start_reception()

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        top = QHBoxLayout()
        top.addWidget(QLabel(_("Mode:")))
        self._mode_combo = QComboBox()
        self._mode_combo.addItems([_MODE_FSK])
        self._mode_combo.setFixedWidth(150)
        self._mode_combo.currentTextChanged.connect(self._on_mode_changed)
        top.addWidget(self._mode_combo)
        top.addSpacing(16)
        self._source_label = QLabel("")
        top.addWidget(self._source_label)
        top.addStretch()
        self._auto_save_cb = QCheckBox(_("Auto-save MP3"))
        self._auto_save_cb.setChecked(True)
        top.addWidget(self._auto_save_cb)
        root.addLayout(top)

        splitter = QSplitter()

        # Left: sub-tabs -- the raw packets, and the recognised speech
        self._view_tabs = QTabWidget()
        raw_page = QWidget()
        raw_layout = QVBoxLayout(raw_page)
        raw_layout.setContentsMargins(4, 4, 4, 4)
        raw_hint = QLabel(
            _(
                "Received voice packets, one per line, as sent (size, type/address, packet "
                "number, then the 35 data bytes as transmitted, before un-masking). "
                "HADES-SA: CODEC2 700C, packets 0.4 s apart."
            )
        )
        raw_hint.setWordWrap(True)
        raw_hint.setStyleSheet("color: #888;")
        raw_layout.addWidget(raw_hint)
        self._raw_edit = QPlainTextEdit()
        self._raw_edit.setReadOnly(True)
        self._raw_edit.setMaximumBlockCount(_RAW_MAX_LINES)
        mono = self._raw_edit.font()
        mono.setStyleHint(mono.StyleHint.Monospace)
        mono.setFamily("Menlo")
        self._raw_edit.setFont(mono)
        raw_layout.addWidget(self._raw_edit, stretch=1)
        self._raw_count_label = QLabel("")
        self._raw_count_label.setStyleSheet("color: #888;")
        raw_layout.addWidget(self._raw_count_label)
        self._view_tabs.addTab(raw_page, _("Raw Packets"))

        text_page = QWidget()
        text_layout = QVBoxLayout(text_page)
        text_layout.setContentsMargins(4, 4, 4, 4)
        text_top = QHBoxLayout()
        self._recognize_btn = QPushButton(_("🗣 Recognize"))
        self._recognize_btn.setToolTip(
            _(
                "Speech recognition of the selected message (the Whisper model, about "
                "460 MB, is downloaded on first use). CODEC2 700C is a very low bit rate "
                "codec, so the text is approximate."
            )
        )
        self._recognize_btn.clicked.connect(self._on_recognize)
        text_top.addWidget(self._recognize_btn)
        self._language_label = QLabel("")
        text_top.addWidget(self._language_label)
        text_top.addStretch()
        text_layout.addLayout(text_top)
        text_split = QSplitter(Qt.Orientation.Vertical)
        self._text_edit = QPlainTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setPlaceholderText(_("Select a message and press Recognize."))
        text_split.addWidget(self._text_edit)

        translation_page = QWidget()
        translation_layout = QVBoxLayout(translation_page)
        translation_layout.setContentsMargins(0, 0, 0, 0)
        translate_top = QHBoxLayout()
        translate_top.addWidget(QLabel(_("Translation:")))
        self._translate_combo = QComboBox()
        for code, name in translate.LANGUAGES:
            self._translate_combo.addItem(name, code)
        self._translate_combo.setToolTip(_("The language to translate the recognised text into."))
        translate_top.addWidget(self._translate_combo)
        self._translate_btn = QPushButton(_("🌐 Translate"))
        self._translate_btn.setToolTip(
            _(
                "Translate the recognised text with Google Translate. The text is sent "
                "to Google over the internet."
            )
        )
        self._translate_btn.clicked.connect(self._on_translate)
        translate_top.addWidget(self._translate_btn)
        self._auto_translate_cb = QCheckBox(_("Auto-translate"))
        self._auto_translate_cb.setToolTip(
            _("Translate each message as soon as it has been recognised.")
        )
        translate_top.addWidget(self._auto_translate_cb)
        translate_top.addStretch()
        translation_layout.addLayout(translate_top)
        self._translation_edit = QPlainTextEdit()
        self._translation_edit.setReadOnly(True)
        self._translation_edit.setPlaceholderText(_("The translation appears here."))
        translation_layout.addWidget(self._translation_edit, stretch=1)
        text_split.addWidget(translation_page)
        text_split.setStretchFactor(0, 1)
        text_split.setStretchFactor(1, 1)
        text_layout.addWidget(text_split, stretch=1)
        self._load_translate_settings()
        self._translate_combo.currentIndexChanged.connect(self._on_translate_target_changed)
        self._auto_translate_cb.toggled.connect(self._on_auto_translate_toggled)
        self._view_tabs.addTab(text_page, _("Speech-to-Text"))
        splitter.addWidget(self._view_tabs)

        # Right: received messages
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel(_("Received Messages:")))
        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_selection_changed)
        self._list.itemDoubleClicked.connect(lambda _i: self._on_play_message())
        right_layout.addWidget(self._list, stretch=1)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, stretch=1)

        buttons = QHBoxLayout()
        self._decode_iq_btn = QPushButton(_("📂 IQ Recording…"))
        self._decode_iq_btn.setToolTip(
            _(
                "Open an IQ recording like SDR Control's Open… button: its whole-recording "
                "spectrogram opens, click it to play from there; voice messages are "
                "decoded as it plays."
            )
        )
        self._decode_iq_btn.clicked.connect(self._on_decode_iq)
        buttons.addWidget(self._decode_iq_btn)
        self._combo_decode_speed = QComboBox()
        self._combo_decode_speed.addItem(_("Normal (real time)"), "normal")
        self._combo_decode_speed.addItem(_("Fast"), "fast")
        self._combo_decode_speed.setToolTip(
            _(
                "Normal: play the recording through the SDR like a live reception and\n"
                "decode it as it goes (pick the start on the spectrogram).\n"
                "Fast: decode the whole recording file at once, in seconds."
            )
        )
        self._load_decode_speed()
        self._combo_decode_speed.currentIndexChanged.connect(self._on_decode_speed_changed)
        buttons.addWidget(QLabel(_("Speed:")))
        buttons.addWidget(self._combo_decode_speed)
        self._file_recognize_btn = QPushButton(_("📂 Recognize File…"))
        self._file_recognize_btn.setToolTip(
            _(
                "Add an audio file (MP3/WAV, for example a saved voice message) to the list, "
                "to recognise and translate it."
            )
        )
        self._file_recognize_btn.clicked.connect(self._on_add_audio_file)
        buttons.addWidget(self._file_recognize_btn)
        self._play_btn = QPushButton(_("▶ Play…"))
        self._play_btn.setToolTip(_("Choose an already decoded voice file (MP3/WAV) and play it."))
        self._play_btn.clicked.connect(self._on_play_file)
        buttons.addWidget(self._play_btn)
        self._stop_btn = QPushButton(_("■ Stop"))
        self._stop_btn.clicked.connect(self._on_stop)
        buttons.addWidget(self._stop_btn)
        self._save_btn = QPushButton(_("💾 Save MP3…"))
        self._save_btn.setToolTip(_("Save the selected message as an MP3."))
        self._save_btn.clicked.connect(self._on_save)
        buttons.addWidget(self._save_btn)
        buttons.addStretch()
        root.addLayout(buttons)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        root.addWidget(self._status)
        self._update_buttons()
        self._refresh_input_source()

    def _update_buttons(self) -> None:
        item = self._list.currentItem()
        have = isinstance(item, _MessageItem) and item.message.packet_count > 0
        from_file = isinstance(item, _MessageItem) and isinstance(item.message, PcmMessage)
        self._stop_btn.setEnabled(self._playing)
        self._save_btn.setEnabled(have and not from_file)  # a file is already saved audio
        self._recognize_btn.setEnabled(have and self._worker is None)
        recognized = isinstance(item, _MessageItem) and item.recognized is not None
        self._translate_btn.setEnabled(recognized and self._translate_worker is None)

    # ------------------------------------------------------------------ #
    # Radio Control wiring / SDR input
    # ------------------------------------------------------------------ #

    def _wire_radio_signals(self) -> None:
        rc = self._radio_control
        if rc is None:
            return
        for sig, slot in (
            ("rig_connected", self._on_rig_connected),
            ("rig2_connected", self._on_rig_connected),
            ("sdr_connected", self._on_rig_connected),
            ("rig_disconnected", self._on_rig_disconnected),
            ("rig2_disconnected", self._on_rig_disconnected),
            ("sdr_disconnected", self._on_rig_disconnected),
        ):
            if hasattr(rc, sig):
                getattr(rc, sig).connect(slot)

    def _on_mode_changed(self, _mode: str) -> None:
        self._stop_reception()
        self._start_reception()

    def _on_rig_connected(self) -> None:
        self._start_reception()

    def _on_rig_disconnected(self) -> None:
        self._stop_reception()
        self._refresh_input_source()

    def _find_sdr_pipeline(self) -> Any | None:
        rc = self._radio_control
        for attr in ("_rig1", "_rig2"):
            rig = getattr(rc, attr, None)
            if rig is not None and getattr(rig, "is_sdr", False):
                return getattr(rig, "_pipeline", None)
        return None

    def _refresh_input_source(self) -> None:
        if self._find_sdr_pipeline() is not None:
            self._source_label.setText(_("Input: ") + _("SDR (receive only)"))
            self._source_label.setStyleSheet("color: #00bcd4;")
        else:
            self._source_label.setText(_("Input: ") + _("No SDR — connect an SDR in Radio Control"))
            self._source_label.setStyleSheet("color: #f44336;")

    def _start_reception(self) -> None:
        self._refresh_input_source()
        pipeline = self._find_sdr_pipeline()
        if self._demod is not None:
            if pipeline is self._demod_pipeline:
                return
            self._stop_reception()  # MainWindow built a new pipeline (reconnect / playback)
        if pipeline is None:
            return
        try:
            demod = FskVoiceSdrDemod(int(pipeline._device.sample_rate), parent=self)
            demod.packet_received.connect(self._on_demod_packet)
            demod.start()
            self._demod = demod  # held before subscribing so a failure is cleaned up
            pipeline.subscribe(demod.push_samples)
            self._demod_pipeline = pipeline
        except Exception:
            logger.exception("Digital Voice: could not attach to the SDR pipeline")
            self._stop_reception()
            self._status.setText(_("⚠ Could not start the 2FSK demodulator."))
            return
        self._status.setText(_("Listening for voice packets…"))

    def _stop_reception(self) -> None:
        if self._demod_pipeline is not None and self._demod is not None:
            with contextlib.suppress(Exception):
                self._demod_pipeline.unsubscribe(self._demod.push_samples)
        self._demod_pipeline = None
        if self._demod is not None:
            with contextlib.suppress(Exception):
                self._demod.packet_received.disconnect()
            self._demod.stop()
            self._demod = None

    # ------------------------------------------------------------------ #
    # Messages
    # ------------------------------------------------------------------ #

    def add_external_packet(self, number: int, data: bytes, t: float) -> None:
        """A CRC-checked voice packet from another tab (UNNE-1B's type 15 via the Telemetry tab).

        *data* is the 35 CODEC2 bytes with the mask already removed, *t* the stream time in
        seconds. Its number is trusted (no timing repair), see VoiceMessage.trusted.
        """
        self._on_packet(number, data, t, trusted=True)

    def _on_demod_packet(self, number: int, data: bytes, t: float, interval_s: float) -> None:
        """A packet from the 2FSK demodulator (800 bps or 200 bps downlink)."""
        self._on_packet(number, data, t, interval_s=interval_s)

    def _on_packet(
        self,
        number: int,
        data: bytes,
        t: float | None = None,
        trusted: bool = False,
        interval_s: float = PACKET_SECONDS,
    ) -> None:
        raw = bytes([37, 0xFC if trusted else 0xB3, number]) + unmask(data)
        self._raw_edit.appendPlainText(
            f"{datetime.now(UTC):%H:%M:%S}  #{number:02d}  {raw.hex(' ').upper()}"
        )
        message, finished = self._assembler.add(number, data, t, trusted, interval_s)
        if finished is not None:
            self._finish(finished)
        item = self._items.get(id(message))
        if item is None:
            item = _MessageItem(message, datetime.now(UTC).strftime("%H:%M:%S UTC"))
            self._items[id(message)] = item
            self._list.insertItem(0, item)
            if self._list.currentItem() is None:
                self._list.setCurrentItem(item)
        item.refresh()
        total = sum(i.message.packet_count for i in self._items.values())
        self._raw_count_label.setText(_("Packets: ") + str(total))
        self._update_buttons()

    def _on_idle_tick(self) -> None:
        idle = self._assembler.close_if_idle()
        if idle is not None:
            self._finish(idle)

    def _finish(self, message: VoiceMessage) -> None:
        item = self._items.get(id(message))
        if item is not None:
            item.refresh()
        self._status.setText(
            _("Message received: {n} packets, {s:.1f} s").format(
                n=message.packet_count, s=message.duration_s
            )
        )
        if self._auto_save_cb.isChecked() and message.packet_count >= 3:
            self._auto_save(message)

    @staticmethod
    def _save_dir() -> Path:
        from core.data_paths import KIND_VOICE, get_dir

        return get_dir(KIND_VOICE)

    @staticmethod
    def _default_name() -> str:
        return f"Voice_{datetime.now(UTC):%Y%m%d_%H%M%S}.mp3"

    def _auto_save(self, message: VoiceMessage) -> None:
        if not codec2.is_available() or not LAMEENC_AVAILABLE:
            return
        try:
            path = self._save_dir() / self._default_name()
            save_mp3(path, message.to_pcm())
            self._status.setText(_("Saved ") + str(path))
        except Exception:
            logger.exception("Digital Voice: auto-save failed")

    def _current_item(self) -> _MessageItem | None:
        item = self._list.currentItem()
        return item if isinstance(item, _MessageItem) else None

    def _on_selection_changed(self, *_args: Any) -> None:
        item = self._current_item()
        if item is not None and item.recognized is not None:
            self._language_label.setText(_("Language: ") + item.recognized[0])
            self._text_edit.setPlainText(item.recognized[1])
        else:
            self._language_label.setText("")
            self._text_edit.clear()
        self._show_translation()
        self._update_buttons()

    # ------------------------------------------------------------------ #
    # Speech recognition
    # ------------------------------------------------------------------ #

    def _decoded_pcm(self, message: VoiceMessage) -> Any | None:
        if isinstance(message, PcmMessage):
            return message.to_pcm()  # an audio file: no CODEC2 needed
        if not codec2.is_available():
            self._status.setText(
                _("⚠ libcodec2 not found. Install gr-satellites (Help menu) or libcodec2.")
            )
            return None
        return message.to_pcm()

    def _on_recognize(self) -> None:
        item = self._current_item()
        if item is None or self._worker is not None:
            return
        if not recognize.is_available():
            self._status.setText(
                _("⚠ Speech recognition is unavailable: faster-whisper is not installed.")
            )
            return
        pcm = self._decoded_pcm(item.message)
        if pcm is None:
            return
        self._view_tabs.setCurrentIndex(1)
        self._status.setText(
            _("Recognizing… (the first run downloads the Whisper model, about 460 MB)")
        )
        self._worker = _RecognizeWorker(pcm, self)
        self._worker.done.connect(lambda lang, text, it=item: self._on_recognized(it, lang, text))
        self._worker.failed.connect(self._on_recognize_failed)
        self._worker.finished.connect(self._on_worker_finished)
        self._update_buttons()
        self._worker.start()

    def _on_recognized(self, item: _MessageItem, language: str, text: str) -> None:
        item.recognized = (language, text)
        self._status.setText(_("Recognition finished."))
        if self._current_item() is item:
            self._on_selection_changed()
        if self._auto_translate_cb.isChecked():
            self._start_translation(item)

    def _on_recognize_failed(self, message: str) -> None:
        self._status.setText(f"⚠ {message}")

    def _on_worker_finished(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        self._update_buttons()

    # ------------------------------------------------------------------ #
    # Translation
    # ------------------------------------------------------------------ #

    def _load_translate_settings(self) -> None:
        """Restore the target language (default: the UI language) and Auto-translate."""
        from i18n import get_language

        index = self._translate_combo.findData(get_language())
        auto = False
        if hasattr(self._conn, "execute"):
            with contextlib.suppress(Exception):
                row = self._conn.execute(
                    "SELECT value FROM app_settings WHERE key = ?", (_TRANSLATE_TARGET_KEY,)
                ).fetchone()
                if row and self._translate_combo.findData(str(row["value"])) >= 0:
                    index = self._translate_combo.findData(str(row["value"]))
            with contextlib.suppress(Exception):
                row = self._conn.execute(
                    "SELECT value FROM app_settings WHERE key = ?", (_TRANSLATE_AUTO_KEY,)
                ).fetchone()
                auto = bool(row) and str(row["value"]) == "1"
        self._translate_combo.setCurrentIndex(max(index, 0))
        self._auto_translate_cb.setChecked(auto)

    def _save_setting(self, key: str, value: str) -> None:
        if not hasattr(self._conn, "execute"):
            return
        with contextlib.suppress(Exception):
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                (key, value),
            )
            self._conn.commit()

    def _target_language(self) -> str:
        return str(self._translate_combo.currentData())

    def _show_translation(self) -> None:
        """Show the selected message's translation into the chosen language, if it has one."""
        item = self._current_item()
        text = item.translations.get(self._target_language()) if item is not None else None
        self._translation_edit.setPlainText(text or "")

    def _on_translate_target_changed(self, _index: int) -> None:
        self._save_setting(_TRANSLATE_TARGET_KEY, self._target_language())
        self._show_translation()
        item = self._current_item()
        if (
            self._auto_translate_cb.isChecked()
            and item is not None
            and item.recognized is not None
            and self._target_language() not in item.translations
        ):
            self._start_translation(item)

    def _on_auto_translate_toggled(self, checked: bool) -> None:
        self._save_setting(_TRANSLATE_AUTO_KEY, "1" if checked else "0")
        item = self._current_item()
        if (
            checked
            and item is not None
            and item.recognized is not None
            and self._target_language() not in item.translations
        ):
            self._start_translation(item)

    def _on_translate(self) -> None:
        """The Translate button: translate again, even if a translation is already there."""
        item = self._current_item()
        if item is not None and item.recognized is not None:
            item.translations.pop(self._target_language(), None)
            self._start_translation(item)

    def _start_translation(self, item: _MessageItem) -> None:
        """Translate the recognised text of *item* into the chosen language."""
        if self._translate_worker is not None or item.recognized is None:
            return
        language, text = item.recognized
        target = self._target_language()
        if not text.strip():
            return
        if language == target.split("-")[0]:
            item.translations[target] = text  # already in that language
            self._show_translation()
            return
        self._status.setText(_("Translating…"))
        worker = _TranslateWorker(text, target, self)
        worker.done.connect(lambda tgt, out, it=item: self._on_translated(it, tgt, out))
        worker.failed.connect(self._on_translate_failed)
        worker.finished.connect(self._on_translate_worker_finished)
        self._translate_worker = worker
        self._update_buttons()
        worker.start()

    def _on_translated(self, item: _MessageItem, target: str, text: str) -> None:
        item.translations[target] = text
        self._status.setText(_("Translation finished."))
        if self._current_item() is item:
            self._show_translation()

    def _on_translate_failed(self, message: str) -> None:
        self._status.setText("⚠ " + message)

    def _on_translate_worker_finished(self) -> None:
        if self._translate_worker is not None:
            self._translate_worker.deleteLater()
            self._translate_worker = None
        self._update_buttons()

    # ------------------------------------------------------------------ #
    # Playback / saving
    # ------------------------------------------------------------------ #

    def _on_play_message(self) -> None:
        """Play the selected received message (double-click in the list)."""
        item = self._current_item()
        if item is None:
            return
        pcm = self._decoded_pcm(item.message)
        if pcm is None:
            return
        try:
            import sounddevice as sd
        except ImportError:
            self._status.setText(_("sounddevice not installed — pip install sounddevice"))
            return
        try:
            sd.play(pcm, codec2.SAMPLE_RATE)  # the system default output (never the rig's)
        except Exception as exc:
            self._status.setText(f"⚠ {exc}")
            return
        self._set_playing(True, item.message.duration_s)

    def _on_play_file(self) -> None:
        """Choose an already decoded voice file (MP3/WAV) and play it."""
        folder = self._save_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path, _filter = QFileDialog.getOpenFileName(
            self, _("Play Voice File"), str(folder), _("Audio Files (*.mp3 *.wav)")
        )
        if not path:
            return
        try:
            if self._player is None:
                from comms.audio_player import AudioFilePlayer

                self._player = AudioFilePlayer()
            self._player.load(path)
            self._player.play()
        except Exception as exc:
            self._status.setText(f"⚠ {exc}")
            return
        self._status.setText(_("Playing ") + Path(path).name)
        self._set_playing(True, float(self._player.duration_s))

    def _set_playing(self, playing: bool, duration_s: float = 0.0) -> None:
        self._playing = playing
        self._update_buttons()
        if playing:
            QTimer.singleShot(int(duration_s * 1000) + 300, self._on_play_finished)

    def _on_play_finished(self) -> None:
        self._playing = False
        self._update_buttons()

    def _on_stop(self) -> None:
        with contextlib.suppress(Exception):
            import sounddevice as sd

            sd.stop()
        if self._player is not None:
            with contextlib.suppress(Exception):
                self._player.pause()
        self._on_play_finished()

    def _on_save(self) -> None:
        item = self._current_item()
        if item is None:
            return
        if not LAMEENC_AVAILABLE:
            self._status.setText(_("lameenc not installed — pip install lameenc"))
            return
        pcm = self._decoded_pcm(item.message)
        if pcm is None:
            return
        start = str(self._save_dir() / self._default_name())
        path, _filter = QFileDialog.getSaveFileName(self, _("Save MP3"), start, _("MP3 (*.mp3)"))
        if not path:
            return
        try:
            save_mp3(Path(path), pcm)
            self._status.setText(_("Saved ") + path)
        except Exception as exc:
            self._status.setText(f"⚠ {exc}")

    def _load_decode_speed(self) -> None:
        """Restore the Speed choice (Normal / Fast) from app_settings."""
        if not hasattr(self._conn, "execute"):
            return
        with contextlib.suppress(Exception):
            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (_DECODE_SPEED_KEY,)
            ).fetchone()
            if row:
                idx = self._combo_decode_speed.findData(str(row["value"]))
                if idx >= 0:
                    self._combo_decode_speed.setCurrentIndex(idx)

    def _on_decode_speed_changed(self, _index: int) -> None:
        if not hasattr(self._conn, "execute"):
            return
        with contextlib.suppress(Exception):
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                (_DECODE_SPEED_KEY, str(self._combo_decode_speed.currentData())),
            )
            self._conn.commit()

    def _on_fast_decode(self) -> None:
        """Ask for an IQ recording and decode all of it at once."""
        if self._fast_job is not None:
            return
        start_dir = Path.home() / "iq_recordings"
        path, _filter = QFileDialog.getOpenFileName(
            self,
            _("Decode IQ Recording"),
            str(start_dir if start_dir.is_dir() else Path.home()),
            "IQ WAV (*.wav);;All files (*)",
        )
        if path:
            self._start_fast_decode(Path(path))

    def _start_fast_decode(self, path: Path) -> None:
        """Decode *path* in a worker thread; its packets go through _on_packet() as live ones."""
        finished = self._assembler.close_current()  # never mix with a live message
        if finished is not None:
            self._finish(finished)
        job = FskVoiceFileDecoder(path, parent=self)
        job.packet_found.connect(self._on_fast_packet)
        job.progress.connect(self._on_fast_progress)
        job.decoded.connect(self._on_fast_done)
        job.failed.connect(self._on_fast_failed)
        self._fast_job = job
        self._decode_iq_btn.setEnabled(False)
        self._combo_decode_speed.setEnabled(False)
        self._status.setText(_("Fast decode: starting…"))
        job.start()

    def _on_fast_packet(self, number: int, data: bytes, t: float, interval_s: float) -> None:
        if self._fast_job is not None:
            self._on_demod_packet(number, data, t, interval_s)

    def _on_fast_progress(self, fraction: float, packets: int) -> None:
        if self._fast_job is not None:
            self._status.setText(
                _("Fast decode: {pct}% — {n} packet(s)").format(pct=int(fraction * 100), n=packets)
            )

    def _on_fast_done(self, duration_s: float, elapsed_s: float, packets: int) -> None:
        self._finish_fast_decode()
        finished = self._assembler.close_current()  # the end of the recording ends the message
        if finished is not None:
            self._finish(finished)
        self._status.setText(
            _("Decoded {dur} s of recording in {el} s — {n} packet(s)").format(
                dur=round(duration_s), el=round(elapsed_s, 1), n=packets
            )
        )

    def _on_fast_failed(self, message: str) -> None:
        self._finish_fast_decode()
        self._status.setText("⚠ " + _("Could not decode the recording: ") + message)

    def _finish_fast_decode(self) -> None:
        """The worker is done (or gone): release it and restore the buttons."""
        job = self._fast_job
        self._fast_job = None
        if job is not None:
            for signal in (job.packet_found, job.progress, job.decoded, job.failed):
                with contextlib.suppress(Exception):
                    signal.disconnect()
            job.wait(3000)
        self._decode_iq_btn.setEnabled(True)
        self._combo_decode_speed.setEnabled(True)

    def _stop_fast_decode(self) -> None:
        """Cancel a running fast decode (close)."""
        if self._fast_job is not None:
            self._fast_job.cancel()
            self._finish_fast_decode()

    def _on_add_audio_file(self) -> None:
        """Choose an audio file and add it to the message list (as a recognisable message)."""
        folder = self._save_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path, _filter = QFileDialog.getOpenFileName(
            self, _("Recognize File"), str(folder), _("Audio Files (*.mp3 *.wav)")
        )
        if path:
            self.add_audio_file(Path(path))

    def add_audio_file(self, path: Path) -> bool:
        """Add *path* to the list as an audio-file message; False (reason shown) if unreadable."""
        from comms.voice.audio_file import load_audio_file

        try:
            pcm = load_audio_file(path)
        except Exception as exc:
            self._status.setText("⚠ " + _("Could not read the audio file: ") + str(exc))
            return False
        item = _MessageItem(PcmMessage(pcm), path.name)
        self._items[id(item.message)] = item
        self._list.insertItem(0, item)
        self._list.setCurrentItem(item)
        self._status.setText(_("Added ") + path.name)
        self._update_buttons()
        return True

    def _on_decode_iq(self) -> None:
        """Fast: decode the whole recording at once. Normal: same as SDR Control's Open…
        (file picker, spectrogram of the whole recording, click to play from there; the
        recording is played into the demodulator)."""
        if self._combo_decode_speed.currentData() == "fast":
            self._on_fast_decode()
            return
        sdr_ctrl = getattr(self._radio_control, "_sdr_control", None)
        if sdr_ctrl is None or not hasattr(sdr_ctrl, "open_recording"):
            QMessageBox.information(self, _("IQ Recording"), _("SDR Control is not available."))
            return
        sdr_ctrl.open_recording()

    def closeEvent(self, event: Any) -> None:
        self._idle_timer.stop()
        self._stop_fast_decode()
        self._on_stop()
        if self._player is not None:
            with contextlib.suppress(Exception):
                self._player.close()
        if self._worker is not None:
            self._worker.wait(2000)
        if self._translate_worker is not None:
            self._translate_worker.wait(2000)
        self._stop_reception()
        super().closeEvent(event)
