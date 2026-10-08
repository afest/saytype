"""Главное окно SayType V5 — тот же контракт, что у прежнего `MainWindow`
(`transcribe_ui_window.py`): конструктор с теми же именованными параметрами,
методы `notify_*`, `show_window`, сигналы `settings_changed`, `quit_requested`,
`stop_sound_requested`, `transcription_done`. `transcribe_ui.py` не меняется.

Внутри — оболочка V5 (`shell.AppShell`): sidebar, страницы, переходы, тост;
поверх — два top-level индикатора записи (`overlays`). Отдельной плашки записи
внутри окна нет: она дублировала индикатор поверх Windows и закрывала кнопки.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMainWindow, QMenu, QMessageBox, QSystemTrayIcon

from .. import audio_import, engine, profile
from ..transcribe_ui_window import (CALL_CARDS_MAX, load_settings_dict, read_call_entries, read_history_entries,
                                    save_settings_dict, create_autostart_shortcut, remove_autostart_shortcut)
from .icons import app_qicon
from .overlays import FloatingStatus
from .pages.capture import CapturePage
from .pages.placeholders import make_placeholder_pages
from .shell import AppShell
from .tokens import Color, Grid, Motion, load_fonts
from .trayguard import tray_icon_registered
from .history import move_files_to_trash
from .widgets import APP_QSS, FIELD_QSS, LevelFollower, Toast, confirm, info_modal

TRAY_GUARD_MS = 15_000


class MainWindow(QMainWindow):
    settings_changed = Signal(dict)
    quit_requested = Signal()
    transcription_done = Signal()
    stop_sound_requested = Signal()
    _state_change_requested = Signal(str)
    _audio_level_requested = Signal(float)
    _call_state_requested = Signal(bool)
    _call_transcript_requested = Signal(str)
    _call_alert_requested = Signal(bool)
    _call_processing_requested = Signal(bool, float)
    _call_progress_requested = Signal(float)
    _note_text_requested = Signal(str)
    _note_dictation_ended_requested = Signal()
    _error_requested = Signal(str, str, str)
    _cancelling_requested = Signal()
    _cancelled_requested = Signal(str)

    def __init__(
        self,
        *,
        idle_icon_path: Path,
        recording_icon_path: Path,
        processing_icon_path: Path | None = None,
        app_icon_path: Path | None = None,
        history_dir_getter: Callable[[], Path],
        rotation_count_getter: Callable[[], int],
        entry_script: Path,
        toggle_recording_via_ui: Callable[[], None] | None = None,
        toggle_call_via_ui: Callable[[], None] | None = None,
        toggle_note_recording_via_ui: Callable[[], None] | None = None,
        model_busy_getter: Callable[[], bool] | None = None,
        file_import_api: "audio_import.FileImportApi | None" = None,
        cancel_transcription: Callable[[], bool] | None = None,
        model_missing_getter: Callable[[], str] | None = None,
    ) -> None:
        super().__init__()
        load_fonts()
        self._history_dir_getter = history_dir_getter
        self._rotation_count_getter = rotation_count_getter
        self._entry_script = entry_script
        self._toggle_recording_via_ui = toggle_recording_via_ui
        self._toggle_call_via_ui = toggle_call_via_ui
        self._toggle_note_recording_via_ui = toggle_note_recording_via_ui
        self._model_busy_getter = model_busy_getter
        self._file_import_api = file_import_api
        self._cancel_transcription = cancel_transcription
        self._model_missing_getter = model_missing_getter
        self._note_dictation_target = None
        self._last_error_sig = ""
        self._last_error_at = 0.0
        self._error_box = None
        self._user_quit = False
        self._state = "idle"
        self._call_active = False
        self._call_processing = False
        self._rec_started_at = 0.0
        self._call_rec_started = 0.0
        # Память таймера (V5): после успешной обработки показываем длительность
        # ЗАПИСИ, снятую в момент стопа, — время распознавания в неё не входит.
        self._rec_duration = 0
        self._dictation_ok = False
        self._call_rec_duration = 0
        self._entries: list[dict] = []
        self._call_entries: list[dict] = []

        self.setWindowTitle("SayType")
        self.resize(Grid.WINDOW_W, Grid.WINDOW_H)
        self.setMinimumSize(Grid.WINDOW_MIN_W, Grid.WINDOW_MIN_H)
        self.setAcceptDrops(True)
        self.setStyleSheet(f"QMainWindow{{background:{Color.BG};}}" + FIELD_QSS)
        app = QApplication.instance()
        if app is not None and not app.styleSheet():
            app.setStyleSheet(APP_QSS)  # окна вне `ui` (ошибки, прогресс, обновление) — в той же системе

        # Иконки V5 — знак на плитке из `icons`, а не файлы из профиля: пути
        # остаются в конструкторе ради общего контракта с прежним окном. Цвет
        # плитки в трее — состояние, поэтому со старыми кружками не спутать.
        self._idle_icon = app_qicon(Color.APP_TILE)
        self._recording_icon = app_qicon(Color.APP_TILE_RECORDING)
        self._processing_icon = app_qicon(Color.APP_TILE_PROCESSING)
        self._app_icon = self._idle_icon
        self.setWindowIcon(self._app_icon)
        if app is not None:
            app.setWindowIcon(self._app_icon)  # диалоги и окна без родителя — с тем же знаком

        # --- оболочка и страницы ---
        self.shell = AppShell()
        self.setCentralWidget(self.shell)
        self.page_home = CapturePage("voice")
        self.page_calls = CapturePage("call")
        self.shell.host.add_page("home", self.page_home)
        self.shell.host.add_page("calls", self.page_calls)
        self.toast = Toast(self.shell.host)
        placeholders = make_placeholder_pages(self)
        self._pages_ready = {}
        for pid, factory in self._page_factories().items():
            try:
                page = factory()
            except Exception as exc:  # noqa: BLE001 — раздел ещё не перенесён: прежний диалог
                print(f"[ui] страница {pid} недоступна ({exc!r}), используется прежний диалог", file=sys.stderr)
                page = placeholders[pid]
            self.shell.host.add_page(pid, page)
            self._pages_ready[pid] = page
        self.shell.host.width_changed.connect(self._on_main_width)
        self._level = LevelFollower()

        for page in (self.page_home, self.page_calls):
            page.models_requested.connect(lambda: self.navigate("models"))
            page.mic_requested.connect(lambda: self._open_settings_tab("record"))
            page.copy_requested.connect(self._on_row_copy)
            page.note_requested.connect(self._on_row_note)
            page.delete_requested.connect(self._on_row_delete)
        self.page_home.mode_requested.connect(lambda: self._open_settings_tab("recognition"))
        self.page_home.record_clicked.connect(self._on_record_clicked)
        self.page_home.cancel_clicked.connect(self._on_cancel_clicked)
        self.page_home.import_requested.connect(self.open_file_import)
        self.page_calls.record_clicked.connect(self._on_call_clicked)
        self.page_calls.cancel_clicked.connect(self._on_cancel_clicked)
        self.page_home.player_started.connect(self.page_calls.pause_player)
        self.page_calls.player_started.connect(self.page_home.pause_player)
        self.page_home.set_can_cancel(cancel_transcription is not None)
        self.page_calls.set_can_cancel(False)

        # --- плавающие индикаторы ---
        self._overlay = FloatingStatus("voice", anchor_getter=lambda: self)
        self._call_overlay = FloatingStatus("call", anchor_getter=lambda: self)
        self._overlay.cancel_clicked.connect(self._on_cancel_clicked)

        # --- таймеры (только во время записи) ---
        self._rec_timer = QTimer(self)
        self._rec_timer.setInterval(200)
        self._rec_timer.timeout.connect(self._tick_rec_time)
        self._call_timer = QTimer(self)
        self._call_timer.setInterval(200)
        self._call_timer.timeout.connect(self._tick_call_time)
        self._rec_last_sec = -1
        self._call_last_sec = -1

        # --- мосты из worker-потоков ---
        self._state_change_requested.connect(self.set_state)
        self._audio_level_requested.connect(self._on_audio_level)
        self._call_state_requested.connect(self._on_call_state)
        self._call_transcript_requested.connect(self._on_call_transcript)
        self._call_alert_requested.connect(self._on_call_alert)
        self._call_processing_requested.connect(self._on_call_processing)
        self._call_progress_requested.connect(self._on_call_progress)
        self._note_text_requested.connect(self._on_note_text_ready)
        self._note_dictation_ended_requested.connect(self._on_note_dictation_ended)
        self._error_requested.connect(self._on_error)
        self._cancelling_requested.connect(self._on_cancelling)
        self._cancelled_requested.connect(self._on_cancelled)
        self.transcription_done.connect(self._mark_dictation_ok)
        self.transcription_done.connect(self.refresh_history)

        self._build_tray()
        self.apply_settings_to_pages(load_settings_dict())
        self.refresh_history()
        self.shell.navigate("home")

    def _page_factories(self) -> dict:
        """Фабрики страниц разделов; отсутствующий модуль → прежний диалог (placeholders)."""
        def notes():
            from .pages.notes import NotesPage
            return NotesPage(self)

        def about():
            from .pages.about import AboutPage
            return AboutPage(self)

        def settings():
            from .pages.settings import SettingsPage
            return SettingsPage(self)

        def models():
            from .pages.models import ModelsPage
            return ModelsPage(self)

        def stats():
            from .pages.stats import StatsPage
            return StatsPage(self)

        return {"notes": notes, "models": models, "stats": stats, "settings": settings, "about": about}

    # ------------------------------------------------------------------ трей
    def _build_tray(self) -> None:
        self._tray = QSystemTrayIcon(self._idle_icon, self)
        self._tray.setToolTip("SayType · готов")
        menu = QMenu()
        a_open = QAction("Открыть окно", self)
        a_open.triggered.connect(self.show_window)
        self._cancel_action = QAction("Отменить распознавание", self)
        self._cancel_action.setEnabled(False)
        self._cancel_action.triggered.connect(self._on_cancel_clicked)
        a_settings = QAction("Настройки…", self)
        a_settings.triggered.connect(lambda: (self.show_window(), self.navigate("settings")))
        a_about = QAction("О программе…", self)
        a_about.triggered.connect(lambda: (self.show_window(), self.navigate("about")))
        a_donate = QAction("Поддержать разработку…", self)
        a_donate.triggered.connect(self.open_donate)
        a_quit = QAction("Выход", self)
        a_quit.triggered.connect(self._quit)
        for a in (a_open, self._cancel_action, a_settings, a_about, a_donate):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(a_quit)
        self._tray_menu = menu
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()
        # Значок пропадает, если Проводник перезапустился (после сна) и не
        # принял повторную регистрацию, — см. trayguard. Проверка дешёвая.
        self._tray_guard = QTimer(self)
        self._tray_guard.setInterval(TRAY_GUARD_MS)
        self._tray_guard.timeout.connect(self._ensure_tray_registered)
        self._tray_guard.start()

    def _ensure_tray_registered(self) -> None:
        if self._user_quit or not self._tray.isVisible():
            return
        if tray_icon_registered() is False:
            print("[tray] значок не зарегистрирован у Проводника — ставлю заново", file=sys.stderr)
            self._tray.hide()
            self._tray.show()

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.DoubleClick, QSystemTrayIcon.ActivationReason.Trigger):
            self.show_window()

    def _update_tray(self) -> None:
        if self._state == "recording":
            self._tray.setIcon(self._recording_icon)
            self._tray.setToolTip("SayType · запись")
        elif self._state == "processing":
            self._tray.setIcon(self._processing_icon)
            self._tray.setToolTip("SayType · транскрибирую…")
        elif self._call_processing:
            self._tray.setIcon(self._processing_icon)
            self._tray.setToolTip("SayType · обработка созвона…")
        elif self._call_active:
            self._tray.setIcon(self._recording_icon)
            self._tray.setToolTip("SayType · запись созвона")
        else:
            self._tray.setIcon(self._idle_icon)
            self._tray.setToolTip("SayType · готов")

    # ------------------------------------------------------------- окно
    def closeEvent(self, event) -> None:  # noqa: N802
        if self._user_quit:
            event.accept()
            return
        event.ignore()
        self.hide()

    def show_window(self) -> None:
        self.refresh_history()
        self.setWindowState((self.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def navigate(self, pid: str) -> None:
        self.shell.navigate(pid)

    def _on_main_width(self, w: int) -> None:
        total = self.width()
        for pid in ("home", "calls", "notes", "models", "stats", "settings", "about"):
            page = self.shell.host.page(pid)
            if page is not None and hasattr(page, "set_window_width"):
                page.set_window_width(total)

    def _quit(self) -> None:
        self._user_quit = True
        self.quit_requested.emit()
        self._tray.hide()
        for o in (self._overlay, self._call_overlay):
            try:
                o.hide()
                o.deleteLater()
            except Exception:  # noqa: BLE001
                pass
        QApplication.instance().quit()

    # ------------------------------------------------------- notify (контракт)
    @Slot()
    def notify_transcription_done(self) -> None:
        self.transcription_done.emit()

    @Slot()
    def notify_stop_sound(self) -> None:
        self.stop_sound_requested.emit()

    def notify_set_state(self, state: str) -> None:
        self._state_change_requested.emit(state)

    def notify_audio_level(self, level: float) -> None:
        self._audio_level_requested.emit(level)

    def notify_call_state(self, active: bool) -> None:
        self._call_state_requested.emit(bool(active))

    def notify_call_transcript(self, text: str) -> None:
        self._call_transcript_requested.emit(text)

    def notify_call_alert(self, silent: bool) -> None:
        self._call_alert_requested.emit(bool(silent))

    def notify_call_processing(self, active: bool, total_sec: float = 0.0) -> None:
        self._call_processing_requested.emit(bool(active), float(total_sec))

    def notify_call_progress(self, fraction: float) -> None:
        self._call_progress_requested.emit(float(fraction))

    def notify_note_text(self, text: str) -> None:
        self._note_text_requested.emit(text or "")

    def notify_cancelling(self) -> None:
        self._cancelling_requested.emit()

    def notify_cancelled(self, text: str) -> None:
        self._cancelled_requested.emit(text)

    def notify_error(self, title: str, text: str, hint: str = "") -> None:
        self._error_requested.emit(str(title or "Сбой"), str(text or ""), str(hint or ""))

    def notify_note_dictation_ended(self) -> None:
        self._note_dictation_ended_requested.emit()

    def set_note_dictation_target(self, panel) -> None:
        self._note_dictation_target = panel

    # ------------------------------------------------------- состояние диктовки
    @Slot(str)
    def set_state(self, state: str) -> None:
        prev = self._state
        self._state = state
        # Диктовка и созвон независимы, как и в ядре: диктовка во время созвона идёт
        # срезом его микрофона, а кнопки категорий друг друга не блокируют.
        self.page_home.set_state(state, other_active=False)
        self._cancel_action.setEnabled(state == "processing" and self._cancel_transcription is not None)
        if state == "recording":
            self._rec_started_at = time.monotonic()
            self._rec_last_sec = -1
            self._dictation_ok = False
            self._level.reset()
            if prev != "recording":
                self._refresh_mic_name()  # устройство за «системным» могло смениться
            self._overlay.show_recording()
            self._rec_timer.start()
        elif state == "processing":
            self._rec_timer.stop()
            if prev == "recording" and self._rec_started_at:
                self._rec_duration = int(time.monotonic() - self._rec_started_at)
                self.page_home.set_elapsed(self._rec_duration)
            self._overlay.show_processing(can_cancel=self._cancel_transcription is not None)
        else:
            self._rec_timer.stop()
            if prev in ("processing", "recording"):
                if self._dictation_ok:
                    # успешная обработка: длительность записи остаётся серой
                    self.page_home.remember_duration(self._rec_duration)
                else:
                    # отмена, пустая запись или ошибка — прежнее успешное время
                    self.page_home.restore_last()
            self._dictation_ok = False
            if self._overlay.mode() in ("recording", "processing", "cancelling"):
                self._overlay.hide_overlay()
        self._update_tray()

    @Slot()
    def _mark_dictation_ok(self) -> None:
        """`transcription_done` приходит только с успешной обработки (ядро шлёт его
        после сохранения txt/stats) — по нему таймер и запоминает длительность."""
        if self._state in ("processing", "recording"):
            self._dictation_ok = True

    def _call_page_state(self) -> str:
        if self._call_processing:
            return "processing"
        return "recording" if self._call_active else "idle"

    @Slot()
    def _tick_rec_time(self) -> None:
        if self._state != "recording":
            self._rec_timer.stop()
            return
        sec = int(time.monotonic() - self._rec_started_at)
        if sec != self._rec_last_sec:
            self._rec_last_sec = sec
            self.page_home.set_elapsed(sec)
            self._overlay.set_seconds(sec)

    def _on_audio_level(self, rms: float) -> None:
        """RMS от ядра (20 раз в секунду, только во время диктовки) → точки «микрофон слышит»."""
        if self._state != "recording":
            return
        level = self._level.push(rms)
        self._overlay.update_audio_level(level)
        self.page_home.set_level(level)

    def _on_record_clicked(self) -> None:
        if self._state == "processing":
            return
        if self._toggle_recording_via_ui is not None:
            self._toggle_recording_via_ui()
        else:
            QMessageBox.warning(self, "Запись", "Колбэк записи не подключён.")

    def _on_call_clicked(self) -> None:
        if self._toggle_call_via_ui is not None:
            self._toggle_call_via_ui()

    def _on_cancel_clicked(self) -> None:
        if self._cancel_transcription is None:
            return
        try:
            self._cancel_transcription()
        except Exception as exc:  # noqa: BLE001
            print(f"[ui] cancel fail: {exc}", file=sys.stderr, flush=True)

    @Slot()
    def _on_cancelling(self) -> None:
        self._overlay.show_cancelling()
        self.page_home.set_state("cancelling", other_active=False)
        self._cancel_action.setEnabled(False)

    @Slot(str)
    def _on_cancelled(self, text: str) -> None:
        self.page_home.restore_last()
        QTimer.singleShot(0, lambda: self._overlay.show_info(text))
        self.toast.show_text(text)

    @Slot(str, str, str)
    def _on_error(self, title: str, text: str, hint: str) -> None:
        signature = f"{title}|{text}"
        now = time.time()
        if signature == self._last_error_sig and now - self._last_error_at < 30:
            return
        self._last_error_sig, self._last_error_at = signature, now
        try:
            self._overlay.show_error(text or title)
        except Exception:  # noqa: BLE001
            pass
        body = f"{text}\n\n{hint}" if hint else text
        try:
            box = QMessageBox(QMessageBox.Warning, title, body, QMessageBox.Ok, self)
            box.setAttribute(Qt.WA_DeleteOnClose)
            box.setWindowModality(Qt.NonModal)
            self._error_box = box
            box.show()
            box.raise_()
        except Exception as exc:  # noqa: BLE001
            print(f"[window] окно ошибки не открылось: {exc}", file=sys.stderr, flush=True)

    # ------------------------------------------------------------- созвон
    @Slot(bool)
    def _on_call_state(self, active: bool) -> None:
        self._call_active = active
        if active:
            self._call_processing = False
            self._call_rec_started = time.monotonic()
            self._call_last_sec = -1
            self._call_overlay.show_recording()
            self.page_calls.set_state("recording", other_active=False)
            if not self._call_timer.isActive():
                self._call_timer.start()
        else:
            self._call_processing = False
            self._call_timer.stop()
            self._call_overlay.hide_overlay()
            self.page_calls.set_state("idle", other_active=False)
            # ядро шлёт «обработка закончилась» раньше «созвон выключен», поэтому
            # опора — длительность, снятая при стопе, а не флаг обработки
            if self._call_rec_duration > 0:
                # длительность записи созвона, снятая на стопе; время обработки не входит
                self.page_calls.remember_duration(self._call_rec_duration)
            else:
                self.page_calls.restore_last()
            self._call_rec_started = 0.0
            self._call_rec_duration = 0
        self._update_tray()

    @Slot(bool, float)
    def _on_call_processing(self, active: bool, total_sec: float) -> None:
        self._call_processing = active
        if active:
            if total_sec and total_sec > 0:
                self._call_rec_duration = int(round(total_sec))
            elif self._call_rec_started:
                self._call_rec_duration = int(time.monotonic() - self._call_rec_started)
            self.page_calls.set_elapsed(self._call_rec_duration)
            self._call_overlay.show_processing(can_cancel=False)
            self.page_calls.set_state("processing", other_active=False)
            if not self._call_timer.isActive():
                self._call_timer.start()
        else:
            self._call_overlay.hide_overlay()
        self._update_tray()

    @Slot(float)
    def _on_call_progress(self, fraction: float) -> None:
        if self._call_processing:
            self._call_overlay.set_progress(fraction)

    @Slot()
    def _tick_call_time(self) -> None:
        if self._call_active and not self._call_processing:
            sec = int(time.monotonic() - self._call_rec_started)
            if sec != self._call_last_sec:
                self._call_last_sec = sec
                self.page_calls.set_elapsed(sec)
                self._call_overlay.set_seconds(sec)
        elif not self._call_processing:
            self._call_timer.stop()

    @Slot(str)
    def _on_call_transcript(self, text: str) -> None:
        try:
            self.refresh_history()
        except Exception:  # noqa: BLE001
            pass
        self.toast.show_text("Созвон расшифрован — транскрипт в списке созвонов")
        if not self.isVisible():
            self._call_overlay.show_info("Созвон расшифрован", 4)

    @Slot(bool)
    def _on_call_alert(self, silent: bool) -> None:
        if self._call_active and silent:
            self.toast.show_text("Системный звук не обнаружен — проверь источник")

    # ------------------------------------------------------------- заметки
    @Slot(str)
    def _on_note_text_ready(self, text: str) -> None:
        target = self._note_dictation_target
        self._note_dictation_target = None
        if target is None:
            return
        try:
            target.append_dictation_text(text)
        except RuntimeError:
            pass

    @Slot()
    def _on_note_dictation_ended(self) -> None:
        target = self._note_dictation_target
        self._note_dictation_target = None
        if target is None:
            return
        try:
            target.note_dictation_ended()
        except RuntimeError:
            pass

    # ------------------------------------------------------------- история
    def refresh_nomodel_bar(self) -> None:
        reason = ""
        if self._model_missing_getter is not None:
            try:
                reason = self._model_missing_getter() or ""
            except Exception:  # noqa: BLE001
                reason = ""
        self.page_home.notice.set_reason(reason)
        self.page_calls.notice.set_reason(reason)

    def refresh_history(self) -> None:
        history_dir = self._history_dir_getter()
        count = self._rotation_count_getter()
        self.refresh_nomodel_bar()
        self._entries = read_history_entries(history_dir, count)
        self._call_entries = read_call_entries(history_dir / "Calls", CALL_CARDS_MAX)
        self.page_home.set_entries(self._entries)
        self.page_calls.set_entries(self._call_entries)
        # память таймера: до первой записи показываем длительность первой записи списка
        if self.page_home.state() == "idle" and self.page_home.last_seconds() == 0 and self._entries:
            self.page_home.remember_duration(int(self._entries[0].get("duration_sec") or 0))
        if self.page_calls.state() == "idle" and self.page_calls.last_seconds() == 0 and self._call_entries:
            self.page_calls.remember_duration(int(self._call_entries[0].get("duration_sec") or 0))
        self._cards = self.page_home._rows  # совместимость с тестами прежнего окна

    def _on_row_copy(self, row) -> None:
        QApplication.clipboard().setText(row.text_full)
        self.toast.show_text("Скопировано в буфер")
        if not self.isVisible():
            self._overlay.show_saved()

    def _on_row_note(self, row) -> None:
        note_path = profile.new_note_path()
        day, hhmm = row.day_label.text(), row.time_strip.text()
        title = f"{'Созвон' if row.kind == 'call' else 'Диктовка'} {day.lower()} {hhmm}"
        profile.write_note(note_path, f"{title}\n\n{row.text_full}\n")
        profile.write_note_meta(note_path, source=profile.NOTE_SOURCE_DICTATION, source_name="")
        self.toast.show_text("Сохранено в заметки")
        self.open_notes(select=note_path)

    def _on_row_delete(self, row) -> None:
        """Корзина строки: спросить, закрыть файл в плеере, унести файлы в Корзину Windows."""
        files = row.files()
        stamp = f"{row.day_label.text().lower()} {row.time_strip.text()}"
        if row.kind == "call":
            extra = sorted({p.suffix.lstrip(".") for p in files if p.suffix != ".md"})
            if extra:
                text = (f"Транскрипт и аудио ({', '.join(extra)}) созвона от {stamp} уйдут в Корзину "
                        "Windows — оттуда их можно вернуть.")
            else:
                text = f"Транскрипт созвона от {stamp} уйдёт в Корзину Windows — оттуда его можно вернуть."
            title = "Удалить созвон?"
        else:
            title = "Удалить запись?"
            text = f"Диктовка от {stamp} — аудио и текст — уйдёт в Корзину Windows, оттуда её можно вернуть."
        if not confirm(self, title, text):
            return
        page = self.page_calls if row.kind == "call" else self.page_home
        page.release_row(row)
        failed = move_files_to_trash(files)
        self.refresh_history()
        if failed:
            info_modal(self, "Не всё удалилось", "\n".join(failed))
        else:
            self.toast.show_text("Удалено в Корзину")

    # ------------------------------------------------------------- настройки
    def apply_settings_to_pages(self, s: dict) -> None:
        hk = hotkey_display(s.get("hotkey", ""))
        self.page_home.set_hotkey(hk)
        self.page_calls.set_hotkey("Ctrl + Shift + E")
        try:
            name = engine.spec_display(engine.spec_from_settings(s))
        except Exception:  # noqa: BLE001
            name = str(s.get("model", ""))
        self.page_home.set_model_name(name)
        self.page_calls.set_model_name(name)
        self.page_home.set_mode_text(processing_mode_text(s))
        Motion.set_user_enabled(bool(s.get("ui_animations", True)))
        self._mic_setting = (s.get("mic_device") or "").strip()
        self._refresh_mic_name()

    def _refresh_mic_name(self) -> None:
        """Микрофон в шапке «Диктовок» и «Созвонов»: выбранный по имени или тот,
        что сейчас системный в Windows (за «системным» устройство меняется само)."""
        name = getattr(self, "_mic_setting", "")
        if not name:
            try:
                from .. import audio_quality
                sys_name = audio_quality.default_input_name()
            except Exception:  # noqa: BLE001
                sys_name = ""
            name = f"{sys_name} · системный" if sys_name else "системный"
        self.page_home.set_mic_name(name)
        self.page_calls.set_mic_name(name)

    def emit_settings(self, new: dict, replacement_rules=None, autostart_changed: bool = True) -> None:
        """Единая точка применения настроек со страницы: файл → автостарт → сигнал."""
        save_settings_dict(new)
        if replacement_rules is not None:
            profile.save_replacement_rules(replacement_rules)
        if autostart_changed:
            self._apply_autostart(bool(new.get("autostart", False)))
        self.apply_settings_to_pages(new)
        self.settings_changed.emit(new)
        self.refresh_history()

    def _apply_autostart(self, enabled: bool) -> None:
        if enabled:
            ok, msg = create_autostart_shortcut(self._entry_script)
            if not ok:
                QMessageBox.warning(self, "Автостарт", f"Не получилось создать ярлык:\n{msg}")
        else:
            ok, msg = remove_autostart_shortcut()
            if not ok:
                QMessageBox.warning(self, "Автостарт", f"Не получилось удалить ярлык:\n{msg}")

    def model_busy(self) -> bool:
        if self._model_busy_getter is None:
            return False
        try:
            return bool(self._model_busy_getter())
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------- разделы
    def open_settings(self) -> None:
        self.show_window()
        self.navigate("settings")

    def open_models(self) -> None:
        self.show_window()
        self.navigate("models")

    def _open_settings_tab(self, tab: str) -> None:
        page = self.shell.host.page("settings")
        if page is not None and hasattr(page, "show_tab"):
            page.show_tab(tab)
        self.navigate("settings")

    def open_notes(self, select: "Path | None" = None) -> None:
        if not isinstance(select, Path):
            select = None
        self.show_window()
        self.navigate("notes")
        page = self.shell.host.page("notes")
        if page is not None and hasattr(page, "select_note") and select is not None:
            page.select_note(select)

    def open_about(self) -> None:
        self.show_window()
        self.navigate("about")

    def open_donate(self) -> None:
        from ..transcribe_ui_window import open_donate_page
        open_donate_page(self)

    def _open_stats(self) -> None:
        self.navigate("stats")

    # ------------------------------------------------------------- импорт
    def open_file_import(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Аудио или видео для расшифровки", "",
                                                audio_import.FILE_DIALOG_FILTER)
        if chosen:
            self._import_and_open_note(Path(chosen))

    def _import_and_open_note(self, path: Path) -> None:
        note_path = self.start_file_import(path)
        if note_path is not None:
            self.open_notes(select=note_path)

    def start_file_import(self, path: Path) -> "Path | None":
        from ..transcribe_ui_window import FileImportDialog, QUIET_TRACK_PEAK
        if self._file_import_api is None:
            QMessageBox.warning(self, "Импорт", "Импорт файлов недоступен в этой сборке.")
            return None
        if not audio_import.is_supported(path):
            answer = QMessageBox.question(
                self, "Импорт",
                f"Формат «{path.suffix or 'без расширения'}» не в списке проверенных.\n\n"
                "Уверенно работают: " + ", ".join(sorted(audio_import.SUPPORTED_SUFFIXES))
                + "\n\nПопробовать расшифровать этот файл всё равно?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if answer != QMessageBox.Yes:
                return None
        dlg = FileImportDialog(self, path, self._file_import_api)
        from .legacy_theme import apply_v5_dialog_theme
        apply_v5_dialog_theme(dlg)
        accepted = dlg.exec()
        if not accepted:
            if dlg.error_text:
                QMessageBox.warning(self, "Импорт", dlg.error_text)
            return None
        data = dlg.result_data or {}
        text = (data.get("text") or "").strip()
        if not text:
            quiet = float(data.get("peak", 1.0)) < QUIET_TRACK_PEAK
            QMessageBox.information(
                self, "Импорт",
                "Звуковая дорожка почти беззвучная — похоже, в записи нет голоса "
                "(выключенный микрофон или видео без звука)." if quiet else
                "Речь в файле не распознана — текст пустой.")
            return None
        return self._note_from_import(path, text)

    def _note_from_import(self, src: Path, text: str) -> "Path | None":
        note_path = profile.new_note_path()
        profile.write_note(note_path, f"{src.stem}\n\n{text}\n")
        profile.write_note_meta(note_path, source=profile.NOTE_SOURCE_IMPORT, source_name=src.name)
        return note_path

    def import_file_to_note(self, path: Path) -> "Path | None":
        return self.start_file_import(path)

    def _dragged_audio_path(self, ev) -> "Path | None":
        md = ev.mimeData()
        if not md.hasUrls():
            return None
        urls = [u for u in md.urls() if u.isLocalFile()]
        if len(urls) != 1:
            return None
        p = Path(urls[0].toLocalFile())
        if not p.is_file():
            return None
        return p

    def dragEnterEvent(self, ev) -> None:  # noqa: N802
        if self._dragged_audio_path(ev) is not None:
            ev.acceptProposedAction()
        else:
            ev.ignore()

    def dragMoveEvent(self, ev) -> None:  # noqa: N802
        self.dragEnterEvent(ev)

    def dropEvent(self, ev) -> None:  # noqa: N802
        p = self._dragged_audio_path(ev)
        if p is None:
            ev.ignore()
            return
        ev.acceptProposedAction()
        QTimer.singleShot(0, lambda: self._import_and_open_note(p))


def processing_mode_text(s: dict) -> str:
    """Режим обработки для ссылки на «Диктовках»: «Режим: авто · порог 10 с» / batch / streaming."""
    mode = s.get("processing_mode", "auto")
    if mode == "always_batch":
        return "Режим: batch"
    if mode == "always_streaming":
        return "Режим: streaming"
    return f"Режим: авто · порог {int(s.get('auto_threshold_sec', 10))} с"


def hotkey_display(hk: str) -> str:
    """`<ctrl>+<shift>+q` → «Ctrl + Shift + Q» (как kbd в прототипе)."""
    try:
        from ..hotkeys import to_qt
        qt = to_qt(hk or "")
    except Exception:  # noqa: BLE001
        qt = str(hk or "")
    parts = [p.strip() for p in qt.split("+") if p.strip()]
    return " + ".join(p[:1].upper() + p[1:] for p in parts)
