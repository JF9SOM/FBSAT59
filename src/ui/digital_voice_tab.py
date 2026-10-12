"""Digital Voice tab -- Communications > Digital Voice.

Receives CODEC2 voice messages from satellites that send them as raw FSK (HADES-SA:
800 baud 2FSK, CODEC2 700C). Input is an SDR (receive only); packets are gathered into
messages, which are listed, played, and saved as MP3. See comms.voice.hades_voice.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QStandardPaths, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from comms.voice import codec2
from comms.voice.hades_voice import (
    FskVoiceSdrDemod,
    VoiceMessage,
    VoiceMessageAssembler,
)
from comms.voice.mp3 import LAMEENC_AVAILABLE, save_mp3
from i18n import _

logger = logging.getLogger(__name__)

_IDLE_CHECK_MS = 1000


class _MessageItem(QListWidgetItem):
    """List item bound to one VoiceMessage."""

    def __init__(self, message: VoiceMessage, label: str) -> None:
        super().__init__()
        self.message = message
        self.stamp = label
        self.refresh()

    def refresh(self) -> None:
        """Update the text from the message's current state."""
        m = self.message
        text = f"{self.stamp}   {m.packet_count}/{m.length} packets   {m.duration_s:.1f} s"
        if m.missing:
            text += "   " + _("missing: {n}").format(n=len(m.missing))
        if not m.complete:
            text += "   " + _("(receiving…)")
        self.setText(text)


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
        self._sat_name = ""
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
        self._source_label = QLabel("")
        top.addWidget(self._source_label)
        top.addStretch()
        self._auto_save_cb = QCheckBox(_("Auto-save MP3"))
        self._auto_save_cb.setChecked(True)
        top.addWidget(self._auto_save_cb)
        root.addLayout(top)

        hint = QLabel(
            _(
                "CODEC2 700C voice messages sent as 800 baud 2FSK (HADES-SA, 436.875 MHz). "
                "Needs an SDR connected in Radio Control; each pass of the voice slot "
                "(about every 6-9 minutes) adds a message."
            )
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888;")
        root.addWidget(hint)

        self._list = QListWidget()
        self._list.currentItemChanged.connect(lambda *_a: self._update_buttons())
        self._list.itemDoubleClicked.connect(lambda _i: self._on_play())
        root.addWidget(self._list, stretch=1)

        buttons = QHBoxLayout()
        self._play_btn = QPushButton(_("▶ Play"))
        self._play_btn.clicked.connect(self._on_play)
        buttons.addWidget(self._play_btn)
        self._stop_btn = QPushButton(_("■ Stop"))
        self._stop_btn.clicked.connect(self._on_stop)
        buttons.addWidget(self._stop_btn)
        self._save_btn = QPushButton(_("💾 Save MP3…"))
        self._save_btn.clicked.connect(self._on_save)
        buttons.addWidget(self._save_btn)
        self._decode_iq_btn = QPushButton(_("📂 Decode IQ Recording…"))
        self._decode_iq_btn.setToolTip(
            _(
                "Open an IQ recording the way SDR Control's Open… does; voice messages are "
                "decoded as it plays."
            )
        )
        self._decode_iq_btn.clicked.connect(self._on_decode_iq)
        buttons.addWidget(self._decode_iq_btn)
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
        self._play_btn.setEnabled(have and not self._playing)
        self._stop_btn.setEnabled(self._playing)
        self._save_btn.setEnabled(have)

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
        if hasattr(rc, "transmitter_changed"):
            rc.transmitter_changed.connect(self._on_transmitter_changed)

    def _on_transmitter_changed(self, xpdr: Any) -> None:
        if xpdr and isinstance(xpdr, dict):
            self._sat_name = str(xpdr.get("description", ""))

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
        if self._demod is not None:
            return
        pipeline = self._find_sdr_pipeline()
        if pipeline is None:
            return
        try:
            demod = FskVoiceSdrDemod(int(pipeline._device.sample_rate), parent=self)
            demod.packet_received.connect(self._on_packet)
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

    def _on_packet(self, number: int, data: bytes) -> None:
        message, finished = self._assembler.add(number, data)
        if finished is not None:
            self._finish(finished)
        item = self._items.get(id(message))
        if item is None:
            stamp = datetime.now(UTC).strftime("%H:%M:%S UTC")
            item = _MessageItem(message, stamp)
            self._items[id(message)] = item
            self._list.insertItem(0, item)
            if self._list.currentItem() is None:
                self._list.setCurrentItem(item)
        item.refresh()
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
        music = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MusicLocation)
        return Path(music) / "FBSAT59-Voice"

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

    def _current_message(self) -> VoiceMessage | None:
        item = self._list.currentItem()
        return item.message if isinstance(item, _MessageItem) else None

    # ------------------------------------------------------------------ #
    # User actions
    # ------------------------------------------------------------------ #

    def _decoded_pcm(self, message: VoiceMessage) -> Any | None:
        if not codec2.is_available():
            self._status.setText(
                _("⚠ libcodec2 not found. Install gr-satellites (Help menu) or libcodec2.")
            )
            return None
        return message.to_pcm()

    def _on_play(self) -> None:
        message = self._current_message()
        if message is None:
            return
        pcm = self._decoded_pcm(message)
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
        self._playing = True
        self._update_buttons()
        QTimer.singleShot(int(message.duration_s * 1000) + 300, self._on_play_finished)

    def _on_play_finished(self) -> None:
        self._playing = False
        self._update_buttons()

    def _on_stop(self) -> None:
        with contextlib.suppress(Exception):
            import sounddevice as sd

            sd.stop()
        self._on_play_finished()

    def _on_save(self) -> None:
        message = self._current_message()
        if message is None:
            return
        if not LAMEENC_AVAILABLE:
            self._status.setText(_("lameenc not installed — pip install lameenc"))
            return
        pcm = self._decoded_pcm(message)
        if pcm is None:
            return
        from PySide6.QtWidgets import QFileDialog

        start = str(self._save_dir() / self._default_name())
        path, _f = QFileDialog.getSaveFileName(self, _("Save MP3"), start, _("MP3 (*.mp3)"))
        if not path:
            return
        try:
            save_mp3(Path(path), pcm)
            self._status.setText(_("Saved ") + path)
        except Exception as exc:
            self._status.setText(f"⚠ {exc}")

    def _on_decode_iq(self) -> None:
        sdr_ctrl = getattr(self._radio_control, "_sdr_control", None)
        if sdr_ctrl is None or not hasattr(sdr_ctrl, "open_recording"):
            QMessageBox.information(
                self, _("Decode IQ Recording"), _("SDR Control is not available.")
            )
            return
        sdr_ctrl.open_recording()

    def closeEvent(self, event: Any) -> None:
        self._idle_timer.stop()
        self._on_stop()
        self._stop_reception()
        idle = self._assembler.current
        if idle is not None:
            idle.complete = True
        super().closeEvent(event)
