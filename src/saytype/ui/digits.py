"""Цифры SayType V5: один renderer для большого таймера, мини-часов в истории,
метрик статистики, номеров меню и счётчиков.

Глифы — собственные геометрические контуры прототипа (viewBox 100×140,
обводка 15, square cap, bevel join), не шрифт World Time.

Классы:
  * `DigitStrip`  — статичная строка цифр (дата-время, статистика, номера меню);
  * `DigitReel`   — один анимированный разряд с «выглядывающими» соседями;
  * `ClockGrid`   — большой таймер ЧЧ:ММ:СС с подписями и порогами активации;
  * `MiniClock`   — компактные часы строки истории / плеера (mm:ss, hh:mm:ss).

Движение (см. `tokens.Motion`): двигается только изменившийся разряд; при
старте записи ненулевые разряды сохранённого времени прокручиваются назад к нулю
через промежуточные значения; цвет/прозрачность кадра следуют его положению
относительно центра окна. Перерисовывается только анимируемый разряд.
"""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

from .svgpath import parse_path
from .tokens import Color, Font, Grid, Motion, body_font, head_font_plain, mix, qc

GLYPHS = {
    "0": "M22 16H78Q86 16 86 24V112Q86 120 78 120H22Q14 120 14 112V24Q14 16 22 16M18 111L82 25",
    "1": "M30 35L61 16V120M32 120H88",
    "2": "M14 28Q14 16 26 16H74Q86 16 86 28V48Q86 59 74 63L26 78Q14 82 14 94V120H86",
    "3": "M14 16H74Q86 16 86 28V51Q86 68 68 68H35M68 68Q86 68 86 83V108Q86 120 74 120H14",
    "4": "M71 120V16L14 79H89",
    "5": "M86 16H14V65H73Q86 65 86 79V106Q86 120 72 120H14",
    "6": "M82 16H28Q14 16 14 31V105Q14 120 29 120H72Q86 120 86 106V80Q86 66 72 66H17",
    "7": "M14 16H86L34 120",
    "8": ("M28 16H72Q86 16 86 30V49Q86 66 71 66H29Q14 66 14 50V30Q14 16 28 16"
          "M29 66Q14 66 14 82V104Q14 120 29 120H71Q86 120 86 104V82Q86 66 71 66"),
    "9": "M18 120H72Q86 120 86 105V31Q86 16 71 16H28Q14 16 14 30V56Q14 70 28 70H83",
}
COLON_GLYPH = "M4 45H20V61H4ZM4 85H20V101H4Z"   # viewBox 28×140, заливка
GLYPH_W, GLYPH_H = 100.0, 140.0
COLON_W = 28.0
STROKE = 15.0


@lru_cache(maxsize=16)
def glyph_path(ch: str):
    return parse_path(GLYPHS.get(ch, GLYPHS["0"]))


def draw_digit(p: QPainter, ch: str, box: QRectF, color: QColor, opacity: float = 1.0) -> None:
    """Вписать глиф в бокс с сохранением пропорции 100:140 и центрированием (xMidYMid meet)."""
    scale = min(box.width() / GLYPH_W, box.height() / GLYPH_H)
    if scale <= 0:
        return
    w, h = GLYPH_W * scale, GLYPH_H * scale
    x = box.x() + (box.width() - w) / 2
    y = box.y() + (box.height() - h) / 2
    p.save()
    p.setOpacity(p.opacity() * opacity)
    p.translate(x, y)
    p.scale(scale, scale)
    pen = QPen(color, STROKE)
    pen.setCapStyle(Qt.SquareCap)
    pen.setJoinStyle(Qt.BevelJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawPath(glyph_path(ch))
    p.restore()


def draw_colon_glyph(p: QPainter, box: QRectF, color: QColor) -> None:
    scale = min(box.width() / COLON_W, box.height() / GLYPH_H)
    w, h = COLON_W * scale, GLYPH_H * scale
    p.save()
    p.translate(box.x() + (box.width() - w) / 2, box.y() + (box.height() - h) / 2)
    p.scale(scale, scale)
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawPath(parse_path(COLON_GLYPH))
    p.restore()


# ---------------------------------------------------------------------------
# Статичная строка
# ---------------------------------------------------------------------------
class DigitStrip(QWidget):
    """Строка «цифры + разделители» фиксированной высоты.

    Цифры — глифы; `:` — глиф-двоеточие (как в дате-времени строки истории);
    другие символы (`,`, `×`, `/`) — текстом Unbounded, прижаты к нижней линии.
    Ширина цифры по умолчанию считается от высоты (100:140); `digit_width`
    задаёт её явно, `max_digit_width` ограничивает (метрики статистики: 58 px).
    """

    def __init__(self, text: str = "", *, height: int = 23, color: str = Color.INK,
                 digit_width: float | None = None, max_digit_width: float | None = None,
                 gap: float = 1.0, colon_width: float | None = None, symbol_px: int | None = None,
                 parent=None):
        super().__init__(parent)
        self._text = text
        self._h = height
        self._color = color
        self._dw = digit_width
        self._max_dw = max_digit_width
        self._gap = gap
        self._cw = colon_width
        self._symbol_px = symbol_px
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFixedHeight(height)
        self.setFixedWidth(int(round(self._content_width())))

    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        self.setFixedWidth(int(round(self._content_width())))
        self.update()

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def text(self) -> str:
        return self._text

    def _digit_w(self) -> float:
        w = self._dw if self._dw is not None else self._h * GLYPH_W / GLYPH_H
        if self._max_dw is not None:
            w = min(w, self._max_dw)
        return w

    def _colon_w(self) -> float:
        return self._cw if self._cw is not None else self._h * COLON_W / GLYPH_H

    def _symbol_font(self):
        return head_font_plain(self._symbol_px or max(10, int(self._h * 0.52)), Font.HEAD_WEIGHT, -2.0)

    def _content_width(self) -> float:
        w = 0.0
        fm = None
        for ch in self._text:
            if ch.isdigit():
                w += self._digit_w()
            elif ch == ":":
                w += self._colon_w()
            else:
                from PySide6.QtGui import QFontMetricsF
                fm = fm or QFontMetricsF(self._symbol_font())
                w += fm.horizontalAdvance(ch) + 2
            w += self._gap
        return max(0.0, w - self._gap)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(int(round(self._content_width())), self._h)

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        color = qc(self._color)
        x = 0.0
        h = float(self._h)
        for ch in self._text:
            if ch.isdigit():
                w = self._digit_w()
                draw_digit(p, ch, QRectF(x, 0, w, h), color)
            elif ch == ":":
                w = self._colon_w()
                draw_colon_glyph(p, QRectF(x, 0, w, h), color)
            else:
                from PySide6.QtGui import QFontMetricsF
                f = self._symbol_font()
                fm = QFontMetricsF(f)
                w = fm.horizontalAdvance(ch) + 2
                p.setFont(f)
                p.setPen(color)
                # прижать к базовой линии цифр (нижняя граница глифа ≈ 120/140 высоты)
                baseline = h * (120.0 / 140.0)
                p.drawText(QPointF(x + 2, baseline), ch)
            x += w + self._gap
        p.end()


# ---------------------------------------------------------------------------
# Анимированный разряд
# ---------------------------------------------------------------------------
class DigitReel(QWidget):
    """Один разряд: окно высотой H, кадр высотой D (у секунд D = .64·H, соседи выглядывают).

    Состояние — список кадров `frames` (цифры) и дробная позиция `pos` центра
    окна по индексам кадров. В покое frames = [prev, cur, next], pos = 1.
    Ghost-кадры рисуются с opacity .22; вес цвета кадра w = max(0, 1 − |i − pos|).
    """

    def __init__(self, *, mod: int = 10, small: bool = False, parent=None):
        super().__init__(parent)
        self._mod = mod
        self._small = small
        self._value = 0
        self._frames: list[int] = [mod - 1, 0, 1]
        self._pos = 1.0
        self._active = False
        self._tint = 0.0            # 0 — серый, 1 — активный цвет
        self._window_h = 100
        self._pad_x, self._pad_y = (4, 3) if small else (6, 8)
        self._anim: QVariantAnimation | None = None
        self._tint_anim: QVariantAnimation | None = None
        self._idle_color = Color.DIGIT_IDLE
        self._active_color = Color.DIGIT_ACTIVE
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    # --- параметры ---------------------------------------------------------
    def set_colors(self, idle: str, active: str) -> None:
        self._idle_color, self._active_color = idle, active
        self.update()

    def set_window_height(self, h: int) -> None:
        self._window_h = h
        self.setMinimumHeight(h)
        self.setMaximumHeight(h)
        self.update()

    def frame_height(self) -> float:
        return self._window_h * (0.64 if self._small else 1.0)

    def value(self) -> int:
        return self._value

    # --- цвет --------------------------------------------------------------
    def set_active(self, active: bool, animate: bool = True) -> None:
        if active == self._active and (self._tint == (1.0 if active else 0.0)):
            return
        self._active = active
        target = 1.0 if active else 0.0
        if self._tint_anim is not None:
            self._tint_anim.stop()
            self._tint_anim = None
        if not animate or not self._can_animate():
            self._tint = target
            self.update()
            return
        anim = QVariantAnimation(self)
        anim.setStartValue(float(self._tint))
        anim.setEndValue(target)
        anim.setDuration(Motion.UNIT_COLOR_MS)
        anim.setEasingCurve(Motion.DIGIT_CURVE.easing())
        anim.valueChanged.connect(self._on_tint)
        anim.finished.connect(lambda: setattr(self, "_tint_anim", None))
        self._tint_anim = anim
        anim.start()

    def _on_tint(self, v) -> None:
        self._tint = float(v)
        self.update()

    # --- значение ----------------------------------------------------------
    def _can_animate(self) -> bool:
        """Анимировать только видимое: скрытое окно или неоткрытая страница получают
        значение сразу — без кадров и без таймера анимации (бюджет idle/фона)."""
        return self.isVisible() and not Motion.reduced()

    def _settle(self, value: int) -> None:
        self._value = value % self._mod
        self._frames = [(self._value - 1) % self._mod, self._value, (self._value + 1) % self._mod]
        self._pos = 1.0
        self.update()

    def set_value(self, value: int, animate: bool = True, duration_ms: int | None = None) -> None:
        """Переход к новой цифре: старая уходит вверх, новая входит снизу."""
        value %= self._mod
        if value == self._value and self._anim is None:
            return
        old = self._value
        self._stop_anim()
        if not animate or old == value or not self._can_animate():
            self._settle(value)
            return
        self._frames = [(old - 1) % self._mod, old, value, (value + 1) % self._mod]
        self._value = value
        self._run(1.0, 2.0, duration_ms or Motion.DIGIT_MS, Motion.DIGIT_CURVE, 0)

    def roll_to_zero(self, delay_ms: int = 0) -> None:
        """Старт записи: прокрутить сохранённую цифру назад через все значения к нулю."""
        old = self._value
        self._stop_anim()
        if old == 0 or not self._can_animate():
            self._settle(0)
            return
        # кадры: [ghost(mod-1), 0, 1, …, old, ghost(old+1)]; центр от (old+1) к 1
        self._frames = [self._mod - 1] + list(range(0, old + 1)) + [(old + 1) % self._mod]
        self._value = 0
        self._run(float(old + 1), 1.0, Motion.RESET_MS, Motion.RESET_CURVE, delay_ms)

    def _run(self, start: float, end: float, duration: int, curve, delay_ms: int) -> None:
        anim = QVariantAnimation(self)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setDuration(duration)
        anim.setEasingCurve(curve.easing())
        anim.valueChanged.connect(self._on_pos)
        anim.finished.connect(self._on_finished)
        self._anim = anim
        self._pos = start
        if delay_ms > 0:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(delay_ms, lambda a=anim: a.start() if self._anim is a else None)
        else:
            anim.start()
        self.update()

    def _on_pos(self, v) -> None:
        self._pos = float(v)
        self.update()

    def _on_finished(self) -> None:
        self._anim = None
        self._settle(self._value)

    def _stop_anim(self) -> None:
        if self._anim is not None:
            a = self._anim
            self._anim = None
            a.stop()

    # --- отрисовка ---------------------------------------------------------
    def _frame_color(self, w: float) -> QColor:
        cur = mix(self._idle_color, self._active_color, self._tint)
        return mix(Color.DIGIT_IDLE, cur.name(), w)

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setClipRect(self.rect())
        H = float(self.height())
        D = self.frame_height()
        W = float(self.width())
        top = (H - D) / 2.0
        box_w = max(1.0, W - 2 * self._pad_x)
        box_h = max(1.0, D - 2 * self._pad_y)
        for i, ch in enumerate(self._frames):
            y = top + (i - self._pos) * D
            if y + D < 0 or y > H:
                continue
            w = max(0.0, 1.0 - abs(i - self._pos))
            opacity = 0.22 + 0.78 * w
            color = self._frame_color(w)
            box = QRectF(self._pad_x, y + self._pad_y, box_w, box_h)
            draw_digit(p, str(ch), box, color, opacity)
        p.end()

    def is_animating(self) -> bool:
        return self._anim is not None


# ---------------------------------------------------------------------------
# Большой таймер
# ---------------------------------------------------------------------------
class _ClockUnit(QWidget):
    """Подпись («ЧАСЫ») + окно с двумя разрядами; двоеточие слева у минут/секунд."""

    def __init__(self, label: str, mods: tuple[int, int], small: bool, colon: bool, parent=None):
        super().__init__(parent)
        self._label = label
        self._small = small
        self._colon = colon
        self._color = Color.DIGIT_IDLE
        self._tint = 0.0
        self.tens = DigitReel(mod=mods[0], small=small)
        self.ones = DigitReel(mod=mods[1], small=small)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, Grid.CLOCK_LABEL, 0, 0)
        lay.setSpacing(0)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self.tens, 1)
        row.addWidget(self.ones, 1)
        lay.addLayout(row)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_window_height(self, h: int) -> None:
        self.tens.set_window_height(h)
        self.ones.set_window_height(h)
        self.setFixedHeight(Grid.CLOCK_LABEL + h)

    def set_active(self, on: bool, animate: bool = True) -> None:
        self.tens.set_active(on, animate)
        self.ones.set_active(on, animate)
        self._tint = 1.0 if on else 0.0
        self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        W, H = self.width(), self.height()
        # подпись: белый 85 %, нижняя линия
        p.fillRect(QRectF(0, 0, W, Grid.CLOCK_LABEL), qc(Color.BG, Color.CLOCK_LABEL_ALPHA))
        p.setPen(QPen(qc(Color.LINE), 1))
        p.drawLine(QPointF(0, Grid.CLOCK_LABEL - 0.5), QPointF(W, Grid.CLOCK_LABEL - 0.5))
        p.setFont(body_font(Font.TINY, 400, 0.8))
        p.setPen(qc(Color.MUTED))
        p.drawText(QRectF(18, 0, W - 18, Grid.CLOCK_LABEL), Qt.AlignVCenter | Qt.AlignLeft, self._label.upper())
        p.end()


class ClockGrid(QWidget):
    """ЧЧ:ММ:СС — 2fr 2fr 1fr; часы и минуты крупные, секунды меньше с соседями за маской.

    `set_seconds(total)` двигает только изменившиеся разряды; `roll_to_zero()`
    прокручивает ненулевые разряды сохранённого времени назад к нулю;
    `set_recording(True)` включает цвет по порогам (секунды сразу, минуты с 60,
    часы с 3600), `set_recording(False)` возвращает серый.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hours = _ClockUnit("Часы", (10, 10), False, False)
        self.minutes = _ClockUnit("Минуты", (6, 10), False, True)
        self.seconds = _ClockUnit("Секунды", (6, 10), True, True)
        self._units = [self.hours, self.minutes, self.seconds]
        self._seconds = 0
        self._recording = False
        self._window_h = Grid.CLOCK_MIN
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.hours, 2)
        lay.addWidget(self.minutes, 2)
        lay.addWidget(self.seconds, 1)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.set_window_height(self._window_h)

    def set_window_height(self, h: int) -> None:
        self._window_h = h
        for u in self._units:
            u.set_window_height(h)
        self.setFixedHeight(Grid.CLOCK_LABEL + h)
        self.update()

    def window_height(self) -> int:
        return self._window_h

    @staticmethod
    def _split(total: int) -> list[int]:
        total = max(0, int(total))
        hh, rem = divmod(total, 3600)
        mm, ss = divmod(rem, 60)
        hh = min(hh, 99)
        return [hh // 10, hh % 10, mm // 10, mm % 10, ss // 10, ss % 10]

    def _reels(self) -> list[DigitReel]:
        return [self.hours.tens, self.hours.ones, self.minutes.tens, self.minutes.ones,
                self.seconds.tens, self.seconds.ones]

    def seconds_value(self) -> int:
        return self._seconds

    def set_seconds(self, total: int, animate: bool = True) -> None:
        """Актуальное время; пропущенные секунды не проигрываются очередью."""
        digits = self._split(total)
        self._seconds = max(0, int(total))
        for reel, d in zip(self._reels(), digits):
            if reel.value() != d:
                reel.set_value(d, animate)
        if self._recording:
            self._apply_thresholds()

    def roll_to_zero(self) -> None:
        """Старт записи: обратная прокрутка ненулевых разрядов прошлого времени."""
        self._seconds = 0
        for i, reel in enumerate(self._reels()):
            reel.roll_to_zero(delay_ms=i * Motion.RESET_DELAY_PER_DIGIT_MS)

    def set_recording(self, on: bool, animate: bool = True) -> None:
        self._recording = on
        if on:
            self._apply_thresholds(animate)
        else:
            for u in self._units:
                u.set_active(False, animate)

    def _apply_thresholds(self, animate: bool = True) -> None:
        s = self._seconds
        self.seconds.set_active(True, animate)
        self.minutes.set_active(s >= 60, animate)
        self.hours.set_active(s >= 3600, animate)

    def paintEvent(self, ev) -> None:  # noqa: N802
        """Двоеточия у границ минут и секунд — текстом 600 42px на белой плашке 16 px."""
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        f = body_font(Font.COLON_BIG, 600)
        p.setFont(f)
        y = Grid.CLOCK_LABEL + self._window_h / 2.0
        for unit in (self.minutes, self.seconds):
            x = unit.x()
            color = mix(Color.DIGIT_IDLE, Color.DIGIT_ACTIVE, unit.tens._tint)
            p.fillRect(QRectF(x - 8, y - 26, 16, 52), qc(Color.BG))
            p.setPen(color)
            p.drawText(QRectF(x - 8, y - 26, 16, 52), Qt.AlignCenter, ":")
        p.end()


# ---------------------------------------------------------------------------
# Мини-часы (история, плеер)
# ---------------------------------------------------------------------------
class MiniClock(QWidget):
    """mm:ss или hh:mm:ss высотой 28 px: reel-разряды, двоеточие текстом 500 23px."""

    def __init__(self, *, height: int = 28, color: str = Color.DIGIT_MINI, parent=None):
        super().__init__(parent)
        self._h = height
        self._color = color
        self._show_hours = False
        self._seconds = 0
        self._reels = [DigitReel(mod=10), DigitReel(mod=10), DigitReel(mod=6), DigitReel(mod=10),
                       DigitReel(mod=6), DigitReel(mod=10)]
        for r in self._reels:
            r._pad_x, r._pad_y = 1, 0
            r.set_window_height(height)
            r.set_colors(color, color)
            r.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMaximumWidth(128)
        self._relayout()

    def _relayout(self) -> None:
        for r in self._reels:
            r.setParent(self)
            r.show()
        self.update()

    def set_color(self, color: str, animate: bool = True) -> None:
        self._color = color
        for r in self._reels:
            r.set_colors(r._idle_color, color)
            r.set_active(True, animate)

    def set_show_hours(self, on: bool) -> None:
        if on != self._show_hours:
            self._show_hours = on
            self.update()
            self.resizeEvent(None)

    def set_seconds(self, total: int, animate: bool = True, duration_ms: int | None = None) -> None:
        total = max(0, int(total))
        self._seconds = total
        if total >= 3600 and not self._show_hours:
            self.set_show_hours(True)
        hh, rem = divmod(total, 3600)
        mm, ss = divmod(rem, 60)
        hh = min(hh, 99)
        digits = [hh // 10, hh % 10, mm // 10, mm % 10, ss // 10, ss % 10]
        for r, d in zip(self._reels, digits):
            if r.value() != d:
                r.set_value(d, animate, duration_ms or Motion.MINI_DIGIT_MS)

    def seconds_value(self) -> int:
        return self._seconds

    def sizeHint(self) -> QSize:  # noqa: N802
        # Без подсказки раскладка растягивала часы до maximumWidth и ставила по центру
        # свободного места — в строке истории цифры висели посередине колонки (08.10).
        return QSize(self.maximumWidth(), self._h)

    def resizeEvent(self, ev) -> None:  # noqa: N802
        W = self.width()
        colon_w = W * 0.08
        visible = self._reels if self._show_hours else self._reels[2:]
        n_col = 2 if self._show_hours else 1
        cell = (W - colon_w * n_col) / len(visible)
        x = 0.0
        for i, r in enumerate(self._reels):
            if r not in visible:
                r.setGeometry(0, 0, 0, 0)
                continue
            r.setGeometry(int(x), 0, int(cell), self._h)
            x += cell
            idx = visible.index(r)
            if idx in (1, 3) and idx != len(visible) - 1:
                x += colon_w

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setFont(body_font(Font.COLON_MINI, 500))
        p.setPen(qc(self._color))
        W = self.width()
        colon_w = W * 0.08
        visible = self._reels if self._show_hours else self._reels[2:]
        n_col = 2 if self._show_hours else 1
        cell = (W - colon_w * n_col) / len(visible)
        x = cell * 2
        for _ in range(n_col):
            p.drawText(QRectF(x, 0, colon_w, self._h), Qt.AlignCenter, ":")
            x += colon_w + cell * 2
        p.end()


def fmt_mmss(total: float) -> str:
    total = max(0, int(total))
    hh, rem = divmod(total, 3600)
    mm, ss = divmod(rem, 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
