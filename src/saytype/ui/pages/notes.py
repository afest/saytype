"""Страница «Заметки»: список слева (20 %), редактор справа.

Поведение прежнего `NotesDialog`/`NoteEditorPanel` сохранено: автосохранение
по таймеру после правки, «Диктовать → Завершить → Распознаю…» через
`window.set_note_dictation_target(self)` + `window._toggle_note_recording_via_ui`,
метка источника при первой диктовке, импорт файла в новую заметку, drag&drop,
защита от переключения во время диктовки.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPen, QTextCursor
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLineEdit, QPlainTextEdit, QSizePolicy,
                               QVBoxLayout, QWidget)

from ... import audio_import, profile
from ..icons import IconButton
from ..shell import GuideCanvas, PageScroll
from ..tokens import Color, Font, Grid, body_font, qc
from ..widgets import Button, Cell, Label, PageHead, companion_icon_button, confirm, info_modal

SOURCE_LABELS = {
    profile.NOTE_SOURCE_DICTATION: "Надиктовано",
    profile.NOTE_SOURCE_IMPORT: "Из файла",
    profile.NOTE_SOURCE_MANUAL: "Вручную",
}


def _when(mtime: float) -> str:
    d = _dt.datetime.fromtimestamp(mtime)
    today = _dt.date.today()
    if d.date() == today:
        return f"Сегодня, {d:%H:%M}"
    if d.date() == today - _dt.timedelta(days=1):
        return f"Вчера, {d:%H:%M}"
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря"]
    return f"{d.day} {months[d.month - 1]}"


def _split_title(text: str) -> tuple[str, str]:
    lines = text.split("\n")
    title = lines[0].strip() if lines else ""
    rest = "\n".join(lines[1:]).lstrip("\n") if len(lines) > 1 else ""
    return title, rest


class NoteItem(QWidget):
    clicked = Signal(object)

    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.path: Path = info["path"]
        self._title = info.get("title") or "Без названия"
        src = SOURCE_LABELS.get(info.get("source", ""), "Вручную")
        self._meta = f"{src} · {_when(info.get('mtime', 0.0)).split(',')[0]}"
        self._active = False
        self._hover = False
        self.setMinimumHeight(Grid.NOTE_ITEM_MIN_H)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_active(self, on: bool) -> None:
        self._active = on
        self.update()

    def enterEvent(self, ev) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, ev) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        self.clicked.emit(self.path)

    def sizeHint(self):  # noqa: N802
        from PySide6.QtCore import QSize
        return QSize(200, Grid.NOTE_ITEM_MIN_H)

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        W, H = self.width(), self.height()
        if self._active:
            p.fillRect(self.rect(), qc(Color.TINT))
            p.fillRect(QRectF(0, 0, 3, H), qc(Color.ACCENT))
        elif self._hover:
            p.fillRect(self.rect(), qc(Color.NAV_BG_HOVER))
        else:
            p.fillRect(self.rect(), qc(Color.BG))
        p.setPen(QPen(qc(Color.LINE), 1))
        p.drawLine(0, H - 1, W, H - 1)
        p.setFont(body_font(13))
        p.setPen(qc(Color.ACCENT if self._active else Color.INK))
        rect = QRectF(18, 24, W - 36, H - 24 - 30)
        p.drawText(rect, Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, self._title)
        p.setFont(body_font(Font.TINY))
        p.setPen(qc(Color.NOTE_ACTIVE_META if self._active else Color.MUTED))
        p.drawText(QRectF(18, H - 36, W - 36, 20), Qt.AlignLeft | Qt.AlignVCenter, self._meta)
        p.end()


class NotesPage(QWidget):
    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._w = window
        self.path: Path | None = None
        self._recording = False
        self._items: dict[Path, NoteItem] = {}
        self._drop_highlight = False
        self.setAcceptDrops(window._file_import_api is not None)

        content = GuideCanvas()
        v = QVBoxLayout(content)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.head = PageHead("Заметки")
        self.new_btn = Button("Новая заметка", icon="plus", variant="primary")
        self.new_btn.clicked.connect(self._new_note)
        # «Расшифровать файл» — иконкой рядом с «Новой заметкой», а не строкой над
        # списком: оба действия создают заметку (08.10).
        self.import_btn = companion_icon_button(
            "upload", "Расшифровать аудио- или видеофайл в новую заметку — выбрать или перетащить сюда")
        self.import_btn.clicked.connect(self._import_note)
        if window._file_import_api is not None:
            self.head.add_action(self.import_btn)
        self.head.add_action(self.new_btn)
        v.addWidget(self.head)

        layout = Cell()
        hl = QHBoxLayout(layout)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(0)

        # --- список ---
        self.list_col = Cell(bottom=False, right=True)
        lv = QVBoxLayout(self.list_col)
        # справа 1 px: строки заметок заливают себя целиком и закрывали линию колонки —
        # разделитель «список | заметка» пропадал, как только заметка открыта
        lv.setContentsMargins(0, 0, 1, 0)
        lv.setSpacing(0)
        self.list_layout = lv
        hl.addWidget(self.list_col, 1)

        # --- редактор ---
        self.editor = QWidget()
        self.editor.setAutoFillBackground(False)
        ev_ = QVBoxLayout(self.editor)
        ev_.setContentsMargins(0, 0, 0, 0)
        ev_.setSpacing(0)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Название заметки")
        self.title_edit.setFont(body_font(Font.NOTE_TITLE, 400, -0.5))
        self.title_edit.setFixedHeight(Grid.HEAD)
        self.title_edit.setStyleSheet(
            f"QLineEdit{{border:0;background:transparent;padding:0 {Grid.PAD}px;color:{Color.INK};}}")
        self.title_edit.textEdited.connect(self._on_text_changed)
        ev_.addWidget(self.title_edit)

        self.toolbar = Cell(top=True)
        tl = QHBoxLayout(self.toolbar)
        tl.setContentsMargins(Grid.PAD, 14, Grid.PAD, 14)
        tl.setSpacing(10)
        self.toolbar.setMinimumHeight(65)
        self.meta_label = Label("", size=Font.SMALL, color=Color.MUTED)
        self.delete_btn = IconButton("trash", size=Grid.BTN_SQUARE, color=Color.DANGER, hover_color=Color.DANGER,
                                     tooltip="Удалить заметку")
        self.delete_btn.clicked.connect(self._delete_current)
        tl.addWidget(self.meta_label, 1)
        tl.addWidget(self.delete_btn)
        ev_.addWidget(self.toolbar)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText("Начните писать или надиктуйте мысль…")
        self.text_edit.setFont(body_font(Font.BODY))
        self.text_edit.setFrameShape(QPlainTextEdit.NoFrame)
        self.text_edit.setStyleSheet(
            f"QPlainTextEdit{{border:0;background:transparent;padding:28px {Grid.PAD}px;color:{Color.INK};"
            f"selection-background-color:{Color.TINT};selection-color:{Color.INK};}}")
        self.text_edit.setMinimumHeight(350)
        self.text_edit.setAcceptDrops(False)
        self.text_edit.textChanged.connect(self._on_text_changed)
        self.text_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        ev_.addWidget(self.text_edit, 1)

        self.bottom = Cell(top=True, bottom=False)
        bl = QHBoxLayout(self.bottom)
        bl.setContentsMargins(Grid.PAD, 20, Grid.PAD, 20)
        bl.setSpacing(12)
        self.bottom.setMinimumHeight(84)
        self.dictate_btn = Button("Диктовать", icon="mic")
        self.dictate_btn.clicked.connect(self._on_dictate_clicked)
        self.save_state = Label("", size=Font.SMALL, color=Color.MUTED)
        self.copy_btn = IconButton("copy", size=Grid.BTN_SQUARE, tooltip="Копировать")
        self.copy_btn.clicked.connect(self._copy)
        bl.addWidget(self.dictate_btn)
        bl.addStretch(1)
        bl.addWidget(self.save_state)
        bl.addStretch(1)
        bl.addWidget(self.copy_btn)
        ev_.addWidget(self.bottom)

        self.empty = Label("Создайте первую заметку", size=Font.BODY, color=Color.MUTED)
        self.empty.setAlignment(Qt.AlignCenter)
        hl.addWidget(self.editor, 4)
        hl.addWidget(self.empty, 4)
        self.layout_cell = layout
        v.addWidget(layout, 1)

        self.scroll = PageScroll(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.scroll)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._save_now)
        self._set_editor_visible(False)
        self.rebuild()

    # --- адаптив / показ --------------------------------------------------------
    def set_window_width(self, w: int) -> None:
        self.head.set_window_width(w)
        pad = Grid.pad(w)
        self.toolbar.layout().setContentsMargins(pad, 14, pad, 14)
        self.bottom.layout().setContentsMargins(pad, 20, pad, 20)

    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        self.rebuild()

    def hideEvent(self, ev) -> None:  # noqa: N802
        self.save_now_if_dirty()
        super().hideEvent(ev)

    def _set_editor_visible(self, on: bool) -> None:
        self.editor.setVisible(on)
        self.empty.setVisible(not on)

    # --- список -----------------------------------------------------------------
    def rebuild(self) -> None:
        entries = profile.list_notes()
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._items.clear()
        if not entries:
            empty = Label("Пока нет заметок", size=Font.BODY, color=Color.MUTED)
            empty.setContentsMargins(20, 18, 20, 18)  # без отступа подпись липла к линии колонки
            self.list_layout.addWidget(empty)
        for info in entries:
            it = NoteItem(info)
            it.clicked.connect(self._open_note)
            it.set_active(info["path"] == self.path)
            self._items[info["path"]] = it
            self.list_layout.addWidget(it)
        self.list_layout.addStretch(1)
        if self.path is not None and self.path not in self._items:
            self.clear()
        self._w.shell.sidebar.buttons["notes"].set_count(len(entries) or None)

    def select_note(self, path: Path) -> None:
        if path.exists():
            self.path = path
            self.rebuild()
            self.load(path)

    def _guard_recording(self) -> bool:
        if self._recording:
            info_modal(self._w, "Идёт диктовка", "Останови диктовку («Завершить»), прежде чем переключаться.")
            return False
        return True

    def _new_note(self) -> None:
        if not self._guard_recording():
            return
        self.save_now_if_dirty()
        path = profile.new_note_path()
        profile.write_note(path, "")
        self.path = path
        self.rebuild()
        self.load(path)
        self.title_edit.setFocus()

    def _open_note(self, path: Path) -> None:
        if path == self.path:
            return
        if not self._guard_recording():
            return
        self.save_now_if_dirty()
        for p, it in self._items.items():
            it.set_active(p == path)
        self.path = path
        self.load(path)

    def _delete_current(self) -> None:
        if self.path is None or not self._guard_recording():
            return
        title = self.title_edit.text().strip() or "без названия"
        if not confirm(self._w, "Удалить заметку?", f"Заметка «{title}» будет удалена без возможности восстановить.",
                       "Удалить заметку"):
            return
        path = self.path
        self.clear()
        profile.delete_note(path)
        self.rebuild()

    # --- редактор --------------------------------------------------------------
    def load(self, path: Path) -> None:
        self.path = path
        text = profile.read_note(path)
        title, body = _split_title(text)
        self.title_edit.blockSignals(True)
        self.text_edit.blockSignals(True)
        self.title_edit.setText(title)
        self.text_edit.setPlainText(body)
        self.title_edit.blockSignals(False)
        self.text_edit.blockSignals(False)
        self._set_editor_visible(True)
        self._refresh_meta()
        self.save_state.setText("Сохранено")
        self.dictate_btn.setEnabled(self._w._toggle_note_recording_via_ui is not None)
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.text_edit.setTextCursor(cursor)

    def _refresh_meta(self) -> None:
        if self.path is None:
            return
        meta = profile.read_note_meta(self.path)
        src = SOURCE_LABELS.get(meta.get("source", ""), "Вручную")
        name = meta.get("source_name") or ""
        try:
            when = _when(self.path.stat().st_mtime)
        except OSError:
            when = ""
        self.meta_label.setText(f"{src}{(' · ' + name) if name else ''} · {when}")

    def clear(self) -> None:
        if self._save_timer.isActive():
            self._save_timer.stop()
        self.path = None
        self.title_edit.blockSignals(True)
        self.text_edit.blockSignals(True)
        self.title_edit.setText("")
        self.text_edit.setPlainText("")
        self.title_edit.blockSignals(False)
        self.text_edit.blockSignals(False)
        self._set_editor_visible(False)
        self.dictate_btn.setEnabled(False)

    def _on_text_changed(self, *_) -> None:
        if self.path is not None:
            self.save_state.setText("Сохраняю…")
            self._save_timer.start()

    def save_now_if_dirty(self) -> None:
        if self.path is not None and self._save_timer.isActive():
            self._save_now()

    def _save_now(self) -> None:
        if self._save_timer.isActive():
            self._save_timer.stop()
        if self.path is None:
            return
        title = self.title_edit.text().strip()
        body = self.text_edit.toPlainText()
        profile.write_note(self.path, f"{title}\n\n{body}" if (title or body) else "")
        self.save_state.setText("Сохранено")
        if self.path in self._items:
            # заголовок в списке мог измениться
            self.rebuild()

    def _full_text(self) -> str:
        title = self.title_edit.text().strip()
        body = self.text_edit.toPlainText()
        return f"{title}\n\n{body}".strip()

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._full_text())
        self._w.toast.show_text("Скопировано в буфер")

    # --- диктовка --------------------------------------------------------------
    def is_recording(self) -> bool:
        return self._recording

    def _on_dictate_clicked(self) -> None:
        toggle = self._w._toggle_note_recording_via_ui
        if toggle is None or self.path is None:
            return
        if not self._recording:
            if self._w.model_busy():
                info_modal(self._w, "Занято", "Идёт другая запись или транскрипция — дождись конца.")
                return
            self._recording = True
            self.dictate_btn.setText("Завершить")
            self.dictate_btn.set_icon_name("stop")
            self._w.set_note_dictation_target(self)
            toggle()
        else:
            self._recording = False
            self.dictate_btn.setEnabled(False)
            self.dictate_btn.setText("Распознаю…")
            self.dictate_btn.set_icon_name(None)
            toggle()

    def note_dictation_ended(self) -> None:
        self._recording = False
        self.dictate_btn.setEnabled(self.path is not None)
        self.dictate_btn.setText("Диктовать")
        self.dictate_btn.set_icon_name("mic")

    def append_dictation_text(self, text: str) -> None:
        self.note_dictation_ended()
        if not text or self.path is None:
            return
        was_empty = not self.text_edit.toPlainText().strip()
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(("" if was_empty else "\n") + text)
        self._save_now()
        if was_empty:
            meta = profile.read_note_meta(self.path)
            if meta.get("source") == profile.NOTE_SOURCE_MANUAL:
                profile.write_note_meta(self.path, source=profile.NOTE_SOURCE_DICTATION)
                self._refresh_meta()
                self.rebuild()

    # --- импорт ----------------------------------------------------------------
    def _import_note(self) -> None:
        if self._w._file_import_api is None or not self._guard_recording():
            return
        chosen, _ = QFileDialog.getOpenFileName(self, "Аудио или видео для расшифровки", "",
                                                audio_import.FILE_DIALOG_FILTER)
        if chosen:
            self._import_file(Path(chosen))

    def _import_file(self, path: Path) -> None:
        if not self._guard_recording():
            return
        self.save_now_if_dirty()
        note_path = self._w.import_file_to_note(path)
        if note_path is None:
            return
        self.path = note_path
        self.rebuild()
        self.load(note_path)

    def dragEnterEvent(self, ev) -> None:  # noqa: N802
        if self._w._dragged_audio_path(ev) is not None:
            ev.acceptProposedAction()
            self._set_drop(True)
        else:
            ev.ignore()

    def dragMoveEvent(self, ev) -> None:  # noqa: N802
        self.dragEnterEvent(ev)

    def dragLeaveEvent(self, ev) -> None:  # noqa: N802
        self._set_drop(False)

    def dropEvent(self, ev) -> None:  # noqa: N802
        self._set_drop(False)
        p = self._w._dragged_audio_path(ev)
        if p is None:
            ev.ignore()
            return
        ev.acceptProposedAction()
        QTimer.singleShot(0, lambda: self._import_file(p))

    def _set_drop(self, on: bool) -> None:
        if on == self._drop_highlight:
            return
        self._drop_highlight = on
        self.text_edit.setStyleSheet(
            f"QPlainTextEdit{{border:1px dashed {Color.ACCENT};background:{Color.DROP_OVER};padding:28px {Grid.PAD}px;color:{Color.INK};}}"
            if on else
            f"QPlainTextEdit{{border:0;background:transparent;padding:28px {Grid.PAD}px;color:{Color.INK};"
            f"selection-background-color:{Color.TINT};selection-color:{Color.INK};}}")
