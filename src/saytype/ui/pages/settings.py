"""Страница «Настройки» V5: колонка вкладок 20 % + поля выбранной вкладки + футер.

Перенос `SettingsDialog` / `ReplacementsDialog` из `transcribe_ui_window`:
ключи, дефолты, валидация и побочные эффекты прежние, меняется только вид.
Значения читаются `load_settings_dict()` при каждом `reload()` (показ страницы
и «Отмена»); «Сохранить» отдаёт словарь окну — `window.emit_settings(...)`
пишет settings.ini, замены, автостарт и шлёт `settings_changed`.

Тяжёлое (замер микрофона, nvidia-smi, подсчёт hi-fi минут, проверка и
скачивание обновления, удаление CUDA-слоя) идёт в потоках; результат
возвращается в GUI-поток через сигнал.
"""
from __future__ import annotations

import html
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, Signal, Slot
from PySide6.QtGui import QFontMetrics, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QApplication, QFileDialog, QHBoxLayout, QKeySequenceEdit,
                               QLayout, QLineEdit, QProgressDialog, QSizePolicy, QVBoxLayout, QWidget)

from ... import audio_quality, cuda_layer, engine, profile, updater
from ...hotkeys import to_canonical as hotkey_to_canonical
from ...hotkeys import to_qt as hotkey_to_qt
from ...hotkeys import validate as parse_hotkey_valid
from ...transcribe_ui_window import (DEFAULT_CALL_AUDIO_KEEP, DEFAULT_DOWNLOAD_PROXY, DEFAULT_HIFI_ENABLED,
                                     DEFAULT_HIFI_SAMPLE_RATE, DEFAULT_HISTORY_DIR, DEFAULT_MODEL_EXISTING,
                                     DEFAULT_SPEAKER_OTHER, DEFAULT_SPEAKER_SELF, HIFI_SAMPLE_RATES,
                                     HIFI_TARGET_MINUTES, hifi_accumulated_minutes, hotkey_conflicts_with_handy,
                                     load_settings_dict)
from ..digits import DigitStrip
from ..icons import IconButton, icon_pixmap
from ..shell import GuideCanvas, PageScroll
from ..tokens import Color, Font, Grid, Hatch, body_font, qc
from ..widgets import (FIELD_QSS, ActionField, Button, Cell, ComboBox, Field, Label, LineEdit, Modal, PageHead,
                       SpinBox, Spinner, Switch, TextEdit, TextEditorSheet, confirm, info_modal)

TABS = (
    ("record", "Запись"),
    ("recognition", "Распознавание"),
    ("calls", "Созвоны"),
    ("storage", "Хранение"),
    ("hifi", "Hi-fi диктовка"),
    ("app", "Приложение"),
)

# Слова batch / streaming — те же, что в прежнем окне и в «Статистике»: без них
# настройку не находили («в каком режиме мы работаем?»)
MODES = (
    ("auto", "Авто — до порога batch, дольше streaming"),
    ("always_batch", "Всегда batch — распознаёт после записи"),
    ("always_streaming", "Всегда streaming — распознаёт во время записи"),
)

CALL_HOTKEY_TEXT = "Ctrl + Shift + E"
MIC_CHECK_SECONDS = 3
MIC_CHECK_RATE = 48000  # на 16 кГц верхней полосы нет ни у какого источника — замер потерял бы смысл
MIC_CHECK_HINT = ("Запишет 3 секунды с выбранного устройства и покажет, широкая ли полоса. "
                  "Говорите в микрофон, пока идёт замер.")
REPLACEMENTS_HELP = (
    "Регулярные выражения Python; в замене работают ссылки на группы (\\1, \\2). "
    "«Только в конце» привязывает правило к концу текста: полезно для хвостовых "
    "галлюцинаций модели на тишине, но опасно для записи созвона — там короткая "
    "реплика в конце может быть настоящей."
)


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _compact_primary(btn: Button) -> Button:
    """Primary без ширины hero-кнопки (170 px) и в одну высоту с обычной."""
    btn.setStyleSheet(btn.styleSheet() + f"QPushButton{{min-width:0px; min-height:{Grid.BTN_H - 22}px;}}")
    return btn


class _Failure:
    """Исключение из фонового потока, доставленное в GUI-поток."""

    def __init__(self, exc: BaseException):
        self.exc = exc

    def __str__(self) -> str:
        return str(self.exc)


class _Relay(QObject):
    """Мост «поток → GUI»: колбэк и результат приходят queued-соединением."""

    done = Signal(object, object)


# ---------------------------------------------------------------------------
# Вкладки
# ---------------------------------------------------------------------------
class _TabButton(QAbstractButton):
    """Вкладка настроек: min-height 66, padding 18, 12 px; активная — штриховка,
    `#343C50` 500 и inset 3 px accent слева."""

    def __init__(self, key: str, text: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setAttribute(Qt.WA_Hover, True)
        self.setMinimumHeight(Grid.SETTINGS_TAB_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._hover = False
        self._kb_focus = False

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(120, Grid.SETTINGS_TAB_H)

    def enterEvent(self, ev) -> None:  # noqa: N802
        self._hover = True
        super().enterEvent(ev)
        self.update()

    def leaveEvent(self, ev) -> None:  # noqa: N802
        self._hover = False
        super().leaveEvent(ev)
        self.update()

    def focusInEvent(self, ev) -> None:  # noqa: N802
        self._kb_focus = ev.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason)
        super().focusInEvent(ev)
        self.update()

    def focusOutEvent(self, ev) -> None:  # noqa: N802
        self._kb_focus = False
        super().focusOutEvent(ev)
        self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        W, H = self.width(), self.height()
        active = self.isChecked()
        p.fillRect(self.rect(), qc(Color.BG))
        if active:
            p.fillRect(self.rect(), Hatch.brush(self.devicePixelRatioF()))
            p.fillRect(QRectF(0, 0, 3, H), qc(Color.ACCENT))
        elif self._hover:
            p.fillRect(self.rect(), qc(Color.NAV_BG_HOVER))
        font = body_font(Font.NAV, 500 if active else 400)
        p.setFont(font)
        p.setPen(qc(Color.NAV_TEXT_HOVER if (active or self._hover) else Color.MUTED))
        # padding 18; на узкой колонке длинное слово («Распознавание») не переносится —
        # сначала ужимаем поля (до 5 px: на окне 760 колонке 139 px, слову 129),
        # и только потом многоточие. Поле одно на все вкладки — по самой длинной
        # подписи жирным, иначе левый край текста гулял бы между вкладками.
        fm = QFontMetrics(font)
        bold = QFontMetrics(body_font(Font.NAV, 500))
        col = self.parentWidget()
        names = [b.text() for b in col.findChildren(_TabButton)] if col is not None else [self.text()]
        widest = max(bold.horizontalAdvance(t) for t in names or [self.text()])
        pad = 18 if widest <= W - 36 else max(5, (W - widest) // 2)
        text = fm.elidedText(self.text(), Qt.ElideRight, int(W - 2 * pad))
        p.drawText(QRectF(pad, 0, W - 2 * pad, H), Qt.AlignVCenter | Qt.AlignLeft, text)
        p.setPen(QPen(qc(Color.LINE), 1))
        p.drawLine(QPointF(0, H - 0.5), QPointF(W, H - 0.5))
        if self._kb_focus:
            p.setPen(QPen(qc(Color.ACCENT), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(4, 4, W - 8, H - 8))
        p.end()


class _TabColumn(QWidget):
    """`.settings-nav`: белая колонка с правой линией, вкладки сверху."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 1, 0)
        self.lay.setSpacing(0)

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), qc(Color.BG))
        p.setPen(QPen(qc(Color.LINE), 1))
        x = self.width() - 0.5
        p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        p.end()


class _White(QWidget):
    """Белая колонка контента: направляющие под ней не видны."""

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), qc(Color.BG))
        p.end()


class _SettingsLayout(QWidget):
    """`.settings-layout`: вкладки 20 % ширины + колонка контента."""

    def __init__(self, tabs: QWidget, content: QWidget, parent=None):
        super().__init__(parent)
        self.tabs = tabs
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(tabs)
        row.addWidget(content, 1)

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        # +1: правая линия колонки ложится в пиксель направляющей 20 % шапки
        w = int(round(self.width() * 0.2)) + 1
        if self.tabs.width() != w:
            self.tabs.setFixedWidth(w)


class _Footnote(Cell):
    """footnote внутри вкладки: padding 23/30, 12 px muted, нижняя линия."""

    def __init__(self, text: str, parent=None):
        super().__init__(parent=parent)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(Grid.PAD, 23, Grid.PAD, 23)
        self.label = Label(text, size=Font.HINT, color=Color.MUTED, wrap=True)
        self._lay.addWidget(self.label)

    def set_window_width(self, w: int) -> None:
        pad = Grid.pad(w)
        self._lay.setContentsMargins(pad, 23, pad, 23)


class _Footer(Cell):
    """`.settings-footer`: padding 22/30, min-height 85, верхняя линия, кнопки справа."""

    def __init__(self, parent=None):
        super().__init__(bottom=False, top=True, parent=parent)
        self.setMinimumHeight(85)
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(Grid.PAD, 22, Grid.PAD, 22)
        self._row.setSpacing(9)
        self._row.addStretch(1)
        self.cancel = Button("Отмена")
        self.save = _compact_primary(Button("Сохранить", variant="primary"))
        self._row.addWidget(self.cancel, 0, Qt.AlignVCenter)
        self._row.addWidget(self.save, 0, Qt.AlignVCenter)

    def set_window_width(self, w: int) -> None:
        pad = Grid.pad(w)
        self._row.setContentsMargins(pad, 22, pad, 22)


class _BusyButton(Button):
    """Primary-кнопка, которая после нажатия крутит индикатор вместо того, чтобы
    гаснуть: между «Скачать» и первым процентом velopack молчит до 70 секунд."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, variant="primary", parent=parent)
        _compact_primary(self)
        self._spinner = Spinner(14, "#B8C0F6", "#FFFFFF", parent=self)
        self._spinner.hide()

    def start_busy(self, text: str) -> None:
        self.setEnabled(False)
        self.setText("      " + text)
        self._spinner.show()
        self._place()

    def _place(self) -> None:
        self._spinner.move(14, (self.height() - self._spinner.height()) // 2)

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        self._place()


# ---------------------------------------------------------------------------
# Замены в тексте — модальное окно
# ---------------------------------------------------------------------------
class _Check(QAbstractButton):
    """Чекбокс 14×14 + подпись 10 px (в строке правила замен)."""

    BOX = 14

    def __init__(self, text: str, checked: bool = False, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setFont(body_font(10))
        self.setFixedHeight(24)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.BOX + 5 + self.fontMetrics().horizontalAdvance(self.text()) + 2, 24)

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        y = (self.height() - self.BOX) / 2
        box = QRectF(0.5, y + 0.5, self.BOX - 1, self.BOX - 1)
        if self.isChecked():
            p.setPen(QPen(qc(Color.ACCENT), 1))
            p.setBrush(qc(Color.ACCENT))
            p.drawRect(box)
            px = icon_pixmap("check", 12, "#FFFFFF", 2.0, False, self.devicePixelRatioF())
            p.drawPixmap(QPointF(1, y + 1), px)
        else:
            p.setPen(QPen(qc(Color.BTN_BORDER_HOVER if self.hasFocus() else Color.FIELD_BORDER), 1))
            p.setBrush(qc(Color.BG))
            p.drawRect(box)
        p.setPen(qc(Color.MUTED))
        p.setFont(self.font())
        p.drawText(QRectF(self.BOX + 5, 0, self.width() - self.BOX - 5, self.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, self.text())
        p.end()


class _RuleRow(QWidget):
    """`.replacement-full`: шаблон 1fr · замена 1fr · «Только в конце» 115 · [×] 30; gap 9."""

    remove_requested = Signal(object)

    def __init__(self, pattern: str = "", replacement: str = "", tail_only: bool = False, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(9)
        self.pattern = LineEdit(pattern, "Регулярное выражение")
        self.replacement = LineEdit(replacement, "Замена")
        self.tail = _Check("Только в конце", tail_only)
        self.tail.setFixedWidth(max(115, self.tail.sizeHint().width()))
        self.remove = IconButton("close", size=30, icon_size=16, tooltip="Удалить правило")
        self.remove.clicked.connect(lambda: self.remove_requested.emit(self))
        row.addWidget(self.pattern, 1)
        row.addWidget(self.replacement, 1)
        row.addWidget(self.tail, 0, Qt.AlignVCenter)
        row.addWidget(self.remove, 0, Qt.AlignVCenter)


class ReplacementsModal(Modal):
    """Редактор замен: строки правил, «Добавить правило», «Сохранить правила».

    Правило — регулярка, и опечатка в ней молча выключает правило, поэтому
    шаблоны компилируются до сохранения (`profile.validate_pattern`).
    """

    LIST_MAX_H = 360

    def __init__(self, parent, rules: list):
        super().__init__("Замены в тексте", parent, width=680)
        self._rules: list = []
        self._rows: list[_RuleRow] = []
        help_copy = Label(REPLACEMENTS_HELP, size=Font.BODY, color=Color.MUTED, wrap=True)
        self.body_layout.addWidget(help_copy)

        self._list = QWidget()
        self._list_lay = QVBoxLayout(self._list)
        self._list_lay.setContentsMargins(0, 0, 12, 0)
        self._list_lay.setSpacing(12)
        self._empty = Label("Правил пока нет.", size=Font.SMALL, color=Color.MUTED)
        self._list_lay.addWidget(self._empty)
        self._scroll = PageScroll(self._list)
        self.body_layout.addWidget(self._scroll)

        add = Button("Добавить правило", icon="plus", variant="ghost", small=True)
        add.clicked.connect(self._add_empty)
        self.body_layout.addWidget(add, 0, Qt.AlignLeft)

        cancel = self.add_button(Button("Отмена"))
        save = self.add_button(_compact_primary(Button("Сохранить правила", variant="primary")))
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self._validate_and_accept)

        for rule in rules:
            self._append(rule.get("pattern", ""), rule.get("replacement", ""), bool(rule.get("tail_only")))
        self._refit()

    def _append(self, pattern: str, replacement: str, tail_only: bool) -> _RuleRow:
        row = _RuleRow(pattern, replacement, tail_only)
        row.remove_requested.connect(self._remove)
        self._rows.append(row)
        self._list_lay.addWidget(row)
        row.show()  # иначе до отложенного show строка не входит в sizeHint списка
        self._empty.setVisible(False)
        return row

    def _add_empty(self) -> None:
        row = self._append("", "", False)
        self._refit()
        row.pattern.setFocus()
        self._scroll.ensureWidgetVisible(row)
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _remove(self, row: _RuleRow) -> None:
        if row in self._rows:
            self._rows.remove(row)
        row.hide()
        row.setParent(None)
        row.deleteLater()
        self._empty.setVisible(not self._rows)
        self._refit()

    def _refit(self) -> None:
        self._list_lay.activate()
        h = self._list.sizeHint().height()
        self._scroll.setFixedHeight(max(30, min(h, self.LIST_MAX_H)))
        if self.isVisible():
            self._fit_height()

    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        self._fit_height()

    def _fit_height(self) -> None:
        """Высота по фактической ширине: sizeHint переносимого текста считается
        для условной ширины и даёт лишний воздух над списком."""
        # Кэш heightForWidth вложенных раскладок и их QWidgetItem сбрасывается
        # только по отложенному LayoutRequest — сбрасываем сами по цепочке от
        # списка вверх, иначе высота остаётся прежней.
        w = self._scroll
        while w is not None and w is not self:
            w.updateGeometry()
            w = w.parentWidget()
        for lay in self.findChildren(QLayout):
            lay.invalidate()
        if self.layout() is not None:
            self.layout().activate()
        h = self.heightForWidth(self.width())
        if h <= 0 or h == self.height():
            return
        dy = (self.height() - h) // 2
        self.setGeometry(self.x(), max(0, self.y() + dy), self.width(), h)

    def _collect(self) -> list:
        out = []
        for row in self._rows:
            pattern = row.pattern.text().strip()
            if not pattern:
                continue  # пустая строка = «пользователь передумал», не ошибка
            out.append({
                "pattern": pattern,
                "replacement": row.replacement.text(),
                "tail_only": bool(row.tail.isChecked()),
            })
        return out

    def _validate_and_accept(self) -> None:
        for i, row in enumerate(self._rows):
            pattern = row.pattern.text().strip()
            if not pattern:
                continue
            ok, msg = profile.validate_pattern(pattern)
            if not ok:
                row.pattern.setFocus()
                self._scroll.ensureWidgetVisible(row)
                info_modal(self, "Замены",
                           f"Строка {i + 1}: шаблон не компилируется как регулярное выражение.\n\n{msg}")
                return
        self._rules = self._collect()
        self.accept()

    def rules(self) -> list:
        return self._rules


# ---------------------------------------------------------------------------
# Страница
# ---------------------------------------------------------------------------
class SettingsPage(QWidget):
    """Раздел «Настройки». Контракт окна: `model_busy()`, `navigate(pid)`,
    `emit_settings(values, replacement_rules)`, `refresh_history()`."""

    # T-262 / T-443: проверка и скачивание обновления идут в фоновых потоках
    update_progress = Signal(int)
    update_download_failed = Signal(str)

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._win = window
        self._current: dict = {}
        self._width = Grid.WINDOW_W
        self._cuda_declined = False
        self._cuda_requested = False
        self._cuda_state: dict | None = None
        self._cuda_seq = 0
        self._hifi_seq = 0
        self._hifi_stale = True
        self._mic_test_device: str | None = None
        self._replacement_rules: list = []
        self._replacements_edited = False
        self._update_offer_dialog = None
        self._update_progress_dialog = None
        self._model_key = DEFAULT_MODEL_EXISTING
        self._custom_model = ""
        self._fields: list = []

        self._relay = _Relay(self)
        self._relay.done.connect(self._on_bg_done)
        self.update_progress.connect(self._on_update_progress)
        self.update_download_failed.connect(self._on_update_download_failed)

        content = GuideCanvas()
        v = QVBoxLayout(content)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.head = PageHead("Настройки")
        v.addWidget(self.head)

        tabs_col = _TabColumn()
        self._tab_buttons: dict[str, _TabButton] = {}
        for key, text in TABS:
            b = _TabButton(key, text)
            b.clicked.connect(lambda _=False, k=key: self.show_tab(k))
            tabs_col.lay.addWidget(b)
            self._tab_buttons[key] = b
        tabs_col.lay.addStretch(1)

        right = _White()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        self._pages: dict[str, QWidget] = {}
        builders = {
            "record": self._build_record,
            "recognition": self._build_recognition,
            "calls": self._build_calls,
            "storage": self._build_storage,
            "hifi": self._build_hifi,
            "app": self._build_app,
        }
        for key, _ in TABS:
            page = QWidget()
            lay = QVBoxLayout(page)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(0)
            for w in builders[key]():
                lay.addWidget(w)
                if hasattr(w, "set_window_width"):
                    self._fields.append(w)
            page.hide()
            rv.addWidget(page)
            self._pages[key] = page
        rv.addStretch(1)
        self.footer = _Footer()
        self.footer.cancel.clicked.connect(self.reload)
        self.footer.save.clicked.connect(self._save)
        rv.addWidget(self.footer)

        self._layout_row = _SettingsLayout(tabs_col, right)
        v.addWidget(self._layout_row, 1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(PageScroll(content))

        self._tab = ""
        self.show_tab(TABS[0][0])
        self.reload()

    # ------------------------------------------------------------ вкладки
    def _build_record(self) -> list:
        self.mic_combo = ComboBox()
        # Имена драйверов бывают в полсотни символов — без этого список растянул
        # бы колонку под самое длинное имя.
        self.mic_combo.setSizeAdjustPolicy(ComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.mic_combo.setMinimumContentsLength(24)
        self.mic_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.mic_combo.currentIndexChanged.connect(self._on_mic_changed)
        f_mic = Field("Микрофон", self.mic_combo,
                      "«Системный» — то, что выбрано в Windows; после переустановки системы "
                      "или подключения гарнитуры он меняется сам.")

        self.mic_test = ActionField("Проверка микрофона", f"Тест записи · {MIC_CHECK_SECONDS} секунды",
                                    "Проверить", MIC_CHECK_HINT)
        self.mic_test.button.clicked.connect(self._on_check_mic)

        # QKeySequenceEdit: Qt берёт виртуальные коды Windows — не зависит от раскладки
        self.hotkey_edit = QKeySequenceEdit()
        try:
            self.hotkey_edit.setMaximumSequenceLength(1)
        except AttributeError:  # Qt < 6.5
            pass
        self.hotkey_edit.setStyleSheet(FIELD_QSS)
        inner = self.hotkey_edit.findChild(QLineEdit)
        if inner is not None:
            inner.setMinimumHeight(42)
            inner.setPlaceholderText("Нажмите сочетание клавиш")
        clear = Button("Очистить")
        clear.setMinimumHeight(42)
        clear.clicked.connect(self.hotkey_edit.clear)
        hk_row = QWidget()
        hl = QHBoxLayout(hk_row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(9)
        hl.addWidget(self.hotkey_edit, 1)
        hl.addWidget(clear, 0, Qt.AlignVCenter)
        f_hk = Field("Горячая клавиша диктовки", hk_row,
                     "Нажмите один раз для записи, ещё раз — чтобы завершить.")

        self.preroll_sw = Switch()
        self.preroll_sw.setToolTip("Микрофон постоянно активен в фоне и держит последние 500 мс аудио — "
                                   "первые слова не теряются. Windows показывает индикатор микрофона в трее.")
        f_pre = Field("Сохранять начало фразы · pre-roll", self.preroll_sw,
                      "Буфер 500 мс до нажатия клавиши. Микрофон постоянно работает в фоне.", inline=True)

        self.sound_dictation_sw = Switch()
        f_snd = Field("Звуки диктовки", self.sound_dictation_sw,
                      "Разные сигналы при начале и завершении записи.", inline=True)
        return [f_mic, self.mic_test, f_hk, f_pre, f_snd]

    def _build_recognition(self) -> list:
        self.model_field = ActionField("Активная модель", "", "Изменить")
        self.model_field.button.clicked.connect(lambda: self._win.navigate("models"))

        self.mode_combo = ComboBox()
        for key, text in MODES:
            self.mode_combo.addItem(text, key)
        self.mode_combo.currentIndexChanged.connect(self._sync_threshold_enabled)
        f_mode = Field("Режим обработки · batch / streaming", self.mode_combo,
                       "batch — один полный проход после стопа. streaming — запись распознаётся частями, "
                       "пока вы говорите, после стопа остаётся только хвост. Какой режим сработал "
                       "у каждой записи — в «Статистике».")

        self.threshold_spin = SpinBox(1, 60, 10, " с")
        f_thr = Field("Порог переключения batch → streaming", self.threshold_spin,
                      "Только для режима «Авто»: запись короче порога — batch, длиннее — streaming.")

        self.gpu_field = ActionField("Ускорение GPU", "", "Подключить")
        self.gpu_field.button.clicked.connect(self._on_cuda_button)

        self.dict_edit = TextEdit("", rows=4)
        self.dict_edit.setPlaceholderText(
            "Например: имена коллег, названия проектов, термины — через запятую "
            "или короткими фразами. Пусто — модель работает без подсказки."
        )
        self.dict_edit.setToolTip(
            "Текст уходит модели как initial_prompt: она начинает узнавать слова, "
            "которые до этого писала неправильно. Работает как подсказка, а не как "
            "жёсткая замена — для замен есть отдельный редактор ниже."
        )
        self.dict_edit.textChanged.connect(self._update_dict_counter)
        f_dict = Field("Словарь", self.dict_edit, "Имена, термины и названия — подсказка для модели.")
        # Счётчик бюджета: у Whisper на initial_prompt жёсткий лимит, и лишнее
        # молча отрезается с начала строки — поэтому число видно сразу.
        self.dict_counter = Label("", size=Font.HINT, color=Color.MUTED, wrap=True)
        # Длинный словарь в поле на четыре строки не прочитать — «Развернуть»
        # открывает его на весь лист поверх окна, со счётчиком токенов внизу.
        self.dict_toggle = Button("Развернуть", icon="expand", variant="ghost", small=True)
        self.dict_toggle.setToolTip("Открыть словарь на всё окно")
        self.dict_toggle.clicked.connect(self._open_dict_sheet)
        dict_row = QWidget()
        drl = QHBoxLayout(dict_row)
        drl.setContentsMargins(0, 0, 0, 0)
        drl.setSpacing(12)
        drl.addWidget(self.dict_counter, 1)
        drl.addWidget(self.dict_toggle, 0, Qt.AlignVCenter)
        f_dict._outer.addWidget(dict_row)

        self.repl_field = ActionField(
            "Замены в тексте", "", "Редактировать",
            "Правило «шаблон → замена» применяется к готовому тексту. "
            "Помогает, когда подсказки словаря недостаточно.")
        self.repl_field.button.clicked.connect(self._edit_replacements)
        return [self.model_field, f_mode, f_thr, self.gpu_field, f_dict, self.repl_field]

    def _build_calls(self) -> list:
        self.speaker_self_edit = LineEdit("", DEFAULT_SPEAKER_SELF)
        f_self = Field("Моё имя", self.speaker_self_edit)
        self.speaker_other_edit = LineEdit("", DEFAULT_SPEAKER_OTHER)
        f_other = Field("Имя собеседника", self.speaker_other_edit, "Эти имена подписывают реплики в транскрипте.")
        f_hk = Field("Горячая клавиша созвона", Label(CALL_HOTKEY_TEXT, size=13),
                     "В текущем приложении сочетание фиксировано.")
        self.sound_call_sw = Switch()
        f_snd = Field("Звуки созвона", self.sound_call_sw, "Отдельные сигналы начала и завершения.", inline=True)
        self.keep_call_audio_sw = Switch()
        f_mp3 = Field("Постоянно сохранять аудио в MP3", self.keep_call_audio_sw,
                      "MP3 128 кбит/с рядом с транскриптом в Calls. Нужен FFmpeg.", inline=True)
        self.call_keep_spin = SpinBox(1, 20, DEFAULT_CALL_AUDIO_KEEP)
        f_keep = Field("Буфер последних WAV-записей", self.call_keep_spin,
                       "Независимый буфер для повторного прослушивания и расшифровки. "
                       f"По умолчанию — {DEFAULT_CALL_AUDIO_KEEP}.")
        return [f_self, f_other, f_hk, f_snd, f_mp3, f_keep]

    def _build_storage(self) -> list:
        self.path_edit = LineEdit("")
        f_path = Field("Папка истории", self.path_edit)
        f_browse = ActionField("Выбор папки", "Выбрать папку в проводнике Windows", "Обзор…")
        f_browse.button.clicked.connect(self._browse_path)
        self.count_spin = SpinBox(3, 20, 5)
        f_count = Field("Количество диктовок в истории", self.count_spin, "Новая запись заменяет самую старую.")
        note = _Footnote("Транскрипты созвонов хранятся в Calls, заметки — без ротации. "
                         "Размер буфера аудио созвонов задаётся в разделе «Созвоны».")
        return [f_path, f_browse, f_count, note]

    def _build_hifi(self) -> list:
        self.hifi_sw = Switch()
        self.hifi_sw.setToolTip(
            f"Надиктовка пишется в подпапку {profile.HIFI_SUBDIR}\\ на выбранной частоте и не участвует "
            "в ротации истории — материал копится, пока режим включён. Распознавание не меняется: "
            "модель получает ту же запись, приведённую к 16 кГц. Обычная история диктовок в это "
            "время не пополняется."
        )
        self.hifi_sw.toggled.connect(self._sync_hifi_enabled)
        f_hifi = Field("Hi-fi диктовка", self.hifi_sw,
                       "Материал для клона голоса; распознавание работает как обычно.", inline=True)
        self.hifi_rate_combo = ComboBox()
        for rate in HIFI_SAMPLE_RATES:
            self.hifi_rate_combo.addItem(f"{rate:,} Гц".replace(",", " "), rate)
        f_rate = Field("Частота записи", self.hifi_rate_combo)

        prog = QWidget()
        pl = QHBoxLayout(prog)
        pl.setContentsMargins(0, 10, 0, 10)
        pl.setSpacing(18)
        self.hifi_digits = DigitStrip("0", height=74, color=Color.DIGIT_STAT, max_digit_width=58)
        pl.addWidget(self.hifi_digits, 0, Qt.AlignBottom)
        pl.addWidget(Label(f"из {HIFI_TARGET_MINUTES} минут", size=Font.HINT, color=Color.MUTED), 0, Qt.AlignBottom)
        pl.addStretch(1)
        self.hifi_progress = Field("Накопленный материал", prog, "")
        return [f_hifi, f_rate, self.hifi_progress]

    def _build_app(self) -> list:
        self.autostart_sw = Switch()
        f_auto = Field("Запускать вместе с Windows", self.autostart_sw, inline=True)
        self.startmin_sw = Switch()
        f_min = Field("Запускать свёрнутым в трей", self.startmin_sw,
                      "Приложение запускается в области уведомлений без открытого окна.", inline=True)
        self.updates_sw = Switch()
        f_upd = Field("Проверять обновления автоматически", self.updates_sw,
                      "Выключено — приложение само в сеть не ходит; проверить можно вручную.", inline=True)
        self.animations_sw = Switch()
        f_anim = Field("Анимации интерфейса", self.animations_sw,
                       "Прокрутка цифр таймера и переходы между разделами. Выключено — цифры "
                       "просто сменяются, разделы открываются сразу.", inline=True)
        try:
            version = updater.current_version()
        except Exception:  # noqa: BLE001
            version = "—"
        self.update_field = ActionField("Обновления", f"Текущая версия — {version}", "Проверить")
        self.update_field.button.clicked.connect(self._on_check_updates)
        self._update_available = updater.is_available()
        self.update_field.button.setEnabled(self._update_available)
        self.update_field.set_hint("" if self._update_available
                                   else "Обновления доступны только в установленной версии.")
        self.proxy_edit = LineEdit("", "http://127.0.0.1:3065")
        f_proxy = Field("Прокси загрузки моделей", self.proxy_edit,
                        "Пусто — настройки Windows. Например: http://127.0.0.1:3065. "
                        "Используется только для загрузок с Hugging Face.")
        f_wiz = ActionField("Первый запуск", "Микрофон, клавиши, модель и ускорение", "Пройти заново")
        f_wiz.button.clicked.connect(self._on_run_wizard)
        return [f_auto, f_min, f_anim, f_upd, self.update_field, f_proxy, f_wiz]

    # ------------------------------------------------------------ публичное
    def show_tab(self, key: str) -> None:
        """Показать вкладку (`record`, `recognition`, `calls`, `storage`, `hifi`, `app`)."""
        if key not in self._pages:
            return
        self._tab = key
        for k, page in self._pages.items():
            page.setVisible(k == key)
            self._tab_buttons[k].setChecked(k == key)
        if key == "hifi" and self._hifi_stale:
            self._request_hifi_minutes()

    def current_tab(self) -> str:
        return self._tab

    def reload(self) -> None:
        """Перечитать настройки с диска и сбросить несохранённые правки."""
        cur = load_settings_dict()
        self._current = cur
        self._cuda_declined = bool(cur.get("cuda_layer_declined", False))
        self._cuda_requested = False
        self._model_key = cur.get("model", DEFAULT_MODEL_EXISTING)
        self._custom_model = cur.get("custom_model", "") or ""

        # Запись
        self._fill_mic_combo(cur.get("mic_device", "") or "")
        if self._mic_test_device is None:  # идущий замер не трогаем: второй sd.rec поверх первого упал бы
            self.mic_test.button.setEnabled(True)
            self.mic_test.set_hint(MIC_CHECK_HINT)
        self.hotkey_edit.setKeySequence(QKeySequence(hotkey_to_qt(cur["hotkey"])))
        self.preroll_sw.setChecked(bool(cur.get("pre_roll_enabled", False)))
        self.sound_dictation_sw.setChecked(bool(cur.get("sound_notifications_dictation", True)))

        # Распознавание
        try:
            name = engine.spec_display(engine.spec_from_settings(cur))
        except Exception:  # noqa: BLE001
            name = str(cur.get("model", ""))
        self.model_field.value.setText(name)
        mode = cur.get("processing_mode", "auto")
        idx = self.mode_combo.findData(mode)
        self.mode_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.threshold_spin.setValue(max(1, min(60, int(cur.get("auto_threshold_sec", 10)))))
        self._sync_threshold_enabled()
        self.dict_edit.setPlainText(cur.get("dictionary", "") or "")
        self._update_dict_counter()
        self._replacement_rules = profile.load_replacement_rules()
        self._replacements_edited = False
        self._update_repl_label()
        self._request_cuda_state()

        # Созвоны
        self.speaker_self_edit.setText(cur.get("speaker_self", DEFAULT_SPEAKER_SELF))
        self.speaker_other_edit.setText(cur.get("speaker_other", DEFAULT_SPEAKER_OTHER))
        self.sound_call_sw.setChecked(bool(cur.get("sound_notifications_call", True)))
        self.keep_call_audio_sw.setChecked(bool(cur.get("keep_call_audio", False)))
        self.call_keep_spin.setValue(int(cur.get("call_audio_keep", DEFAULT_CALL_AUDIO_KEEP)))

        # Хранение
        self.path_edit.setText(cur.get("history_dir", DEFAULT_HISTORY_DIR))
        self.count_spin.setValue(int(cur.get("rotation_count", 5)))

        # Hi-fi
        self.hifi_sw.setChecked(bool(cur.get("hifi_enabled", DEFAULT_HIFI_ENABLED)))
        rate = int(cur.get("hifi_sample_rate", DEFAULT_HIFI_SAMPLE_RATE))
        ridx = self.hifi_rate_combo.findData(rate)
        self.hifi_rate_combo.setCurrentIndex(ridx if ridx >= 0 else 0)
        self._sync_hifi_enabled()
        self._hifi_stale = True
        if self._tab == "hifi":
            self._request_hifi_minutes()

        # Приложение
        self.autostart_sw.setChecked(bool(cur.get("autostart", False)))
        self.startmin_sw.setChecked(bool(cur.get("start_minimized", False)))
        self.updates_sw.setChecked(bool(cur.get("check_updates", True)))
        self.animations_sw.setChecked(bool(cur.get("ui_animations", True)))
        self.proxy_edit.setText(cur.get("download_proxy", DEFAULT_DOWNLOAD_PROXY))

    def values(self) -> dict:
        """Словарь для `save_settings_dict` — те же ключи, что у прежнего диалога."""
        mode = self.mode_combo.currentData() or "auto"
        # Модель настройками не трогаем — она из раздела «Модели», отдаём как
        # пришла, иначе «Сохранить» перетёр бы свежий выбор.
        return {
            "model": self._model_key,
            "custom_model": self._custom_model,
            "hotkey": self._hotkey_canonical(),
            "history_dir": self.path_edit.text().strip(),
            "mic_device": (self.mic_combo.currentData() or ""),  # пусто = системный
            "rotation_count": int(self.count_spin.value()),
            "autostart": bool(self.autostart_sw.isChecked()),
            "start_minimized": bool(self.startmin_sw.isChecked()),
            "processing_mode": mode,
            "auto_threshold_sec": int(self.threshold_spin.value()),
            "pre_roll_enabled": bool(self.preroll_sw.isChecked()),
            "hifi_enabled": bool(self.hifi_sw.isChecked()),
            "hifi_sample_rate": int(self.hifi_rate_combo.currentData() or DEFAULT_HIFI_SAMPLE_RATE),
            "keep_call_audio": bool(self.keep_call_audio_sw.isChecked()),
            "call_audio_keep": int(self.call_keep_spin.value()),
            "dictionary": self.dict_edit.toPlainText().strip(),
            "speaker_self": self.speaker_self_edit.text().strip() or DEFAULT_SPEAKER_SELF,
            "speaker_other": self.speaker_other_edit.text().strip() or DEFAULT_SPEAKER_OTHER,
            # нажали «Подключить» — прежний отказ снимаем, иначе следующий старт
            # считал бы, что от ускорения отказались навсегда
            "cuda_layer_declined": False if self._cuda_requested else self._cuda_declined,
            "check_updates": bool(self.updates_sw.isChecked()),
            "ui_animations": bool(self.animations_sw.isChecked()),
            "sound_notifications_dictation": bool(self.sound_dictation_sw.isChecked()),
            "sound_notifications_call": bool(self.sound_call_sw.isChecked()),
            "download_proxy": self.proxy_edit.text().strip(),
        }

    def replacement_rules(self) -> "list | None":
        """Правила замен, если редактор открывали, иначе None («файл не трогать»):
        иначе «Сохранить» переписывал бы правила, поправленные в файле руками."""
        return self._replacement_rules if self._replacements_edited else None

    def set_window_width(self, w: int) -> None:
        self._width = w
        self.head.set_window_width(w)
        self.footer.set_window_width(w)
        for f in self._fields:
            f.set_window_width(w)

    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        # Спонтанный показ — окно вернули из свёрнутого: правки не сбрасываем.
        if not ev.spontaneous():
            self.reload()

    # ------------------------------------------------------------ сохранение
    def _hotkey_canonical(self) -> str:
        seq = self.hotkey_edit.keySequence()
        if seq.isEmpty():
            return ""
        return hotkey_to_canonical(seq.toString())

    def _validate(self) -> bool:
        hotkey = self._hotkey_canonical()
        if not hotkey:
            self.show_tab("record")
            info_modal(self, "Горячая клавиша",
                       "Горячая клавиша не может быть пустой. Кликните в поле и нажмите сочетание.")
            return False
        ok, msg = parse_hotkey_valid(hotkey)
        if not ok:
            self.show_tab("record")
            info_modal(self, "Горячая клавиша", f"Сочетание не подходит: {msg}")
            return False
        if hotkey_conflicts_with_handy(hotkey):
            if not confirm(self, "Горячая клавиша",
                           "Это сочетание может пересекаться с Handy (ctrl_left+`) или системным "
                           "сочетанием. Сохранить всё равно?", ok_text="Сохранить", danger=False):
                return False
        return True

    def _save(self) -> bool:
        if not self._validate():
            return False
        self._win.emit_settings(self.values(), self.replacement_rules())
        toast = getattr(self._win, "toast", None)
        if toast is not None and hasattr(toast, "show_text"):
            toast.show_text("Настройки сохранены")
        self.reload()
        return True

    # ------------------------------------------------------------ фон
    def _bg(self, name: str, fn, callback) -> None:
        relay = self._relay

        def run() -> None:
            try:
                res = fn()
            except Exception as exc:  # noqa: BLE001
                res = _Failure(exc)
            try:
                relay.done.emit(callback, res)
            except RuntimeError:
                pass  # страницу уже удалили

        threading.Thread(target=run, daemon=True, name=name).start()

    @Slot(object, object)
    def _on_bg_done(self, callback, result) -> None:
        callback(result)

    # ------------------------------------------------------------ микрофон
    def _fill_mic_combo(self, chosen: str) -> None:
        """Первый пункт — «Системный» с именем того, что Windows считает
        системным сейчас; отключённое устройство показывается строкой, а не
        подменяется молча системным."""
        self.mic_combo.blockSignals(True)
        self.mic_combo.clear()
        try:
            default_name = audio_quality.default_input_name()
        except Exception:  # noqa: BLE001
            default_name = ""
        self.mic_combo.addItem(f"Системный микрофон — {default_name}" if default_name else "Системный микрофон", "")
        try:
            devices = audio_quality.input_devices()
        except Exception:  # noqa: BLE001
            devices = []
        for dev in devices:
            self.mic_combo.addItem(dev["name"], dev["name"])
        if chosen:
            idx = self.mic_combo.findData(chosen)
            if idx < 0:
                self.mic_combo.addItem(f"{chosen} — не подключён", chosen)
                idx = self.mic_combo.count() - 1
            self.mic_combo.setCurrentIndex(idx)
        else:
            self.mic_combo.setCurrentIndex(0)
        self.mic_combo.blockSignals(False)

    def _on_mic_changed(self) -> None:
        if self._mic_test_device is None:
            self.mic_test.set_hint(MIC_CHECK_HINT)

    def _on_check_mic(self) -> None:
        """3 секунды с выбранного устройства на 48 кГц → полоса и уровень.
        Устройство не открылось — так и говорим: это тоже ответ на «почему тихо»."""
        name = self.mic_combo.currentData() or ""
        self._mic_test_device = name
        self.mic_test.button.setEnabled(False)
        self.mic_test.set_hint("Идёт замер, говорите в микрофон…", Color.WARNING)

        def work():
            import sounddevice as sd

            index = audio_quality.resolve_device_index(name) if name else None
            data = sd.rec(int(MIC_CHECK_SECONDS * MIC_CHECK_RATE), samplerate=MIC_CHECK_RATE, channels=1,
                          dtype="float32", device=index)
            sd.wait()
            return audio_quality.check_samples(data[:, 0], MIC_CHECK_RATE)

        self._bg("mic-check", work, lambda res, dev=name: self._on_mic_checked(dev, res))

    def _on_mic_checked(self, device: str, res) -> None:
        self.mic_test.button.setEnabled(True)
        tested = self._mic_test_device
        self._mic_test_device = None
        if tested != device or (self.mic_combo.currentData() or "") != device:
            self.mic_test.set_hint(MIC_CHECK_HINT)  # пока шёл замер, выбрали другое устройство
            return
        if isinstance(res, _Failure):
            self.mic_test.set_hint(f"Устройство не открылось: {res}", Color.ERROR)
            return
        colour = Color.ERROR if res.narrowband else (Color.MUTED if res.measured else Color.WARNING)
        self.mic_test.set_hint(audio_quality.describe(res, hifi=self.hifi_sw.isChecked()), colour)

    # ------------------------------------------------------------ распознавание
    def _sync_threshold_enabled(self, *_):
        self.threshold_spin.setEnabled((self.mode_combo.currentData() or "auto") == "auto")

    def _update_dict_counter(self) -> None:
        line, color = self._dict_status(self.dict_edit.toPlainText())
        self.dict_counter.set_color(color)
        self.dict_counter.setText(line)

    def _open_dict_sheet(self) -> None:
        sheet = TextEditorSheet("Словарь", self.dict_edit.toPlainText(), self.dict_edit.placeholderText(),
                                status_fn=self._dict_status, parent=self)
        if sheet.exec() == TextEditorSheet.Accepted:
            self.dict_edit.setPlainText(sheet.text())
        sheet.deleteLater()

    @staticmethod
    def _dict_status(raw: str) -> tuple[str, str]:
        """«N / 223 токена»: точное число — токенизатор загруженной модели, иначе
        оценка, и это помечено, чтобы оценка не читалась как факт."""
        text = raw.strip()
        try:
            exact = engine.count_prompt_tokens(text)
        except Exception:  # noqa: BLE001
            exact = None
        budget = profile.PROMPT_TOKEN_BUDGET
        unit = _plural(budget, "токен", "токена", "токенов")
        if exact is None:
            n = profile.estimate_prompt_tokens(text)
            line = f"≈ {n} / {budget} {unit} · оценка"
        else:
            n = exact
            line = f"{n} / {budget} {unit}"
        if n > budget:
            line += " — превышение: модель отбросит начало словаря"
            color = Color.OVER_BUDGET
        else:
            color = Color.MUTED
        return line, color

    def _update_repl_label(self) -> None:
        n = len(self._replacement_rules)
        if not n:
            self.repl_field.value.setText("Правил нет")
            return
        text = f"{n} {_plural(n, 'правило', 'правила', 'правил')}"
        tail = sum(1 for r in self._replacement_rules if r.get("tail_only"))
        if tail:
            text += f" · из них {tail} только в конце текста"
        self.repl_field.value.setText(text)

    def _edit_replacements(self) -> None:
        dlg = ReplacementsModal(self, self._replacement_rules)
        if dlg.exec() == Modal.Accepted:
            self._replacement_rules = dlg.rules()
            self._replacements_edited = True
            self._update_repl_label()

    # --- ускорение GPU (CUDA-слой)
    def _request_cuda_state(self) -> None:
        self._cuda_seq += 1
        seq = self._cuda_seq
        self._cuda_state = None
        self.gpu_field.value.setText("Проверяю видеокарту…")
        self.gpu_field.button.hide()
        self.gpu_field.set_hint("")

        def probe() -> dict:
            installed = cuda_layer.is_installed()
            size = cuda_layer.installed_bytes() if installed else 0
            env = (not installed) and cuda_layer.runtime_in_environment()
            gpu = None if (installed or env) else cuda_layer.gpu()
            return {"installed": installed, "size": size, "env": env, "gpu": gpu}

        self._bg("cuda-state", probe, lambda res, s=seq: self._on_cuda_state(s, res))

    def _on_cuda_state(self, seq: int, res) -> None:
        if seq != self._cuda_seq:
            return
        f = self.gpu_field
        if isinstance(res, _Failure):
            self._cuda_state = None
            f.value.setText("Используется процессор")
            f.button.hide()
            f.set_hint(f"Не удалось проверить видеокарту: {res}")
            return
        self._cuda_state = res
        f.button.setEnabled(True)
        if res["installed"]:
            f.value.setText(f"CUDA подключена · {engine.fmt_bytes(res['size'])}")
            f.button.setText("Отключить")
            f.button.show()
            f.set_hint("Транскрипция идёт на видеокарте. Отключение освободит место, "
                       "приложение продолжит работать на процессоре.")
        elif res["env"]:
            f.value.setText("CUDA подключена · системная")
            f.button.hide()
            f.set_hint("CUDA уже есть в системе — отдельная докачка не нужна.")
        elif res["gpu"]:
            f.value.setText("Используется процессор")
            f.button.setText("Подключить")
            f.button.show()
            f.set_hint(f"Найдена {res['gpu'].get('name', 'NVIDIA')}. Докачка ~{cuda_layer.size_hint_mb()} МБ "
                       "заметно ускорит транскрипцию.")
        else:
            f.value.setText("Используется процессор")
            f.button.hide()
            f.set_hint("Видеокарта NVIDIA не найдена — работаем на процессоре.")

    def _on_cuda_button(self) -> None:
        st = self._cuda_state or {}
        if st.get("installed"):
            if not confirm(self, "Отключить ускорение GPU?",
                           "CUDA-слой будет удалён с диска, транскрипция перейдёт на процессор. "
                           "Подключить заново можно здесь же.", ok_text="Отключить"):
                return
            self.gpu_field.button.setEnabled(False)
            self._bg("cuda-remove", cuda_layer.remove, self._on_cuda_removed)
            return
        if not st.get("gpu"):
            return
        # Импорт здесь: transcribe_ui сам импортирует модуль окна, встречный
        # импорт на уровне файла замкнул бы цикл.
        from ...transcribe_ui import download_cuda_layer

        # Как прежний диалог: «Скачать» принимает настройки (с них снимается
        # отказ от слоя), потом стартует скачивание с собственным прогрессом.
        self._cuda_requested = True
        if not self._save():
            self._cuda_requested = False
            return
        download_cuda_layer(parent=self.window())

    def _on_cuda_removed(self, res) -> None:
        if isinstance(res, _Failure):
            info_modal(self, "Ускорение GPU", f"Не удалось удалить: {res}")
        else:
            _ok, msg = res
            info_modal(self, "Ускорение GPU", msg)
        self._request_cuda_state()

    # ------------------------------------------------------------ хранение
    def _browse_path(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Папка для истории записей", self.path_edit.text())
        if chosen:
            self.path_edit.setText(chosen)

    # ------------------------------------------------------------ hi-fi
    def _sync_hifi_enabled(self, *_):
        self.hifi_rate_combo.setEnabled(self.hifi_sw.isChecked())

    def _request_hifi_minutes(self) -> None:
        """Минуты — по факту WAV на диске при каждом показе: удалённые вручную
        файлы не должны числиться накопленными."""
        self._hifi_stale = False
        self._hifi_seq += 1
        seq = self._hifi_seq
        hdir = self._current.get("history_dir", DEFAULT_HISTORY_DIR)
        self.hifi_progress.set_hint("Считаю…")
        self._bg("hifi-count", lambda: hifi_accumulated_minutes(Path(hdir)),
                 lambda res, s=seq: self._on_hifi_minutes(s, res))

    def _on_hifi_minutes(self, seq: int, res) -> None:
        if seq != self._hifi_seq:
            return
        if isinstance(res, _Failure):
            self.hifi_digits.set_text("0")
            self.hifi_progress.set_hint(f"Не удалось прочитать {profile.HIFI_SUBDIR}\\", Color.ERROR)
            return
        self.hifi_digits.set_text(f"{float(res):.0f}")
        self.hifi_progress.set_hint(
            f"WAV в {profile.HIFI_SUBDIR}\\ внутри папки истории — ориентир для клона голоса. "
            "Сам режим не выключится: выключите его, когда материала хватит.")

    # ------------------------------------------------------------ приложение
    def _on_check_updates(self) -> None:
        self.update_field.button.setEnabled(False)
        self.update_field.set_hint("Проверяю…")
        self._bg("update-check", lambda: updater.check(quiet=False), self._on_update_checked)

    def _on_update_checked(self, res) -> None:
        """Итог проверки: предложение обновиться, «всё свежее» или причина отказа."""
        self.update_field.button.setEnabled(self._update_available)
        if isinstance(res, _Failure):
            exc = res.exc
            self.update_field.set_hint(f"Проверка не удалась: {exc.__class__.__name__}: {exc}", Color.ERROR)
            return
        info = res
        if info is None:
            self.update_field.set_hint(f"Установлена последняя версия ({updater.current_version()}).")
            return
        version = updater.version_of(info)
        self.update_field.set_hint(f"Доступна версия {version}.")
        notes = updater.notes_of(info)
        release_url = updater.release_page_url(info)

        # Не exec(): окно живёт всю закачку и крутит индикатор, пока velopack
        # молчит; закроется, когда появится настоящий прогресс-бар.
        dlg = Modal("Доступно обновление", self, width=560)
        dlg.setModal(False)
        text = Label("", size=Font.BODY, color=Color.MUTED, wrap=True)
        text.setTextFormat(Qt.RichText)
        text.setOpenExternalLinks(True)
        text.setTextInteractionFlags(Qt.TextBrowserInteraction)
        text.setText(
            f"<b style='color:{Color.INK}'>Версия {html.escape(version)} готова к установке.</b><br><br>"
            + ((html.escape(notes).replace("\n", "<br>") + "<br><br>") if notes else "")
            + f"Что изменилось: <a style='color:{Color.ACCENT}' href=\"{html.escape(release_url)}\">"
              "страница релиза</a>.<br><br>"
            + "Скачать и перезапустить приложение? Записи, настройки и скачанные модели останутся на месте."
        )
        dlg.body_layout.addWidget(text)
        no_btn = dlg.add_button(Button("Не сейчас"))
        yes_btn = dlg.add_button(_BusyButton("Скачать и перезапустить"))
        no_btn.clicked.connect(dlg.reject)

        def start_download() -> None:
            size_mb = updater.download_size_mb(info)
            yes_btn.start_busy(f"Готовлюсь к обновлению… ~{size_mb} МБ" if size_mb else "Готовлюсь к обновлению…")
            no_btn.setEnabled(False)
            self.update_field.button.setEnabled(False)
            self._update_offer_dialog = dlg
            progress, failed = self.update_progress, self.update_download_failed

            def run() -> None:
                # При успехе управление не возвращается: velopack перезапускает
                # процесс сам. Возврат — только ошибка.
                try:
                    updater.download_and_apply(info, progress_cb=lambda pct: progress.emit(int(pct)))
                except Exception as exc:  # noqa: BLE001
                    try:
                        failed.emit(str(exc))
                    except RuntimeError:
                        pass

            threading.Thread(target=run, daemon=True, name="update-apply").start()

        yes_btn.clicked.connect(start_download)
        dlg.show()

    def _close_update_offer(self) -> None:
        offer = self._update_offer_dialog
        if offer is not None:
            offer.close()
            self._update_offer_dialog = None

    @Slot(int)
    def _on_update_progress(self, percent: int) -> None:
        dlg = self._update_progress_dialog
        if dlg is None:
            # Шкала — только с первым реальным процентом: раньше она показала бы
            # пустую полосу на минуту.
            dlg = QProgressDialog("Скачиваю обновление…", "", 0, 100, self)
            dlg.setWindowTitle("Обновление SayType")
            dlg.setWindowModality(Qt.WindowModal)
            dlg.setMinimumDuration(0)
            dlg.setAutoClose(False)
            dlg.setAutoReset(False)
            self._update_progress_dialog = dlg
            dlg.show()
            QApplication.processEvents()  # быстрый апдейт мог бы перезапустить процесс до первой отрисовки
            self._close_update_offer()
        dlg.setValue(max(0, min(100, percent)))

    @Slot(str)
    def _on_update_download_failed(self, error: str) -> None:
        dlg = self._update_progress_dialog
        if dlg is not None:
            dlg.close()
            self._update_progress_dialog = None
        self._close_update_offer()
        self.update_field.button.setEnabled(self._update_available)
        self.update_field.set_hint(f"Обновление не установилось: {error}", Color.ERROR)
        info_modal(self, "Обновление не установилось",
                   f"Не получилось скачать или применить обновление:\n{error}\n\nПопробуйте ещё раз позже.")

    def _on_run_wizard(self) -> None:
        """Мастер пишет те же ключи сам; после него страница перечитывает их,
        чтобы старое состояние полей не перетёрло его выбор."""
        from ...transcribe_ui import run_first_run_wizard

        run_first_run_wizard(force=True)
        self.reload()
