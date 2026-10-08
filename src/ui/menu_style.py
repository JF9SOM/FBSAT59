"""Fusion style tweak that keeps menu separators visible in dark mode."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QProxyStyle,
    QStyle,
    QStyleOption,
    QStyleOptionMenuItem,
    QWidget,
)


class VisibleSeparatorStyle(QProxyStyle):
    """Draw menu separators with a palette-derived colour visible on dark menus.

    Fusion paints the separator as a slightly darker shade of the menu
    background, which disappears in dark mode.  Blend the text colour into
    the background instead so the line is faint but visible in both modes.
    """

    def drawControl(
        self,
        element: QStyle.ControlElement,
        option: QStyleOption,
        painter: QPainter,
        widget: QWidget | None = None,
    ) -> None:
        """Paint separators ourselves; delegate everything else."""
        if (
            element == QStyle.ControlElement.CE_MenuItem
            and isinstance(option, QStyleOptionMenuItem)
            and option.menuItemType == QStyleOptionMenuItem.MenuItemType.Separator
        ):
            bg = option.palette.window().color()
            fg = option.palette.windowText().color()
            line = QColor(
                (bg.red() * 7 + fg.red() * 3) // 10,
                (bg.green() * 7 + fg.green() * 3) // 10,
                (bg.blue() * 7 + fg.blue() * 3) // 10,
            )
            y = option.rect.center().y()
            painter.save()
            painter.setPen(line)
            painter.drawLine(option.rect.left() + 6, y, option.rect.right() - 6, y)
            painter.restore()
            return
        super().drawControl(element, option, painter, widget)
