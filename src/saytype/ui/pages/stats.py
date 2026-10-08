"""Страница «Статистика» V5: фильтр режима, крупные метрики цифрами-глифами,
контекст (аудио, начало периода, устройство), вкладки «Тренд» / «По длительности»
и график, нарисованный QPainter'ом по разметке прототипа (viewBox 1000×300).

Данные и расчёты — те же, что у прежнего `StatsDialog`: `read_stats_jsonl`
(последние 100 записей + самая первая строка файла), `compute_stats_aggregates`,
фильтр `MODE_FILTERS` / `_filter_by_mode` (записи без `mode` — full), бакеты
`DURATION_BUCKETS`, скользящее среднее `_compute_rolling` (N=5). Файл
перечитывается при показе страницы и при смене фильтра; перерисовка — только при
смене данных, фильтра или вкладки, таймеров нет.

Шкала графика: в прототипе фиксированные 0–20× (демоданные ~10×). На реальном
журнале бывают значения в сотни ×, поэтому верх шкалы — 20× или ближайший
«круглый» шаг ×4 по максимуму сглаженных линий (бакетов); сетка всегда из пяти линий.
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QFontMetrics, QFontMetricsF, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

from ...transcribe_ui_window import (DURATION_BUCKETS, MODE_FILTERS, _bucket_stats, _compute_rolling,
                                     _filter_by_mode, _split_by_mode, compute_stats_aggregates, read_stats_jsonl)
from ..digits import DigitStrip, draw_digit
from ..shell import GuideCanvas, PageScroll
from ..tokens import Color, Font, Grid, Motion, body_font, clamp_vw, qc, tabular
from ..widgets import Cell, Label, PageHead

LAST_N = 100
MODE_COLORS = {"full": Color.FULL, "streaming": Color.STREAMING}
BIN_NAMES = ("<10 с", "10–30 с", "30–60 с", "60+ с")   # подписи прототипа к DURATION_BUCKETS
TABS = (("trend", "Тренд"), ("buckets", "По длительности"))
FOOTNOTE = ("Агрегаты по последним 100 записям из _stats.jsonl. Файл не ротируется — растёт по мере "
            "работы (≈150 байт на транскрипцию).")


# ---------------------------------------------------------------------------
# Форматирование
# ---------------------------------------------------------------------------
def _ru1(v: float) -> str:
    return f"{float(v):.1f}".replace(".", ",")


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _records_word(n: int) -> str:
    return _plural(n, "запись", "записи", "записей")


def _hms(total_sec: float) -> str:
    total = max(0, int(total_sec))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_date(ts) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromisoformat(str(ts)).strftime("%d.%m.%Y")
    except ValueError:
        parts = str(ts)[:10].split("-")
        return f"{parts[2]}.{parts[1]}.{parts[0]}" if len(parts) == 3 else str(ts)


def _device_name(dev: str) -> str:
    head = (dev or "").split("/")[0].strip()
    return head.upper() if head and head.lower() != "unknown" else "Неизвестно"


def _device_text(devices: dict, count: int) -> tuple[str, str]:
    """«CUDA · 100 записей» по самому частому устройству; подсказка — все устройства."""
    if not devices:
        return "—", ""
    dev, n = max(devices.items(), key=lambda x: x[1])
    text = f"{_device_name(dev)} · {n} {_records_word(n)}"
    if n < count:
        text += f" из {count}"
    tip = "\n".join(f"{d}: {c}" for d, c in sorted(devices.items(), key=lambda x: -x[1]))
    return text, tip


def _nice_scale(vmax: float) -> tuple[float, float]:
    """Верх шкалы и шаг сетки (5 линий: 0…4·шаг). До 20× — как в прототипе: 0/5/10/15/20."""
    if vmax <= 20:
        return 20.0, 5.0
    for step in (6, 8, 10, 15, 20, 25, 30, 40, 50, 60, 75, 100, 125, 150, 200, 250, 300, 400, 500, 750, 1000):
        if step * 4 >= vmax:
            return float(step * 4), float(step)
    step = math.ceil(vmax / 4 / 500) * 500
    return float(step * 4), float(step)


def _x_ticks(n: int) -> list[int]:
    if n <= 10:
        return list(range(1, n + 1))
    step = 5 if n <= 25 else 10 if n <= 60 else 20
    return [1] + list(range(step, n + 1, step))


# ---------------------------------------------------------------------------
# Цифры метрики
# ---------------------------------------------------------------------------
class _Readout(DigitStrip):
    """`.stat-readout`: цифры глифами (ширина ≤ 58), «,» и «×» — Unbounded.

    Как `flex: 0 1 58px` в прототипе: если строка не помещается в ячейку,
    цифры сужаются (глиф вписывается в бокс с сохранением пропорции).

    Счётчик: `set_text(…, animate=True)` — каждый разряд прокручивается от нуля
    до своей цифры через промежуточные значения (как сброс таймера, только вверх),
    разряды стартуют с задержкой 20 мс друг за другом. Ширина — по итоговому
    значению с первого кадра: ячейка не дёргается.
    """

    MAX_W = 250

    def __init__(self, parent=None):
        super().__init__("", height=74, color=Color.DIGIT_STAT, max_digit_width=Grid.STAT_DIGIT_MAX_W,
                         gap=0.0, symbol_px=38, parent=parent)
        self._avail: float | None = None
        self._dw_fit: float | None = None
        self._anim: QVariantAnimation | None = None
        self._t = 1.0                 # 0..1 — доля общего времени счёта
        self._easing = Motion.RESET_CURVE.easing()

    def configure(self, height: int, symbol_px: int) -> None:
        self._h = int(height)
        self._symbol_px = int(symbol_px)
        self.setFixedHeight(self._h)
        self._refit()

    def set_text(self, text: str, animate: bool = False, delay_ms: int = 0) -> None:
        self._stop()
        self._text = text
        self._refit()
        if not animate or Motion.reduced() or not any(ch.isdigit() for ch in text):
            return
        self._t = 0.0
        self.update()
        n = sum(1 for ch in text if ch.isdigit())
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(Motion.RESET_MS + Motion.RESET_DELAY_PER_DIGIT_MS * max(0, n - 1))
        anim.valueChanged.connect(self._on_t)
        anim.finished.connect(self._stop)
        self._anim = anim
        if delay_ms > 0:  # переход страницы: счёт начинается, когда она уже на месте
            QTimer.singleShot(delay_ms, lambda a=anim: a.start() if self._anim is a else None)
        else:
            anim.start()

    def _on_t(self, v) -> None:
        self._t = float(v)
        self.update()

    def _stop(self) -> None:
        if self._anim is not None:
            a = self._anim
            self._anim = None
            a.stop()
        self._t = 1.0
        self.update()

    def is_counting(self) -> bool:
        return self._anim is not None

    def _digit_pos(self, index: int, digit: int, total_ms: float) -> float:
        """Дробная позиция разряда: 0 → его цифра, по кривой сброса таймера."""
        if self._t >= 1.0:
            return float(digit)
        local = (self._t * total_ms - index * Motion.RESET_DELAY_PER_DIGIT_MS) / Motion.RESET_MS
        local = max(0.0, min(1.0, local))
        return digit * self._easing.valueForProgress(local)

    def paintEvent(self, ev) -> None:  # noqa: N802
        if self._t >= 1.0:
            super().paintEvent(ev)
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        color = qc(self._color)
        h = float(self._h)
        n = sum(1 for ch in self._text if ch.isdigit())
        total_ms = Motion.RESET_MS + Motion.RESET_DELAY_PER_DIGIT_MS * max(0, n - 1)
        x, i = 0.0, 0
        for ch in self._text:
            if ch.isdigit():
                w = self._digit_w()
                pos = self._digit_pos(i, int(ch), total_ms)
                lo = int(pos)
                frac = pos - lo
                p.save()
                p.setClipRect(QRectF(x, 0, w, h))
                # старая цифра уходит вверх, следующая входит снизу; прозрачность по
                # положению — как у кадров разряда таймера (0,22 у края окна, 1 в центре)
                draw_digit(p, str(lo % 10), QRectF(x, -frac * h, w, h), color, 1.0 - 0.78 * frac)
                if frac > 0.001:
                    draw_digit(p, str((lo + 1) % 10), QRectF(x, (1.0 - frac) * h, w, h), color, 0.22 + 0.78 * frac)
                p.restore()
                i += 1
            else:
                f = self._symbol_font()
                w = QFontMetricsF(f).horizontalAdvance(ch) + 2
                p.setFont(f)
                p.setPen(color)
                p.drawText(QPointF(x + 2, h * (120.0 / 140.0)), ch)
            x += w + self._gap
        p.end()

    def set_avail(self, w: float) -> None:
        if self._avail is None or abs(w - self._avail) >= 0.5:
            self._avail = w
            self._refit()

    def _natural_dw(self) -> float:
        return min(self._h * 100.0 / 140.0, float(Grid.STAT_DIGIT_MAX_W))

    def _parts(self) -> tuple[int, float]:
        fm = QFontMetricsF(self._symbol_font())
        n = sum(1 for ch in self._text if ch.isdigit())
        sym = sum(fm.horizontalAdvance(ch) + 2 for ch in self._text if not ch.isdigit())
        return n, sym

    def natural_width(self) -> float:
        n, sym = self._parts()
        return n * self._natural_dw() + sym

    def _refit(self) -> None:
        n, sym = self._parts()
        dw = self._natural_dw()
        avail = min(self._avail if self._avail is not None else float("inf"), float(self.MAX_W))
        if n and n * dw + sym > avail:
            dw = max(1.0, (avail - sym) / n)
        self._dw_fit = dw
        self.setFixedWidth(max(1, int(round(self._content_width()))))
        self.update()

    def _digit_w(self) -> float:
        dw = getattr(self, "_dw_fit", None)
        return dw if dw is not None else super()._digit_w()


# ---------------------------------------------------------------------------
# Ячейки строк
# ---------------------------------------------------------------------------
class _FrRow(Cell):
    """Строка ячеек по долям (`grid-template-columns: 1fr 2fr …`, нижняя линия).

    Как `minmax(auto, 1fr)` в CSS: колонка не уже своего содержимого
    (`min_content_width()` у ячейки), остаток делится по долям. Высота — по
    самой высокой ячейке при её ширине (учитывает перенос текста).
    """

    def __init__(self, fr: list[float], parent=None):
        super().__init__(bottom=True, parent=parent)
        self._fr = [float(f) for f in fr]
        self._items: list[QWidget] = []
        self.setFixedHeight(60)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def add(self, w: QWidget) -> QWidget:
        w.setParent(self)
        self._items.append(w)
        return w

    def _widths(self, W: float) -> list[float]:
        n = len(self._items)
        fr = self._fr[:n]
        mins = [float(it.min_content_width()) if hasattr(it, "min_content_width") else 0.0 for it in self._items]
        widths = [0.0] * n
        free = list(range(n))
        remaining = float(W)
        while free:
            total = sum(fr[i] for i in free)
            over = [i for i in free if total > 0 and mins[i] > remaining * fr[i] / total]
            if not over:
                break
            for i in over:
                widths[i] = mins[i]
                remaining -= mins[i]
                free.remove(i)
        if remaining < 0:
            # содержимое шире строки — все колонки ужимаются в одной пропорции,
            # чтобы цифры в соседних метриках остались одного размера
            total_min = sum(mins) or 1.0
            return [W * m / total_min for m in mins]
        total = sum(fr[i] for i in free)
        for i in free:
            widths[i] = remaining * fr[i] / total if total else 0.0
        return widths

    def relayout(self) -> None:
        if not self._items:
            return
        W = self.width()
        widths = self._widths(W)
        xs = [0]
        acc = 0.0
        for w in widths:
            acc += w
            xs.append(int(round(acc)))
        heights = []
        for it, x0, x1 in zip(self._items, xs, xs[1:]):
            if hasattr(it, "fit"):
                it.fit(x1 - x0)
            if it.hasHeightForWidth():
                heights.append(it.heightForWidth(x1 - x0))
            else:
                heights.append(it.sizeHint().height())
        h = max(heights) if heights else 0
        for it, x0, x1 in zip(self._items, xs, xs[1:]):
            it.setGeometry(x0, 0, x1 - x0, h)
        if self.height() != h + 1:
            self.setFixedHeight(h + 1)

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        self.relayout()

    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        self.relayout()


class _Metric(Cell):
    """Ячейка `.stats-metrics`: лейбл 11 px muted, через 21 px — цифры."""

    def __init__(self, label: str, last: bool = False, parent=None):
        super().__init__(bottom=False, right=not last, parent=parent)
        self._padx, self._pady = 30, 30
        self.label = Label(label, size=11, color=Color.MUTED)
        self.readout = _Readout()
        self._v = QVBoxLayout(self)
        self._v.setSpacing(21)
        self._v.addWidget(self.label)
        self._v.addWidget(self.readout, 0, Qt.AlignLeft)
        self._apply()

    def _apply(self) -> None:
        self._v.setContentsMargins(self._padx, self._pady, self._padx, self._pady)

    def configure(self, padx: int, pady: int, gap: int, height: int, symbol_px: int) -> None:
        self._padx, self._pady = padx, pady
        self._v.setSpacing(gap)
        self._apply()
        self.readout.configure(height, symbol_px)

    def set_value(self, text: str, animate: bool = False, delay_ms: int = 0) -> None:
        self.readout.set_text(text, animate, delay_ms)
        self.readout.setAccessibleName(f"{self.label.text()}: {text}")

    def min_content_width(self) -> float:
        lw = QFontMetrics(self.label.font()).horizontalAdvance(self.label.text())
        return max(self.readout.natural_width(), lw) + 2 * self._padx

    def fit(self, w: int) -> None:
        self.readout.set_avail(max(0, w - 2 * self._padx))

    def sizeHint(self) -> QSize:  # noqa: N802
        lh = QFontMetrics(self.label.font()).height()
        return QSize(0, self._pady + lh + self._v.spacing() + self.readout.height() + self._pady)


class _ContextItem(Cell):
    """Ячейка `.stats-context`: лейбл 11 px muted, через 8 px — значение 13 px (табличные цифры)."""

    def __init__(self, label: str, last: bool = False, parent=None):
        super().__init__(bottom=False, right=not last, parent=parent)
        self._padx, self._pady = 30, 24
        self.label = Label(label, size=11, color=Color.MUTED)
        self.value = Label("", size=13)
        self.value.setFont(tabular(body_font(13)))
        self._v = QVBoxLayout(self)
        self._v.setSpacing(8)
        self._v.addWidget(self.label)
        self._v.addWidget(self.value)
        self._v.setContentsMargins(self._padx, self._pady, self._padx, self._pady)

    def configure(self, padx: int, pady: int) -> None:
        self._padx, self._pady = padx, pady
        self._v.setContentsMargins(padx, pady, padx, pady)

    def min_content_width(self) -> float:
        fm_l = QFontMetrics(self.label.font())
        fm_v = QFontMetrics(self.value.font())
        return max(fm_l.horizontalAdvance(self.label.text()), fm_v.horizontalAdvance(self.value.text())) \
            + 2 * self._padx

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, self._pady + QFontMetrics(self.label.font()).height() + 8
                     + QFontMetrics(self.value.font()).height() + self._pady)


class _ExplainerItem(Cell):
    """`.stats-explainer p`: padding 23/30, 12 px muted; заголовок — строкой выше, ink."""

    def __init__(self, title: str, text: str, last: bool = False, parent=None):
        super().__init__(bottom=False, right=not last, parent=parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(30, 23, 30, 23)
        v.setSpacing(5)
        v.addWidget(Label(title, size=Font.SMALL, color=Color.INK))
        v.addWidget(Label(text, size=Font.SMALL, color=Color.MUTED, wrap=True))
        self._v = v

    def configure(self, padx: int) -> None:
        self._v.setContentsMargins(padx, 23, padx, 23)


# ---------------------------------------------------------------------------
# Фильтры и вкладки
# ---------------------------------------------------------------------------
class _StripButton(QAbstractButton):
    """Кнопка `.stats-filters` / `.stats-chart-tabs`: 12 px, padding 20/24, правая линия.

    Фильтр: активный — фон accent, белый текст (точка full белеет, streaming — нет).
    Вкладка: активная — текст #343C50 и подчёркивание 2 px #A5AFC0.
    """

    def __init__(self, text: str, *, tab: bool, dot: str | None = None, dot_active: str | None = None,
                 parent=None):
        super().__init__(parent)
        self._text = text
        self._tab = tab
        self._dot = dot
        self._dot_active = dot_active or dot
        self._active = False
        self._padx = 24
        self._kb_focus = False
        self.setText(text)
        self.setFont(body_font(Font.SMALL))
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setAttribute(Qt.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

    def set_active(self, on: bool) -> None:
        if on != self._active:
            self._active = on
            self.update()

    def set_padding(self, padx: int) -> None:
        self._padx = padx

    def natural_width(self) -> int:
        fm = QFontMetrics(self.font())
        return fm.horizontalAdvance(self._text) + (7 + 10 if self._dot else 0) + 2 * self._padx

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.natural_width(), 64)

    def focusInEvent(self, ev) -> None:  # noqa: N802
        self._kb_focus = ev.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason)
        super().focusInEvent(ev)
        self.update()

    def focusOutEvent(self, ev) -> None:  # noqa: N802
        self._kb_focus = False
        super().focusOutEvent(ev)
        self.update()

    def enterEvent(self, ev) -> None:  # noqa: N802
        super().enterEvent(ev)
        self.update()

    def leaveEvent(self, ev) -> None:  # noqa: N802
        super().leaveEvent(ev)
        self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        W, H = self.width(), self.height()
        filled = self._active and not self._tab
        if filled:
            p.fillRect(self.rect(), qc(Color.ACCENT))
        elif self.underMouse():
            p.fillRect(self.rect(), qc(Color.STATS_HOVER))
        else:
            p.fillRect(self.rect(), qc(Color.BG))
        if self._tab and self._active:
            p.fillRect(QRectF(0, H - 2, W, 2), qc(Color.TAB_UNDERLINE))
        p.setPen(QPen(qc(Color.LINE), 1))
        p.drawLine(QPointF(W - 0.5, 0), QPointF(W - 0.5, H))
        if filled:
            color = "#FFFFFF"
        elif self._tab and self._active:
            color = Color.NAV_TEXT_HOVER
        else:
            color = Color.INK
        fm = QFontMetrics(self.font())
        tw = fm.horizontalAdvance(self._text)
        dot_w = 7 + 10 if self._dot else 0
        x = (W - tw - dot_w) / 2
        if self._dot:
            p.setPen(Qt.NoPen)
            p.setBrush(qc(self._dot_active if self._active else self._dot))
            p.drawEllipse(QRectF(x, H / 2 - 3.5, 7, 7))
            x += dot_w
        p.setPen(qc(color))
        p.setFont(self.font())
        p.drawText(QRectF(x, 0, tw + 2, H), Qt.AlignVCenter | Qt.AlignLeft, self._text)
        if self._kb_focus:
            p.setPen(QPen(qc(Color.ACCENT), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(3, 3, W - 7, H - 6))
        p.end()


class _Strip(Cell):
    """`.stats-filters` / `.stats-chart-tabs`: min-height 65, кнопки min-width 20 % (модуль сетки)."""

    def __init__(self, parent=None):
        super().__init__(bottom=True, parent=parent)
        self.setFixedHeight(65)
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 1)
        self._row.setSpacing(0)
        self.buttons: list[_StripButton] = []

    def add_button(self, b: _StripButton) -> _StripButton:
        self._row.addWidget(b)
        self.buttons.append(b)
        return b

    def finish(self, extra: QWidget | None = None) -> None:
        self._row.addStretch(1)
        if extra is not None:
            self._row.addWidget(extra)

    def set_padding(self, padx: int) -> None:
        for b in self.buttons:
            b.set_padding(padx)
        self._fit()

    def _fit(self) -> None:
        W = self.width()
        for i, b in enumerate(self.buttons):
            module = int(round(W * 0.2 * (i + 1))) - int(round(W * 0.2 * i))
            b.setFixedWidth(max(module, b.natural_width()))

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        self._fit()


class _Legend(QWidget):
    """`.chart-legend`: точки режимов 7 px + подпись 11 px; gap 20, padding 20/30."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._modes: list[str] = []
        self._padx = 30
        self.setFont(body_font(11))
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

    def set_modes(self, modes: list[str]) -> None:
        self._modes = list(modes)
        self._resize()
        self.update()

    def set_padding(self, padx: int) -> None:
        self._padx = padx
        self._resize()

    def _resize(self) -> None:
        fm = QFontMetrics(self.font())
        w = sum(7 + 8 + fm.horizontalAdvance(m) for m in self._modes) + 20 * max(0, len(self._modes) - 1)
        self.setFixedWidth(w + 2 * self._padx)

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        fm = QFontMetrics(self.font())
        H = self.height()
        x = float(self._padx)
        p.setFont(self.font())
        for m in self._modes:
            p.setPen(Qt.NoPen)
            p.setBrush(qc(MODE_COLORS.get(m, Color.FULL)))
            p.drawEllipse(QRectF(x, H / 2 - 3.5, 7, 7))
            x += 15
            p.setPen(qc(Color.INK))
            tw = fm.horizontalAdvance(m)
            p.drawText(QRectF(x, 0, tw + 2, H), Qt.AlignVCenter | Qt.AlignLeft, m)
            x += tw + 20
        p.end()


# ---------------------------------------------------------------------------
# График
# ---------------------------------------------------------------------------
class _Plot(QWidget):
    """SVG прототипа `viewBox 0 0 1000 300`, отмасштабированный на ширину; текст 11 #788193.

    Виджет шире области графика на паддинг ячейки (20 px с боков): подписи оси
    («200×») выходят за viewBox влево, как при `overflow: visible` в прототипе.
    """

    VB_W, VB_H = 1000.0, 300.0
    PAD_X = 20

    def __init__(self, parent=None):
        super().__init__(parent)
        self._records: list[dict] = []
        self._mode = "all"
        self._tab = "trend"
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(300)
        self._t = 1.0                    # прогресс заполнения данных 0..1 (1 — график целиком)
        self._anim: QVariantAnimation | None = None
        self._easing = Motion.CHART_CURVE.easing()

    def set_data(self, records: list[dict], mode: str, tab: str,
                 animate: bool = False, delay_ms: int = 0) -> None:
        self._records, self._mode, self._tab = records, mode, tab
        self._stop()
        if animate and not Motion.reduced():
            self._start(delay_ms)
        self.update()

    # --- анимация данных (оси и подписи статичны) ------------------------------
    def _total_ms(self) -> float:
        if self._tab != "buckets":
            return float(Motion.CHART_MS)
        bars = len(DURATION_BUCKETS) * len(self._groups())
        return float(Motion.CHART_MS + Motion.CHART_BAR_DELAY_MS * max(0, bars - 1))

    def _start(self, delay_ms: int) -> None:
        self._t = 0.0
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(int(self._total_ms()))
        anim.valueChanged.connect(self._on_t)
        anim.finished.connect(self._stop)
        self._anim = anim
        if delay_ms > 0:  # переход страницы: график рисуется, когда она уже на месте
            QTimer.singleShot(delay_ms, lambda a=anim: a.start() if self._anim is a else None)
        else:
            anim.start()

    def _on_t(self, v) -> None:
        self._t = float(v)
        self.update()

    def _stop(self) -> None:
        if self._anim is not None:
            a = self._anim
            self._anim = None
            a.stop()
            a.deleteLater()
        self._t = 1.0
        self.update()

    def _ease(self, x: float) -> float:
        return float(self._easing.valueForProgress(max(0.0, min(1.0, x))))

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        h = max(60, int(round((self.width() - 2 * self.PAD_X) * self.VB_H / self.VB_W)))
        if h != self.height():
            self.setFixedHeight(h)

    def _groups(self) -> list[tuple[str, list[dict]]]:
        if self._mode == "all":
            full, streaming = _split_by_mode(self._records)
            return [("full", full), ("streaming", streaming)]
        return [(self._mode, self._records)]

    # --- рисование ----------------------------------------------------------
    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        s = max(1.0, self.width() - 2 * self.PAD_X) / self.VB_W
        p.translate(self.PAD_X, 0)
        p.scale(s, s)
        # 11 единиц viewBox, но на экране не мельче 9 px (на узком окне SVG-текст
        # прототипа уменьшается до ~7 px и перестаёт читаться)
        font = body_font(max(11, int(math.ceil(9 / s))))
        p.setFont(font)
        fm = QFontMetricsF(font)
        if self._tab == "buckets":
            self._paint_buckets(p, fm)
        else:
            self._paint_trend(p, fm)
        p.end()

    @staticmethod
    def _text(p: QPainter, fm: QFontMetricsF, text: str, x: float, y: float, anchor: str = "start") -> None:
        w = fm.horizontalAdvance(text)
        if anchor == "end":
            x -= w
        elif anchor == "middle":
            x -= w / 2
        p.setPen(qc(Color.CHART_AXIS))
        p.drawText(QPointF(x, y), text)

    def _grid(self, p: QPainter, fm: QFontMetricsF, vmax: float) -> float:
        """Пять горизонталей 50→950 и подписи «v×» слева; возвращает масштаб по y."""
        top, step = _nice_scale(vmax)
        k = 200.0 / top
        pen = QPen(qc(Color.LINE), 1)
        pen.setCapStyle(Qt.FlatCap)
        for j in range(5):
            v = step * j
            y = 240 - v * k
            p.setPen(pen)
            p.drawLine(QPointF(50, y), QPointF(950, y))
            label = f"{int(v)}×" if float(v).is_integer() else f"{_ru1(v)}×"
            self._text(p, fm, label, 32, y + 4, "end")
        return k

    def _paint_trend(self, p: QPainter, fm: QFontMetricsF) -> None:
        series = []
        for mode, recs in self._groups():
            raw = [float(r.get("ratio_x", 0)) for r in recs]
            if len(raw) < 2:
                continue
            series.append((mode, raw, _compute_rolling(raw, 5)))
        vmax = max((max(sm) for _, _, sm in series), default=0.0)
        k = self._grid(p, fm, vmax)
        if not series:
            self._text(p, fm, "Недостаточно данных для тренда — нужно хотя бы 2 записи в режиме",
                       500, 145, "middle")
            return
        n = max(len(raw) for _, raw, _ in series)

        def poly(vals: list[float]) -> QPolygonF:
            return QPolygonF([QPointF(50 + i * 900.0 / (n - 1), 240 - v * k) for i, v in enumerate(vals)])

        single = self._mode != "all"
        animating = self._t < 1.0
        if animating:  # линия «рисуется» слева направо: виден только отрезок до фронта
            p.save()
            p.setClipRect(QRectF(0, 0, 50 + 900.0 * self._ease(self._t) + 1.5, self.VB_H))
        for mode, raw, smooth in series:
            if single:
                pen = QPen(qc(Color.CHART_RAW), 1.5)
                pen.setJoinStyle(Qt.MiterJoin)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawPolyline(poly(raw))
            pen = QPen(qc(MODE_COLORS.get(mode, Color.FULL)), 3)
            pen.setJoinStyle(Qt.MiterJoin)
            pen.setCapStyle(Qt.FlatCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPolyline(poly(smooth))
        if animating:
            p.restore()
        for t in _x_ticks(n):
            self._text(p, fm, str(t), 50 + (t - 1) * 900.0 / (n - 1), 268, "middle")

    def _paint_buckets(self, p: QPainter, fm: QFontMetricsF) -> None:
        stats = [(mode, _bucket_stats(recs)) for mode, recs in self._groups()]
        vmax = max((avg for _, st in stats for _, avg, _ in st), default=0.0)
        k = self._grid(p, fm, vmax)
        G = len(stats)
        for i in range(len(DURATION_BUCKETS)):
            center = 162 + i * 225
            for j, (mode, st) in enumerate(stats):
                _name, avg, cnt = st[i]
                x = center + (j - (G - 1) / 2) * 66
                if self._t < 1.0:  # лесенка: каждый следующий столбик стартует чуть позже
                    local = (self._t * self._total_ms() - (i * G + j) * Motion.CHART_BAR_DELAY_MS) / Motion.CHART_MS
                    h = avg * k * self._ease(local)
                else:
                    h = avg * k
                p.fillRect(QRectF(x - 27, 240 - h, 54, h), qc(MODE_COLORS.get(mode, Color.FULL)))
                self._text(p, fm, f"{_ru1(avg)}×", x, 228 - h, "middle")
                self._text(p, fm, f"n={cnt}", x, 263, "middle")
            name = BIN_NAMES[i] if i < len(BIN_NAMES) else DURATION_BUCKETS[i][0]
            self._text(p, fm, name, center, 287, "middle")


# ---------------------------------------------------------------------------
# Страница
# ---------------------------------------------------------------------------
class StatsPage(QWidget):
    """Раздел «Статистика». Контракт окна: `_history_dir_getter()` → папка истории."""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._win = window
        self._mode = "all"
        self._tab = "trend"
        self._chart_delay = 0
        self._recent: list[dict] = []
        self._oldest: dict | None = None

        content = GuideCanvas()
        v = QVBoxLayout(content)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self.head = PageHead("Статистика")
        self.count_label = Label("", size=Font.SMALL, color=Color.MUTED)
        # белая подложка: шапка прозрачная, направляющая не должна резать текст
        self.count_label.setStyleSheet(f"color:{Color.MUTED}; background:{Color.BG}; padding:4px 0;")
        self.count_label.setToolTip(FOOTNOTE)
        self.head.add_action(self.count_label)
        v.addWidget(self.head)

        # 1. фильтр режима
        self.filters = _Strip()
        self._filter_btns: dict[str, _StripButton] = {}
        for key, label in MODE_FILTERS:
            dot = MODE_COLORS.get(key)
            b = _StripButton(label, tab=False, dot=dot, dot_active="#FFFFFF" if key == "full" else dot)
            b.clicked.connect(lambda _=False, k=key: self._set_mode(k))
            self.filters.add_button(b)
            self._filter_btns[key] = b
        self.filters.finish()

        # 2. метрики 1fr 2fr 1fr 1fr
        self.metrics_row = _FrRow([1, 2, 1, 1])
        self.m_count = self.metrics_row.add(_Metric("Записей"))
        self.m_avg = self.metrics_row.add(_Metric("Среднее ускорение"))
        self.m_p50 = self.metrics_row.add(_Metric("Медиана · p50"))
        self.m_p95 = self.metrics_row.add(_Metric("p95", last=True))
        self._metrics = [self.m_count, self.m_avg, self.m_p50, self.m_p95]

        # 3. контекст 2fr 2fr 1fr
        self.context_row = _FrRow([2, 2, 1])
        self.c_audio = self.context_row.add(_ContextItem("Всего аудио"))
        self.c_start = self.context_row.add(_ContextItem("Начало периода"))
        self.c_device = self.context_row.add(_ContextItem("Устройство", last=True))
        self._context = [self.c_audio, self.c_start, self.c_device]

        # 4. вкладки графика + легенда
        self.tabs = _Strip()
        self._tab_btns: dict[str, _StripButton] = {}
        for key, label in TABS:
            b = _StripButton(label, tab=True)
            b.clicked.connect(lambda _=False, k=key: self._set_tab(k))
            self.tabs.add_button(b)
            self._tab_btns[key] = b
        self.legend = _Legend()
        self.tabs.finish(self.legend)

        # 5. график + подпись
        self.plot_cell = Cell()
        pv = QVBoxLayout(self.plot_cell)
        pv.setContentsMargins(0, 25, 0, 30)   # боковые 20 px — внутри `_Plot` (подписи оси)
        pv.setSpacing(10)
        self.plot = _Plot()
        self.caption = Label("", size=Font.TINY, color=Color.MUTED, wrap=True)
        self.caption.setAlignment(Qt.AlignHCenter)
        self.caption.setContentsMargins(_Plot.PAD_X, 0, _Plot.PAD_X, 0)
        pv.addWidget(self.plot)
        pv.addWidget(self.caption)

        # 6. пояснение режимов
        self.explainer = _FrRow([1, 1])
        self.e_full = self.explainer.add(_ExplainerItem("Full", "Обработка после завершения записи."))
        self.e_stream = self.explainer.add(_ExplainerItem("Streaming", "Обработка частями во время записи.",
                                                          last=True))

        # пусто
        self.empty = Cell()
        ev = QVBoxLayout(self.empty)
        ev.setContentsMargins(Grid.PAD, 40, Grid.PAD, 40)
        self.empty_label = Label(
            "Пока нет записей в _stats.jsonl. Сделайте 2–3 диктовки горячей клавишей — здесь появятся "
            "ускорение, медиана и графики.", size=Font.BODY, color=Color.MUTED, wrap=True)
        self.empty_label.setAlignment(Qt.AlignHCenter)
        ev.addWidget(self.empty_label)
        self.empty.hide()

        self._data_widgets = [self.filters, self.metrics_row, self.context_row, self.tabs, self.plot_cell,
                              self.explainer]
        for w in self._data_widgets:
            v.addWidget(w)
        v.addWidget(self.empty)
        v.addStretch(1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(PageScroll(content))
        self.set_window_width(Grid.WINDOW_W)

    # --- контракт страницы ---------------------------------------------------
    def set_window_width(self, w: int) -> None:
        narrow = w <= 850
        self.head.set_window_width(w)
        height = 46 if narrow else int(round(clamp_vw(w, Grid.STAT_READOUT_MIN, Grid.STAT_READOUT_VW,
                                                         Grid.STAT_READOUT_MAX)))
        symbol = int(round(clamp_vw(w, Font.STAT_SYMBOL_MIN, 3.0, Font.STAT_SYMBOL_MAX)))
        for m in self._metrics:
            m.configure(18 if narrow else 30, 24 if narrow else 30, 17 if narrow else 21, height, symbol)
        for c in self._context:
            c.configure(18 if narrow else 30, 20 if narrow else 24)
        for e in (self.e_full, self.e_stream):
            e.configure(18 if narrow else 30)
        self.filters.set_padding(14 if narrow else 24)
        self.tabs.set_padding(14 if narrow else 24)
        self.legend.set_padding(15 if narrow else 30)
        for row in (self.metrics_row, self.context_row, self.explainer):
            row.relayout()

    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        # переход страницы снимает её в начале: счётчик ждёт, пока страница встанет на место
        self.refresh(count_delay_ms=Motion.PAGE_MS)

    def refresh(self, count_delay_ms: int = 0) -> None:
        """Перечитать `_stats.jsonl` и перерисовать; метрики набираются счётчиком, график рисуется."""
        self._chart_delay = count_delay_ms
        self._reload()
        self._render(count_delay_ms)

    # --- данные --------------------------------------------------------------
    def _stats_path(self) -> Path | None:
        try:
            return Path(self._win._history_dir_getter()) / "_stats.jsonl"
        except Exception:  # noqa: BLE001
            return None

    def _reload(self) -> None:
        path = self._stats_path()
        if path is None:
            self._recent, self._oldest = [], None
            return
        self._recent, self._oldest = read_stats_jsonl(path, last_n=LAST_N)

    def _set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        self.refresh()

    def _set_tab(self, tab: str) -> None:
        if tab == self._tab:
            return
        self._tab = tab
        self._chart_delay = 0
        self._render_chart(_filter_by_mode(self._recent, self._mode))

    # --- отрисовка ------------------------------------------------------------
    def _render(self, count_delay_ms: int = 0) -> None:
        has = bool(self._recent)
        for w in self._data_widgets:
            w.setVisible(has)
        self.empty.setVisible(not has)
        n = len(self._recent)
        self.count_label.setText(f"Последние {n} {_records_word(n)}" if has else "")
        if not has:
            return
        for key, b in self._filter_btns.items():
            b.set_active(key == self._mode)
        records = _filter_by_mode(self._recent, self._mode)
        agg = compute_stats_aggregates(records, self._oldest)
        count = self.isVisible()  # скрытая страница считать не должна: таймеров вне экрана нет
        self.m_count.set_value(str(agg["count"]), count, count_delay_ms)
        self.m_avg.set_value(f"{_ru1(agg['avg_ratio'])}×", count, count_delay_ms)
        self.m_p50.set_value(f"{_ru1(agg['p50_ratio'])}×", count, count_delay_ms)
        self.m_p95.set_value(f"{_ru1(agg['p95_ratio'])}×", count, count_delay_ms)
        self.c_audio.value.setText(_hms(agg["sum_duration_sec"]))
        self.c_audio.value.setToolTip(f"{int(agg['sum_duration_sec'])} сек")
        self.c_start.value.setText(_fmt_date(agg["oldest_ts"]))
        self.c_start.value.setToolTip(f"Первая запись в журнале: {agg['oldest_ts']}" if agg["oldest_ts"] else "")
        dev_text, dev_tip = _device_text(agg["devices"], agg["count"])
        self.c_device.value.setText(dev_text)
        self.c_device.value.setToolTip(dev_tip)
        self.metrics_row.relayout()
        self.context_row.relayout()
        self._render_chart(records)

    def _render_chart(self, records: list[dict]) -> None:
        for key, b in self._tab_btns.items():
            b.set_active(key == self._tab)
        self.legend.set_modes(["full", "streaming"] if self._mode == "all" else [self._mode])
        self.plot.set_data(records, self._mode, self._tab, self.isVisible(), self._chart_delay)
        if self._tab == "trend":
            cap = "Номер записи внутри режима · среднее по 5 записям"
            if self._mode != "all":
                cap += " · серая линия — отдельные записи"
        else:
            cap = "Длительность аудио · n — количество записей"
        self.caption.setText(cap)
