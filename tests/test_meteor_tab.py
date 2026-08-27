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
from PySide6.QtGui import QImage
from pytestqt.qtbot import QtBot

from ui.meteor_tab import MeteorTab, _ThumbItem


def _make_image(w: int = 40, h: int = 20, fill: int = 0xFFFFFFFF) -> QImage:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(fill)
    return img


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


class TestFlip180:
    def test_default_is_not_rotated(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        assert w._image_rotated is False
        img = _make_image()
        assert w._apply_rotation(img) is img

    def test_toggle_rotates_the_displayed_image(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        w._show_image(_make_image())
        assert w._image_rotated is False

        w._btn_flip.setChecked(True)

        assert w._image_rotated is True
        rotated = w._apply_rotation(w._current_original_image)
        assert rotated.pixelColor(0, 0) == w._current_original_image.pixelColor(
            w._current_original_image.width() - 1, w._current_original_image.height() - 1
        )

    def test_toggle_off_restores_original_orientation(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        w._show_image(_make_image())
        w._btn_flip.setChecked(True)
        w._btn_flip.setChecked(False)
        assert w._image_rotated is False
        assert w._apply_rotation(w._current_original_image) is w._current_original_image


class TestFitModeExclusivity:
    def test_fit_width_and_height_are_mutually_exclusive(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        w._show_image(_make_image())

        w._btn_fit_width.setChecked(True)
        assert w._fit_mode == "width"
        assert not w._btn_fit_height.isChecked()

        w._btn_fit_height.setChecked(True)
        assert w._fit_mode == "height"
        assert not w._btn_fit_width.isChecked()

        w._btn_fit_height.setChecked(False)
        assert w._fit_mode == "fit"

    def test_default_fit_mode_scales_to_fit_both_dimensions(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        # A very wide image in a roughly-square viewport: "fit" (both) must
        # not exceed the viewport in either dimension.
        w._show_image(_make_image(w=2000, h=100))
        viewport = w._image_scroll.viewport().size()
        pixmap = w._image_label.pixmap()
        assert not pixmap.isNull()
        assert pixmap.width() <= viewport.width()
        assert pixmap.height() <= viewport.height()

    def test_fit_width_matches_viewport_width_and_may_overflow_height(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        w._btn_fit_width.setChecked(True)
        # A tall, narrow image: fitting its width to a wide-ish viewport
        # should make the resulting height far exceed the viewport (which
        # is exactly why _image_scroll needs to allow scrolling).
        w._show_image(_make_image(w=100, h=3000))
        viewport = w._image_scroll.viewport().size()
        pixmap = w._image_label.pixmap()
        assert not pixmap.isNull()
        assert pixmap.width() == viewport.width()
        assert pixmap.height() > viewport.height()

    def test_fit_height_matches_viewport_height_and_may_overflow_width(self, qtbot: QtBot) -> None:
        w = _make_tab(qtbot)
        w._btn_fit_height.setChecked(True)
        w._show_image(_make_image(w=3000, h=100))
        viewport = w._image_scroll.viewport().size()
        pixmap = w._image_label.pixmap()
        assert not pixmap.isNull()
        assert pixmap.height() == viewport.height()
        assert pixmap.width() > viewport.width()


class TestSaveImageAs:
    def test_saves_the_rotated_image(self, qtbot: QtBot, tmp_path: Path) -> None:
        w = _make_tab(qtbot)
        w._show_image(_make_image())
        w._btn_flip.setChecked(True)

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
