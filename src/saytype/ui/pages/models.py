"""Страница «Модели» V5: сетка карточек (пресеты + свои модели), скачивание с
прогрессом прямо в карточке, удаление весов, «Своя модель» в модальном окне.

Логика — из прежнего `ModelsDialog` (`transcribe_ui_window.py`) без изменений:
поток `model-dl-<spec>` с отменой через `should_cancel`, счётчик скорости
`engine.DownloadProgress`, тик раз в секунду только пока идёт закачка, итог
скачивания остаётся строкой в карточке, после успешной закачки модель сразу
выбирается. Выбор пишется в settings.ini через `save_settings_dict`, как и
раньше; новое — окно узнаёт о смене модели сразу, а не при закрытии диалога.

Состояние закачек живёт в странице, а не в карточках: карточки пересобираются
(`_rebuild`) и подхватывают его. Старые карточки уходят через `deleteLater` —
пересборку могут вызвать изнутри клика по самой карточке (T-269).
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal, Slot
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QAbstractButton, QApplication, QDialog, QFileDialog, QGridLayout, QHBoxLayout,
                               QSizePolicy, QVBoxLayout, QWidget)

from ... import engine
from ...transcribe_ui_window import load_custom_models, load_settings_dict, save_custom_models, save_settings_dict


def _save_model_keys(current: dict) -> None:
    """Сохранить настройки без ключа `dictionary`.

    `save_settings_dict` переписывает `dictionary.txt`, если ключ есть в словаре, а
    `load_settings_dict` отдаёт словарь уже склеенным в строку без строк-комментариев.
    Прежний `ModelsDialog` так и терял комментарии при выборе модели; раздел
    «Модели» словарь не правит — и не трогает его файл.
    """
    d = dict(current)
    d.pop("dictionary", None)
    save_settings_dict(d)
from ..icons import IconButton, qicon
from ..shell import GuideCanvas, PageScroll
from ..tokens import Color, Font, Grid, Hatch, Motion, body_font, qc
from ..widgets import Button, Cell, Label, LineEdit, Modal, PageHead, confirm, info_modal

CARD_PAD = 30
BUSY_TEXT = "Идёт запись или транскрипция — сменить модель нельзя. Останови и попробуй снова."
HELP_TEXT = (
    "Распознавание работает локально: интернет нужен только для скачивания, веса качаются один раз. "
    "Чем крупнее модель, тем точнее и медленнее; скорость зависит ещё от компьютера, языка и длины "
    "записи. Своя модель — repo id с HuggingFace или папка с моделью в формате CTranslate2."
)
CUSTOM_HINT = (
    "Repo id с HuggingFace (например, deepdml/faster-whisper-large-v3-turbo-ct2) или папка с моделью "
    "в формате CTranslate2. Модель в формате transformers (.safetensors) сначала конвертируется "
    "командой ct2-transformers-converter — покажу её, если формат не подойдёт."
)
ON_ACCENT_QSS = ("QPushButton{color:#FFFFFF; border-color:rgba(255,255,255,0.55);}"
                 "QPushButton:hover{background:rgba(255,255,255,0.14); border-color:#FFFFFF;}"
                 "QPushButton:disabled{color:rgba(255,255,255,0.6); border-color:rgba(255,255,255,0.3);}")
ON_ACCENT_GHOST_QSS = ("QPushButton{color:#FFFFFF; border-color:transparent;}"
                       "QPushButton:hover{background:rgba(255,255,255,0.14); border-color:transparent;}"
                       "QPushButton:disabled{color:rgba(255,255,255,0.6);}")


def _ru_size(n: int) -> str:
    """`engine.fmt_bytes` с русской запятой: «1,6 ГБ»."""
    return engine.fmt_bytes(int(n)).replace(".", ",")


def _on_accent(btn: Button, icon: str | None = None, ghost: bool = False) -> Button:
    """Кнопка на синей (выбранной) карточке: белый текст и рамка."""
    btn.setStyleSheet(btn.styleSheet() + (ON_ACCENT_GHOST_QSS if ghost else ON_ACCENT_QSS))
    if icon:
        btn.setIcon(qicon(icon, Grid.ICON, "#FFFFFF"))
    return btn


# ---------------------------------------------------------------------------
# Мелкие элементы карточки
# ---------------------------------------------------------------------------
class _Dots(QWidget):
    """Шкала из 5 точек (точность / скорость): заполненные — акцент, пустые — светлые.
    На синей карточке — белые и полупрозрачные белые."""

    N, D, GAP = 5, 7, 4

    def __init__(self, value: int, on_accent: bool, parent=None):
        super().__init__(parent)
        self._value = max(0, min(self.N, int(value)))
        self._on = qc("#FFFFFF") if on_accent else qc(Color.ACCENT)
        self._off = qc("#FFFFFF", 0.35) if on_accent else qc(Color.MODEL_DOT_OFF)
        self.setFixedSize(self.N * self.D + (self.N - 1) * self.GAP, self.D)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        for i in range(self.N):
            p.setBrush(self._on if i < self._value else self._off)
            p.drawEllipse(QRectF(i * (self.D + self.GAP), 0, self.D, self.D))
        p.end()


class _ProgressTrack(QWidget):
    """`.progress-track`: 5 px, radius 4, заливка меняет ширину за 200 мс."""

    def __init__(self, track: "str | QColor", fill: str, parent=None):
        super().__init__(parent)
        self._track = track if isinstance(track, QColor) else qc(track)
        self._fill = qc(fill)
        self._value = 0.0
        self._anim: QVariantAnimation | None = None
        self.setFixedHeight(5)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_fraction(self, f: float) -> None:
        f = max(0.0, min(1.0, float(f)))
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
        if Motion.reduced() or not self.isVisible() or abs(f - self._value) < 1e-3:
            self._value = f
            self.update()
            return
        a = QVariantAnimation(self)
        a.setStartValue(self._value)
        a.setEndValue(f)
        a.setDuration(Motion.PROGRESS_MS)
        a.valueChanged.connect(self._on_value)
        self._anim = a
        a.start()

    def _on_value(self, v) -> None:
        self._value = float(v)
        self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        r = QRectF(self.rect())
        p.setBrush(self._track)
        p.drawRoundedRect(r, 2.5, 2.5)
        if self._value > 0:
            p.setBrush(self._fill)
            p.drawRoundedRect(QRectF(0, 0, max(5.0, r.width() * self._value), r.height()), 2.5, 2.5)
        p.end()


class _DownloadBlock(QWidget):
    """Идёт загрузка: строка `DownloadProgress.text()` 12 px, трек 5 px, ghost sm «Отменить»."""

    cancel_clicked = Signal()

    def __init__(self, on_accent: bool, parent=None):
        super().__init__(parent)
        sub = Color.MODEL_SELECTED_SUB if on_accent else Color.MUTED
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.text = Label("", size=Font.SMALL, color=sub, wrap=True)
        if on_accent:
            self.track = _ProgressTrack(qc("#FFFFFF", 0.28), "#FFFFFF")
        else:
            self.track = _ProgressTrack(Color.PROGRESS_TRACK, Color.ACCENT)
        self.cancel = Button("Отменить", variant="ghost", small=True)
        if on_accent:
            _on_accent(self.cancel, ghost=True)
        self.cancel.clicked.connect(self.cancel_clicked.emit)
        v.addWidget(self.text)
        v.addSpacing(12)
        v.addWidget(self.track)
        v.addSpacing(12)
        v.addWidget(self.cancel, 0, Qt.AlignLeft)
        self.setMinimumWidth(160)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

    def sizeHint(self) -> QSize:  # noqa: N802
        h = self.layout().heightForWidth(240) if self.layout().hasHeightForWidth() else super().sizeHint().height()
        return QSize(240, h)

    def set_progress(self, done: int, total: int, text: str, stalled: bool) -> None:
        label = text or (f"{done / 1e6:.0f} / {total / 1e6:.0f} МБ" if total > 0 else f"{done / 1e6:.0f} МБ")
        self.text.setText(label)
        if total > 0:
            self.track.set_fraction(done / total)
        elif stalled:
            self.track.set_fraction(0.0)

    def set_cancelling(self) -> None:
        self.cancel.setEnabled(False)
        self.cancel.setText("Останавливаю…")


# ---------------------------------------------------------------------------
# Карточка модели
# ---------------------------------------------------------------------------
class _ModelCard(Cell):
    """`.model`: padding 30, min-height 235; имя 23 px, описание 13 px muted, внизу размер и действия.

    `dl` — живая закачка этой модели ({"tracker", "cancelling"}) или None;
    `result` — итог прошлой закачки (kind, текст) или None.
    """

    activate_requested = Signal(str)
    download_requested = Signal(str)
    delete_requested = Signal(str)
    forget_requested = Signal(str)
    cancel_requested = Signal(str)

    def __init__(self, info: dict, dl: dict | None, result: tuple[str, str] | None, *, right_line: bool,
                 parent=None):
        super().__init__(bottom=True, right=right_line, parent=parent)
        self.spec: str = info["spec"]
        self._downloaded = bool(info.get("downloaded"))
        self._active = bool(info.get("active"))
        self._downloading = dl is not None
        self._selected = self._active
        self._hover_t = 0.0
        self._anim: QVariantAnimation | None = None
        self.setMinimumHeight(Grid.MODEL_CARD_MIN_H)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setCursor(Qt.PointingHandCursor if self._clickable() else Qt.ArrowCursor)

        sel = self._selected
        ink = "#FFFFFF" if sel else Color.INK
        sub = Color.MODEL_SELECTED_SUB if sel else Color.MUTED

        root = QVBoxLayout(self)
        root.setContentsMargins(CARD_PAD, CARD_PAD, CARD_PAD, CARD_PAD)
        root.setSpacing(0)
        self.title = Label(info.get("title") or self.spec, size=Font.MODEL_TITLE, color=ink,
                           letter_spacing=-0.8, wrap=True)
        self.desc = Label(info.get("desc") or "", size=13, color=sub, wrap=True)
        root.addWidget(self.title)
        root.addSpacing(12)
        root.addWidget(self.desc)
        root.addSpacing(16)
        meters = QHBoxLayout()
        meters.setContentsMargins(0, 0, 0, 0)
        meters.setSpacing(8)
        has_meters = False
        for label, key in (("Точность", "accuracy"), ("Скорость", "speed")):
            val = int(info.get(key) or 0)
            if not val:
                continue
            if has_meters:
                meters.addSpacing(14)
            has_meters = True
            meters.addWidget(Label(label, size=Font.HINT, color=sub), 0, Qt.AlignVCenter)
            meters.addWidget(_Dots(val, sel), 0, Qt.AlignVCenter)
        if has_meters:
            meters.addStretch(1)
            root.addLayout(meters)
        root.addSpacing(23)
        root.addStretch(1)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(8)
        size_text = info.get("size_text") or ""
        if self._active:
            size_text += " · выбрана" + ("" if self._downloaded else " · не скачана")
        self.size_label = Label(size_text, size=Font.SMALL, color=sub)
        if self._active and not self._downloaded:
            self.size_label.setToolTip("Эта модель выбрана в настройках, но её весов нет на диске — "
                                       "распознавать пока нечем. Нажми «Скачать».")
        bottom.addWidget(self.size_label, 0, Qt.AlignVCenter)
        bottom.addStretch(1)
        right = QHBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(10)

        self.block: _DownloadBlock | None = None
        if self._downloading:
            self.block = _DownloadBlock(sel)
            self.block.cancel_clicked.connect(lambda: self.cancel_requested.emit(self.spec))
            tracker = dl.get("tracker")
            if tracker is not None:
                self.block.set_progress(tracker.done, tracker.total, tracker.text(), tracker.stalled())
            else:
                self.block.set_progress(0, 0, "", False)
            if dl.get("cancelling"):
                self.block.set_cancelling()
            right.addWidget(self.block, 0, Qt.AlignVCenter)
        else:
            if info.get("custom") and not self._active:
                forget = Button("Убрать из списка", variant="ghost", small=True)
                forget.setToolTip("Убрать свою модель из списка (файлы на диске не трогаются)")
                forget.clicked.connect(lambda: self.forget_requested.emit(self.spec))
                right.addWidget(forget, 0, Qt.AlignVCenter)
            if self._active:
                if not self._downloaded:
                    # T-405: «выбрана» и «готова к работе» — разные вещи: без весов
                    # на диске распознавать нечем, поэтому рядом — «Скачать».
                    right.addWidget(self._download_button(info, result, on_accent=True), 0, Qt.AlignVCenter)
            elif not self._downloaded:
                right.addWidget(self._download_button(info, result, on_accent=False), 0, Qt.AlignVCenter)
            if self._downloaded and not self._active and not info.get("local"):
                trash = IconButton("trash", size=Grid.BTN_SQUARE, icon_size=Grid.ICON, color=Color.DANGER,
                                   hover_color=Color.DANGER, hover_bg=Color.TINT, tooltip="Удалить веса с диска")
                trash.clicked.connect(lambda: self.delete_requested.emit(self.spec))
                right.addWidget(trash, 0, Qt.AlignVCenter)
        bottom.addLayout(right)
        root.addLayout(bottom)

        # T-405: итог скачивания остаётся на экране — «готова», «отменено, скачано
        # X из Y», текст ошибки.
        if result is not None and not self._downloading and result[1]:
            kind, text = result
            color = Color.MODEL_SELECTED_SUB if sel else {"ok": Color.SUCCESS, "cancelled": Color.WARNING}.get(
                kind, Color.ERROR)
            root.addSpacing(10)
            root.addWidget(Label(text, size=Font.SMALL, color=color, wrap=True))

    def _download_button(self, info: dict, result, on_accent: bool) -> Button:
        again = result is not None and result[0] == "cancelled"
        btn = Button("Докачать" if again else "Скачать", icon="download")
        size_mb = int(info.get("size_mb") or 0)
        if size_mb:
            btn.setToolTip(f"Скачать · {_ru_size(size_mb * 1_000_000)}")
        if on_accent:
            _on_accent(btn, "download")
        btn.clicked.connect(lambda: self.download_requested.emit(self.spec))
        return btn

    def _clickable(self) -> bool:
        return self._downloaded and not self._active and not self._downloading

    # --- обновление закачки без пересборки ---------------------------------
    def show_progress(self, done: int, total: int, text: str = "", stalled: bool = False) -> None:
        if self.block is not None:
            self.block.set_progress(done, total, text, stalled)

    def show_cancelling(self) -> None:
        if self.block is not None:
            self.block.set_cancelling()

    # --- наведение, отрисовка и клик -----------------------------------------
    def _hoverable(self) -> bool:
        return self._clickable()

    def _run_hover(self, target: float) -> None:
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
        if Motion.reduced():
            self._hover_t = target
            self.update()
            return
        a = QVariantAnimation(self)
        a.setStartValue(self._hover_t)
        a.setEndValue(target)
        a.setDuration(Motion.NAV_HATCH_MS)
        a.setEasingCurve(Motion.NAV_HATCH_CURVE.easing())
        a.valueChanged.connect(self._on_hover_t)
        self._anim = a
        a.start()

    def _on_hover_t(self, v) -> None:
        self._hover_t = float(v)
        self.update()

    def enterEvent(self, ev) -> None:  # noqa: N802
        super().enterEvent(ev)
        if self._hoverable():
            self._run_hover(1.0)

    def leaveEvent(self, ev) -> None:  # noqa: N802
        super().leaveEvent(ev)
        if self._hover_t > 0 or self._anim is not None:
            self._run_hover(0.0)

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), qc(Color.ACCENT if self._selected else Color.BG))
        t = self._hover_t
        if t > 0 and not self._selected:
            bg = QColor(Color.NAV_BG_HOVER)
            bg.setAlphaF(t)
            p.fillRect(self.rect(), bg)
            p.save()
            p.setOpacity(0.8 * min(1.0, t * 1.4))
            p.fillRect(self.rect(), Hatch.brush(self.devicePixelRatioF()))
            p.restore()
        p.setPen(QPen(qc(Color.ACCENT if self._selected else Color.LINE), 1))
        w, h = self.width(), self.height()
        if self._lines["bottom"]:
            p.drawLine(QPointF(0, h - 0.5), QPointF(w, h - 0.5))
        if self._lines["right"]:
            p.drawLine(QPointF(w - 0.5, 0), QPointF(w - 0.5, h))
        p.end()

    def mousePressEvent(self, ev) -> None:  # noqa: N802 — клик по карточке = выбрать
        # T-269: базовая обработка ДО эмита — обработчик пересобирает сетку, и
        # после эмита карточка уже помечена к удалению. Кнопки («Скачать», корзина,
        # «Убрать») забирают клик себе и сюда не доходят.
        super().mousePressEvent(ev)
        if ev.button() == Qt.LeftButton and self._clickable():
            self.activate_requested.emit(self.spec)


# ---------------------------------------------------------------------------
# «О моделях» и футер
# ---------------------------------------------------------------------------
class _Disclosure(QAbstractButton):
    """summary `details`: треугольник + текст 12 px muted."""

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self._text = text
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setFont(body_font(Font.SMALL))
        fm = QFontMetrics(self.font())
        self.setFixedSize(14 + fm.horizontalAdvance(text) + 2, max(18, fm.height() + 4))
        self.setAttribute(Qt.WA_Hover, True)

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        color = qc(Color.INK if self.underMouse() else Color.MUTED)
        cy = self.height() / 2
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        if self.isChecked():
            pts = [QPointF(0, cy - 2.5), QPointF(7, cy - 2.5), QPointF(3.5, cy + 3)]
        else:
            pts = [QPointF(1, cy - 3.5), QPointF(6.5, cy), QPointF(1, cy + 3.5)]
        p.drawPolygon(QPolygonF(pts))
        p.setPen(color)
        p.setFont(self.font())
        p.drawText(QRectF(14, 0, self.width() - 14, self.height()), Qt.AlignVCenter | Qt.AlignLeft, self._text)
        if self.hasFocus():
            p.setPen(QPen(qc(Color.ACCENT), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(1, 1, self.width() - 2, self.height() - 2))
        p.end()

    def enterEvent(self, ev) -> None:  # noqa: N802
        super().enterEvent(ev)
        self.update()

    def leaveEvent(self, ev) -> None:  # noqa: N802
        super().leaveEvent(ev)
        self.update()


class _HelpCell(Cell):
    """`details.model-help`: padding 24/30, раскрывается абзацем 12 px muted (max 650)."""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(CARD_PAD, 24, CARD_PAD, 24)
        v.setSpacing(12)
        self.toggle = _Disclosure("О моделях")
        self.text = Label(HELP_TEXT, size=Font.SMALL, color=Color.MUTED, wrap=True)
        self.text.setMaximumWidth(650)
        self.text.setVisible(False)
        self.toggle.toggled.connect(self.text.setVisible)
        v.addWidget(self.toggle, 0, Qt.AlignLeft)
        v.addWidget(self.text, 0, Qt.AlignLeft)


class _FooterCell(Cell):
    """«Модели занимают N · путь» — 12 px muted, путь выделяется мышью."""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(CARD_PAD, 20, CARD_PAD, 20)
        self.label = Label("", size=Font.SMALL, color=Color.MUTED, wrap=True)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.label)

    def set_text(self, text: str) -> None:
        self.label.setText(text)


# ---------------------------------------------------------------------------
# «Своя модель»
# ---------------------------------------------------------------------------
class _CustomModelModal(Modal):
    """Ввод своей модели: HF repo id или папка с CT2-моделью; проверка `engine.validate_spec`."""

    def __init__(self, parent=None):
        super().__init__("Своя модель", parent, width=600)
        self._spec = ""
        self.body_layout.setSpacing(0)
        self.body_layout.addWidget(Label("Название модели", size=Font.LABEL))
        self.body_layout.addSpacing(10)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(9)
        self.edit = LineEdit(placeholder="Например, моя модель Whisper")
        browse = Button("Выбрать папку", icon="folder")
        browse.clicked.connect(self._browse)
        row.addWidget(self.edit, 1)
        row.addWidget(browse)
        self.body_layout.addLayout(row)
        self.body_layout.addSpacing(8)
        self.hint = Label(CUSTOM_HINT, size=Font.HINT, color=Color.MUTED, wrap=True)
        self.body_layout.addWidget(self.hint)
        cancel = self.add_button(Button("Отмена"))
        ok = self.add_button(Button("Добавить", variant="primary"))
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self._accept)
        self.edit.returnPressed.connect(self._accept)

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Папка с CT2-моделью", str(engine.models_root()))
        if chosen:
            self.edit.setText(chosen)

    def _error(self, text: str) -> None:
        self.hint.setText(text)
        self.hint.setStyleSheet(f"color:{Color.ERROR}; background:transparent;")

    def _accept(self) -> None:
        spec = self.edit.text().strip().strip('"')
        if not spec:
            self._error("Укажи repo id или выбери папку.")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ok, msg = engine.validate_spec(spec)
        finally:
            QApplication.restoreOverrideCursor()
        if not ok:
            self._error(msg)
            return
        self._spec = spec
        self.accept()

    def spec(self) -> str:
        return self._spec


# ---------------------------------------------------------------------------
# Страница
# ---------------------------------------------------------------------------
class ModelsPage(QWidget):
    """Раздел «Модели». Меняет только `model` / `custom_model` / `custom_models` в settings.ini;
    саму подмену модели в движке делает `transcribe_ui` по `window.settings_changed`.

    Контракт окна: `model_busy()`, `apply_settings_to_pages(dict)`, `settings_changed`
    (Signal(dict)), `refresh_nomodel_bar()`.
    """

    _dl_progress = Signal(str, int, int)   # spec, done, total
    _dl_finished = Signal(str, str)        # spec, error ("" — успех)
    _dl_cancelled = Signal(str, int, int)  # spec, скачано, всего

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._win = window
        self._window_width = Grid.WINDOW_W
        self._active_spec = ""
        self._customs: list[str] = []
        self._cards: dict[str, _ModelCard] = {}
        self._downloading: set[str] = set()
        self._dl_cancel: set[str] = set()                        # нажали «Отменить»
        self._dl_track: dict[str, engine.DownloadProgress] = {}   # скорость / остаток / застой
        self._dl_results: dict[str, tuple[str, str]] = {}         # чем кончилось: kind, текст

        content = GuideCanvas()
        v = QVBoxLayout(content)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.head = PageHead("Модели")
        self.add_btn = Button("Своя модель", icon="plus")
        # белая подложка: шапка прозрачная, направляющая не должна проходить сквозь кнопку
        self.add_btn.setStyleSheet(self.add_btn.styleSheet() + f"QPushButton{{background:{Color.BG};}}"
                                   f"QPushButton:hover{{background:{Color.BTN_BG_HOVER};}}")
        self.add_btn.clicked.connect(self._add_custom)
        self.head.add_action(self.add_btn)
        v.addWidget(self.head)

        self.grid_cell = Cell(fill=False)        # `.model-grid`: прозрачная, линия снизу
        self.grid = QGridLayout(self.grid_cell)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(0)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1)
        v.addWidget(self.grid_cell)
        self.help = _HelpCell()
        v.addWidget(self.help)
        self.footer = _FooterCell()
        v.addWidget(self.footer)
        v.addStretch(1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(PageScroll(content))

        self._dl_progress.connect(self._on_dl_progress)
        self._dl_finished.connect(self._on_dl_finished)
        self._dl_cancelled.connect(self._on_dl_cancelled)
        # Тик раз в секунду и только пока идёт закачка: когда байты перестали
        # приходить, колбэка прогресса нет вовсе, а «нет данных N сек» нужно
        # показать как раз тогда.
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._refresh_download_labels)

    # --- контракт страницы ---------------------------------------------------
    def set_window_width(self, w: int) -> None:
        self._window_width = w
        self.head.set_window_width(w)

    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        self.refresh()

    def hideEvent(self, ev) -> None:  # noqa: N802
        super().hideEvent(ev)
        # итог закачки показывается до ухода со страницы (живые закачки не трогаем)
        for spec in list(self._dl_results):
            if spec not in self._downloading:
                self._dl_results.pop(spec, None)

    def refresh(self) -> None:
        """Перечитать настройки и диск, пересобрать карточки."""
        current = load_settings_dict()
        self._active_spec = engine.spec_from_settings(current)
        self._customs = load_custom_models()
        # своя модель из старых настроек — подхватить в список, чтобы не потерялась
        legacy = (current.get("custom_model") or "").strip()
        if legacy and legacy.lower() not in {c.lower() for c in self._customs}:
            self._customs.append(legacy)
        self._rebuild()

    # --- сборка ---------------------------------------------------------------
    def _entries(self) -> tuple[list[dict], int]:
        try:
            cached = engine.list_cached_models()
        except Exception:  # noqa: BLE001
            cached = []
        on_disk = {e["repo"].lower(): e["bytes"] for e in cached}
        total = sum(e["bytes"] for e in cached)

        def disk_bytes(spec: str) -> int:
            return int(on_disk.get((engine.repo_for_spec(spec) or "").lower(), 0))

        out: list[dict] = []
        for key in engine.PRESET_KEYS:
            meta = engine.preset_meta(key)
            downloaded = engine.is_cached(key)
            size = disk_bytes(key) if downloaded else 0
            out.append({
                "spec": key, "title": meta["title"], "desc": meta["desc"],
                "size_mb": meta["size_mb"], "downloaded": downloaded,
                "accuracy": meta.get("accuracy", 0), "speed": meta.get("speed", 0),
                "active": key == self._active_spec, "custom": False, "local": False,
                "size_text": _ru_size(size) if size else _ru_size(meta["size_mb"] * 1_000_000),
            })
        for spec in self._customs:
            downloaded = engine.is_cached(spec)
            local = engine.is_local_path(spec)
            if local:
                size_text = "Папка на диске" if downloaded else "Папка не найдена"
            else:
                size = disk_bytes(spec) if downloaded else 0
                size_text = "HuggingFace · " + (_ru_size(size) if size else "на диске" if downloaded
                                                else "ещё не скачана")
            out.append({
                "spec": spec, "title": engine.spec_display(spec),
                "desc": "Своя модель · " + (spec if len(spec) < 70 else spec[:67] + "…"),
                "size_mb": 0, "downloaded": downloaded,
                "active": spec == self._active_spec, "custom": True, "local": local,
                "size_text": size_text,
            })
        return out, total

    def _rebuild(self) -> None:
        entries, total = self._entries()
        # T-269: пересборку зовут и из клика по карточке — старые карточки не
        # удаляем немедленно: глушим сигналы, прячем и отдаём на deleteLater.
        for card in self._cards.values():
            card.blockSignals(True)
            card.hide()
            self.grid.removeWidget(card)
            card.deleteLater()
        self._cards.clear()
        for i, e in enumerate(entries):
            spec = e["spec"]
            dl = None
            if spec in self._downloading:
                dl = {"tracker": self._dl_track.get(spec), "cancelling": spec in self._dl_cancel}
            result = None if dl is not None else self._dl_results.get(spec)
            card = _ModelCard(e, dl, result, right_line=(i % 2 == 0), parent=self.grid_cell)
            card.activate_requested.connect(self._activate)
            card.download_requested.connect(self._download)
            card.delete_requested.connect(self._delete)
            card.forget_requested.connect(self._forget_custom)
            card.cancel_requested.connect(self._cancel_download)
            self.grid.addWidget(card, i // 2, i % 2)
            self._cards[spec] = card
        self.footer.set_text(f"Модели занимают {_ru_size(total)} · {engine.models_root()}")

    # --- окно ----------------------------------------------------------------
    def _busy(self) -> bool:
        try:
            return bool(self._win.model_busy())
        except Exception:  # noqa: BLE001
            return False

    def _settings_written(self, before: dict) -> None:
        """После записи ini: полоса «модель не скачана», и — если модель сменилась — сигнал окну."""
        after = load_settings_dict()
        self._win.refresh_nomodel_bar()
        if (after.get("model"), after.get("custom_model")) != (before.get("model"), before.get("custom_model")):
            self._win.apply_settings_to_pages(after)
            self._win.settings_changed.emit(after)

    # --- действия ------------------------------------------------------------
    def _activate(self, spec: str) -> None:
        if self._busy():
            info_modal(self.window(), "Модель", BUSY_TEXT)
            return
        if spec in self._downloading:
            return
        if not engine.is_cached(spec):
            self._download(spec)
            return
        before = load_settings_dict()
        current = dict(before)
        if spec in engine.PRESET_KEYS:
            current["model"] = spec
            current["custom_model"] = ""
        else:
            current["model"] = engine.CUSTOM_KEY
            current["custom_model"] = spec
        _save_model_keys(current)
        save_custom_models(self._customs)
        self._active_spec = spec
        # пересборка ДО сигнала: смена модели в движке может открыть модальный прогресс
        self._rebuild()
        self._settings_written(before)

    def _download(self, spec: str) -> None:
        if spec in self._downloading:
            return
        self._downloading.add(spec)
        self._dl_cancel.discard(spec)
        self._dl_results.pop(spec, None)
        tracker = engine.DownloadProgress()
        self._dl_track[spec] = tracker
        self._rebuild()
        card = self._cards.get(spec)
        if card is not None:
            card.show_progress(0, 0, "Начинаю…")
        self._tick.start()

        def _worker() -> None:
            try:
                engine.ensure_downloaded(
                    spec,
                    progress_cb=lambda d, t: self._dl_progress.emit(spec, int(d), int(t)),
                    should_cancel=lambda: spec in self._dl_cancel,
                )
                self._dl_finished.emit(spec, "")
            except engine.DownloadCancelled as exc:
                self._dl_cancelled.emit(spec, exc.done_bytes, exc.total_bytes)
            except engine.ModelDownloadError as exc:
                self._dl_finished.emit(spec, exc.full_text())
            except Exception as exc:  # noqa: BLE001
                self._dl_finished.emit(spec, f"{exc.__class__.__name__}: {exc}")

        threading.Thread(target=_worker, daemon=True, name=f"model-dl-{spec}").start()

    def _cancel_download(self, spec: str) -> None:
        """Остановить скачивание. Уже скачанное с диска не сносим — это докачка."""
        if spec not in self._downloading:
            return
        self._dl_cancel.add(spec)
        card = self._cards.get(spec)
        if card is not None:
            card.show_cancelling()

    def _refresh_download_labels(self) -> None:
        """Раз в секунду переписать строку прогресса: скорость, остаток, застой."""
        if not self._downloading:
            self._tick.stop()
            return
        for spec in list(self._downloading):
            tracker = self._dl_track.get(spec)
            card = self._cards.get(spec)
            if tracker is None or card is None or spec in self._dl_cancel:
                continue  # при отмене там уже «Останавливаю…»
            card.show_progress(tracker.done, tracker.total, tracker.text(), tracker.stalled())

    @Slot(str, int, int)
    def _on_dl_progress(self, spec: str, done: int, total: int) -> None:
        tracker = self._dl_track.get(spec)
        card = self._cards.get(spec)
        if tracker is not None:
            tracker.feed(done, total)
        if card is not None and spec not in self._dl_cancel:
            text = tracker.text() if tracker is not None else ""
            card.show_progress(done, total, text, bool(tracker and tracker.stalled()))

    def _finish_download(self, spec: str, kind: str, text: str) -> None:
        """Общий хвост любого исхода: снять флаги, оставить итог в карточке."""
        self._downloading.discard(spec)
        self._dl_cancel.discard(spec)
        self._dl_track.pop(spec, None)
        self._dl_results[spec] = (kind, text)
        if not self._downloading:
            self._tick.stop()

    @Slot(str, str)
    def _on_dl_finished(self, spec: str, error: str) -> None:
        if error:
            self._finish_download(spec, "error", error.split("\n")[0])
            self._rebuild()
            info_modal(self.window(), "Скачивание модели", f"{engine.spec_display(spec)} не скачалась:\n\n{error}")
            return
        self._finish_download(spec, "ok", "Модель готова к работе.")
        if self._busy():
            self._rebuild()
        else:
            self._activate(spec)

    @Slot(str, int, int)
    def _on_dl_cancelled(self, spec: str, done: int, total: int) -> None:
        """Отмена — не сбой: окна с ошибкой нет, итог остаётся строкой в карточке."""
        if total > 0:
            text = (f"Отменено — скачано {done / 1e6:.0f} из {total / 1e6:.0f} МБ, "
                    "они остались на диске. «Докачать» продолжит с места.")
        else:
            text = (f"Отменено — скачано {done / 1e6:.0f} МБ, они остались на диске. "
                    "«Докачать» продолжит с места.")
        self._finish_download(spec, "cancelled", text)
        self._rebuild()

    def _delete(self, spec: str) -> None:
        repo = (engine.repo_for_spec(spec) or "").lower()
        entries = [e for e in engine.list_cached_models() if e["repo"].lower() == repo]
        if not entries:
            info_modal(self.window(), "Модель", "Веса не найдены на диске — удалять нечего.")
            self._rebuild()
            return
        total = sum(e["bytes"] for e in entries)
        if not confirm(self.window(), "Удалить модель?",
                       f"Удалить веса {engine.spec_display(spec)} и освободить {_ru_size(total)}? "
                       "При следующем выборе модель скачается заново.", ok_text="Удалить"):
            return
        errors = [msg for ok, msg in (engine.delete_cached_model(e["path"]) for e in entries) if not ok]
        self._rebuild()
        self._win.refresh_nomodel_bar()
        if errors:
            info_modal(self.window(), "Модель", "Не всё удалилось:\n" + "\n".join(errors))

    def _forget_custom(self, spec: str) -> None:
        before = load_settings_dict()
        self._customs = [c for c in self._customs if c.lower() != spec.lower()]
        save_custom_models(self._customs)
        current = dict(before)
        if (current.get("custom_model") or "").lower() == spec.lower():
            current["custom_model"] = ""
        _save_model_keys(current)
        self._active_spec = engine.spec_from_settings(load_settings_dict())
        self._rebuild()
        self._settings_written(before)

    def _add_custom(self) -> None:
        dlg = _CustomModelModal(self.window())
        if dlg.exec() != QDialog.Accepted:
            return
        spec = dlg.spec()
        if spec and spec.lower() not in {c.lower() for c in self._customs}:
            self._customs.append(spec)
            save_custom_models(self._customs)
        self._rebuild()
