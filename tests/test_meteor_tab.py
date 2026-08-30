"""Tests for ui/meteor_tab.py — MeteorTab's image display/processing controls.

Uses the qtbot fixture (pytest-qt) rather than manual QApplication + close()
management — see CLAUDE.md's note on why. _load_meteor_settings()/
_load_sdr_settings() are monkeypatched so these tests never touch the
real user database.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QWheelEvent
from pytestqt.qtbot import QtBot

from ui.meteor_tab import MeteorTab, _ThumbItem


def _make_image(w: int = 40, h: int = 20, fill: int = 0xFFFFFFFF) -> QImage:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(fill)
    return img


def _make_wheel_event(angle_delta_y: int) -> QWheelEvent:
    return QWheelEvent(
        QPointF(10, 10),
        QPointF(10, 10),
        QPoint(0, 0),
        QPoint(0, angle_delta_y),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )


@pytest.fixture(autouse=True)
def _no_real_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ui.meteor_tab._load_meteor_settings", lambda: {})
    monkeypatch.setattr("ui.meteor_tab._load_sdr_settings", lambda: {})
    monkeypatch.setattr("ui.meteor_tab._save_meteor_settings", lambda data: None)


def _make_tab(qtbot: QtBot) -> MeteorTab:
    w = MeteorTab()
    qtbot.addWidget(w)
    w.resize(500, 400)
    w.show()
    qtbot.waitExposed(w)
    return w


def _trigger_context_menu_item(w: MeteorTab, role: str) -> None:
    """Simulate right-clicking the image and picking the menu item for
    *role* (one of "save"/"flip"/"fit_both"/"fit_width"/"fit_height" — see
    _build_image_context_menu()'s actions dict) without going through the
    modal menu.exec() call itself. Patching QMenu.exec is unreliable for a
    PySide6 C++-bound method — it was observed to hang a test run rather
    than actually substituting the fake return value — so
    _on_image_context_menu() is deliberately split into a menu-building
    half and a choice-handling half that this calls directly instead."""
    assert w._current_original_image is not None, "no image shown to right-click"
    _menu, actions = w._build_image_context_menu()
    w._handle_image_context_menu_choice(actions[role], actions)


class TestImageContextMenu:
    """Flip 180° / Fit Width / Fit Height / Save Image As… were moved from
    buttons into the image's right-click menu -- a third button row
    alongside Open Folder/Open Past/Clear/Gain didn't fit in one row
    without crowding it or eating into the preview's vertical space."""

    def test_noop_with_no_image_shown(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        with patch("ui.meteor_tab.QMenu") as mock_menu_cls:
            w._on_image_context_menu(None)  # type: ignore[arg-type]
        mock_menu_cls.assert_not_called()

    def test_menu_actions_reflect_current_state(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        w._show_image(_make_image())
        w._image_rotated = True
        w._fit_mode = "height"

        _menu, actions = w._build_image_context_menu()

        assert actions["flip"].isChecked() is True
        assert actions["fit_height"].isChecked() is True
        assert actions["fit_width"].isChecked() is False
        assert actions["fit_both"].isChecked() is False

    def test_flip_menu_item_toggles_rotation(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        w._show_image(_make_image())
        assert w._image_rotated is False

        _trigger_context_menu_item(w, "flip")

        assert w._image_rotated is True
        rotated = w._apply_rotation(w._current_original_image)
        assert rotated.pixelColor(0, 0) == w._current_original_image.pixelColor(
            w._current_original_image.width() - 1, w._current_original_image.height() - 1
        )

        _trigger_context_menu_item(w, "flip")
        assert w._image_rotated is False
        assert w._apply_rotation(w._current_original_image) is w._current_original_image

    def test_fit_width_menu_item_matches_viewport_width(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        # A tall, narrow image: fitting its width to a wide-ish viewport
        # should make the resulting height far exceed the viewport (which
        # is exactly why _image_scroll needs to allow scrolling).
        w._show_image(_make_image(w=100, h=3000))

        _trigger_context_menu_item(w, "fit_width")

        assert w._fit_mode == "width"
        viewport = w._image_scroll.viewport().size()
        pixmap = w._image_label.pixmap()
        assert not pixmap.isNull()
        assert pixmap.width() == viewport.width()
        assert pixmap.height() > viewport.height()

    def test_fit_height_menu_item_matches_viewport_height(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        w._show_image(_make_image(w=3000, h=100))

        _trigger_context_menu_item(w, "fit_height")

        assert w._fit_mode == "height"
        viewport = w._image_scroll.viewport().size()
        pixmap = w._image_label.pixmap()
        assert not pixmap.isNull()
        assert pixmap.height() == viewport.height()
        assert pixmap.width() > viewport.width()

    def test_fit_both_menu_item_reverts_from_width(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        w._show_image(_make_image(w=2000, h=100))
        w._fit_mode = "width"

        _trigger_context_menu_item(w, "fit_both")

        assert w._fit_mode == "fit"
        viewport = w._image_scroll.viewport().size()
        pixmap = w._image_label.pixmap()
        assert pixmap.width() <= viewport.width()
        assert pixmap.height() <= viewport.height()

    def test_save_menu_item_saves_the_rotated_image(self, qtbot: QtBot, tmp_path: Path) -> None:
        w = _make_tab(qtbot)
        w._show_image(_make_image())
        w._image_rotated = True

        out_path = tmp_path / "saved.png"
        with patch(
            "ui.meteor_tab.QFileDialog.getSaveFileName",
            return_value=(str(out_path), "PNG (*.png)"),
        ):
            _trigger_context_menu_item(w, "save")

        assert out_path.is_file()
        saved = QImage(str(out_path))
        expected = w._apply_rotation(w._current_original_image)
        assert saved.pixelColor(0, 0) == expected.pixelColor(0, 0)


class TestSaveImageAs:
    """Direct tests of _on_save_image_as() itself (also reachable via the
    context menu — see TestImageContextMenu.test_save_menu_item_*)."""

    def test_saves_the_rotated_image(self, qtbot: QtBot, tmp_path: Path) -> None:
        w = _make_tab(qtbot)
        w._show_image(_make_image())
        w._image_rotated = True

        out_path = tmp_path / "saved.png"
        with patch(
            "ui.meteor_tab.QFileDialog.getSaveFileName",
            return_value=(str(out_path), "PNG (*.png)"),
        ):
            w._on_save_image_as()

        assert out_path.is_file()
        saved = QImage(str(out_path))
        expected = w._apply_rotation(w._current_original_image)
        assert saved.pixelColor(0, 0) == expected.pixelColor(0, 0)

    def test_noop_with_no_image_shown(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        with patch("ui.meteor_tab.QFileDialog.getSaveFileName") as mock_dialog:
            w._on_save_image_as()
        mock_dialog.assert_not_called()


class TestCitiesOverlayButton:
    def test_shows_message_when_nothing_selected(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        with patch("ui.meteor_tab.QMessageBox.information") as mock_box:
            w._on_add_cities_overlay()
        mock_box.assert_called_once()

    def test_warns_when_product_cbor_missing(self, qtbot: QtBot, tmp_path: Path) -> None:
        w = _make_tab(qtbot)
        png_path = tmp_path / "msu_mr_rgb_AVHRR_221_False_Color.png"
        png_path.write_bytes(b"\x89PNG\r\n")
        item = _ThumbItem(_make_image(), png_path.name, path=png_path)
        w._history_list.addItem(item)
        w._history_list.setCurrentItem(item)

        with patch("ui.meteor_tab.QMessageBox.warning") as mock_box:
            w._on_add_cities_overlay()
        mock_box.assert_called_once()

    def test_starts_overlay_process_when_product_cbor_found(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        w = _make_tab(qtbot)
        png_path = tmp_path / "msu_mr_rgb_AVHRR_221_False_Color.png"
        png_path.write_bytes(b"\x89PNG\r\n")
        (tmp_path / "product.cbor").write_bytes(b"\x00")
        item = _ThumbItem(_make_image(), png_path.name, path=png_path)
        w._history_list.addItem(item)
        w._history_list.setCurrentItem(item)

        fake_process = MagicMock()
        fake_process.isRunning.return_value = False
        with patch("ui.meteor_tab.CitiesOverlayProcess", return_value=fake_process) as mock_cls:
            w._on_add_cities_overlay()

        mock_cls.assert_called_once()
        fake_process.start.assert_called_once()
        assert not w._btn_cities_overlay.isEnabled()

    def test_on_overlay_ok_adds_to_history_and_shows_it(self, qtbot: QtBot, tmp_path: Path) -> None:
        w = _make_tab(qtbot)
        w._btn_cities_overlay.setEnabled(False)
        out_path = tmp_path / "result_cities.png"
        _make_image().save(str(out_path))

        before_count = w._history_list.count()
        w._on_cities_overlay_ok(str(out_path))

        assert w._btn_cities_overlay.isEnabled()
        assert w._history_list.count() == before_count + 1
        assert w._current_original_image is not None

    def test_on_overlay_err_shows_warning_and_reenables_button(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        w._btn_cities_overlay.setEnabled(False)
        with patch("ui.meteor_tab.QMessageBox.warning") as mock_box:
            w._on_cities_overlay_err("satdump exploded")
        assert w._btn_cities_overlay.isEnabled()
        mock_box.assert_called_once()


class TestCitiesOverlayButtonVisibility:
    """The button (now under the history list) hides itself once a
    cities-overlay image already exists for the selected reception, or
    the selection *is* one -- see _update_cities_overlay_button_visibility().
    """

    def test_visible_when_nothing_selected(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        assert w._btn_cities_overlay.isVisible()

    def test_visible_when_output_does_not_exist_yet(self, qtbot: QtBot, tmp_path: Path) -> None:
        w = _make_tab(qtbot)
        png_path = tmp_path / "msu_mr_rgb_AVHRR_221_False_Color.png"
        png_path.write_bytes(b"\x89PNG\r\n")
        item = _ThumbItem(_make_image(), png_path.name, path=png_path)
        w._history_list.addItem(item)
        w._history_list.setCurrentItem(item)
        assert w._btn_cities_overlay.isVisible()

    def test_hidden_when_output_already_exists(self, qtbot: QtBot, tmp_path: Path) -> None:
        w = _make_tab(qtbot)
        png_path = tmp_path / "msu_mr_rgb_AVHRR_221_False_Color.png"
        png_path.write_bytes(b"\x89PNG\r\n")
        (tmp_path / "msu_mr_rgb_AVHRR_221_False_Color_cities.png").write_bytes(b"\x89PNG\r\n")
        item = _ThumbItem(_make_image(), png_path.name, path=png_path)
        w._history_list.addItem(item)
        w._history_list.setCurrentItem(item)
        assert not w._btn_cities_overlay.isVisible()

    def test_hidden_when_selection_is_itself_a_cities_image(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        w = _make_tab(qtbot)
        png_path = tmp_path / "msu_mr_rgb_AVHRR_221_False_Color_cities.png"
        png_path.write_bytes(b"\x89PNG\r\n")
        item = _ThumbItem(_make_image(), png_path.name, path=png_path)
        w._history_list.addItem(item)
        w._history_list.setCurrentItem(item)
        assert not w._btn_cities_overlay.isVisible()

    def test_hidden_after_overlay_generated(self, qtbot: QtBot, tmp_path: Path) -> None:
        w = _make_tab(qtbot)
        out_path = tmp_path / "result_cities.png"
        _make_image().save(str(out_path))

        w._on_cities_overlay_ok(str(out_path))

        assert not w._btn_cities_overlay.isVisible()

    def test_reappears_after_clear_history(self, qtbot: QtBot, tmp_path: Path) -> None:
        w = _make_tab(qtbot)
        png_path = tmp_path / "msu_mr_rgb_AVHRR_221_False_Color_cities.png"
        png_path.write_bytes(b"\x89PNG\r\n")
        item = _ThumbItem(_make_image(), png_path.name, path=png_path)
        w._history_list.addItem(item)
        w._history_list.setCurrentItem(item)
        assert not w._btn_cities_overlay.isVisible()

        w._on_clear_history()

        assert w._btn_cities_overlay.isVisible()


class TestMouseWheelZoom:
    """Mouse-wheel zoom on the image preview — requested on GitHub Issue #27
    after Flip 180°/Fit Width/Fit Height. Installed as an eventFilter on
    _image_label (see _setup_ui()) rather than overriding QScrollArea's own
    wheelEvent, so the scroll area's default wheel-scroll behavior for an
    overflowing Fit Width/Height image is bypassed only for this widget."""

    def test_wheel_up_zooms_in(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        w._show_image(_make_image(w=100, h=100))
        before = w._image_label.pixmap().width()

        handled = w.eventFilter(w._image_label, _make_wheel_event(120))

        assert handled is True
        assert w._zoom_factor is not None
        assert w._image_label.pixmap().width() > before

    def test_wheel_down_zooms_out(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        w._show_image(_make_image(w=100, h=100))
        before = w._image_label.pixmap().width()

        w.eventFilter(w._image_label, _make_wheel_event(-120))

        assert w._image_label.pixmap().width() < before

    def test_zoom_factor_is_clamped(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        w._show_image(_make_image(w=100, h=100))

        for _ in range(200):
            w.eventFilter(w._image_label, _make_wheel_event(120))
        assert w._zoom_factor is not None
        assert w._zoom_factor <= 10.0

        for _ in range(200):
            w.eventFilter(w._image_label, _make_wheel_event(-120))
        assert w._zoom_factor >= 0.05

    def test_zoom_does_not_affect_events_for_other_widgets(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        w._show_image(_make_image())
        handled = w.eventFilter(w._history_list, _make_wheel_event(120))
        assert handled is False
        assert w._zoom_factor is None

    def test_noop_with_no_image_shown(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        w.eventFilter(w._image_label, _make_wheel_event(120))
        assert w._zoom_factor is None

    def test_showing_a_new_image_resets_zoom(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        w._show_image(_make_image(w=100, h=100))
        w.eventFilter(w._image_label, _make_wheel_event(120))
        assert w._zoom_factor is not None

        w._show_image(_make_image(w=50, h=50))

        assert w._zoom_factor is None

    def test_picking_a_fit_menu_item_resets_zoom(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        w._show_image(_make_image(w=100, h=100))
        w.eventFilter(w._image_label, _make_wheel_event(120))
        assert w._zoom_factor is not None

        _trigger_context_menu_item(w, "fit_both")

        assert w._zoom_factor is None


class TestSaveImageAsDefaultPath:
    """Default save location — moved from the Desktop to the reception's
    own folder under ~/Pictures/fbsat59_meteor/ (GitHub Issue #27: a saved
    image landing on the Desktop was unexpected when everything else
    SatDump writes goes to Pictures)."""

    def test_defaults_to_the_selected_image_s_own_folder(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        w = _make_tab(qtbot)
        reception_dir = tmp_path / "20260829_120000" / "MSU-MR"
        reception_dir.mkdir(parents=True)
        png_path = reception_dir / "msu_mr_rgb_AVHRR_221_False_Color.png"
        png_path.write_bytes(b"\x89PNG\r\n")
        item = _ThumbItem(_make_image(), png_path.name, path=png_path)
        w._history_list.addItem(item)
        w._history_list.setCurrentItem(item)
        w._show_image(_make_image())

        with patch("ui.meteor_tab.QFileDialog.getSaveFileName") as mock_dialog:
            mock_dialog.return_value = ("", "")
            w._on_save_image_as()

        default_path = mock_dialog.call_args.args[2]
        assert Path(default_path).parent == reception_dir
        assert "Desktop" not in default_path

    def test_falls_back_to_pictures_folder_without_a_known_path(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        w._show_image(_make_image())

        with patch("ui.meteor_tab.QFileDialog.getSaveFileName") as mock_dialog:
            mock_dialog.return_value = ("", "")
            w._on_save_image_as()

        default_path = mock_dialog.call_args.args[2]
        assert "fbsat59_meteor" in default_path
        assert "Desktop" not in default_path
