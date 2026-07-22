"""
SDR Control tab widget.

SdrControlWidget — Active only when an SDR device is connected (Rig 1 or Rig 2).
Contains three panels:
  - Spectrum display  (real-time FFT via QtCharts QLineSeries)
  - Demodulator       (mode / filter / volume / AGC / start-stop audio)
  - IQ Recorder       (bandwidth / record / stop / elapsed time)

The widget is notified of transponder changes from RadioControlWidget so it
can auto-select the correct demodulation mode.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtCore import QPointF, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from i18n import _
from sdr import LAMEENC_AVAILABLE, SOAPY_AVAILABLE, AudioRecorder

if SOAPY_AVAILABLE:
    from sdr.demodulator import DemodMode

logger = logging.getLogger(__name__)

# Spectrum chart Y-axis range (dBFS)
_SPECTRUM_YMIN: float = -90.0
_SPECTRUM_YMAX: float = 0.0

# Passband tune step options (label, Hz)
_TUNE_STEPS: list[tuple[str, int]] = [
    ("100 Hz", 100),
    ("500 Hz", 500),
    ("1 kHz", 1_000),
    ("5 kHz", 5_000),
    ("10 kHz", 10_000),
]

# IQ recording bandwidths offered in the dropdown
_REC_BANDWIDTHS: list[tuple[str, int]] = [
    ("50 kHz", 50_000),
    ("100 kHz", 100_000),
    ("250 kHz", 250_000),
    ("500 kHz", 500_000),
    ("1 MHz", 1_000_000),
]


class SdrControlWidget(QWidget):
    """
    SDR Control panel.

    Call set_pipeline(pipeline) when an SDR connects.
    Call set_pipeline(None) when it disconnects.
    """

    # Emitted when the user tunes within the transponder passband.
    # Value is the cumulative offset in Hz from the Doppler-corrected centre.
    tune_offset_changed: Signal = Signal(float)

    # Emitted when the SDR Lock button is toggled. Independent of Radio
    # Control's own Lock ("L") button/_trsp_lock, which is for CAT-rig dial
    # feedback and Rig1<->Rig2 TX mirroring — this one only ever affects
    # whichever rig slot is the SDR.
    sdr_lock_changed: Signal = Signal(bool)

    # Emitted when the Freq box is manually edited (editingFinished) while a
    # transponder is selected. Value is the requested absolute frequency in
    # Hz. MainWindow (not this widget) knows the Doppler-corrected centre
    # needed to fold this into _sdr_tune_offset — see
    # MainWindow._on_sdr_manual_freq_requested(). With no transponder
    # selected, _on_freq_edited() instead retunes the device directly and
    # this signal is not emitted at all.
    manual_freq_requested: Signal = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pipeline: Any = None  # SDRPipeline | None
        self._recording = False
        self._tune_offset_hz: float = 0.0  # cumulative passband tune offset
        # Whether Radio Control currently has a transponder selected — see
        # set_transponder_active().
        self._transponder_active: bool = False
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1_000)
        self._status_timer.timeout.connect(self._update_rec_status)
        self._setup_ui()
        self._set_sdr_connected(False)  # grey out until SDR connects

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _set_sdr_connected(self, connected: bool) -> None:
        """Enable/disable controls except the file-manager buttons.

        Panels are kept enabled so Qt parent-disables don't block child widgets.
        Instead, each child widget is enabled/disabled individually, skipping
        the two file-manager buttons which must remain accessible at all times.
        """
        _always_enabled = {self._open_folder_btn, self._open_audio_folder_btn}
        for panel in (
            self._spectrum_panel,
            self._tune_panel,
            self._demod_panel,
            self._recorder_panel,
        ):
            for child in panel.findChildren(QWidget):
                if child not in _always_enabled:
                    child.setEnabled(connected)
            # Keep the group-box title/frame itself enabled so it renders normally
            # (disabling the QGroupBox would grey out the title text too, which is fine,
            # but more importantly it would re-disable our exempt children above).

    def set_pipeline(self, pipeline: Any) -> None:  # SDRPipeline | None
        """Attach or detach the active SDRPipeline."""
        # Detach old pipeline
        if self._pipeline is not None:
            try:
                self._pipeline.spectrum_ready.disconnect(self._on_spectrum)
                self._pipeline.center_freq_changed.disconnect(self._on_center_freq)
                self._pipeline.status_changed.disconnect(self._on_status)
            except Exception:
                pass

        self._pipeline = pipeline
        self._set_sdr_connected(pipeline is not None)

        if pipeline is not None:
            pipeline.spectrum_ready.connect(self._on_spectrum)
            pipeline.center_freq_changed.connect(self._on_center_freq)
            pipeline.status_changed.connect(self._on_status)
            self._status_label.setText(_("SDR Connected"))
            # Apply the currently selected demod mode to the new pipeline.
            # Without this, switching satellites before Connect leaves the
            # pipeline in its default mode (USB) regardless of what the
            # combo box shows.
            self._on_mode_changed(self._mode_combo.currentIndex())
            # Reflect whatever the device is already tuned to (e.g. the
            # Doppler-corrected transponder frequency, or the device's
            # power-on default) so the manual tune field doesn't silently
            # retune the SDR the first time the user presses Tune.
            device = getattr(pipeline, "_device", None)
            if device is not None:
                self._manual_freq_spin.blockSignals(True)
                self._manual_freq_spin.setValue(device.center_freq / 1e6)
                self._manual_freq_spin.blockSignals(False)
        else:
            self._status_label.setText(_("SDR Disconnected"))
            self._freq_overlay.setText("—")
            self._stop_audio()
            self._stop_recording()
            self._stop_audio_recording()

    def set_transponder_mode(self, satnogs_mode: str) -> None:
        """Auto-select demodulator mode from a SATNOGS transponder mode string."""
        if not SOAPY_AVAILABLE:
            return
        mode = DemodMode.from_satnogs(satnogs_mode)
        mode_map = {
            DemodMode.NFM: 0,
            DemodMode.USB: 1,
            DemodMode.LSB: 2,
            DemodMode.CW: 3,
        }
        idx = mode_map.get(mode, 1)
        self._mode_combo.setCurrentIndex(idx)

    def reset_tune_offset(self) -> None:
        """Reset the passband tune offset to zero.

        Called on transponder change, and by the "T" button — which now
        means "reset to the transponder's Doppler-corrected centre
        frequency", matching Radio Control's own "T" — to return the SDR
        there after using the arrows/Freq box to explore its passband.
        The "T" button is disabled whenever no transponder is selected
        (see set_transponder_active()), so this only ever fires with one
        selected.
        """
        self._tune_offset_hz = 0.0
        self.tune_offset_changed.emit(0.0)

    def set_transponder_active(self, active: bool) -> None:
        """Track whether Radio Control currently has a transponder selected.

        Gates the "T" button — there is no Doppler-corrected centre to
        reset to without one — and switches the arrows/Freq box between
        offset-based tuning (persists through MainWindow's
        dl_rig1 = dl_corr + _sdr_tune_offset write, regardless of SDR
        Lock state) and tuning the device directly. The latter is needed
        because MainWindow._doppler_cycle() returns immediately without a
        transponder selected, so nothing would ever consume an
        accumulated offset in that state.
        """
        self._transponder_active = active
        self._tune_btn.setEnabled(active)

    def start_audio_recording_for_autotrack(self, norad: int, sat_name: str) -> None:
        """Start MP3 audio recording (called by Autotrack on AOS)."""
        if self._pipeline is not None and not self._audio_recorder.is_active:
            self._start_audio_recording(norad_override=norad, name_override=sat_name)

    def stop_audio_recording_for_autotrack(self) -> None:
        """Stop MP3 audio recording (called by Autotrack on LOS)."""
        if self._audio_recorder.is_active:
            self._stop_audio_recording()

    def start_iq_recording_for_autotrack(self) -> None:
        """Start IQ recording (called by Autotrack on AOS)."""
        if self._pipeline is not None and not self._recording:
            self._start_recording()

    def stop_iq_recording_for_autotrack(self) -> None:
        """Stop IQ recording (called by Autotrack on LOS)."""
        if self._recording:
            self._stop_recording()

    def set_iq_save_dir(self, path: str) -> None:
        """Update the IQ recording save directory (from SDR settings)."""
        self._iq_save_dir = Path(path) if path else Path.home() / "iq_recordings"

    def set_audio_save_dir(self, path: str) -> None:
        """Update the audio (MP3) recording save directory."""
        self._audio_save_dir = Path(path) if path else Path.home() / "audio_recordings"
        self._audio_recorder = AudioRecorder(self._audio_save_dir)

    def set_satellite_info(self, norad: int, name: str) -> None:
        """Store satellite info used to name IQ recordings."""
        self._sat_norad = norad
        self._sat_name = name

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setSpacing(6)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setSpacing(8)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        # Status bar
        self._status_label = QLabel(_("SDR Disconnected"))
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setStyleSheet("color: gray; font-weight: bold;")
        layout.addWidget(self._status_label)

        # Spectrum
        self._spectrum_panel = self._build_spectrum_panel()
        layout.addWidget(self._spectrum_panel)
        # Passband tuning (also hosts the manual absolute-frequency field)
        self._tune_panel = self._build_tune_panel()
        layout.addWidget(self._tune_panel)
        # Demodulator
        self._demod_panel = self._build_demod_panel()
        layout.addWidget(self._demod_panel)
        # IQ recorder
        self._recorder_panel = self._build_recorder_panel()
        layout.addWidget(self._recorder_panel)
        layout.addStretch()

        # Private state
        self._iq_save_dir = Path.home() / "iq_recordings"
        self._audio_save_dir = Path.home() / "audio_recordings"
        self._sat_norad = 0
        self._sat_name = "unknown"
        self._audio_recorder = AudioRecorder(self._audio_save_dir)
        self._audio_rec_timer = QTimer(self)
        self._audio_rec_timer.setInterval(1_000)
        self._audio_rec_timer.timeout.connect(self._update_audio_rec_status)

    def _build_spectrum_panel(self) -> QGroupBox:
        grp = QGroupBox(_("Spectrum"))
        v = QVBoxLayout(grp)

        # QtCharts spectrum display
        self._spectrum_series = QLineSeries()
        pen = QPen(QColor("#00dcff"))
        pen.setWidth(1)
        self._spectrum_series.setPen(pen)

        # Centre-frequency marker — a vertical dashed line at the SDR's
        # actual tuned frequency, drawn on top of the trace. The FFT data
        # (SDRPipeline._compute_fft()) is already centred on center_freq,
        # so this mostly reinforces that visually, but it also gives an
        # explicit "this is where you're tuned" reference point regardless
        # of where a signal peak happens to fall — there was previously no
        # visual indicator of the tuned frequency on the chart at all
        # (GitHub Issue #12 follow-up: "Would be nice if this was centered
        # or a mark that the tune is at").
        self._center_marker_series = QLineSeries()
        marker_pen = QPen(QColor("#ff3b30"))
        marker_pen.setWidth(1)
        marker_pen.setStyle(Qt.PenStyle.DashLine)
        self._center_marker_series.setPen(marker_pen)

        self._spectrum_chart = QChart()
        self._spectrum_chart.addSeries(self._spectrum_series)
        self._spectrum_chart.addSeries(self._center_marker_series)
        self._spectrum_chart.setBackgroundBrush(QColor("#1a1a2e"))
        self._spectrum_chart.legend().hide()
        self._spectrum_chart.setMargins(
            __import__("PySide6.QtCore", fromlist=["QMargins"]).QMargins(4, 4, 4, 4)
        )

        # Axes
        self._freq_axis = QValueAxis()
        self._freq_axis.setTitleText(_("Frequency (MHz)"))
        self._freq_axis.setLabelFormat("%.3f")
        self._freq_axis.setLabelsColor(QColor("#cccccc"))
        self._freq_axis.setGridLineColor(QColor("#333355"))

        self._pwr_axis = QValueAxis()
        self._pwr_axis.setTitleText(_("Power (dBFS)"))
        self._pwr_axis.setRange(_SPECTRUM_YMIN, _SPECTRUM_YMAX)
        self._pwr_axis.setLabelsColor(QColor("#cccccc"))
        self._pwr_axis.setGridLineColor(QColor("#333355"))

        self._spectrum_chart.addAxis(self._freq_axis, Qt.AlignmentFlag.AlignBottom)
        self._spectrum_chart.addAxis(self._pwr_axis, Qt.AlignmentFlag.AlignLeft)
        self._spectrum_series.attachAxis(self._freq_axis)
        self._spectrum_series.attachAxis(self._pwr_axis)
        self._center_marker_series.attachAxis(self._freq_axis)
        self._center_marker_series.attachAxis(self._pwr_axis)

        chart_view = QChartView(self._spectrum_chart)
        chart_view.setMinimumHeight(160)
        chart_view.setRenderHint(
            __import__("PySide6.QtGui", fromlist=["QPainter"]).QPainter.RenderHint.Antialiasing,
            False,
        )

        # Centre-frequency display row — shown above the chart with dark
        # styling to blend visually with the spectrum background.
        freq_row = QHBoxLayout()
        freq_row.addStretch()
        freq_lbl = QLabel(_("RX:"))
        freq_lbl.setStyleSheet("color: #aaaaaa; font-size: 12px;")
        self._freq_overlay = QLabel("—")
        self._freq_overlay.setStyleSheet(
            "color: black;font-size: 13px;font-weight: bold;font-family: monospace;"
        )
        freq_row.addWidget(freq_lbl)
        freq_row.addWidget(self._freq_overlay)
        freq_row.addStretch()
        v.addLayout(freq_row)

        v.addWidget(chart_view)
        return grp

    def _on_freq_edited(self) -> None:
        """Apply a manually typed absolute frequency (Freq box committed).

        With a transponder selected, this is routed to MainWindow (via
        manual_freq_requested) so it can be folded into the same
        persistent _sdr_tune_offset the arrow buttons use — surviving
        regardless of SDR Lock state — instead of being written straight
        to the hardware, where the very next Doppler cycle would silently
        revert it (the root confusion behind GitHub Issue #12: the old
        "Freq:" field bypassed the offset mechanism entirely). With no
        transponder selected there is no Doppler cycle running to revert
        anything (_doppler_cycle() returns immediately without one), so
        the SDR is retuned directly, preserving the original free-entry
        use case this field was added for (2026-07-11) — e.g. listening
        to an unrelated terrestrial reference signal.
        """
        if self._transponder_active:
            self.manual_freq_requested.emit(self._manual_freq_spin.value() * 1e6)
            return
        if self._pipeline is None:
            return
        device = getattr(self._pipeline, "_device", None)
        if device is None:
            return
        device.set_center_freq(self._manual_freq_spin.value() * 1e6)

    def _build_tune_panel(self) -> QGroupBox:
        """Build the Passband Tune group box.

        The Freq: field always shows the SDR's actual live frequency
        (kept in sync via _on_center_freq() below whenever it isn't
        focused). When a transponder is selected, the arrows and typing a
        new value both move a persistent offset from its Doppler-
        corrected centre (see _apply_tune()/_on_freq_edited() and
        MainWindow's dl_rig1 = dl_corr + _sdr_tune_offset write, which
        applies regardless of SDR Lock state). With no transponder
        selected there is no Doppler cycle to carry that offset at all,
        so both controls instead retune the SDR directly — preserving the
        field's original free-entry purpose.
        """
        grp = QGroupBox(_("Passband Tune"))
        row = QHBoxLayout(grp)
        row.setSpacing(4)

        # Step size selector
        row.addWidget(QLabel(_("Step:")))
        self._tune_step_combo = QComboBox()
        for label, _hz in _TUNE_STEPS:
            self._tune_step_combo.addItem(label)
        self._tune_step_combo.setCurrentIndex(2)  # 1 kHz default
        self._tune_step_combo.setFixedWidth(80)
        row.addWidget(self._tune_step_combo)

        row.addSpacing(8)

        # Large-step down (×10)
        btn_dd = QPushButton("◀◀")
        btn_dd.setFixedWidth(36)
        btn_dd.setToolTip(_("Tune down ×10 step"))
        btn_dd.clicked.connect(lambda: self._apply_tune(-10))
        row.addWidget(btn_dd)

        # Single-step down
        btn_d = QPushButton("◀")
        btn_d.setFixedWidth(30)
        btn_d.setToolTip(_("Tune down"))
        btn_d.clicked.connect(lambda: self._apply_tune(-1))
        row.addWidget(btn_d)

        freq_hint = _(
            "Freq: while a transponder is selected, tracks its Doppler-"
            "corrected centre frequency — use the arrows or type a value "
            "to move within its passband (kept regardless of SDR Lock "
            "state). With no transponder selected, tune the SDR to any "
            "frequency directly."
        )
        row.addWidget(QLabel(_("Freq:")))
        self._manual_freq_spin = QDoubleSpinBox()
        self._manual_freq_spin.setDecimals(6)
        self._manual_freq_spin.setRange(0.1, 6000.0)
        self._manual_freq_spin.setSingleStep(0.001)
        self._manual_freq_spin.setSuffix(" MHz")
        self._manual_freq_spin.setValue(435.0)
        self._manual_freq_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._manual_freq_spin.setToolTip(freq_hint)
        self._manual_freq_spin.editingFinished.connect(self._on_freq_edited)
        row.addWidget(self._manual_freq_spin)

        # Single-step up
        btn_u = QPushButton("▶")
        btn_u.setFixedWidth(30)
        btn_u.setToolTip(_("Tune up"))
        btn_u.clicked.connect(lambda: self._apply_tune(+1))
        row.addWidget(btn_u)

        # Large-step up (×10)
        btn_uu = QPushButton("▶▶")
        btn_uu.setFixedWidth(36)
        btn_uu.setToolTip(_("Tune up ×10 step"))
        btn_uu.clicked.connect(lambda: self._apply_tune(+10))
        row.addWidget(btn_uu)

        row.addSpacing(8)

        # "T" — reset to the transponder's Doppler-corrected centre
        # frequency, matching Radio Control's own "T". Disabled while no
        # transponder is selected (see set_transponder_active()): there is
        # no "centre" to reset to in that state.
        self._tune_btn = QPushButton(_("T"))
        self._tune_btn.setFixedWidth(56)
        self._tune_btn.setToolTip(
            _(
                "T: reset to the transponder's Doppler-corrected centre "
                "frequency. Disabled when no transponder is selected."
            )
        )
        self._tune_btn.clicked.connect(self.reset_tune_offset)
        self._tune_btn.setEnabled(False)
        row.addWidget(self._tune_btn)

        self._sdr_lock_btn = QPushButton(_("L"))
        self._sdr_lock_btn.setFixedWidth(56)
        self._sdr_lock_btn.setToolTip(
            _(
                "SDR Lock: while on, Doppler correction stops retuning the SDR "
                "so you can freely use Passband Tune / Freq above; Doppler "
                "correction resumes from wherever you leave it once turned off"
            )
        )
        self._sdr_lock_btn.setCheckable(True)
        self._sdr_lock_btn.setStyleSheet(
            "QPushButton:checked { background-color: #f1c40f; color: #000; font-weight: bold; }"
        )
        self._sdr_lock_btn.toggled.connect(self.sdr_lock_changed.emit)
        row.addWidget(self._sdr_lock_btn)

        row.addStretch()

        return grp

    def sync_tune_offset(self, offset_hz: float) -> None:
        """Keep the internal offset bookkeeping in sync, without re-emitting tune_offset_changed.

        Called by MainWindow whenever it updates _sdr_tune_offset from a
        source other than the ◀◀/◀/▶/▶▶ buttons — SDR Lock's per-cycle
        recompute, or a manually typed absolute frequency via the Freq box
        (_on_freq_edited() → MainWindow._on_sdr_manual_freq_requested())
        — so the buttons keep stepping from the correct baseline
        afterward instead of a stale accumulated value. The Freq box's
        on-screen value needs no explicit update here: it is kept live-
        synced to the SDR's actual frequency via _on_center_freq()
        regardless of how that frequency was set.
        """
        self._tune_offset_hz = offset_hz

    def _apply_tune(self, multiplier: int) -> None:
        """Shift the tuned frequency by multiplier × the current step size.

        With a transponder selected, this accumulates a persistent offset
        from the Doppler-corrected centre frequency (kept alive by
        MainWindow's dl_rig1 = dl_corr + _sdr_tune_offset write regardless
        of SDR Lock state). With no transponder selected there is no
        Doppler cycle running to consume that offset at all
        (_doppler_cycle() returns immediately without one selected), so
        the SDR is retuned directly instead.
        """
        idx = self._tune_step_combo.currentIndex()
        step_hz = _TUNE_STEPS[idx][1] if 0 <= idx < len(_TUNE_STEPS) else 1_000
        delta_hz = float(multiplier * step_hz)
        if self._transponder_active:
            self._tune_offset_hz += delta_hz
            # Nudge the display immediately for responsive feedback; the
            # next center_freq_changed tick (~100ms) reconciles it with
            # the exact device value regardless.
            self._manual_freq_spin.blockSignals(True)
            self._manual_freq_spin.setValue(self._manual_freq_spin.value() + delta_hz / 1e6)
            self._manual_freq_spin.blockSignals(False)
            self.tune_offset_changed.emit(self._tune_offset_hz)
            return
        if self._pipeline is None:
            return
        device = getattr(self._pipeline, "_device", None)
        if device is None:
            return
        new_freq_hz = self._manual_freq_spin.value() * 1e6 + delta_hz
        device.set_center_freq(new_freq_hz)
        self._manual_freq_spin.blockSignals(True)
        self._manual_freq_spin.setValue(new_freq_hz / 1e6)
        self._manual_freq_spin.blockSignals(False)

    def _build_demod_panel(self) -> QGroupBox:
        grp = QGroupBox(_("Demodulator"))
        form = QVBoxLayout(grp)

        # Mode row
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel(_("Mode:")))
        self._mode_combo = QComboBox()
        for m in ("NFM", "USB", "LSB", "CW"):
            self._mode_combo.addItem(m)
        self._mode_combo.setCurrentIndex(1)  # USB default
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self._mode_combo)
        mode_row.addStretch()
        form.addLayout(mode_row)

        # Volume slider
        vol_row = QHBoxLayout()
        vol_row.addWidget(QLabel(_("Volume:")))
        self._vol_slider = QSlider(Qt.Orientation.Horizontal)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setValue(70)
        self._vol_slider.valueChanged.connect(self._on_volume_changed)
        self._vol_label = QLabel("70%")
        vol_row.addWidget(self._vol_slider)
        vol_row.addWidget(self._vol_label)
        form.addLayout(vol_row)

        # AGC checkbox
        agc_row = QHBoxLayout()
        self._agc_rb_on = QRadioButton(_("AGC On"))
        self._agc_rb_on.setChecked(True)
        self._agc_rb_off = QRadioButton(_("AGC Off"))
        self._agc_rb_on.toggled.connect(self._on_agc_changed)
        agc_row.addWidget(self._agc_rb_on)
        agc_row.addWidget(self._agc_rb_off)
        agc_row.addStretch()
        form.addLayout(agc_row)

        # Audio playback + MP3 recording buttons
        btn_row = QHBoxLayout()
        self._start_audio_btn = QPushButton(_("▶ Start Audio"))
        self._stop_audio_btn = QPushButton(_("■ Stop Audio"))
        self._stop_audio_btn.setEnabled(False)
        self._start_audio_btn.clicked.connect(self._start_audio)
        self._stop_audio_btn.clicked.connect(self._stop_audio)
        btn_row.addWidget(self._start_audio_btn)
        btn_row.addWidget(self._stop_audio_btn)
        btn_row.addStretch()

        self._audio_rec_btn = QPushButton(_("● REC Audio"))
        self._audio_rec_btn.setStyleSheet("color: red; font-weight: bold;")
        self._audio_rec_btn.setEnabled(LAMEENC_AVAILABLE)
        if not LAMEENC_AVAILABLE:
            self._audio_rec_btn.setToolTip(_("lameenc not installed — pip install lameenc"))
        self._audio_stop_rec_btn = QPushButton(_("■ STOP"))
        self._audio_stop_rec_btn.setEnabled(False)
        self._audio_rec_status_label = QLabel("")
        self._audio_rec_btn.clicked.connect(self._start_audio_recording)
        self._audio_stop_rec_btn.clicked.connect(self._stop_audio_recording)
        btn_row.addWidget(self._audio_rec_btn)
        btn_row.addWidget(self._audio_stop_rec_btn)
        btn_row.addWidget(self._audio_rec_status_label)

        self._open_audio_folder_btn = QPushButton(_("📁"))
        self._open_audio_folder_btn.setToolTip(_("Open audio recordings folder in file manager"))
        self._open_audio_folder_btn.setFixedWidth(32)
        self._open_audio_folder_btn.clicked.connect(self._open_audio_folder)
        btn_row.addWidget(self._open_audio_folder_btn)

        form.addLayout(btn_row)
        return grp

    def _build_recorder_panel(self) -> QGroupBox:
        grp = QGroupBox(_("IQ Recorder"))  # noqa: F823
        v = QVBoxLayout(grp)

        bw_row = QHBoxLayout()
        bw_row.addWidget(QLabel(_("Record BW:")))
        self._rec_bw_combo = QComboBox()
        for label, _bw in _REC_BANDWIDTHS:
            self._rec_bw_combo.addItem(label)
        self._rec_bw_combo.setCurrentIndex(2)  # 250 kHz default
        bw_row.addWidget(self._rec_bw_combo)
        bw_row.addStretch()
        v.addLayout(bw_row)

        file_row = QHBoxLayout()
        file_row.addWidget(QLabel(_("File:")))
        self._rec_file_label = QLabel("—")
        self._rec_file_label.setStyleSheet("color: gray; font-size: 10px;")
        file_row.addWidget(self._rec_file_label)
        v.addLayout(file_row)

        ctrl_row = QHBoxLayout()
        self._rec_btn = QPushButton(_("● REC"))
        self._rec_btn.setStyleSheet("color: red; font-weight: bold;")
        self._stop_rec_btn = QPushButton(_("■ STOP"))
        self._stop_rec_btn.setEnabled(False)
        self._rec_status_label = QLabel("00:00:00  0 MB")
        self._rec_btn.clicked.connect(self._start_recording)
        self._stop_rec_btn.clicked.connect(self._stop_recording)
        ctrl_row.addWidget(self._rec_btn)
        ctrl_row.addWidget(self._stop_rec_btn)
        ctrl_row.addWidget(self._rec_status_label)
        ctrl_row.addStretch()
        self._open_folder_btn = QPushButton(_("📁"))
        self._open_folder_btn.setToolTip(_("Open IQ recordings folder in file manager"))
        self._open_folder_btn.setFixedWidth(32)
        self._open_folder_btn.clicked.connect(self._open_iq_folder)
        ctrl_row.addWidget(self._open_folder_btn)
        v.addLayout(ctrl_row)
        return grp

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @Slot(list)
    def _on_spectrum(self, points: list[tuple[float, float]]) -> None:
        """Update the spectrum chart with new FFT data."""
        if not points:
            return
        self._spectrum_series.clear()
        # Convert Hz to MHz for the axis
        pts = [(f / 1e6, p) for f, p in points]
        # Use replace() for efficiency when series already has data
        self._spectrum_series.replace([QPointF(f, p) for f, p in pts])
        freqs = [f for f, _ in pts]
        if freqs:
            self._freq_axis.setRange(min(freqs), max(freqs))

    @Slot(float)
    def _on_center_freq(self, freq_hz: float) -> None:
        """Update the centre-frequency overlay label and the manual tune field.

        Keeps _manual_freq_spin showing the SDR's actual live frequency
        instead of whatever value it was last set to — it previously only
        reflected the device's frequency at connect time and then went
        stale, so a manual Tune press while a transponder was selected
        looked like it "reverted" to a wrong frequency when it was really
        just an outdated display never catching up with the Doppler
        correction loop overwriting the real frequency underneath it
        (GitHub Issue #12 follow-up).
        """
        mhz = freq_hz / 1e6
        self._freq_overlay.setText(f"{mhz:.6f} MHz")
        self._center_marker_series.replace(
            [QPointF(mhz, _SPECTRUM_YMIN), QPointF(mhz, _SPECTRUM_YMAX)]
        )
        # Skip while the user has the field focused (mid-edit): this fires at
        # ~10fps, so overwriting it unconditionally clobbered every keystroke
        # of a manual retune before editingFinished could ever see the typed
        # value, making it impossible to enter a new frequency at all
        # (regression found right after the fix above shipped, Issue #12).
        if not self._manual_freq_spin.hasFocus():
            self._manual_freq_spin.blockSignals(True)
            self._manual_freq_spin.setValue(mhz)
            self._manual_freq_spin.blockSignals(False)

    @Slot(str)
    def _on_status(self, msg: str) -> None:
        self._status_label.setText(msg)
        self._status_label.setStyleSheet(
            "color: #00dcff; font-weight: bold;"
            if "stream" in msg.lower()
            else "color: gray; font-weight: bold;"
        )

    def _on_mode_changed(self, idx: int) -> None:
        if self._pipeline is None or not SOAPY_AVAILABLE:
            return
        modes = [DemodMode.NFM, DemodMode.USB, DemodMode.LSB, DemodMode.CW]
        if 0 <= idx < len(modes):
            self._pipeline.set_demod_mode(modes[idx])

    def _on_volume_changed(self, value: int) -> None:
        self._vol_label.setText(f"{value}%")
        if self._pipeline is not None:
            self._pipeline.set_audio_gain(value / 100.0)

    def _on_agc_changed(self, on: bool) -> None:
        if self._pipeline is not None:
            self._pipeline.set_agc(on)

    def _start_audio(self) -> None:
        if self._pipeline is None:
            return
        self._pipeline.set_audio_enabled(True)
        self._start_audio_btn.setEnabled(False)
        self._stop_audio_btn.setEnabled(True)

    def _stop_audio(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_audio_enabled(False)
        self._start_audio_btn.setEnabled(True)
        self._stop_audio_btn.setEnabled(False)

    def _start_recording(self) -> None:
        if self._pipeline is None or self._recording:
            return
        idx = self._rec_bw_combo.currentIndex()
        bw_hz = _REC_BANDWIDTHS[idx][1] if 0 <= idx < len(_REC_BANDWIDTHS) else 250_000

        # Adjust device sample rate for recording bandwidth
        if hasattr(self._pipeline, "_device") and self._pipeline._device is not None:
            self._pipeline._device.set_sample_rate(float(bw_hz))

        file_path = self._pipeline.recorder.start(
            sample_rate=bw_hz,
            norad=self._sat_norad,
            sat_name=self._sat_name,
        )
        self._recording = True
        self._rec_file_label.setText(file_path.name)
        self._rec_btn.setEnabled(False)
        self._stop_rec_btn.setEnabled(True)
        self._status_timer.start()

    def _stop_recording(self) -> None:
        if not self._recording:
            return
        self._recording = False
        self._status_timer.stop()
        if self._pipeline is not None:
            self._pipeline.recorder.stop()
        self._rec_btn.setEnabled(True)
        self._stop_rec_btn.setEnabled(False)
        self._rec_status_label.setText("00:00:00  0 MB")

    def _open_iq_folder(self) -> None:
        """Open the IQ recordings save directory in the OS file manager."""
        self._iq_save_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._iq_save_dir)))

    def _start_audio_recording(
        self,
        norad_override: int | None = None,
        name_override: str | None = None,
    ) -> None:
        """Start MP3 audio recording (requires lameenc and active audio stream)."""
        if not LAMEENC_AVAILABLE or self._pipeline is None:
            return
        if self._audio_recorder.is_active:
            return
        file_path = self._audio_recorder.start(
            norad=norad_override if norad_override is not None else self._sat_norad,
            sat_name=name_override if name_override is not None else self._sat_name,
        )
        self._pipeline.audio_ready.connect(self._audio_recorder.put_pcm)
        self._audio_rec_btn.setEnabled(False)
        self._audio_stop_rec_btn.setEnabled(True)
        self._audio_rec_status_label.setText(file_path.name)
        self._audio_rec_timer.start()

    def _stop_audio_recording(self) -> None:
        """Stop MP3 audio recording and disconnect the pipeline signal."""
        if not self._audio_recorder.is_active:
            return
        if self._pipeline is not None:
            import contextlib

            with contextlib.suppress(Exception):
                self._pipeline.audio_ready.disconnect(self._audio_recorder.put_pcm)
        self._audio_recorder.stop()
        self._audio_rec_timer.stop()
        self._audio_rec_btn.setEnabled(LAMEENC_AVAILABLE)
        self._audio_stop_rec_btn.setEnabled(False)
        self._audio_rec_status_label.setText("")

    def _open_audio_folder(self) -> None:
        """Open the audio recordings save directory in the OS file manager."""
        self._audio_save_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._audio_save_dir)))

    def _update_audio_rec_status(self) -> None:
        """Update elapsed time and file size label during audio recording."""
        if not self._audio_recorder.is_active:
            return
        elapsed = int(self._audio_recorder.elapsed_seconds)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        mb = self._audio_recorder.bytes_written / 1e6
        self._audio_rec_status_label.setText(f"{h:02d}:{m:02d}:{s:02d}  {mb:.1f} MB")

    def _update_rec_status(self) -> None:
        if self._pipeline is None:
            return
        rec = self._pipeline.recorder
        elapsed = int(rec.elapsed_seconds)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        mb = rec.bytes_written / 1e6
        self._rec_status_label.setText(f"{h:02d}:{m:02d}:{s:02d}  {mb:.1f} MB")
