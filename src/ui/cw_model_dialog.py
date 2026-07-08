"""Help > CW Model Installation… dialog.

One-click install: installs onnxruntime via pip (if needed), then
downloads model.onnx from e04/deepcw-engine on GitHub.

Install steps (single background thread):
  1. pip install onnxruntime  — skipped if already present
  2. Download model.onnx      — from raw.githubusercontent.com
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from comms.cw.model_info import (
    MODEL_FILE,
    MODEL_URL,
    find_model,
    get_user_cw_model_dir,
    is_onnxruntime_available,
)
from i18n import _

_TOTAL_STEPS = 2  # onnxruntime + model.onnx


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------


class _InstallWorker(QThread):
    """Installs onnxruntime via pip and downloads model.onnx."""

    progress = Signal(int)  # 0-100
    status = Signal(str)
    finished_ok = Signal(str)  # install directory
    finished_err = Signal(str)  # error message

    def run(self) -> None:
        step = 0

        # Step 1: install onnxruntime if missing
        if not is_onnxruntime_available():
            self.status.emit(_("Installing onnxruntime…"))
            try:
                import subprocess

                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "onnxruntime"],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if result.returncode != 0:
                    self.finished_err.emit(
                        "pip install onnxruntime failed:\n" + result.stderr[-500:]
                    )
                    return
            except Exception as exc:
                self.finished_err.emit(f"pip install onnxruntime: {exc}")
                return
        step += 1
        self.progress.emit(step * 100 // _TOTAL_STEPS)

        # Step 2: download model.onnx
        import urllib.request

        dest_dir = get_user_cw_model_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / MODEL_FILE
        self.status.emit(_("Downloading {name}…").format(name=MODEL_FILE))
        base_pct = step * 100 // _TOTAL_STEPS

        def _hook(block: int, block_size: int, total: int, _base: int = base_pct) -> None:
            if total > 0:
                within = min(block * block_size * 100 // total, 100)
                self.progress.emit(_base + within // _TOTAL_STEPS)

        try:
            urllib.request.urlretrieve(MODEL_URL, dest, reporthook=_hook)
        except Exception as exc:
            self.finished_err.emit(f"{MODEL_FILE}: {exc}")
            return

        self.progress.emit(100)
        self.finished_ok.emit(str(dest_dir))


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


class CwModelDialog(QDialog):
    """Help > CW Model Installation… dialog."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("CW Model Installation"))
        self.setMinimumWidth(520)
        self._worker: _InstallWorker | None = None
        self._setup_ui()
        self._refresh_status()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)

        # Status group
        status_box = QGroupBox(_("Current Status"))
        sl = QVBoxLayout(status_box)
        self._lbl_ort = QLabel()
        self._lbl_ort.setWordWrap(True)
        self._lbl_model = QLabel()
        self._lbl_model.setWordWrap(True)
        sl.addWidget(self._lbl_ort)
        sl.addWidget(self._lbl_model)
        root.addWidget(status_box)

        # About group
        about_box = QGroupBox(_("About DeepCW"))
        al = QVBoxLayout(about_box)
        about_lbl = QLabel(
            _(
                "The DeepCW model is provided by "
                "<a href='https://github.com/e04/deepcw-engine'>e04/deepcw-engine</a>.<br><br>"
                "It uses a CRNN + CTC architecture trained to decode CW (Morse code) "
                "from audio spectrograms with near-zero error at −4 dB S/N.<br><br>"
                "<b>Clicking Install will automatically:</b><br>"
                "  1. Install <tt>onnxruntime</tt> (Python ML runtime) via pip<br>"
                "  2. Download <tt>model.onnx</tt> (~15 MB) from GitHub"
            )
        )
        about_lbl.setOpenExternalLinks(True)
        about_lbl.setWordWrap(True)
        al.addWidget(about_lbl)
        root.addWidget(about_box)

        # Install group
        inst_box = QGroupBox(_("Install"))
        il = QVBoxLayout(inst_box)
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._lbl_dl = QLabel()
        self._lbl_dl.setVisible(False)
        il.addWidget(self._progress)
        il.addWidget(self._lbl_dl)
        btn_row = QHBoxLayout()
        self._btn_inst = QPushButton(_("Install / Update"))
        self._btn_inst.clicked.connect(self._on_install)
        btn_row.addStretch()
        btn_row.addWidget(self._btn_inst)
        il.addLayout(btn_row)
        root.addWidget(inst_box)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def _refresh_status(self) -> None:
        if is_onnxruntime_available():
            self._lbl_ort.setText(
                "<b style='color:#27ae60'>&#x2714; onnxruntime " + _("installed") + "</b>"
            )
        else:
            self._lbl_ort.setText(
                "<b style='color:#e74c3c'>&#x2718; onnxruntime "
                + _("not installed")
                + "</b>"
                + " — "
                + _("will be installed automatically")
            )

        path = find_model()
        if path:
            size_kb = path.stat().st_size // 1024
            self._lbl_model.setText(
                f"<b style='color:#27ae60'>&#x2714; {MODEL_FILE}</b> ({size_kb} KB)"
            )
        else:
            not_found = _("not found")
            self._lbl_model.setText(
                f"<b style='color:#e74c3c'>&#x2718; {MODEL_FILE}</b> — {not_found}"
            )

    def _on_install(self) -> None:
        self._btn_inst.setEnabled(False)
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._lbl_dl.setVisible(True)
        self._lbl_dl.setText(_("Starting…"))

        self._worker = _InstallWorker(self)
        self._worker.progress.connect(self._progress.setValue)
        self._worker.status.connect(self._lbl_dl.setText)
        self._worker.finished_ok.connect(self._on_ok)
        self._worker.finished_err.connect(self._on_err)
        self._worker.start()

    def _on_ok(self, path: str) -> None:
        self._progress.setValue(100)
        self._lbl_dl.setText(_("Installed to: ") + path)
        self._btn_inst.setEnabled(True)
        self._refresh_status()

    def _on_err(self, msg: str) -> None:
        self._lbl_dl.setText(_("Error: ") + msg)
        self._btn_inst.setEnabled(True)
        self._progress.setVisible(False)
