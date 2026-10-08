"""Страницы «Диктовки» и «Созвоны»: заголовок, большой таймер, строка действия
(кнопка, «из файла», микрофон, режим, модель), список последних записей с плеером.

Время — от монотонных часов, которые даёт окно (`set_elapsed`), страница
только показывает актуальные секунды: пропущенные тики не проигрываются.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics, QPainter
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QSpacerItem, QVBoxLayout, QWidget

from ..digits import ClockGrid
from ..history import AudioPlayer, RecordRow
from ..icons import icon_pixmap
from ..shell import GuideCanvas, PageScroll
from ..tokens import Color, Font, Grid, body_font, qc
from ..widgets import Button, Cell, Heading, Label, LevelDot, Spinner, companion_icon_button, level_icon


class _InstrumentHead(Cell):
    """94 px: только h1. Микрофон и состояние живут в строке с кнопкой (`_Controls`):
    шапка — название раздела, остальное — рядом с действием (08.10)."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent=parent)
        self.setFixedHeight(Grid.HEAD)
        self.title = Heading(title)
        row = QHBoxLayout(self)
        row.setContentsMargins(Grid.PAD, 0, Grid.PAD, 0)
        row.addWidget(self.title, 0, Qt.AlignVCenter)
        row.addStretch(1)
        self._row = row

    def set_window_width(self, w: int) -> None:
        self.title.set_size(Grid.h1_size(w))
        self._row.setContentsMargins(Grid.pad(w), 0, Grid.pad(w), 0)


class _NoticeBar(Cell):
    """Полоса «модель не скачана» (T-405) над таймером; видна только при проблеме."""

    open_models = Signal()

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(Grid.PAD, 14, Grid.PAD, 14)
        row.setSpacing(14)
        self.text = Label("", size=Font.SMALL, color=Color.ERROR, wrap=True)
        self.btn = Button("Открыть «Модели»", small=True)
        self.btn.clicked.connect(self.open_models.emit)
        row.addWidget(self.text, 1)
        row.addWidget(self.btn, 0, Qt.AlignVCenter)
        self.hide()

    def set_reason(self, reason: str) -> None:
        self.text.setText(reason)
        self.setVisible(bool(reason))


class _Controls(Cell):
    """88 px: primary-кнопка, «из файла» (у диктовки) и hotkey слева; справа —
    ссылки с шевроном: микрофон, режим обработки (у диктовки), модель."""

    def __init__(self, kind: str, parent=None):
        super().__init__(parent=parent)
        self.kind = kind
        self._recording = False
        self._level_step: int | None = None
        self._hotkey = ""
        self.setFixedHeight(Grid.CONTROLS)
        row = QHBoxLayout(self)
        row.setContentsMargins(Grid.PAD, 0, Grid.PAD, 0)
        row.setSpacing(20)
        self.button = Button(self._idle_text(), icon=self._idle_icon(), variant="primary")
        self.button.setMinimumWidth(Grid.BTN_PRIMARY_MIN_W)
        self.spinner = Spinner(15, "#9AA5F2", "#FFFFFF")
        self.spinner.hide()
        # «Распознаю…» встаёт на место подсказки hotkey: рядом с кнопкой, которая
        # в это время говорит «Отменить», — шапка остаётся только с названием.
        self.kbd = Label("", size=Font.KBD, color=Color.KBD)
        row.addWidget(self.button, 0, Qt.AlignVCenter)
        self.import_btn = None
        if kind == "voice":
            self.import_btn = companion_icon_button(
                "upload", "Расшифровать аудио- или видеофайл — выбрать или перетащить в окно")
            row.addSpacing(-10)  # пара «записать + из файла» плотнее остальных промежутков
            row.addWidget(self.import_btn, 0, Qt.AlignVCenter)
        row.addWidget(self.kbd, 0, Qt.AlignVCenter)
        row.addStretch(1)
        self.mic_link = _MicLink()
        self.mic_link.setToolTip("Микрофон — открыть настройки записи")
        row.addWidget(self.mic_link, 0, Qt.AlignVCenter)
        # добавка к шагу раскладки между ссылками; в узком окне снимается (set_window_width)
        self._link_gaps: list[QSpacerItem] = []

        def gap() -> None:
            sp = QSpacerItem(self.LINK_GAP, 0, QSizePolicy.Fixed, QSizePolicy.Minimum)
            row.addSpacerItem(sp)
            self._link_gaps.append(sp)

        gap()
        # режим обработки (batch / streaming) — только у диктовки: созвон всегда распознаётся после записи
        self.mode_link = _EngineLink()
        self.mode_link.setToolTip("Режим обработки — открыть настройки распознавания")
        row.addWidget(self.mode_link, 0, Qt.AlignVCenter)
        self.mode_link.setVisible(kind == "voice")  # после addWidget: без родителя мелькнул бы окном
        if kind == "voice":
            gap()
        self.model_link = _EngineLink()
        self.model_link.setToolTip("Модель распознавания — открыть «Модели»")
        row.addWidget(self.model_link, 0, Qt.AlignVCenter)
        self._row = row

    def _idle_text(self) -> str:
        return "Записать созвон" if self.kind == "call" else "Начать запись"

    def _idle_icon(self) -> str:
        return "phone" if self.kind == "call" else "mic"

    def set_state(self, state: str, other_active: bool, can_cancel: bool) -> None:
        self.button.setEnabled(True)
        self._recording = state == "recording" and not other_active
        self._level_step = None
        if other_active:
            self.button.setText("Другая запись активна")
            self.button.set_icon_name(self._idle_icon())
            self.button.setEnabled(False)
        elif state == "recording":
            self.button.setText("Завершить")
            self.button.set_icon_name(None)
            self.set_level(0.0)
        elif state == "processing":
            self.button.setText("Отменить" if can_cancel else "Распознаю…")
            self.button.set_icon_name(None)
            self.button.setEnabled(can_cancel)
        elif state == "cancelling":
            self.button.setText("Останавливаю…")
            self.button.set_icon_name(None)
            self.button.setEnabled(False)
        else:
            self.button.setText(self._idle_text())
            self.button.set_icon_name(self._idle_icon())

    def set_level(self, level: float) -> None:
        """Во время записи точка в кнопке «Завершить» дышит в такт голосу."""
        if not self._recording:
            return
        step = int(round(max(0.0, min(1.0, level)) * 8))
        if step == self._level_step:
            return
        self._level_step = step
        self.button.setIcon(level_icon(step, dpr=self.button.devicePixelRatioF()))
        self.button.setIconSize(QSize(16, 16))

    def set_hotkey(self, text: str) -> None:
        self._hotkey = text
        self.kbd.setText(text)

    def set_busy_text(self, text: str) -> None:
        """«Распознаю…» / «Останавливаю…» вместо hotkey; пусто — снова hotkey.

        Только там же, где помещается hotkey: в узком окне надпись делала строку шире
        окна, и на время распознавания вся страница растягивалась, а потом сжималась
        обратно (08.10). Там состояние и так видно — по кнопке и плашке записи."""
        self.kbd.setText(text or self._hotkey)
        self.kbd.setVisible(self._kbd_fits)

    def set_window_width(self, w: int) -> None:
        self._row.setContentsMargins(Grid.pad(w), 0, Grid.pad(w), 0)
        self._kbd_fits = w > 1100
        self.kbd.setVisible(self._kbd_fits)
        # в узком окне имя микрофона короче и ссылки плотнее — иначе строка шире окна
        # и вся страница режется справа (минимум окна и размер по умолчанию — 760 px)
        self.mic_link.set_tier(_MicLink.FULL if w > 1100 else (_MicLink.SHORT if w > 900 else _MicLink.WORD))
        for sp in self._link_gaps:
            sp.changeSize(self.LINK_GAP if w > 900 else 0, 0, QSizePolicy.Fixed, QSizePolicy.Minimum)
        self._row.invalidate()

    _kbd_fits = True
    LINK_GAP = 18


class _EngineLink(QWidget):
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._hover = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFont(body_font(11))
        self.setFixedHeight(24)
        self.setAttribute(Qt.WA_Hover, True)

    def set_text(self, text: str) -> None:
        self._text = text
        from PySide6.QtGui import QFontMetrics
        self.setFixedWidth(QFontMetrics(self.font()).horizontalAdvance(text) + 6 + 14 + 4)
        self.update()

    def enterEvent(self, ev) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, ev) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        self.clicked.emit()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        color = Color.ACCENT if self._hover else Color.MUTED
        p.setPen(qc(color))
        p.setFont(self.font())
        p.drawText(QRectF(0, 0, self.width() - 20, self.height()), Qt.AlignVCenter | Qt.AlignLeft, self._text)
        p.drawPixmap(QPointF(self.width() - 14, (self.height() - 14) / 2),
                     icon_pixmap("chevron", 14, color, None, False, self.devicePixelRatioF()))
        p.end()


class _MicLink(_EngineLink):
    """Микрофон в строке с кнопкой: точка уровня, имя, шеврон → настройки записи.

    Точка во время записи дышит в такт голосу — видно, что микрофон слышит;
    «Идёт запись» текстом не пишем, это видно по таймеру и кнопке.
    """

    DOT = 22  # LevelDot рисует ореол вокруг точки — место под него входит в размер

    # Ширина строки с кнопкой ограничена окном (минимум и размер по умолчанию — 760 px):
    # имя микрофона ужимается ступенями, полное — всегда в подсказке.
    FULL, SHORT, WORD = "full", "short", "word"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full = ""
        self._short = ""
        self._tier = self.FULL
        self.dot = LevelDot(self.DOT, parent=self)
        self.dot.move(0, (self.height() - self.DOT) // 2)

    def set_mic_name(self, name: str) -> None:
        if "микрофон" in name.lower():  # Windows обычно сам называет «Микрофон (…)»
            self._full = name
        else:
            self._full = f"Микрофон · {name}" if name else "Микрофон"
        # короткое — только устройство: «Микрофон (DJI MIC MINI) · системный» → «DJI MIC MINI»
        short = name.split(" · ")[0].strip()
        if "(" in short and short.endswith(")"):
            short = short[short.index("(") + 1:-1].strip()
        self._short = short or "Микрофон"
        self._apply()

    def set_tier(self, tier: str) -> None:
        if tier != self._tier:
            self._tier = tier
            self._apply()

    def _apply(self) -> None:
        fm = QFontMetrics(self.font())
        if self._tier == self.FULL:
            shown = fm.elidedText(self._full, Qt.ElideRight, 260)
        elif self._tier == self.SHORT:
            shown = fm.elidedText(self._short, Qt.ElideRight, 130)
        else:
            shown = "Микрофон"
        self.setToolTip("Микрофон — открыть настройки записи" + (f"\n{self._full}" if shown != self._full else ""))
        self.set_text(shown)

    def set_text(self, text: str) -> None:
        super().set_text(text)
        self.setFixedWidth(self.width() + self._text_x())

    def _text_x(self) -> int:
        return self.DOT  # текст сразу за ореолом точки: дышащий ореол не наезжает на буквы

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        color = Color.ACCENT if self._hover else Color.MUTED
        p.setPen(qc(color))
        p.setFont(self.font())
        x = self._text_x()
        p.drawText(QRectF(x, 0, self.width() - x - 20, self.height()), Qt.AlignVCenter | Qt.AlignLeft, self._text)
        p.drawPixmap(QPointF(self.width() - 14, (self.height() - 14) / 2),
                     icon_pixmap("chevron", 14, color, None, False, self.devicePixelRatioF()))
        p.end()


class _EmptyRow(Cell):
    def __init__(self, text: str, parent=None):
        super().__init__(parent=parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(Grid.PAD, 40, Grid.PAD, 40)
        self.label = Label(text, size=Font.BODY, color=Color.MUTED)
        lay.addWidget(self.label, 0, Qt.AlignHCenter)


class CapturePage(QWidget):
    """kind = "voice" | "call"."""

    record_clicked = Signal()
    cancel_clicked = Signal()
    models_requested = Signal()
    mode_requested = Signal()            # ссылка режима обработки → настройки «Распознавание»
    mic_requested = Signal()             # ссылка микрофона → настройки «Запись»
    import_requested = Signal()
    copy_requested = Signal(object)      # RecordRow
    note_requested = Signal(object)      # RecordRow
    delete_requested = Signal(object)    # RecordRow — окно спрашивает и уносит файлы в Корзину
    player_started = Signal()            # окно ставит на паузу плеер другой страницы

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self._state = "idle"
        self._other_active = False
        self._can_cancel = True
        self._last_seconds = 0
        self._rows: list[RecordRow] = []
        self._keys: list[tuple] = []
        self._empty: QWidget | None = None
        self._active_row: RecordRow | None = None
        self._window_width = Grid.WINDOW_W

        content = GuideCanvas()
        v = QVBoxLayout(content)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self.head = _InstrumentHead("Созвоны" if kind == "call" else "Диктовки")
        self.notice = _NoticeBar()
        self.notice.open_models.connect(self.models_requested.emit)
        self.clock = ClockGrid()
        self.clock_cell = _ClockCell(self.clock)
        self.controls = _Controls(kind)
        self.controls.button.clicked.connect(self._on_button)
        self.controls.model_link.clicked.connect(self.models_requested.emit)
        self.controls.mode_link.clicked.connect(self.mode_requested.emit)
        self.controls.mic_link.clicked.connect(self.mic_requested.emit)
        self.import_btn = self.controls.import_btn
        if self.import_btn is not None:
            self.import_btn.clicked.connect(self.import_requested.emit)
        # Меньше служебного на экране (08.10): заголовка «Последние …» со счётчиком
        # и кнопок «Хранить N» / «Буфер WAV N» нет — хранение настраивается в
        # Настройках, строки идут сразу под кнопкой. Подписи «Я · Микрофон» /
        # «Собеседник · Звук компьютера» у созвона тоже сняты: микрофон — в ссылке.

        v.addWidget(self.head)
        v.addWidget(self.notice)
        v.addWidget(self.clock_cell)
        v.addWidget(self.controls)
        self.list_box = QWidget()
        self.list_box.setAutoFillBackground(False)
        self.list_layout = QVBoxLayout(self.list_box)
        # снизу 6 px: полоса перемотки последней строки растёт и под её край
        self.list_layout.setContentsMargins(0, 0, 0, 6)
        self.list_layout.setSpacing(0)
        v.addWidget(self.list_box)
        v.addStretch(1)

        self.scroll = PageScroll(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.scroll)

        self.player = AudioPlayer(self)
        self.player.position_changed.connect(self._on_position)
        self.player.duration_changed.connect(self._on_duration)
        self.player.state_changed.connect(self._on_player_state)
        self.player.finished.connect(self._on_finished)
        self.set_state("idle", False)

    # --- адаптив ----------------------------------------------------------
    def set_window_width(self, w: int) -> None:
        self._window_width = w
        self.head.set_window_width(w)
        self.clock.set_window_height(Grid.clock_height(w))
        self.controls.set_window_width(w)
        for r in self._rows:
            r.set_window_width(w)

    # --- состояние записи -------------------------------------------------
    def set_hotkey(self, text: str) -> None:
        self.controls.set_hotkey(text)

    def set_model_name(self, name: str) -> None:
        self.controls.model_link.set_text(name)

    def set_mode_text(self, text: str) -> None:
        self.controls.mode_link.set_text(text)

    def set_can_cancel(self, on: bool) -> None:
        self._can_cancel = on
        self.controls.set_state(self._state, self._other_active, on)

    def state(self) -> str:
        return self._state

    def set_state(self, state: str, other_active: bool) -> None:
        """idle | recording | processing | cancelling; other_active — идёт запись другой категории."""
        prev = self._state
        self._state = state
        self._other_active = other_active
        self.controls.set_state(state, other_active, self._can_cancel)
        self.controls.mic_link.dot.set_live(state == "recording")
        if state == "recording":
            self.controls.set_busy_text("")
            if prev != "recording":
                self.clock.roll_to_zero()
                self.clock.set_recording(True)
        elif state in ("processing", "cancelling"):
            self.controls.set_busy_text("Распознаю…" if state == "processing" else "Останавливаю…")
        else:
            self.controls.set_busy_text("")
            self.clock.set_recording(False)

    def set_level(self, level: float) -> None:
        """Уровень голоса 0..1 (уже огибающая): точка у микрофона и в кнопке «Завершить»."""
        if self._state != "recording":
            return
        self.controls.mic_link.dot.set_level(level)
        self.controls.set_level(level)

    def set_mic_name(self, name: str) -> None:
        self.controls.mic_link.set_mic_name(name)

    def set_elapsed(self, seconds: int) -> None:
        if self._state == "recording":
            self.clock.set_seconds(seconds)

    def remember_duration(self, seconds: int) -> None:
        """После успешной обработки: показать длительность серым и запомнить её."""
        self._last_seconds = max(0, int(seconds))
        self.clock.set_recording(False)
        self.clock.set_seconds(self._last_seconds, animate=False)

    def restore_last(self) -> None:
        """Отмена: вернуть время последней успешно завершённой записи."""
        self.clock.set_recording(False)
        self.clock.set_seconds(self._last_seconds, animate=False)

    def last_seconds(self) -> int:
        return self._last_seconds

    def _on_button(self) -> None:
        if self._state == "processing":
            self.cancel_clicked.emit()
        else:
            self.record_clicked.emit()

    # --- история ------------------------------------------------------------
    @staticmethod
    def _entry_key(e: dict) -> tuple:
        """Ключ строки: тот же файл и те же версии txt/meta/md → строку не пересоздаём.

        Текст и метрики дописываются ядром после wav, поэтому в ключ входят их mtime.
        """
        def mt(path) -> float:
            try:
                return Path(path).stat().st_mtime if path else 0.0
            except OSError:
                return 0.0

        if e.get("md_path"):
            return ("call", str(e["md_path"]), e.get("mtime") or mt(e["md_path"]))
        wav = e.get("wav_path")
        txt = e.get("txt_path") or (Path(wav).with_suffix(".txt") if wav else None)
        meta = Path(wav).parent / f"{Path(wav).stem}.meta.json" if wav else None
        return ("voice", str(wav), mt(txt), mt(meta))

    def set_entries(self, entries: list[dict]) -> None:
        """Обновить список. Неизменившиеся строки переиспользуются: после новой
        записи создаётся одна строка, а не весь список (дешевле для GUI-потока
        сразу после распознавания и не сбивает плеер)."""
        keys = [self._entry_key(e) for e in entries]
        if keys == self._keys and (self._rows or not entries) and self._empty is None and entries:
            return
        if not entries and self._empty is not None and not self._rows:
            return
        old_rows = dict(zip(self._keys, self._rows))
        # снять всё из раскладки, не удаляя переиспользуемые строки
        while self.list_layout.count():
            self.list_layout.takeAt(0)
        if self._empty is not None:
            self._empty.deleteLater()
            self._empty = None
        new_rows: list[RecordRow] = []
        for key, e in zip(keys, entries):
            row = old_rows.pop(key, None)
            if row is None:
                row = RecordRow(e, self.kind)
                row.set_window_width(self._window_width)
                row.play_requested.connect(self._on_row_play)
                row.pause_requested.connect(lambda r: self.player.pause())
                row.seek_requested.connect(lambda r, s: self.player.seek(s))
                row.copy_requested.connect(self.copy_requested.emit)
                row.note_requested.connect(self.note_requested.emit)
                row.delete_requested.connect(self.delete_requested.emit)
            self.list_layout.addWidget(row)
            new_rows.append(row)
        for row in old_rows.values():  # ушли в ротацию
            if row is self._active_row:
                self.player.stop()
                self._active_row = None
            row.hide()
            row.deleteLater()
        if not entries:
            self._empty = _EmptyRow(
                "Созвонов пока нет — Ctrl + Shift + E начнёт запись." if self.kind == "call"
                else "Диктовок пока нет — нажмите горячую клавишу и говорите.")
            self.list_layout.addWidget(self._empty)
        self._rows = new_rows
        self._keys = keys

    def release_row(self, row: RecordRow) -> None:
        """Перед удалением: если аудио строки загружено в плеер — закрыть файл."""
        if row is self._active_row or (row.audio_path is not None and self.player.source() == row.audio_path):
            self.player.release()
            row.set_audio_state(False, False)
            if row is self._active_row:
                self._active_row = None

    def first_entry_duration(self) -> int:
        for r in self._rows:
            return int(r.duration_sec)
        return 0

    def _on_row_play(self, row: RecordRow) -> None:
        if row.audio_path is None:
            return
        if self._active_row is not None and self._active_row is not row:
            self._active_row.set_audio_state(False, False)
        self._active_row = row
        row.set_audio_state(True, True)
        self.player_started.emit()
        self.player.play(row.audio_path)

    def pause_player(self) -> None:
        if self.player.is_playing():
            self.player.pause()

    def _on_position(self, sec: float) -> None:
        if self._active_row is not None:
            self._active_row.set_position(sec, self.player.duration())

    def _on_duration(self, sec: float) -> None:
        if self._active_row is not None and sec > 0:
            if self._active_row.duration_sec < 1:
                self._active_row.duration_sec = sec

    def _on_player_state(self, playing: bool) -> None:
        if self._active_row is not None:
            self._active_row.set_audio_state(True, playing)

    def _on_finished(self) -> None:
        if self._active_row is not None:
            self._active_row.set_finished()


class _ClockCell(Cell):
    """Обёртка таймера без заливки: направляющие видны сквозь окна цифр."""

    def __init__(self, clock: ClockGrid, parent=None):
        super().__init__(fill=False, parent=parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(clock)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
