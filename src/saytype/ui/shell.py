"""Оболочка V5: sidebar с меню и контейнер страниц с переходом «накрывания».

* `NavButton` — пункт меню: контурная иконка, подпись, счётчик, декоративный
  SVG-номер, обрезанный нижней границей; hover въезжает штриховкой слева,
  активный — штриховка + серый текст + inset-полоса 2 px.
* `Sidebar` — бренд (знак + wordmark), список, нижний блок; всегда компактный
  (64 px, только иконки), `Grid.SIDEBAR_ALWAYS_COMPACT`. Название раздела при
  наведении выдвигается вкладкой `_NavFlyout` — продолжением пункта, без подсказки.
* `PageHost` — страницы одна поверх другой; переход рисует слой `_Transition`
  из двух снимков (уходящая и новая страница), а живая новая страница уже стоит
  под ним: кадр анимации — две картинки, а не перерисовка всей страницы.
  Направление одно для всех переходов (`Motion.PAGE_DIRECTION`), 650 мс
  cubic-bezier(.22,.8,.22,1); повторный клик отменяет переход.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractButton, QApplication, QHBoxLayout, QScrollArea, QSizePolicy, QVBoxLayout,
                               QWidget)

from .digits import draw_digit
from .icons import icon_pixmap, mark_pixmap
from .tokens import Color, Font, Grid, Hatch, Motion, body_font, head_font_plain, qc


class NavButton(QAbstractButton):
    hovered = Signal(bool)   # компактное меню: AppShell выдвигает вкладку с названием

    def __init__(self, page_id: str, label: str, icon: str, index: int, parent=None):
        super().__init__(parent)
        self.page_id = page_id
        self._label = label
        self._icon = icon
        self._index = index
        self._count: int | None = None
        self._active = False
        self._compact = False
        self._hover_t = 0.0
        self._icon_t = 0.0
        self._anim: QVariantAnimation | None = None
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setMinimumHeight(Grid.NAV_ITEM)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFont(body_font(Font.NAV, 400, 0.15))
        self.setAttribute(Qt.WA_Hover, True)
        self._kb_focus = False

    def focusInEvent(self, ev) -> None:  # noqa: N802
        self._kb_focus = ev.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason)
        super().focusInEvent(ev)
        self.update()

    def focusOutEvent(self, ev) -> None:  # noqa: N802
        self._kb_focus = False
        super().focusOutEvent(ev)
        self.update()

    def set_count(self, n: int | None) -> None:
        self._count = n
        self.update()

    def set_active(self, on: bool) -> None:
        self._active = on
        self.setChecked(on)
        self.update()

    def set_compact(self, on: bool) -> None:
        self._compact = on
        self.setAccessibleName(self._label)  # подпись в компактном меню — вкладкой `_NavFlyout`, не подсказкой
        self.update()

    def label(self) -> str:
        return self._label

    def is_active(self) -> bool:
        return self._active

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(100, Grid.NAV_ITEM)

    # hover-анимация: штриховка въезжает слева (420 мс), иконка сдвигается на 2 px
    def enterEvent(self, ev) -> None:  # noqa: N802
        super().enterEvent(ev)
        self._run(1.0)
        if self._compact:
            self.hovered.emit(True)

    def leaveEvent(self, ev) -> None:  # noqa: N802
        super().leaveEvent(ev)
        self._run(0.0)
        if self._compact:
            self.hovered.emit(False)

    def _run(self, target: float) -> None:
        if self._anim:
            self._anim.stop()
        if Motion.reduced():
            self._hover_t = target
            self.update()
            return
        a = QVariantAnimation(self)
        a.setStartValue(self._hover_t)
        a.setEndValue(target)
        a.setDuration(Motion.NAV_HATCH_MS)
        a.setEasingCurve(Motion.NAV_HATCH_CURVE.easing())
        a.valueChanged.connect(self._on_t)
        self._anim = a
        a.start()

    def _on_t(self, v) -> None:
        self._hover_t = float(v)
        self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        W, H = self.width(), self.height()
        p.setClipRect(self.rect())
        t = self._hover_t
        # фон
        if self._active:
            p.fillRect(self.rect(), qc(Color.NAV_BG_ACTIVE))
        elif t > 0:
            bg = QColor(Color.NAV_BG_HOVER)
            bg.setAlphaF(t)
            p.fillRect(self.rect(), bg)
        # штриховка
        dpr = self.devicePixelRatioF()
        if self._active:
            p.save()
            p.setOpacity(0.8)
            p.fillRect(self.rect(), Hatch.brush(dpr))
            p.restore()
        elif t > 0:
            p.save()
            p.setOpacity(0.55 * min(1.0, t * 1.4))
            p.translate(-W * (1.0 - t), 0)
            p.fillRect(QRect(0, 0, W, H), Hatch.brush(dpr))
            p.restore()
        # inset-полоса у активного
        if self._active:
            p.fillRect(QRectF(0, 0, 2, H), qc(Color.NAV_INSET))
        # декоративный номер (не в компактном режиме)
        if not self._compact:
            idx_color = QColor(Color.NAV_INDEX_ACTIVE if self._active else Color.NAV_INDEX)
            idx_color.setAlphaF(0.26 if self._active else 0.33)
            x0 = W - Grid.NAV_INDEX_RIGHT - Grid.NAV_INDEX_W
            y0 = H - Grid.NAV_INDEX_BOTTOM - Grid.NAV_INDEX_H  # bottom:-24 → верх блока = H + 24 − 64
            cell_w = Grid.NAV_INDEX_W / 2.0
            s = f"{self._index:02d}"
            for i, ch in enumerate(s):
                box = QRectF(x0 + i * cell_w, y0, cell_w - 1, Grid.NAV_INDEX_H)
                draw_digit(p, ch, box, idx_color)
        # иконка и подпись
        color = Color.NAV_TEXT_HOVER if (self._active or t > 0.5) else Color.NAV_TEXT
        icon_size = 21 if self._compact else Grid.ICON_NAV
        px = icon_pixmap(self._icon, icon_size, color, None, True, dpr)
        if self._compact:
            ix = (W - icon_size) / 2
        else:
            ix = Grid.NAV_PAD_X + 2 * t
        p.drawPixmap(QPointF(ix, (H - icon_size) / 2), px)
        if not self._compact:
            p.setFont(self.font())
            p.setPen(qc(color))
            tx = Grid.NAV_PAD_X + icon_size + Grid.NAV_GAP
            p.drawText(QRectF(tx, 0, W - tx - 12, H), Qt.AlignVCenter | Qt.AlignLeft, self._label)
            if self._count is not None:
                p.setFont(body_font(11))
                p.drawText(QRectF(0, 0, W - Grid.NAV_PAD_X, H), Qt.AlignVCenter | Qt.AlignRight, str(self._count))
        # нижняя линия
        p.setPen(QPen(qc(Color.LINE), 1))
        p.drawLine(QPointF(0, H - 0.5), QPointF(W, H - 0.5))
        if self._kb_focus:
            p.setPen(QPen(qc(Color.ACCENT), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(3, 3, W - 6, H - 6))
        p.end()


class Brand(QWidget):
    """Знак 44×43 + «SayType» Unbounded 11 px; высота 94, нижняя линия."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._compact = False
        self.setFixedHeight(Grid.HEAD)

    def set_compact(self, on: bool) -> None:
        self._compact = on
        self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        W, H = self.width(), self.height()
        dpr = self.devicePixelRatioF()
        if self._compact:
            size = 35
            p.drawPixmap(QPointF((W - size) / 2, (H - size) / 2), mark_pixmap(size, Color.MARK, dpr))
        else:
            size = Grid.BRAND_MARK
            total = size + 4 + 11
            y = (H - total) / 2
            p.drawPixmap(QPointF(24, y), mark_pixmap(size, Color.MARK, dpr))
            p.setFont(head_font_plain(11, Font.HEAD_WEIGHT, -0.4))
            p.setPen(qc(Color.INK))
            p.drawText(QRectF(24, y + size + 4, W - 24, 14), Qt.AlignLeft | Qt.AlignVCenter, "SayType")
        p.setPen(QPen(qc(Color.LINE), 1))
        p.drawLine(QPointF(0, H - 0.5), QPointF(W, H - 0.5))
        p.end()


class Sidebar(QWidget):
    navigate = Signal(str)
    hover_changed = Signal(object)   # NavButton под курсором или None

    ITEMS = [
        ("home", "Диктовки", "mic"),
        ("calls", "Созвоны", "phone"),
        ("notes", "Заметки", "note"),
        ("models", "Модели", "chip"),
        ("stats", "Статистика", "chart"),
    ]
    BOTTOM = [
        ("settings", "Настройки", "settings"),
        ("about", "О приложении", "info"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAutoFillBackground(False)
        self.setFixedWidth(Grid.sidebar_width(Grid.WINDOW_W))
        self.buttons: dict[str, NavButton] = {}
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.brand = Brand()
        v.addWidget(self.brand)
        for i, (pid, label, icon) in enumerate(self.ITEMS, start=1):
            b = NavButton(pid, label, icon, i)
            b.clicked.connect(lambda _=False, pid=pid: self.navigate.emit(pid))
            self.buttons[pid] = b
            v.addWidget(b)
        v.addStretch(1)
        self._bottom_top = _TopLine()
        v.addWidget(self._bottom_top)
        for i, (pid, label, icon) in enumerate(self.BOTTOM, start=len(self.ITEMS) + 1):
            b = NavButton(pid, label, icon, i)
            b.clicked.connect(lambda _=False, pid=pid: self.navigate.emit(pid))
            self.buttons[pid] = b
            v.addWidget(b)
        for b in self.buttons.values():
            b.hovered.connect(lambda on, b=b: self.hover_changed.emit(b if on else None))
        self.set_active("home")
        self.set_window_width(Grid.WINDOW_W)

    def set_active(self, pid: str) -> None:
        for k, b in self.buttons.items():
            b.set_active(k == pid)

    def set_window_width(self, w: int) -> None:
        width = Grid.sidebar_width(w)
        self.setFixedWidth(width)
        compact = width <= Grid.SIDEBAR_COMPACT
        self.brand.set_compact(compact)
        for b in self.buttons.values():
            b.set_compact(compact)

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), qc(Color.BG))
        p.setPen(QPen(qc(Color.LINE), 1))
        p.drawLine(QPointF(self.width() - 0.5, 0), QPointF(self.width() - 0.5, self.height()))
        p.end()


class _TopLine(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(1)

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), qc(Color.LINE))
        p.end()


class GuideCanvas(QWidget):
    """Корень содержимого страницы: белый фон и направляющие 20/40/60/80 % по
    ширине контента (без полосы прокрутки), под ячейками."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAutoFillBackground(False)

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), qc(Color.BG))
        p.setPen(QPen(qc(Color.LINE, 0.7), 1))
        W, H = self.width(), self.height()
        for g in Grid.GUIDES:
            x = int(round(W * g)) + 0.5
            p.drawLine(QPointF(x, 0), QPointF(x, H))
        p.end()


class PageScroll(QScrollArea):
    """Вертикальная прокрутка страницы без рамки и с прозрачным фоном (направляющие видны)."""

    def __init__(self, content: QWidget, parent=None):
        super().__init__(parent)
        self.setWidget(content)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.viewport().setAutoFillBackground(False)
        content.setAutoFillBackground(False)
        self.setStyleSheet("QScrollArea{background:transparent;border:0;} QScrollArea>QWidget>QWidget{background:transparent;}")
        self.verticalScrollBar().setStyleSheet(
            "QScrollBar:vertical{background:transparent;width:8px;margin:0;}"
            "QScrollBar::handle:vertical{background:#DCE0EA;min-height:30px;}"
            "QScrollBar::handle:vertical:hover{background:#C4CBDE;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
            "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}")


class _Transition(QWidget):
    """Слой перехода: два снимка (уходящая и новая страница) поверх живой страницы.

    Непрозрачный — Qt не перерисовывает страницы под ним, кадр стоит две картинки.
    Мышь пропускает: клик уходит в новую страницу, которая уже стоит на месте.
    """

    def __init__(self, parent: QWidget, old_pm: QPixmap | None, new_pm: QPixmap, direction: int):
        super().__init__(parent)
        self._old = old_pm
        self._new = new_pm
        self._dir = direction
        self._t = 0.0
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setGeometry(parent.rect())

    def set_t(self, t: float) -> None:
        self._t = t
        self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), qc(Color.BG))
        W = self.width()
        t = self._t
        if self._old is not None:
            p.setOpacity(1.0 - (1.0 - Motion.PAGE_LEAVE_OPACITY) * t)
            p.drawPixmap(int(round(self._dir * W * Motion.PAGE_LEAVE_SHIFT * t)), 0, self._old)
            p.setOpacity(1.0)
        # новая входит со стороны, противоположной движению: слева направо — из-за левого края
        p.drawPixmap(int(round(-self._dir * W * (1.0 - t))), 0, self._new)
        p.end()


def _settle_now(root: QWidget) -> None:
    """Довести только что пересобранную страницу до кадра без цикла событий.

    QLayout показывает виджет, добавленный в видимую раскладку, отложенным вызовом
    (`_q_showIfNotHidden`) — до него виджет скрыт и лежит в углу. Показываем сразу то,
    что Qt показал бы следом (не скрытое явно), и прогоняем отложенную раскладку.
    Чужие отложенные вызовы при этом не выполняются — только показ и геометрия.
    """
    for w in root.findChildren(QWidget):  # родители раньше детей
        par = w.parentWidget()
        if (w.isHidden() and not w.isWindow() and par is not None and par.isVisible()
                and not w.testAttribute(Qt.WA_WState_ExplicitShowHide)):
            w.show()
    for _ in range(3):  # раскладка вложенной прокрутки порождает следующий запрос
        QApplication.sendPostedEvents(None, QEvent.LayoutRequest)


class PageHost(QWidget):
    """Контейнер страниц. Рисует направляющие 20/40/60/80 % под контентом."""

    page_changed = Signal(str)
    width_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pages: dict[str, QWidget] = {}
        self._current: str | None = None
        self._layer: _Transition | None = None
        self._anim: QVariantAnimation | None = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAutoFillBackground(False)

    def add_page(self, pid: str, page: QWidget) -> None:
        page.setParent(self)
        page.hide()
        self._pages[pid] = page
        page.setGeometry(self.rect())

    def page(self, pid: str) -> QWidget | None:
        return self._pages.get(pid)

    def current(self) -> str | None:
        return self._current

    def show_page(self, pid: str, animate: bool = True) -> None:
        if pid not in self._pages:
            return
        if pid == self._current:
            return
        old_id = self._current
        self._cleanup()
        new_page = self._pages[pid]
        old_page = self._pages.get(old_id) if old_id else None
        reduced = Motion.reduced() or not animate or old_page is None or not self.isVisible()
        old_pm: QPixmap | None = old_page.grab() if (old_page is not None and not reduced) else None
        if old_page is not None:
            old_page.hide()
        self._current = pid
        new_page.setGeometry(self.rect())
        new_page.show()
        new_page.raise_()
        self.page_changed.emit(pid)
        if reduced:
            return
        # Страница могла пересобраться в своём showEvent («Модели», «Заметки»). Новые
        # виджеты Qt показывает отложенным вызовом, раскладка тоже ждёт в очереди — снимок
        # выходил пустым: страница въезжала без карточек, а они выпрыгивали после (08.10).
        _settle_now(new_page)
        layer = _Transition(self, old_pm, new_page.grab(), Motion.PAGE_DIRECTION)
        layer.show()
        layer.raise_()
        self._layer = layer
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(Motion.PAGE_MS)
        anim.setEasingCurve(Motion.PAGE_CURVE.easing())
        anim.valueChanged.connect(lambda v: layer.set_t(float(v)))
        anim.finished.connect(self._cleanup)
        self._anim = anim
        anim.start()

    def _cleanup(self) -> None:
        if self._anim is not None:
            a = self._anim
            self._anim = None
            a.stop()
        if self._layer is not None:
            self._layer.hide()
            self._layer.deleteLater()
            self._layer = None

    def resizeEvent(self, ev) -> None:  # noqa: N802
        for page in self._pages.values():
            page.resize(self.size())
        if self._layer is not None:  # снимки старого размера — переход дальше не нужен
            self._cleanup()
        self.width_changed.emit(self.width())

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), qc(Color.BG))
        p.end()


class _NavFlyout(QWidget):
    """Вкладка с названием раздела, выдвигается из пункта компактного меню при наведении.

    Продолжает пункт: та же высота, фон и штриховка наведения (у активного —
    активного), перекрывает правую границу меню — пункт и вкладка читаются одной
    плашкой. Выезжает слева направо за `Motion.NAV_HATCH_MS`, текст проявляется
    следом. Мышь пропускает: вкладка лежит поверх страницы и клики не крадёт.
    """

    PAD = 18

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._btn: NavButton | None = None
        self._t = 0.0
        self._anim: QVariantAnimation | None = None
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFont(body_font(Font.NAV, 400, 0.15))
        self._retract_timer = QTimer(self)
        self._retract_timer.setSingleShot(True)
        self._retract_timer.setInterval(70)   # переезд на соседний пункт — без мигания
        self._retract_timer.timeout.connect(lambda: self._run(0.0))
        self.hide()

    def show_for(self, btn: NavButton) -> None:
        self._retract_timer.stop()
        moved = btn is not self._btn and self.isVisible()
        self._btn = btn
        par = self.parentWidget()
        pos = btn.mapTo(par, QPoint(0, 0))
        w = QFontMetrics(self.font()).horizontalAdvance(btn.label()) + 2 * self.PAD
        self.setGeometry(pos.x() + btn.width() - 1, pos.y(), w + 1, btn.height())
        self.show()
        self.raise_()
        if moved and self._t > 0.0:
            self._t = 0.35   # с пункта на пункт — короткий доезд, а не полный выезд с нуля
        self._run(1.0)

    def retract_soon(self) -> None:
        self._retract_timer.start()

    def _run(self, target: float) -> None:
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
        if Motion.reduced():
            self._t = target
            self.setVisible(target > 0.0)
            self.update()
            return
        a = QVariantAnimation(self)
        a.setStartValue(self._t)
        a.setEndValue(target)
        a.setDuration(int(Motion.NAV_HATCH_MS * (0.6 if target == 0.0 else 1.0)))
        a.setEasingCurve(Motion.NAV_HATCH_CURVE.easing())
        a.valueChanged.connect(self._on_t)
        if target == 0.0:
            a.finished.connect(self.hide)
        self._anim = a
        a.start()

    def _on_t(self, v) -> None:
        self._t = float(v)
        self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        if self._btn is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        H = self.height()
        w = self.width() * self._t
        p.setClipRect(QRectF(0, 0, w, H))
        active = self._btn.is_active()
        p.fillRect(self.rect(), qc(Color.NAV_BG_ACTIVE if active else Color.NAV_BG_HOVER))
        p.save()
        p.setOpacity(0.8 if active else 0.55)
        p.fillRect(self.rect(), Hatch.brush(self.devicePixelRatioF()))
        p.restore()
        p.setPen(QPen(qc(Color.LINE), 1))
        p.drawLine(QPointF(0, H - 0.5), QPointF(self.width(), H - 0.5))
        p.drawLine(QPointF(0, 0.5), QPointF(self.width(), 0.5))
        p.drawLine(QPointF(self.width() - 0.5, 0), QPointF(self.width() - 0.5, H))
        p.setFont(self.font())
        p.setPen(qc(Color.NAV_TEXT_HOVER))
        p.setOpacity(max(0.0, min(1.0, (self._t - 0.3) / 0.7)))
        p.drawText(QRectF(self.PAD - 8 * (1.0 - self._t), 0, self.width(), H),
                   Qt.AlignVCenter | Qt.AlignLeft, self._btn.label())
        p.end()


class AppShell(QWidget):
    """Sidebar + PageHost. Сообщает страницам ширину main для адаптивных размеров."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sidebar = Sidebar()
        self.host = PageHost()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.sidebar)
        lay.addWidget(self.host, 1)
        self.sidebar.navigate.connect(self.navigate)
        self.flyout = _NavFlyout(self)
        self.sidebar.hover_changed.connect(
            lambda b: self.flyout.show_for(b) if b is not None else self.flyout.retract_soon())
        # Только сама оболочка: стиль без селектора наследуют все вложенные виджеты —
        # колонки строки истории становились белыми и закрывали линии и полосу
        # перемотки, а подсказки получали белый фон под белым текстом.
        self.setObjectName("appShell")
        self.setStyleSheet(f"#appShell{{background:{Color.BG};}}")

    def navigate(self, pid: str) -> None:
        self.sidebar.set_active(pid)
        self.host.show_page(pid)
        self.flyout.raise_()   # слой перехода страниц не должен накрыть вкладку
        self.flyout.update()

    def resizeEvent(self, ev) -> None:  # noqa: N802
        self.sidebar.set_window_width(self.width())
        super().resizeEvent(ev)
