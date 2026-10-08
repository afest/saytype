"""Контурные иконки V5 и знак SayType — рисуются из SVG-путей прототипа.

Все иконки: viewBox 0 0 24 24, обводка `currentColor`, без заливки,
`stroke-linecap: square`, `stroke-linejoin: miter`. Пиксмапы кэшируются по
(имя, размер, цвет, толщина, DPR) — на масштабе 125–200 % остаются резкими.
"""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QToolButton

from .svgpath import parse_path
from .tokens import Color, Grid, qc

# Иконки меню — «прямые соединения», stroke 1.65
NAV_ICONS = {
    "mic": "M9 3H15V13L13 15H11L9 13ZM6 10V14L9 18H15L18 14V10M12 18V22M8 22H16",
    "phone": "M4 3H8L10 8L7 11L13 17L16 14L21 16V20L19 22H16L8 17L2 9V5Z",
    "note": "M5 2H15L20 7V22H5ZM15 2V8H20M8 12H16M8 16H16",
    "chip": "M6 6H18V18H6ZM9 9H15V15H9ZM9 2V6M15 2V6M9 18V22M15 18V22M2 9H6M2 15H6M18 9H22M18 15H22",
    "chart": "M4 3V21H22M9 16V10M14 16V5M19 16V8",
    "settings": "M9 2H15L16 5L19 6L22 11V13L19 18L16 19L15 22H9L8 19L5 18L2 13V11L5 6L8 5ZM12 8L16 12L12 16L8 12Z",
    "info": "M4 3H20V21H4ZM12 10V17M12 6V7",
}

# Общий набор, stroke 1.7
ICONS = {
    "home": "M3 10 12 3l9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1Z",
    "mic": "M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0ZM5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8",
    "phone": "M5 3h4l2 5-3 2a15 15 0 0 0 6 6l2-3 5 2v4a2 2 0 0 1-2 2C10 21 3 14 3 5a2 2 0 0 1 2-2Z",
    "upload": "M12 16V3M7 8l5-5 5 5M4 15v5a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-5",
    "note": "M5 3h10l4 4v14H5ZM14 3v5h5M8 12h8M8 16h6",
    "chip": "M7 7h10v10H7ZM10 10h4v4h-4ZM9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4",
    "chart": "M4 3v17h17M9 15V9M14 15V5M19 15v-4",
    "settings": ("M19.518 9.264L21.781 9.921L21.781 14.079L19.518 14.736L19.250 15.381L20.387 17.446"
                 "L17.446 20.387L15.381 19.250L14.736 19.518L14.079 21.781L9.921 21.781L9.264 19.518"
                 "L8.619 19.250L6.554 20.387L3.613 17.446L4.750 15.381L4.482 14.736L2.219 14.079"
                 "L2.219 9.921L4.482 9.264L4.750 8.619L3.613 6.554L6.554 3.613L8.619 4.750L9.264 4.482"
                 "L9.921 2.219L14.079 2.219L14.736 4.482L15.381 4.750L17.446 3.613L20.387 6.554"
                 "L19.250 8.619ZM15 12a3 3 0 1 1-6 0a3 3 0 0 1 6 0"),
    "info": "M12 8h.01M12 11v6M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
    "shield": "M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6ZM8 12l3 3 5-6",
    "flask": "M9 3h6M10 3v6L4 19a1 1 0 0 0 1 2h14a1 1 0 0 0 1-2L14 9V3M7 15h10",
    "arrow": "M5 12h14M14 7l5 5-5 5",
    "chevron": "M9 5l7 7-7 7",
    "copy": "M9 8h11v13H9ZM15 8V3H4v13h5",
    "play": "M8 4l12 8-12 8Z",
    "pause": "M8 4v16M16 4v16",
    "stop": "M6 6h12v12H6Z",
    "close": "M6 6l12 12M6 18 18 6",
    "check": "M5 12l4 4L19 6",
    "plus": "M12 5v14M5 12h14",
    "download": "M12 3v13M7 11l5 5 5-5M4 17v4h16v-4",
    "folder": "M3 6h7l2 3h9v11H3ZM3 6V4h7l2 2h7v3",
    "trash": "M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7",
    "headphones": "M4 16v-5a8 8 0 0 1 16 0v5M4 13h3v8H5a1 1 0 0 1-1-1ZM20 13h-3v8h2a1 1 0 0 0 1-1Z",
    "clock": "M12 7v5l3 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
    "back": "M19 12H5M10 7l-5 5 5 5",
    "heart": "M20 5c-3-3-6-1-8 1-2-2-5-4-8-1-5 5 8 15 8 15s13-10 8-15Z",
    "bolt": "M13 2 4 14h7l-1 8 10-13h-7Z",
    "wifi": "M3 8a15 15 0 0 1 18 0M6 12a10 10 0 0 1 12 0M9 16a5 5 0 0 1 6 0M12 20h.01",
    "volume": "M11 4 6 8H3v8h3l5 4ZM15 8a6 6 0 0 1 0 8M18 5a10 10 0 0 1 0 14",
    "chevronUp": "m7 14 5-5 5 5",
    "cursor": "M8 3h8M12 3v18M8 21h8",
    "error": "M12 8v5M12 17h.01M12 3 2 21h20Z",
    "moon": "M20 15A9 9 0 0 1 9 4a9 9 0 1 0 11 11Z",
    "sun": "M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5",
    "expand": "M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5",
}

MARK_PATH = "M82 17H32Q17 17 17 32V36Q17 50 32 50H68Q83 50 83 65V68Q83 83 68 83H18"
MARK_CUT = "M-4 104L104-4"


def _path_for(name: str, nav: bool):
    table = NAV_ICONS if nav else ICONS
    return parse_path(table.get(name) or ICONS["note"])


@lru_cache(maxsize=1024)
def icon_pixmap(name: str, size: int = Grid.ICON, color: str = Color.INK,
                stroke: float | None = None, nav: bool = False, dpr: float = 1.0) -> QPixmap:
    """Контурная иконка нужного размера и цвета, резкая на любом DPR."""
    if stroke is None:
        stroke = Grid.ICON_NAV_STROKE if nav else Grid.ICON_STROKE
    px = QPixmap(int(round(size * dpr)), int(round(size * dpr)))
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)
    scale = size / 24.0
    p.scale(scale, scale)
    pen = QPen(qc(color), stroke)
    pen.setCapStyle(Qt.SquareCap)
    pen.setJoinStyle(Qt.MiterJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawPath(_path_for(name, nav))
    p.end()
    return px


def qicon(name: str, size: int = Grid.ICON, color: str = Color.INK, nav: bool = False,
          dpr: float = 2.0) -> QIcon:
    """QIcon с запасом по DPR (2×), чтобы Qt масштабировал вниз, а не вверх."""
    ic = QIcon()
    ic.addPixmap(icon_pixmap(name, size, color, None, nav, dpr))
    return ic


@lru_cache(maxsize=64)
def mark_pixmap(size: int, color: str = Color.MARK, dpr: float = 1.0) -> QPixmap:
    """Знак — квадратная S с диагональным вырезом (маска из mark.svg)."""
    px = QPixmap(int(round(size * dpr)), int(round(size * dpr)))
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)
    s = size / 100.0
    p.scale(s, s)
    pen = QPen(qc(color), 17)
    pen.setCapStyle(Qt.SquareCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawPath(parse_path(MARK_PATH))
    # вырез: та же геометрия, что у SVG-маски — линия толщиной 7 из (-4,104) в (104,-4)
    p.setCompositionMode(QPainter.CompositionMode_DestinationOut)
    cut = QPen(QColor(0, 0, 0, 255), 7)
    cut.setCapStyle(Qt.FlatCap)
    p.setPen(cut)
    p.drawPath(parse_path(MARK_CUT))
    p.end()
    return px


# Размеры, из которых Windows выбирает картинку: трей 16/20/24/32 на масштабе
# 100–200 %, заголовок окна и панель задач — до 48, Alt+Tab и ярлык — крупнее.
APP_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


@lru_cache(maxsize=64)
def app_tile_pixmap(size: int, bg: str = Color.APP_TILE, dpr: float = 1.0) -> QPixmap:
    """Иконка приложения: белый знак на цветной плитке со скруглением.

    Плитка, а не голый знак: тёмная S терялась бы на тёмной панели задач, а
    цвет плитки в трее показывает состояние (покой / запись / распознавание).
    """
    px = QPixmap(int(round(size * dpr)), int(round(size * dpr)))
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(qc(bg))
    radius = size * Grid.APP_TILE_RADIUS
    p.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)
    inner = max(6, int(round(size * Grid.APP_TILE_MARK)))
    off = (size - inner) / 2.0
    p.drawPixmap(QPointF(off, off), mark_pixmap(inner, Color.APP_TILE_MARK, dpr))
    p.end()
    return px


@lru_cache(maxsize=8)
def app_qicon(bg: str = Color.APP_TILE) -> QIcon:
    """QIcon со всеми размерами плитки — для окна, трея и диалогов."""
    ic = QIcon()
    for size in APP_ICON_SIZES:
        ic.addPixmap(app_tile_pixmap(size, bg))
    return ic


class IconButton(QToolButton):
    """Квадратная кнопка-иконка без рамки: цвет покоя / hover / нажатого («играет»)."""

    def __init__(self, name: str, *, size: int = Grid.BTN_SQUARE, icon_size: int = Grid.ICON,
                 color: str = Color.ICON_ACTION, hover_color: str = Color.ACCENT,
                 hover_bg: str = Color.TINT, nav: bool = False, tooltip: str = "", parent=None):
        super().__init__(parent)
        self._name = name
        self._color = color
        self._hover_color = hover_color
        self._hover_bg = hover_bg
        self._nav = nav
        self._icon_size = icon_size
        self._pressed_state = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.setAutoRaise(True)
        self.setFocusPolicy(Qt.TabFocus)
        if tooltip:
            self.setToolTip(tooltip)
        self.setStyleSheet("QToolButton{border:0;background:transparent;}")
        self._kb_focus = False

    def focusInEvent(self, ev) -> None:  # noqa: N802
        self._kb_focus = ev.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason)
        super().focusInEvent(ev)
        self.update()

    def focusOutEvent(self, ev) -> None:  # noqa: N802
        self._kb_focus = False
        super().focusOutEvent(ev)
        self.update()

    def set_icon_name(self, name: str) -> None:
        self._name = name
        self.update()

    def set_pressed_state(self, on: bool) -> None:
        """aria-pressed: кнопка «играет» показывается акцентом."""
        self._pressed_state = on
        self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        hover = self.underMouse() or self._kb_focus
        if (hover or self._pressed_state) and self.isEnabled():
            if hover:
                p.fillRect(self.rect(), qc(self._hover_bg))
            color = self._hover_color
        else:
            color = self._color
        dpr = self.devicePixelRatioF()
        px = icon_pixmap(self._name, self._icon_size, color, None, self._nav, dpr)
        x = (self.width() - self._icon_size) / 2
        y = (self.height() - self._icon_size) / 2
        if not self.isEnabled():
            p.setOpacity(0.25)
        p.drawPixmap(QPointF(x, y), px)
        if self._kb_focus:
            pen = QPen(qc(Color.ACCENT), 2)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(1, 1, self.width() - 2, self.height() - 2))
        p.end()

    def enterEvent(self, ev) -> None:  # noqa: N802
        super().enterEvent(ev)
        self.update()

    def leaveEvent(self, ev) -> None:  # noqa: N802
        super().leaveEvent(ev)
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.width(), self.height())
