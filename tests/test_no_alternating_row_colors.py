"""No table/list in the app may alternate its row background colours.

Alternating rows always leave every second row grey, and the app's light text
is hard to read on it (reported on the Telemetry tab, 2026-09-24). Every row is
drawn on the same dark background instead.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"


def test_no_widget_enables_alternating_row_colors() -> None:
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"setAlternatingRowColors\(\s*True\s*\)", text) or re.search(
            r"alternate-background-color", text
        ):
            offenders.append(str(path.relative_to(_SRC)))
    assert offenders == []
