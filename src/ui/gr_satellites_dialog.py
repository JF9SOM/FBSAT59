"""Help > gr-satellites… dialog.

Detects whether gr-satellites (and GNU Radio) is installed and shows
platform-specific installation guidance.  Does NOT perform automatic
installation — gr-satellites requires GNU Radio 3.10+ which itself needs
careful system-level setup that varies by distribution.

Detection:
  1. ``gr_satellites`` Python module importable → installed
  2. ``gr_satellites`` CLI on PATH (``which gr_satellites``) → installed
  3. Neither → not installed

"""

from __future__ import annotations

import shutil
import sys

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from i18n import _

# ---------------------------------------------------------------------------
# Detection helper
# ---------------------------------------------------------------------------


def _detect_gr_satellites() -> tuple[bool, str]:
    """Return (is_installed, version_or_path_string)."""
    # Try importing the Python module first
    try:
        import importlib.util

        spec = importlib.util.find_spec("gr_satellites")
        if spec is not None:
            try:
                try:
                    import gr_satellites as _grs  # noqa: F401
                except ImportError:
                    _grs = None
                ver = getattr(_grs, "__version__", "installed") if _grs is not None else "installed"
                return True, f"gr_satellites Python module ({ver})"
            except Exception:
                return True, "gr_satellites Python module (import ok)"
    except Exception:
        pass

    # Try CLI
    cli = shutil.which("gr_satellites")
    if cli:
        return True, f"CLI: {cli}"

    return False, ""


# ---------------------------------------------------------------------------
# Platform-specific installation instructions
# ---------------------------------------------------------------------------


def _get_instructions() -> str:
    """Build platform-specific installation guidance.

    Built lazily (not as module-level constants) so the narrative text
    picks up the current UI language each time the dialog is opened.
    """
    if sys.platform == "linux":
        requires_note = _("gr-satellites requires GNU Radio 3.10 (available via Ubuntu 22.04+).")
        oot_note = _("Or use the OOT module directly:")
        verify_note = _("After installation, verify with: <tt>gr_satellites --help</tt>")
        return (
            f"<b>Ubuntu / Debian</b><br>\n{requires_note}<br><br>\n\n"
            "<pre>sudo apt install gnuradio python3-gnuradio\n"
            "pip install gr-satellites</pre>\n\n"
            f"{oot_note}\n"
            "<pre>git clone https://github.com/daniestevez/gr-satellites.git\n"
            "cd gr-satellites\n"
            "mkdir build && cd build\n"
            "cmake .. && make -j$(nproc)\n"
            "sudo make install\n"
            "sudo ldconfig</pre>\n\n"
            f"{verify_note}\n"
        )
    if sys.platform == "darwin":
        build_note = _("If brew gnuradio is outdated, build from source:")
        return (
            "<b>macOS (Homebrew)</b><br><br>\n"
            "<pre>brew install gnuradio\n"
            "pip install gr-satellites</pre>\n\n"
            f"{build_note}\n"
            "<pre>git clone https://github.com/daniestevez/gr-satellites.git\n"
            "cd gr-satellites\n"
            "mkdir build && cd build\n"
            "cmake .. && make -j$(sysctl -n hw.logicalcpu)\n"
            "sudo make install</pre>\n"
        )
    if sys.platform == "win32":
        installer_note = _("GNU Radio on Windows is available via the official installer:")
        after_note = _("After installing GNU Radio, install gr-satellites:")
        wsl_note = _(
            "Note: Windows support for gr-satellites may be limited.\n"
            "Using WSL2 with Ubuntu is the most reliable option."
        )
        return (
            f"<b>Windows</b><br><br>\n{installer_note}<br>\n"
            '<a href="https://www.gnuradio.org/blog/2020-06-29-windows-support/">\n'
            "gnuradio.org — Windows Support</a><br><br>\n\n"
            f"{after_note}<br>\n"
            "<pre>pip install gr-satellites</pre>\n\n"
            f"{wsl_note}\n"
        )
    generic_note = _(
        "Install GNU Radio 3.10+ from your system package manager or\n"
        '<a href="https://www.gnuradio.org/">gnuradio.org</a>, then:'
    )
    return f"{generic_note}<br>\n<pre>pip install gr-satellites</pre>\n"


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


class GrSatellitesDialog(QDialog):
    """Help > gr-satellites… — status and installation guidance.

    Does not perform automatic installation.  The dialog is intentionally
    read-only and informational, matching the design in CLAUDE.md.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("gr-satellites Installation"))
        self.resize(580, 520)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # -- Detection status --
        is_installed, detail = _detect_gr_satellites()
        status_grp = QGroupBox(_("Detection Status"))
        sv = QVBoxLayout(status_grp)

        if is_installed:
            icon, color = "✅", "#2ecc71"
            msg = _("gr-satellites is installed: ") + detail
        else:
            icon, color = "❌", "#e74c3c"
            msg = _("gr-satellites is NOT installed.")

        lbl = QLabel(f"{icon}  {msg}")
        lbl.setStyleSheet(f"color: {color}; font-weight: bold;")
        lbl.setWordWrap(True)
        sv.addWidget(lbl)
        layout.addWidget(status_grp)

        # -- What is gr-satellites --
        about_grp = QGroupBox(_("About gr-satellites"))
        av = QVBoxLayout(about_grp)
        about_lbl = QLabel(
            _(
                "gr-satellites is an open-source GNU Radio out-of-tree (OOT) module "
                "that decodes telemetry from 100+ amateur satellites.\n\n"
                "When installed, FBSAT59 can launch gr_satellites as a "
                "subprocess and display decoded telemetry in the Telemetry tab "
                "(9600 baud and other non-AFSK modes will become available)."
            )
        )
        about_lbl.setWordWrap(True)
        av.addWidget(about_lbl)
        layout.addWidget(about_grp)

        # -- GNU Radio version requirement --
        req_grp = QGroupBox(_("Requirements"))
        rv = QVBoxLayout(req_grp)
        req_lbl = QLabel(
            _(
                "• GNU Radio 3.10 or later\n"
                "• Python 3.8+\n"
                "• gr-satellites 5.x (pip install gr-satellites)\n\n"
                "GNU Radio must be installed first at the system level.\n"
                "Do NOT install GNU Radio via pip — use your OS package manager."
            )
        )
        req_lbl.setWordWrap(True)
        rv.addWidget(req_lbl)
        layout.addWidget(req_grp)

        # -- Installation instructions --
        inst_grp = QGroupBox(_("Installation"))
        iv = QVBoxLayout(inst_grp)
        inst_lbl = QLabel(_get_instructions())
        inst_lbl.setWordWrap(True)
        inst_lbl.setOpenExternalLinks(True)
        inst_lbl.setTextFormat(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.TextFormat.RichText)
        iv.addWidget(inst_lbl)
        layout.addWidget(inst_grp)

        # -- Close button --
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)
