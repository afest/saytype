"""Временные страницы разделов, которые ещё не перенесены на V5 (этап 4):
шапка в новой оболочке + кнопка, открывающая прежний диалог. Заменяются
полноценными страницами по мере переноса; контракт — `set_window_width`.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..shell import GuideCanvas, PageScroll
from ..tokens import Color, Font, Grid
from ..widgets import Button, Cell, Label, PageHead


class LegacyDialogPage(QWidget):
    def __init__(self, title: str, hint: str, button_text: str, opener, parent=None):
        super().__init__(parent)
        content = GuideCanvas()
        v = QVBoxLayout(content)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.head = PageHead(title)
        v.addWidget(self.head)
        body = Cell()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(Grid.PAD, 35, Grid.PAD, 35)
        bl.setSpacing(18)
        self.hint = Label(hint, size=Font.BODY, color=Color.MUTED, wrap=True)
        self.button = Button(button_text, variant="primary")
        self.button.clicked.connect(opener)
        bl.addWidget(self.hint)
        bl.addWidget(self.button, 0, Qt.AlignLeft)
        v.addWidget(body)
        v.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(PageScroll(content))

    def set_window_width(self, w: int) -> None:
        self.head.set_window_width(w)


def make_placeholder_pages(window) -> dict[str, QWidget]:
    """Страницы на прежних диалогах; каждая заменяется при переносе раздела."""
    from ...transcribe_ui_window import (AboutDialog, ModelsDialog, NotesDialog, SettingsDialog, StatsDialog,
                                        load_settings_dict, save_settings_dict)
    from ... import profile

    def open_settings():
        current = load_settings_dict()
        dlg = SettingsDialog(window, current, model_locked=window.model_busy())
        if dlg.exec() != 1:
            return
        new = dlg.values()
        window.emit_settings(new, dlg.replacement_rules())

    def open_models():
        before = load_settings_dict()
        dlg = ModelsDialog(window, before, model_locked=window.model_busy())
        dlg.exec()
        after = load_settings_dict()
        window.refresh_nomodel_bar()
        if (after.get("model"), after.get("custom_model")) != (before.get("model"), before.get("custom_model")):
            window.apply_settings_to_pages(after)
            window.settings_changed.emit(after)

    def open_notes():
        dlg = NotesDialog(
            window,
            toggle_note_recording_via_ui=window._toggle_note_recording_via_ui,
            model_busy_getter=window._model_busy_getter,
            register_dictation_target=window.set_note_dictation_target,
            import_file_to_note=(window.import_file_to_note if window._file_import_api is not None else None),
            select=None,
        )
        dlg.exec()

    def open_stats():
        StatsDialog(window, window._history_dir_getter() / "_stats.jsonl").exec()

    def open_about():
        AboutDialog(window).exec()

    return {
        "notes": LegacyDialogPage("Заметки", "Раздел переносится на новый интерфейс. Пока — прежнее окно.",
                                  "Открыть заметки", open_notes),
        "models": LegacyDialogPage("Модели", "Раздел переносится на новый интерфейс. Пока — прежнее окно.",
                                   "Открыть модели", open_models),
        "stats": LegacyDialogPage("Статистика", "Раздел переносится на новый интерфейс. Пока — прежнее окно.",
                                  "Открыть статистику", open_stats),
        "settings": LegacyDialogPage("Настройки", "Раздел переносится на новый интерфейс. Пока — прежнее окно.",
                                     "Открыть настройки", open_settings),
        "about": LegacyDialogPage("О приложении", "Раздел переносится на новый интерфейс. Пока — прежнее окно.",
                                  "Открыть «О программе»", open_about),
    }
