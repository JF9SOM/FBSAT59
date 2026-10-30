"""Pass panel: the Target and Group Min El boxes are one shared setting."""

from __future__ import annotations

from pytestqt.qtbot import QtBot

from ui.pass_panel import PassPanel


def test_editing_either_box_updates_the_other_and_emits(qtbot: QtBot) -> None:
    panel = PassPanel()
    qtbot.addWidget(panel)
    seen: list[float] = []
    panel.min_elevation_changed.connect(seen.append)

    panel._target_min_el.setValue(20)
    assert panel._group_min_el.value() == 20
    assert panel.min_elevation() == 20.0

    panel._group_min_el.setValue(35)
    assert panel._target_min_el.value() == 35
    assert seen == [20.0, 35.0]


def test_set_min_elevation_does_not_emit(qtbot: QtBot) -> None:
    panel = PassPanel()
    qtbot.addWidget(panel)
    seen: list[float] = []
    panel.min_elevation_changed.connect(seen.append)

    panel.set_min_elevation(12)
    assert panel._target_min_el.value() == 12
    assert panel._group_min_el.value() == 12
    assert seen == []
