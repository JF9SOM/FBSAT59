"""Paste-and-decode page of the Telemetry tab: ASCII telemetry lines pasted from elsewhere.

Other stations' logs (e.g. JJ1WTK's satellite log) list packets as text such as
``[0 2026-10-20 23:07:35 UTC] OK0KST>CQ:U,9008906,...,JA1HCB,...<0x00>``. The page decodes
such lines with the satellite's format definition and lists every field. Nothing pasted here
is stored (no log entry, no SatNOGS upload).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from comms.telemetry.decoder import TelemetryField, decode_telemetry, list_formats
from i18n import _

# More than this many pasted lines are ignored (keeps the page responsive).
MAX_LINES = 2000
_DEBOUNCE_MS = 300

# "[...] SRC>DEST[,PATH]:payload" with an optional bracketed prefix (the log's time stamp).
_HEADER_RE = re.compile(
    r"^(?:\[(?P<prefix>[^\]]*)\]\s*)?"
    r"(?P<src>[A-Za-z0-9]{1,6}(?:-\d{1,2})?)>(?P<dest>[^:,]*)(?:,[^:]*)?:(?P<info>.*)$"
)
_STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?: UTC)?")
_ESCAPE_RE = re.compile(r"<0x([0-9A-Fa-f]{2})>")


@dataclass(frozen=True)
class PastedLine:
    """One pasted line split into its parts."""

    callsign: str  # source callsign without SSID, "" when the line has no header
    stamp: str  # the log's time stamp, "" if none
    payload: bytes


def parse_pasted_line(line: str) -> PastedLine | None:
    """Split a pasted line into callsign, time stamp and payload bytes; None for a blank line.

    A line without a ``SRC>DEST:`` header is taken as the payload itself. ``<0xNN>`` escapes
    (a log's way of showing non-printable bytes) become those bytes.
    """
    line = line.strip()
    if not line:
        return None
    match = _HEADER_RE.match(line)
    callsign = ""
    stamp = ""
    info = line
    if match:
        callsign = match.group("src").split("-")[0].upper()
        info = match.group("info")
        stamp_match = _STAMP_RE.search(match.group("prefix") or "")
        stamp = stamp_match.group(0) if stamp_match else ""
    payload = _ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), info).encode("latin-1", "replace")
    return PastedLine(callsign, stamp, payload)


def norad_for_callsign(callsign: str) -> int | None:
    """NORAD id of the satellite whose format definition carries *callsign*, if any."""
    wanted = callsign.upper()
    if not wanted:
        return None
    for fmt in list_formats():
        if str(fmt.get("callsign", "")).upper() == wanted and isinstance(fmt.get("norad"), int):
            return int(fmt["norad"])
    return None


def format_field_value(f: TelemetryField) -> str:
    """A decoded field's value as shown in the "Decoded Fields" tables."""
    if f.is_string:
        return f.unit
    value = f.scaled_value
    if f.is_integer:
        text = f"{round(value):,}"
    elif abs(value) >= 1000:
        text = f"{value:,.2f}"
    elif abs(value) >= 1:
        text = f"{value:.4f}"
    else:
        text = f"{value:.6f}"
    return f"{text} {f.unit}" if f.unit else text


class AsciiPasteWidget(QWidget):
    """Text box for pasted telemetry lines and a tree of the decoded fields of each line."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._default_norad: int | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(_DEBOUNCE_MS)
        self._timer.timeout.connect(self.decode_text)

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel(_("Satellite:")))
        self._combo = QComboBox()
        self._combo.addItem(_("Auto (from the callsign in each line)"), None)
        formats = [f for f in list_formats() if isinstance(f.get("norad"), int)]
        for fmt in sorted(formats, key=lambda f: str(f.get("name", "")).lower()):
            call = fmt.get("callsign")
            label = f"{fmt['name']} ({call})" if call else str(fmt["name"])
            self._combo.addItem(label, int(fmt["norad"]))
        self._combo.currentIndexChanged.connect(self.decode_text)
        top.addWidget(self._combo, stretch=1)
        self._clear_btn = QPushButton(_("Clear"))
        self._clear_btn.clicked.connect(self._on_clear)
        top.addWidget(self._clear_btn)
        layout.addLayout(top)

        self._input = QPlainTextEdit()
        self._input.setPlaceholderText(
            _(
                "Paste telemetry lines here, e.g. a satellite log with lines like\n"
                "[0 2026-10-20 23:07:35 UTC] OK0KST>CQ:U,9008906,9008995,1,0,...\n"
                "Nothing pasted here is saved."
            )
        )
        self._input.setMaximumHeight(130)
        self._input.textChanged.connect(self._timer.start)
        layout.addWidget(self._input)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels([_("Line / Field"), _("Value")])
        header = self._tree.header()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setStretchLastSection(True)
        layout.addWidget(self._tree, stretch=1)

        self._status = QLabel("")
        layout.addWidget(self._status)

    def set_default_norad(self, norad: int | None) -> None:
        """The satellite used for lines without a callsign header (the tab's selection)."""
        self._default_norad = norad

    def _on_clear(self) -> None:
        self._input.clear()
        self._tree.clear()
        self._status.setText("")

    def decode_text(self, *_args: object) -> None:
        """Decode the pasted text again and list every line's fields."""
        self._tree.clear()
        lines = [ln for ln in self._input.toPlainText().splitlines() if ln.strip()]
        decoded = 0
        for number, line in enumerate(lines[:MAX_LINES], start=1):
            parsed = parse_pasted_line(line)
            if parsed is None:
                continue
            if self._add_line(number, line, parsed):
                decoded += 1
        self._tree.expandAll()
        note = _("{ok} of {n} lines decoded").format(ok=decoded, n=len(lines)) if lines else ""
        if len(lines) > MAX_LINES:
            note += _(" (only the first {n} lines are used)").format(n=MAX_LINES)
        self._status.setText(note)

    def _add_line(self, number: int, raw_line: str, parsed: PastedLine) -> bool:
        """Add one line (title + its fields) to the tree; True if any field was decoded."""
        chosen = self._combo.currentData()
        norad = chosen if isinstance(chosen, int) else norad_for_callsign(parsed.callsign)
        if norad is None and parsed.callsign == "":
            norad = self._default_norad
        stamp = f"  {parsed.stamp}" if parsed.stamp else ""
        top = QTreeWidgetItem(self._tree)
        top.setToolTip(0, raw_line.strip())
        if norad is None:
            top.setText(0, f"#{number}{stamp}")
            reason = (
                _("no satellite definition for {call}").format(call=parsed.callsign)
                if parsed.callsign
                else _("select a satellite")
            )
            top.setText(1, reason)
            return False
        frame = decode_telemetry(parsed.callsign, parsed.payload, norad)
        title = f"#{number}  {frame.satellite_name}{stamp}"
        if not frame.has_fields:
            top.setText(0, title)
            top.setText(1, _("not decodable with this satellite's definition"))
            return False
        label = frame.telemetry_label or str(frame.telemetry_id or "")
        top.setText(0, f"{title}  —  {label}" if label else title)
        for f in frame.fields:
            child = QTreeWidgetItem(top)
            child.setText(0, f.label)
            child.setText(1, format_field_value(f))
        return True
