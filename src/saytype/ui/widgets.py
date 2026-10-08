"""Переиспользуемые контролы V5: кнопки, переключатель, поля, шапки, ячейки,
тост, точка уровня микрофона, спиннер, модальное окно, окно чтения текста.
Все значения — из `tokens`.
"""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import (QEasingCurve, QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QApplication, QComboBox, QDialog, QFrame, QGraphicsOpacityEffect,
                               QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSizePolicy,
                               QSpinBox, QVBoxLayout, QWidget)

from .icons import IconButton, icon_pixmap, qicon
from .tokens import Color, Font, Grid, Hatch, Motion, body_font, head_font, qc

# ---------------------------------------------------------------------------
# QSS
# ---------------------------------------------------------------------------
BODY_FONT_QSS = "font-family:'Segoe UI Variable Text','Segoe UI',sans-serif;"

# Подсказка наследует стили цепочки виджетов, у которых она всплыла, и ближайший
# стиль побеждает. Стиль без селектора (`color:…; background:…`) на этой цепочке
# перекрасил бы подсказку — поэтому правило подсказки идёт в такие стили явно.
TOOLTIP_QSS = (f"QToolTip {{ {BODY_FONT_QSS} font-size:{Font.SMALL}px; color:#FFFFFF; "
               f"background:{Color.TOAST_BG}; border:0; padding:6px 9px; }}")

FIELD_QSS = f"""
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {{
    {BODY_FONT_QSS} font-size:{Font.BODY}px; color:{Color.INK}; background:{Color.BG};
    border:1px solid {Color.FIELD_BORDER}; border-radius:0; padding:10px 12px;
    selection-background-color:{Color.TINT}; selection-color:{Color.INK};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{
    border:1px solid {Color.FIELD_FOCUS};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QPlainTextEdit:disabled {{
    color:#9AA1B1; border-color:#E6E9F0;
}}
QComboBox::drop-down {{ border:0; width:28px; }}
QComboBox QAbstractItemView {{
    {BODY_FONT_QSS} font-size:{Font.BODY}px; background:{Color.BG}; color:{Color.INK};
    border:1px solid {Color.FIELD_BORDER}; selection-background-color:{Color.TINT}; selection-color:{Color.INK};
    outline:0; padding:4px;
}}
QSpinBox::up-button, QSpinBox::down-button {{ width:18px; border:0; background:transparent; }}
QScrollBar:vertical {{ background:transparent; width:10px; margin:0; }}
QScrollBar::handle:vertical {{ background:#D7DCE8; min-height:30px; border-radius:0; }}
QScrollBar::handle:vertical:hover {{ background:#C4CBDE; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}
QScrollBar:horizontal {{ height:0; }}
{TOOLTIP_QSS}
"""


# Общий стиль приложения — для окон, которые создаёт не `ui`, а `transcribe_ui.py`
# (QMessageBox, QProgressDialog, диалог обновления, мастер): те же шрифт, цвета,
# кнопки без скруглений. Собственные стили диалогов (по objectName) имеют приоритет.
APP_QSS = FIELD_QSS + f"""
QMessageBox, QProgressDialog, QDialog {{ background:{Color.BG}; color:{Color.INK}; {BODY_FONT_QSS} font-size:{Font.BODY}px; }}
QMessageBox QLabel, QProgressDialog QLabel {{ color:{Color.INK}; {BODY_FONT_QSS} font-size:{Font.BODY}px; }}
QMessageBox QPushButton, QProgressDialog QPushButton, QDialog QPushButton, QDialogButtonBox QPushButton {{
    {BODY_FONT_QSS} font-size:{Font.BTN}px; color:{Color.INK}; background:transparent;
    border:1px solid {Color.BTN_BORDER}; border-radius:0; padding:8px 15px; min-width:80px; }}
QMessageBox QPushButton:hover, QProgressDialog QPushButton:hover, QDialog QPushButton:hover {{
    background:{Color.BTN_BG_HOVER}; border-color:{Color.BTN_BORDER_HOVER}; }}
QMessageBox QPushButton:default, QDialog QPushButton:default {{
    background:{Color.ACCENT}; border-color:{Color.ACCENT}; color:#FFFFFF; }}
QProgressBar {{ background:{Color.PROGRESS_TRACK}; border:0; border-radius:0; height:5px; text-align:center;
    color:transparent; }}
QProgressBar::chunk {{ background:{Color.ACCENT}; }}
QMenu {{ background:{Color.BG}; color:{Color.INK}; border:1px solid {Color.LINE}; padding:6px; {BODY_FONT_QSS} font-size:{Font.BTN}px; }}
QMenu::item {{ padding:8px 18px; }}
QMenu::item:selected {{ background:{Color.TINT}; color:{Color.INK}; }}
QMenu::item:disabled {{ color:#A4AAB8; }}
QMenu::separator {{ height:1px; background:{Color.LINE}; margin:6px 4px; }}
"""


def _btn_qss(variant: str) -> str:
    base = (f"QPushButton{{{BODY_FONT_QSS} font-size:{Font.BTN}px; font-weight:400; color:{Color.INK};"
            f" background:transparent; border:1px solid {Color.BTN_BORDER}; border-radius:0;"
            f" padding:10px 15px; min-height:{Grid.BTN_H - 22}px;}}"
            f"QPushButton:hover{{background:{Color.BTN_BG_HOVER}; border-color:{Color.BTN_BORDER_HOVER};}}"
            f"QPushButton:disabled{{color:#A4AAB8; border-color:#E9ECF3;}}"
            f"QPushButton:focus{{outline:0;}}")
    if variant == "primary":
        base += (f"QPushButton{{background:{Color.ACCENT}; border-color:{Color.ACCENT}; color:#FFFFFF;"
                 f" min-width:{Grid.BTN_PRIMARY_MIN_W - 36}px; min-height:{Grid.BTN_PRIMARY_H - 22}px; padding:10px 18px;}}"
                 f"QPushButton:hover{{background:{Color.ACCENT_HOVER}; border-color:{Color.ACCENT_HOVER};}}"
                 f"QPushButton:disabled{{background:#9AA5F2; border-color:#9AA5F2; color:#FFFFFF;}}")
    elif variant == "ghost":
        base += (f"QPushButton{{border-color:transparent; background:transparent;}}"
                 f"QPushButton:hover{{background:{Color.TINT}; border-color:transparent;}}")
    elif variant == "danger":
        base += f"QPushButton{{color:{Color.DANGER};}}"
    return base


class Button(QPushButton):
    """`.btn` прототипа: default / primary / ghost / danger, модификаторы sm и square."""

    def __init__(self, text: str = "", *, icon: str | None = None, variant: str = "default",
                 small: bool = False, square: bool = False, parent=None):
        super().__init__(text, parent)
        self._variant = variant
        self._icon_name = icon
        self._small = small
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        qss = _btn_qss(variant)
        if small:
            qss += f"QPushButton{{font-size:{Font.BTN_SM}px; padding:5px 10px; min-height:{Grid.BTN_SM_H - 12}px;}}"
        if square:
            qss += f"QPushButton{{padding:0; min-width:{Grid.BTN_SQUARE}px; max-width:{Grid.BTN_SQUARE}px; min-height:{Grid.BTN_SQUARE}px; max-height:{Grid.BTN_SQUARE}px;}}"
        self.setStyleSheet(qss)
        self._apply_icon()

    def _icon_color(self) -> str:
        if self._variant == "primary":
            return "#FFFFFF"
        if self._variant == "danger":
            return Color.DANGER
        return Color.INK

    def _apply_icon(self) -> None:
        if self._icon_name:
            size = 16 if self._small else Grid.ICON
            self.setIcon(qicon(self._icon_name, size, self._icon_color()))
            self.setIconSize(QSize(size, size))
        else:
            from PySide6.QtGui import QIcon
            self.setIcon(QIcon())

    def set_icon_name(self, name: str | None) -> None:
        self._icon_name = name
        self._apply_icon()

    def set_variant(self, variant: str) -> None:
        if variant != self._variant:
            self._variant = variant
            self.setStyleSheet(_btn_qss(variant))
            self._apply_icon()


def companion_icon_button(icon: str, tooltip: str) -> Button:
    """Квадратная кнопка-иконка в рамке рядом с primary («из файла» у «Начать запись»
    и у «Новой заметки»): тот же рост, что у primary, — пара читается одним блоком."""
    c = Grid.BTN_PRIMARY_H - 2  # размер в QSS — без рамки 1 px с каждой стороны
    btn = Button("", icon=icon)
    btn.setStyleSheet(btn.styleSheet() + (
        f"QPushButton{{padding:0; min-width:{c}px; max-width:{c}px; min-height:{c}px; max-height:{c}px;}}"))
    btn.setToolTip(tooltip)
    return btn


class Switch(QAbstractButton):
    """Прямоугольный, как вся сетка: дорожка 38×20 без скруглений, квадратная
    кнопка 14×14 с отступом 3, переезд за 150 мс."""

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setFixedSize(Grid.SWITCH_W, Grid.SWITCH_H)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self._t = 1.0 if checked else 0.0
        self._anim: QVariantAnimation | None = None
        self._kb_focus = False
        self.toggled.connect(self._animate)

    def focusInEvent(self, ev) -> None:  # noqa: N802
        self._kb_focus = ev.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason)
        super().focusInEvent(ev)
        self.update()

    def focusOutEvent(self, ev) -> None:  # noqa: N802
        self._kb_focus = False
        super().focusOutEvent(ev)
        self.update()

    def _animate(self, on: bool) -> None:
        if self._anim:
            self._anim.stop()
        if Motion.reduced():
            self._t = 1.0 if on else 0.0
            self.update()
            return
        a = QVariantAnimation(self)
        a.setStartValue(self._t)
        a.setEndValue(1.0 if on else 0.0)
        a.setDuration(Motion.BTN_MS)
        a.setEasingCurve(QEasingCurve.OutCubic)
        a.valueChanged.connect(self._on_t)
        self._anim = a
        a.start()

    def _on_t(self, v) -> None:
        self._t = float(v)
        self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        bg = QColor(Color.SWITCH_OFF)
        on = QColor(Color.ACCENT)
        t = self._t
        c = QColor(round(bg.red() + (on.red() - bg.red()) * t), round(bg.green() + (on.green() - bg.green()) * t),
                   round(bg.blue() + (on.blue() - bg.blue()) * t))
        if not self.isEnabled():
            c.setAlphaF(0.45)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawRect(QRectF(0, 0, Grid.SWITCH_W, Grid.SWITCH_H))
        inset = (Grid.SWITCH_H - Grid.SWITCH_KNOB) / 2
        travel = Grid.SWITCH_W - Grid.SWITCH_KNOB - 2 * inset
        p.setBrush(QColor("#FFFFFF"))
        p.drawRect(QRectF(inset + travel * t, inset, Grid.SWITCH_KNOB, Grid.SWITCH_KNOB))
        if self._kb_focus:
            pen = QPen(qc(Color.ACCENT), 2)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(1, 1, Grid.SWITCH_W - 2, Grid.SWITCH_H - 2))
        p.end()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(Grid.SWITCH_W, Grid.SWITCH_H)


class Label(QLabel):
    def __init__(self, text: str = "", *, size: int = Font.BODY, color: str = Color.INK, weight: int = 400,
                 letter_spacing: float = 0.0, upper: bool = False, wrap: bool = False, parent=None):
        super().__init__(text, parent)
        f = body_font(size, weight, letter_spacing)
        if upper:
            f.setCapitalization(QFont.Capitalization.AllUppercase)
        self.setFont(f)
        self.set_color(color)
        self.setWordWrap(wrap)
        self.setTextInteractionFlags(Qt.NoTextInteraction)

    def set_color(self, color: str) -> None:
        self.setStyleSheet(f"color:{color}; background:transparent;" + TOOLTIP_QSS)


class Heading(QLabel):
    """h1 страницы — Unbounded 450, капс, letter-spacing −0.9."""

    def __init__(self, text: str, size: int = Font.H1_MAX, parent=None):
        super().__init__(text, parent)
        self.set_size(size)
        self.setStyleSheet(f"color:{Color.INK}; background:transparent;" + TOOLTIP_QSS)

    def set_size(self, size: int) -> None:
        self.setFont(head_font(size))


class LineEdit(QLineEdit):
    def __init__(self, text: str = "", placeholder: str = "", mono: bool = False, parent=None):
        super().__init__(text, parent)
        self.setPlaceholderText(placeholder)
        self.setStyleSheet(FIELD_QSS)
        self.setMinimumHeight(42)


class ComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(FIELD_QSS)
        self.setMinimumHeight(42)


class SpinBox(QSpinBox):
    def __init__(self, lo: int, hi: int, value: int, suffix: str = "", parent=None):
        super().__init__(parent)
        self.setRange(lo, hi)
        self.setValue(value)
        if suffix:
            self.setSuffix(suffix)
        self.setStyleSheet(FIELD_QSS)
        self.setMinimumHeight(42)
        self.setMaximumWidth(130)


class TextEdit(QPlainTextEdit):
    def __init__(self, text: str = "", rows: int = 4, parent=None):
        super().__init__(text, parent)
        self.setStyleSheet(FIELD_QSS)
        fm = QFontMetrics(body_font(Font.BODY))
        self.setFixedHeight(int(fm.lineSpacing() * rows + 24))


# ---------------------------------------------------------------------------
# Ячейки и шапки
# ---------------------------------------------------------------------------
class Cell(QFrame):
    """Белая ячейка сетки с нижней (и опционально правой/левой) линией."""

    def __init__(self, *, bottom: bool = True, right: bool = False, left: bool = False, top: bool = False,
                 fill: bool = True, parent=None):
        super().__init__(parent)
        self._lines = dict(bottom=bottom, right=right, left=left, top=top)
        self._fill = fill
        self.setAttribute(Qt.WA_StyledBackground, False)

    def set_lines(self, **lines) -> None:
        self._lines.update(lines)
        self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        if self._fill:
            p.fillRect(self.rect(), qc(Color.BG))
        p.setPen(QPen(qc(Color.LINE), 1))
        w, h = self.width(), self.height()
        if self._lines["bottom"]:
            p.drawLine(QPointF(0, h - 0.5), QPointF(w, h - 0.5))
        if self._lines["top"]:
            p.drawLine(QPointF(0, 0.5), QPointF(w, 0.5))
        if self._lines["right"]:
            p.drawLine(QPointF(w - 0.5, 0), QPointF(w - 0.5, h))
        if self._lines["left"]:
            p.drawLine(QPointF(0.5, 0), QPointF(0.5, h))
        p.end()


class PageHead(Cell):
    """94 px: h1 слева, слот действия справа; фон прозрачный (направляющие видны)."""

    def __init__(self, title: str, action: QWidget | None = None, parent=None):
        super().__init__(fill=False, parent=parent)
        self.setFixedHeight(Grid.HEAD)
        self.title = Heading(title)
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(Grid.PAD, 0, Grid.PAD, 0)
        self._row.setSpacing(16)
        self._row.addWidget(self.title, 0, Qt.AlignVCenter)
        self._row.addStretch(1)
        self._action_slot = QHBoxLayout()
        self._action_slot.setContentsMargins(0, 0, 0, 0)
        self._action_slot.setSpacing(10)
        self._row.addLayout(self._action_slot)
        if action is not None:
            self.add_action(action)

    def add_action(self, w: QWidget) -> None:
        self._action_slot.addWidget(w, 0, Qt.AlignVCenter)

    def set_window_width(self, w: int) -> None:
        self.title.set_size(Grid.h1_size(w))
        self._row.setContentsMargins(Grid.pad(w), 0, Grid.pad(w), 0)


class SectionHead(Cell):
    """78 px: h2 16px + счётчик + действия справа."""

    def __init__(self, title: str, count: int | None = None, parent=None):
        super().__init__(parent=parent)
        self.setFixedHeight(Grid.SECTION_HEAD)
        row = QHBoxLayout(self)
        row.setContentsMargins(Grid.PAD, 0, Grid.PAD, 0)
        row.setSpacing(10)
        self.title = Label(title, size=Font.H2, letter_spacing=-0.3)
        self.count = Label("" if count is None else str(count), size=11, color=Color.COUNT)
        row.addWidget(self.title, 0, Qt.AlignVCenter)
        row.addSpacing(-3)
        row.addWidget(self.count, 0, Qt.AlignVCenter)
        row.addStretch(1)
        self._actions = QHBoxLayout()
        self._actions.setContentsMargins(0, 0, 0, 0)
        self._actions.setSpacing(10)
        row.addLayout(self._actions)
        self._row = row

    def set_count(self, n: int) -> None:
        self.count.setText(str(n))

    def add_action(self, w: QWidget) -> None:
        self._actions.addWidget(w, 0, Qt.AlignVCenter)

    def set_window_width(self, w: int) -> None:
        self._row.setContentsMargins(Grid.pad(w), 0, Grid.pad(w), 0)


class Field(Cell):
    """`.field`: лейбл 14 px, контрол, подсказка 12 px muted; padding 26/30, min-height 107."""

    def __init__(self, label: str, control: QWidget | None = None, hint: str = "", *, inline: bool = False,
                 parent=None):
        super().__init__(parent=parent)
        self.setMinimumHeight(Grid.FIELD_MIN_H)
        self._inline = inline
        outer = QVBoxLayout(self)
        outer.setContentsMargins(Grid.PAD, 26, Grid.PAD, 26)
        outer.setSpacing(10)
        self.label = Label(label, size=Font.LABEL)
        self.hint = Label(hint, size=Font.HINT, color=Color.MUTED, wrap=True)
        if inline:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(14)
            text = QVBoxLayout()
            text.setContentsMargins(0, 0, 0, 0)
            text.setSpacing(8)
            text.addWidget(self.label)
            text.addWidget(self.hint)
            row.addLayout(text, 1)
            if control is not None:
                row.addWidget(control, 0, Qt.AlignVCenter)
            outer.addLayout(row)
        else:
            outer.addWidget(self.label)
            if control is not None:
                outer.addWidget(control)
            outer.addWidget(self.hint)
        # Видимость — только после вставки в layout: setVisible(True) у виджета
        # без родителя показывает его отдельным окном, и на старте мелькали
        # десятки окошек-подсказок (05.10.2026).
        self.hint.setVisible(bool(hint))
        self._outer = outer
        self.control = control

    def set_hint(self, text: str, color: str = Color.MUTED) -> None:
        self.hint.setText(text)
        self.hint.set_color(color)
        self.hint.setVisible(bool(text))

    def set_window_width(self, w: int) -> None:
        pad = Grid.pad(w)
        self._outer.setContentsMargins(pad, 26, pad, 26)


class ActionField(Field):
    """Лейбл + строка «значение 13 px · кнопка sm» (settingAction)."""

    def __init__(self, label: str, value: str, button_text: str, hint: str = "", *, icon: str | None = None,
                 parent=None):
        row_w = QWidget()
        row = QHBoxLayout(row_w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self.value = Label(value, size=13, wrap=True)
        self.button = Button(button_text, icon=icon, small=True)
        row.addWidget(self.value, 1)
        row.addWidget(self.button, 0, Qt.AlignVCenter)
        super().__init__(label, row_w, hint, parent=parent)


# ---------------------------------------------------------------------------
# Спиннер
# ---------------------------------------------------------------------------
class Spinner(QWidget):
    """Кольцо 15×15, 2 px, верх белый/акцент; крутится только пока виден."""

    def __init__(self, size: int = 15, color: str = "#888888", top: str = "#FFFFFF", parent=None):
        super().__init__(parent)
        self._size = size
        self._color, self._top = color, top
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(60)  # как у прежнего окна: реже — меньше работы GUI-потока во время распознавания
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(size, size)

    def showEvent(self, ev) -> None:  # noqa: N802
        if not Motion.reduced():
            self._timer.start()

    def hideEvent(self, ev) -> None:  # noqa: N802
        self._timer.stop()

    def _tick(self) -> None:
        self._angle = (self._angle + 360 * 60 / Motion.SPINNER_MS) % 360
        self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(1, 1, self._size - 2, self._size - 2)
        p.setPen(QPen(qc(self._color), 2))
        p.drawEllipse(r)
        p.setPen(QPen(qc(self._top), 2))
        p.drawArc(r, int((90 - self._angle) * 16), -90 * 16)
        p.end()


# ---------------------------------------------------------------------------
# Тост и плавающий статус (внутри main)
# ---------------------------------------------------------------------------
class Toast(QWidget):
    """Тост внизу по центру main: #202A4C, 12 px, виден 3400 мс, въезд 200 мс."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._label = Label("", size=Font.SMALL, color="#FFFFFF")
        self._label.setAlignment(Qt.AlignCenter)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 12, 18, 12)
        lay.addWidget(self._label)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._fx = QGraphicsOpacityEffect(self)
        self._fx.setOpacity(0.0)
        self.setGraphicsEffect(self._fx)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)
        self._anim: QVariantAnimation | None = None
        self.hide()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(qc(Color.TOAST_BG))
        p.drawRoundedRect(QRectF(self.rect()), 5, 5)
        p.end()

    def show_text(self, text: str, ms: int = Motion.TOAST_VISIBLE_MS) -> None:
        self._label.setText(text)
        self.adjustSize()
        self._place(0)
        self.show()
        self.raise_()
        self._run(self._fx.opacity(), 1.0, up=True)
        self._hide_timer.start(ms)

    def _place(self, dy: int) -> None:
        pw, ph = self.parentWidget().width(), self.parentWidget().height()
        self.move((pw - self.width()) // 2, ph - self.height() - 24 + dy)

    def _run(self, a: float, b: float, up: bool) -> None:
        if self._anim:
            self._anim.stop()
        if Motion.reduced():
            self._fx.setOpacity(b)
            self._place(0)
            if b == 0.0:
                self.hide()
            return
        anim = QVariantAnimation(self)
        anim.setStartValue(float(a))
        anim.setEndValue(float(b))
        anim.setDuration(Motion.TOAST_MS)
        anim.setEasingCurve(QEasingCurve.OutCubic)

        def step(v):
            v = float(v)
            self._fx.setOpacity(v)
            self._place(int(20 * (1 - v)))

        anim.valueChanged.connect(step)
        if b == 0.0:
            anim.finished.connect(self.hide)
        self._anim = anim
        anim.start()

    def _fade_out(self) -> None:
        self._run(self._fx.opacity(), 0.0, up=False)


# ---------------------------------------------------------------------------
# Уровень микрофона
# ---------------------------------------------------------------------------
LEVEL_FLOOR_DB = -55.0   # тише — точка в покое
LEVEL_CEIL_DB = -18.0    # громче — полный размах
LEVEL_RELEASE = 0.72     # спад за одно обновление (ядро шлёт уровень 20 раз в секунду)


def level_from_rms(rms: float) -> float:
    """RMS 0..1 от ядра → 0..1 по шкале децибел: речь в обычной громкости даёт 0,4–0,9."""
    import math
    if rms <= 1e-6:
        return 0.0
    db = 20.0 * math.log10(rms)
    return max(0.0, min(1.0, (db - LEVEL_FLOOR_DB) / (LEVEL_CEIL_DB - LEVEL_FLOOR_DB)))


class LevelFollower:
    """Огибающая уровня: подъём сразу, спад плавно — точка не мерцает между словами."""

    def __init__(self):
        self.value = 0.0

    def push(self, rms: float) -> float:
        v = max(level_from_rms(rms), self.value * LEVEL_RELEASE)
        self.value = v if v >= 0.02 else 0.0  # без хвоста: тишина — ровно ноль
        return self.value

    def reset(self) -> None:
        self.value = 0.0


class LevelDot(QWidget):
    """Точка «микрофон слышит»: ядро 6 px, ореол растёт с уровнем голоса.

    В покое — ровная точка; во время записи ореол дышит в такт голосу, и видно,
    что говоришь не в пустоту. Перерисовывается только сама точка и только когда
    ядро присылает уровень (во время записи).
    """

    def __init__(self, size: int = 18, color: str = Color.SIGNAL, parent=None):
        super().__init__(parent)
        self._size = size
        self._color = color
        self._level = 0.0
        self._live = False
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def set_live(self, on: bool) -> None:
        """Идёт запись: ореол показывается (в покое — только точка)."""
        self._live = on
        if not on:
            self._level = 0.0
        self.update()

    def set_level(self, level: float) -> None:
        level = max(0.0, min(1.0, level))
        if abs(level - self._level) < 0.02 and (level > 0.0 or self._level == 0.0):
            return
        self._level = level
        self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        c = self._size / 2.0
        core = 3.0
        if self._live:
            halo = core + 1.5 + (c - core - 1.5) * self._level
            p.setBrush(qc(self._color, 0.10 + 0.22 * self._level))
            p.drawEllipse(QPointF(c, c), halo, halo)
        p.setBrush(qc(self._color))
        p.drawEllipse(QPointF(c, c), core, core)
        p.end()


@lru_cache(maxsize=32)
def level_icon(step: int, steps: int = 8, size: int = 16, color: str = "#FFFFFF", dpr: float = 1.0):
    """Иконка кнопки «Завершить»: белая точка, ореол по уровню (шаг 0..steps)."""
    from PySide6.QtGui import QIcon, QPixmap
    px = QPixmap(int(size * dpr), int(size * dpr))
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    c = size / 2.0
    t = step / float(steps)
    halo = 4.0 + (c - 4.0) * t
    p.setBrush(qc(color, 0.18 + 0.30 * t))
    p.drawEllipse(QPointF(c, c), halo, halo)
    p.setBrush(qc(color))
    p.drawEllipse(QPointF(c, c), 3.5, 3.5)
    p.end()
    return QIcon(px)


# ---------------------------------------------------------------------------
# Слой-модалка внутри окна и лист: чтение созвона, правка словаря
# ---------------------------------------------------------------------------
def _card_shadow(card: QWidget) -> None:
    try:
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
        sh = QGraphicsDropShadowEffect(card)
        sh.setBlurRadius(60)
        sh.setOffset(0, 18)
        sh.setColor(QColor(0x14, 0x21, 0x4B, 0x33))
        card.setGraphicsEffect(sh)
    except Exception:  # noqa: BLE001
        pass


class OverlayDialog(QWidget):
    """Диалог слоем внутри главного окна: затемнение на всё окно + карточка по центру.

    Не отдельное модальное окно (как было до 08.10): такое Windows держит над
    заблокированным родителем — окно приложения нельзя было ни растянуть, ни сдвинуть,
    а лист растягивался на всё окно, и на развёрнутом приложении текст не читался.
    Слой — дочерний виджет окна: следует за его размером, а карточка держит свои
    минимум и максимум (`_card_rect`). Затемнение показывает, что открыта модалка, и
    забирает мышь у страницы под ним.

    Интерфейс как у QDialog, чтобы вызывающий код не менялся: `exec()` (свой цикл
    событий до закрытия), `accept()` / `reject()`, `Accepted` / `Rejected`, `finished`.
    """

    Accepted = QDialog.DialogCode.Accepted
    Rejected = QDialog.DialogCode.Rejected
    accepted = Signal()
    rejected = Signal()
    finished = Signal(int)

    CLOSE_ON_BACKDROP = False    # клик по затемнению = «закрыть» (только где нечего потерять)

    def __init__(self, parent=None):
        host = parent.window() if parent is not None else QApplication.activeWindow()
        super().__init__(host)
        self._host = host
        self._result = self.Rejected
        self._loop = None
        self._t = 1.0
        self._anim: QVariantAnimation | None = None
        self.setFocusPolicy(Qt.StrongFocus)
        self.hide()
        self.card = QFrame(self)
        self.card.setObjectName("overlayCard")
        if host is not None:
            host.installEventFilter(self)

    # --- размер и место -------------------------------------------------------
    def _card_rect(self, W: int, H: int) -> QRect:  # переопределяют Sheet / Modal
        return QRect(0, 0, W, H)

    def _place(self) -> None:
        if self._host is None:
            return
        self.setGeometry(self._host.rect())
        r = self._card_rect(self.width(), self.height())
        r.translate(0, round((1.0 - self._t) * Motion.OVERLAY_RISE))
        self.card.setGeometry(r)

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802
        if obj is self._host and self.isVisible():
            if ev.type() == QEvent.Resize:
                self._place()
            elif ev.type() == QEvent.Hide:  # окно ушло в трей — модалка закрывается, цикл exec отпускается
                self.reject()
        return False

    # --- показ ------------------------------------------------------------------
    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        self._result = self.Rejected
        self.raise_()
        self._run_in()
        self.setFocus(Qt.PopupFocusReason)

    def _run_in(self) -> None:
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
        if Motion.reduced():
            self._t = 1.0
            self._place()
            self.update()
            return
        self._t = 0.0
        self._place()
        a = QVariantAnimation(self)
        a.setStartValue(0.0)
        a.setEndValue(1.0)
        a.setDuration(Motion.OVERLAY_MS)
        a.setEasingCurve(Motion.PAGE_CURVE.easing())
        a.valueChanged.connect(self._on_t)
        self._anim = a
        a.start()

    def _on_t(self, v) -> None:
        self._t = float(v)
        self._place()
        self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), qc(Color.SCRIM, Color.SCRIM_ALPHA * self._t))
        p.end()

    # --- ввод -------------------------------------------------------------------
    def mousePressEvent(self, ev) -> None:  # noqa: N802
        # клик мимо карточки страница под слоем не получает
        if self.CLOSE_ON_BACKDROP and not self.card.geometry().contains(ev.position().toPoint()):
            self.reject()
        ev.accept()

    def wheelEvent(self, ev) -> None:  # noqa: N802
        ev.accept()  # колесо над затемнением не крутит страницу под ним

    def keyPressEvent(self, ev) -> None:  # noqa: N802
        if ev.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(ev)

    def closeEvent(self, ev) -> None:  # noqa: N802
        if not self.isHidden():
            self.reject()
        ev.accept()

    # --- интерфейс QDialog ------------------------------------------------------
    def exec(self) -> "QDialog.DialogCode":
        from PySide6.QtCore import QEventLoop
        self.show()
        loop = QEventLoop()
        self._loop = loop
        loop.exec()
        self._loop = None
        return self._result

    def accept(self) -> None:
        self.done(self.Accepted)

    def reject(self) -> None:
        self.done(self.Rejected)

    def done(self, result) -> None:
        if self.isHidden() and self._loop is None:
            return
        self._result = result
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
        self.hide()
        self.finished.emit(int(result.value if hasattr(result, "value") else result))
        (self.accepted if result == self.Accepted else self.rejected).emit()
        if self._loop is not None:
            self._loop.quit()
        if self.testAttribute(Qt.WA_DeleteOnClose):
            self.deleteLater()

    def result(self):
        return self._result


class Sheet(OverlayDialog):
    """Лист с длинным: шапка (заголовок, действия, крестик), тело со своей прокруткой,
    необязательный футер. Esc закрывает.

    Для длинного: созвон на полтора часа и словарь в сотню строк в ячейке
    страницы превращаются в огромную прокрутку, из которой не выйти одним действием.
    Ширина — под чтение (`Grid.SHEET_MAX_W`), высота — до `Grid.SHEET_MAX_H`; в маленьком
    окне лист занимает его за вычетом `Grid.SHEET_MARGIN`.
    """

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        card = self.card
        card.setStyleSheet(f"#overlayCard{{background:{Color.BG}; border:1px solid {Color.MODAL_BORDER};}}")
        _card_shadow(card)
        self._v = QVBoxLayout(card)
        self._v.setContentsMargins(0, 0, 0, 0)
        self._v.setSpacing(0)
        head = QWidget()
        self._head = QHBoxLayout(head)
        self._head.setContentsMargins(Grid.PAD, 16, 16, 16)
        self._head.setSpacing(10)
        self._head.addWidget(Label(title, size=Font.H2, letter_spacing=-0.3), 1)
        self.close_btn = IconButton("close", size=36, icon_size=Grid.ICON, color=Color.ICON_ACTION,
                                    tooltip="Закрыть · Esc")
        self.close_btn.clicked.connect(self.reject)
        self._head.addWidget(self.close_btn, 0, Qt.AlignVCenter)
        self._v.addWidget(head)
        self._v.addWidget(_hline())

    def add_head_action(self, w: QWidget) -> None:
        self._head.insertWidget(self._head.count() - 1, w, 0, Qt.AlignVCenter)

    def set_body(self, w: QWidget) -> None:
        self._v.addWidget(w, 1)

    def add_footer(self, w: QWidget) -> None:
        self._v.addWidget(_hline())
        self._v.addWidget(w)

    def _card_rect(self, W: int, H: int) -> QRect:
        m = Grid.SHEET_MARGIN
        # минимум не больше самого окна: окно 760×560 — лист всё равно целиком на экране
        w = max(min(Grid.SHEET_MIN_W, W - 2 * 8), min(Grid.SHEET_MAX_W, W - 2 * m))
        h = max(min(Grid.SHEET_MIN_H, H - 2 * 8), min(Grid.SHEET_MAX_H, H - 2 * m))
        return QRect((W - w) // 2, (H - h) // 2, w, h)


class TextViewer(Sheet):
    """Полный текст записи только для чтения, с «Копировать». Клик мимо листа закрывает:
    терять тут нечего."""

    CLOSE_ON_BACKDROP = True

    def __init__(self, title: str, html_text: str, plain_text: str, parent=None):
        super().__init__(title, parent)
        from PySide6.QtWidgets import QTextBrowser
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._plain = plain_text
        self.copy_btn = Button("Копировать", icon="copy", small=True)
        self.copy_btn.clicked.connect(self._copy)
        self.add_head_action(self.copy_btn)
        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.setFrameShape(QFrame.NoFrame)
        self.view.setFont(body_font(Font.BODY))
        self.view.setStyleSheet(f"QTextBrowser{{background:{Color.BG}; color:{Color.INK}; border:0;}}" + FIELD_QSS)
        self.view.document().setDocumentMargin(Grid.PAD - 2)
        self.view.setHtml(html_text)
        self.set_body(self.view)

    def _copy(self) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self._plain)
        self.copy_btn.setText("Скопировано")


class TextEditorSheet(Sheet):
    """Правка длинного текста на весь лист: «Готово» отдаёт текст, «Отмена» / Esc — нет.

    `status_fn(text) -> (строка, цвет)` — подпись в футере (счётчик токенов словаря).
    """

    def __init__(self, title: str, text: str, placeholder: str = "", status_fn=None, parent=None):
        super().__init__(title, parent)
        self._status_fn = status_fn
        self.edit = QPlainTextEdit(text)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setFrameShape(QFrame.NoFrame)
        self.edit.setFont(body_font(Font.BODY))
        self.edit.setStyleSheet(
            f"QPlainTextEdit{{border:0; background:{Color.BG}; color:{Color.INK}; padding:22px {Grid.PAD - 4}px;"
            f" selection-background-color:{Color.TINT}; selection-color:{Color.INK};}}" + TOOLTIP_QSS)
        self.set_body(self.edit)
        foot = QWidget()
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(Grid.PAD, 14, 16, 14)
        fl.setSpacing(9)
        self.status = Label("", size=Font.HINT, color=Color.MUTED)
        fl.addWidget(self.status, 1)
        cancel = Button("Отмена")
        cancel.clicked.connect(self.reject)
        ok = Button("Готово", variant="primary")
        ok.setStyleSheet(ok.styleSheet() + f"QPushButton{{min-width:0px; min-height:{Grid.BTN_H - 22}px;}}")
        ok.clicked.connect(self.accept)
        fl.addWidget(cancel)
        fl.addWidget(ok)
        self.add_footer(foot)
        self.edit.textChanged.connect(self._update_status)
        self._update_status()

    def _update_status(self) -> None:
        if self._status_fn is None:
            return
        line, color = self._status_fn(self.edit.toPlainText())
        self.status.setText(line)
        self.status.set_color(color)

    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        self.edit.setFocus()

    def text(self) -> str:
        return self.edit.toPlainText()


# ---------------------------------------------------------------------------
# Модальное окно
# ---------------------------------------------------------------------------
class Modal(OverlayDialog):
    """`.modal`: шапка 20 px + крестик, тело, футер с кнопками справа; radius 9, граница #E1E6F0.
    Слоем внутри окна с затемнением (см. `OverlayDialog`); ширина `width`, высота по содержимому."""

    def __init__(self, title: str, parent=None, width: int = 680):
        super().__init__(parent)
        self._w = width
        self._card = self.card
        self._card.setStyleSheet(
            f"#overlayCard{{background:{Color.BG}; border:1px solid {Color.MODAL_BORDER}; border-radius:9px;}}")
        _card_shadow(self._card)
        v = QVBoxLayout(self._card)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(25, 22, 25, 22)
        self.title = Label(title, size=20, letter_spacing=-0.3)
        hl.addWidget(self.title, 1)
        self.close_btn = IconButton("close", size=36, icon_size=Grid.ICON, color=Color.ICON_ACTION, tooltip="Закрыть")
        self.close_btn.clicked.connect(self.reject)
        hl.addWidget(self.close_btn)
        v.addWidget(head)
        v.addWidget(_hline())
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(25, 23, 25, 23)
        self.body_layout.setSpacing(14)
        v.addWidget(self.body, 1)
        v.addWidget(_hline())
        foot = QWidget()
        self.foot_layout = QHBoxLayout(foot)
        self.foot_layout.setContentsMargins(25, 16, 25, 16)
        self.foot_layout.setSpacing(9)
        self.foot_layout.addStretch(1)
        v.addWidget(foot)
    def add_button(self, btn: QPushButton) -> QPushButton:
        self.foot_layout.addWidget(btn)
        return btn

    def _card_rect(self, W: int, H: int) -> QRect:
        m = Grid.MODAL_MARGIN
        w = min(self._w, 980, W - 2 * m)
        lay = self._card.layout()
        h = self._card.heightForWidth(w) if lay is not None and lay.hasHeightForWidth() else -1
        if h <= 0:
            h = self._card.sizeHint().height()
        h = min(h, H - 2 * m)
        return QRect((W - w) // 2, (H - h) // 2, w, h)


def _hline() -> QFrame:
    f = QFrame()
    f.setFixedHeight(1)
    f.setStyleSheet(f"background:{Color.LINE};")
    return f


def confirm(parent, title: str, text: str, ok_text: str = "Удалить", danger: bool = True) -> bool:
    """Модальное подтверждение в стиле V5. Возвращает True по подтверждению."""
    dlg = Modal(title, parent, width=520)
    dlg.body_layout.addWidget(Label(text, size=Font.BODY, color=Color.MUTED, wrap=True))
    cancel = dlg.add_button(Button("Отмена"))
    ok = dlg.add_button(Button(ok_text, variant="danger" if danger else "primary"))
    cancel.clicked.connect(dlg.reject)
    ok.clicked.connect(dlg.accept)
    return dlg.exec() == QDialog.Accepted


def info_modal(parent, title: str, text: str, ok_text: str = "Понятно") -> None:
    dlg = Modal(title, parent, width=520)
    dlg.body_layout.addWidget(Label(text, size=Font.BODY, color=Color.MUTED, wrap=True))
    ok = dlg.add_button(Button(ok_text, variant="primary"))
    ok.clicked.connect(dlg.accept)
    dlg.exec()
