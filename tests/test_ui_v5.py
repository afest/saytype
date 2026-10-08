"""Интерфейс V5 (`saytype.ui`, T-487): цифры, границы таймера, контракт окна.

Что проверяем:
  * ClockGrid: 59→60 активирует минуты, 3599→3600 — часы; разряды считаются верно;
    reduced-motion ставит значение мгновенно, без кадров анимации;
  * DigitReel: обычный переход строит 4 кадра, сброс к нулю — через все значения;
  * MiniClock: часы появляются с 3600 с;
  * svgpath: дуги и относительные команды дают непустой контур;
  * history: разбор штампа времени, очистка md созвона, html транскрипта;
  * MainWindow (новое окно): тот же контракт, что у прежнего — конструктор,
    14 `notify_*`, три сигнала; история читается в строки; `_note_from_import`
    создаёт заметку с меткой источника; hotkey показывается как «Ctrl + Shift + Q».

Запуск:  python -m tests.test_ui_v5   (из корня репозитория)
"""

import json
import os
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_TMP = tempfile.mkdtemp(prefix="saytype-ui-v5-test-")
os.environ["LOCALAPPDATA"] = _TMP
os.environ.pop("APPDATA", None)
os.environ.pop("SAYTYPE_PROFILE_DIR", None)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)
        if "pytest" in sys.modules:  # под pytest провал обязан ронять тест, а не только печататься
            raise AssertionError(f"{name} {detail}")


def _app():
    return QApplication.instance() or QApplication(sys.argv)


def test_clock_boundaries() -> None:
    print("границы таймера:")
    _app()
    from saytype.ui import tokens
    from saytype.ui.digits import ClockGrid, DigitReel, MiniClock

    tokens.Motion.reduced = staticmethod(lambda: True)  # детерминированно, без анимации
    c = ClockGrid()
    c.set_recording(True, animate=False)
    c.set_seconds(59, animate=False)
    check("59 → разряды 00:00:59", [r.value() for r in c._reels()] == [0, 0, 0, 0, 5, 9])
    check("59 → минуты серые", c.minutes.tens._tint == 0.0 and c.seconds.tens._tint == 1.0)
    c.set_seconds(60, animate=False)
    check("60 → разряды 00:01:00", [r.value() for r in c._reels()] == [0, 0, 0, 1, 0, 0])
    check("60 → минуты активны", c.minutes.tens._tint == 1.0)
    c.set_seconds(3599, animate=False)
    check("3599 → часы серые", c.hours.tens._tint == 0.0 and [r.value() for r in c._reels()] == [0, 0, 5, 9, 5, 9])
    c.set_seconds(3600, animate=False)
    check("3600 → 01:00:00 и часы активны", [r.value() for r in c._reels()] == [0, 1, 0, 0, 0, 0]
          and c.hours.tens._tint == 1.0)
    c.set_recording(False, animate=False)
    check("после обработки всё серое", all(u.tens._tint == 0.0 for u in c._units))
    c.set_seconds(157, animate=False)
    c.roll_to_zero()
    check("сброс к нулю (reduced) мгновенный", [r.value() for r in c._reels()] == [0] * 6)

    tokens.Motion.reduced = staticmethod(lambda: False)
    reel = DigitReel(mod=10)
    reel.set_value(5, animate=True)
    check("скрытый разряд не анимируется", reel.value() == 5 and not reel.is_animating())
    reel.show()
    reel.set_value(3, animate=False)
    reel.set_value(4, animate=True)
    check("переход 3→4: кадры [2,3,4,5], анимация идёт", reel._frames == [2, 3, 4, 5] and reel.is_animating())
    reel._stop_anim()
    reel._settle(7)
    reel.roll_to_zero()
    check("сброс 7→0 через все значения", reel._frames == [9, 0, 1, 2, 3, 4, 5, 6, 7, 8] and reel.value() == 0)
    reel._stop_anim()
    reel._settle(0)
    reel.roll_to_zero()
    check("сброс из 0 без анимации", not reel.is_animating())

    m = MiniClock()
    m.set_seconds(3599, animate=False)
    check("мини-часы: до часа без часов", not m._show_hours)
    m.set_seconds(3600, animate=False)
    check("мини-часы: с 3600 показывают часы", m._show_hours)
    tokens.Motion.reduced = staticmethod(lambda: True)


def test_svgpath_and_history_helpers() -> None:
    print("svg-пути и помощники истории:")
    _app()
    from saytype.ui.svgpath import parse_path
    from saytype.ui.history import clean_call_markdown, stamp_parts, text_to_html
    from saytype.ui.icons import ICONS, NAV_ICONS, icon_pixmap, mark_pixmap
    from saytype.ui.digits import GLYPHS, glyph_path

    arc = parse_path(ICONS["mic"])
    check("дуги (a/A) дают контур", not arc.boundingRect().isEmpty())
    rel = parse_path("m7 14 5-5 5 5")
    check("относительные команды", not rel.boundingRect().isEmpty())
    for ch in GLYPHS:
        check(f"глиф {ch} в пределах viewBox", glyph_path(ch).boundingRect().right() <= 100)
    check("все иконки рисуются", all(not icon_pixmap(n, 20, "#000000", None, n in NAV_ICONS, 1.0).isNull()
                                     for n in list(ICONS) + list(NAV_ICONS)))
    check("знак рисуется", not mark_pixmap(44).isNull())

    day, hhmm = stamp_parts("2026-10-03T13-25-08")
    check("штамп времени чч:мм", hhmm == "13:25")
    day2, _ = stamp_parts("2026-09-11 18-01-26")
    check("старая дата дд.мм.гггг", day2 == "11.09.2026")
    md = "# Расшифровка\n\n> служебное\n\n---\n\n[00:58] **[Я]:** Привет\n\n[01:02] **[Собеседник]:** Да"
    cleaned = clean_call_markdown(md)
    check("md созвона: убраны заголовок/цитата/разделитель", cleaned.startswith("[00:58]") and "#" not in cleaned)
    html = text_to_html("Первый абзац\n\nВторой **жирный**", 14)
    check("html транскрипта: абзацы и жирный", html.count("<p") == 2 and "<b>жирный</b>" in html)


def _build_window(history: Path):
    from saytype import audio_import, profile
    from saytype.ui.main_window import MainWindow

    icons = profile.profile_dir() / "icons"
    icons.mkdir(parents=True, exist_ok=True)
    from PySide6.QtGui import QPixmap
    for name in ("idle", "recording", "processing", "app"):
        px = QPixmap(16, 16)
        px.fill()
        px.save(str(icons / f"{name}.png"))
    api = audio_import.FileImportApi(begin=lambda: True, run=lambda *a, **k: {}, end=lambda: None)
    return MainWindow(
        idle_icon_path=icons / "idle.png",
        recording_icon_path=icons / "recording.png",
        processing_icon_path=icons / "processing.png",
        app_icon_path=icons / "app.png",
        history_dir_getter=lambda: history,
        rotation_count_getter=lambda: 5,
        entry_script=Path(__file__).resolve().parent.parent / "src" / "saytype" / "transcribe_ui.py",
        model_busy_getter=lambda: False,
        file_import_api=api,
        cancel_transcription=lambda: True,
        model_missing_getter=lambda: "",
    )


def test_window_contract() -> None:
    print("контракт нового окна:")
    app = _app()
    from saytype import profile
    from saytype.ui.main_window import MainWindow, hotkey_display

    history = Path(_TMP) / "history"
    history.mkdir(parents=True, exist_ok=True)
    stem = "2026-10-03T12-00-00"
    with wave.open(str(history / f"{stem}.wav"), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 16000 * 3)
    (history / f"{stem}.txt").write_text("Проверочная запись", encoding="utf-8")
    (history / f"{stem}.meta.json").write_text(json.dumps({"duration_sec": 3.0, "elapsed_sec": 0.5, "ratio_x": 6.0}),
                                               encoding="utf-8")

    for name in ("notify_transcription_done", "notify_stop_sound", "notify_set_state", "notify_audio_level",
                 "notify_call_state", "notify_call_transcript", "notify_call_alert", "notify_call_processing",
                 "notify_call_progress", "notify_note_text", "notify_note_dictation_ended", "notify_cancelling",
                 "notify_cancelled", "notify_error", "show_window", "set_note_dictation_target",
                 "refresh_history", "refresh_nomodel_bar", "start_file_import", "import_file_to_note",
                 "_note_from_import", "open_settings", "open_models", "open_notes", "open_about"):
        check(f"метод {name}", callable(getattr(MainWindow, name, None)))
    for sig in ("settings_changed", "quit_requested", "stop_sound_requested", "transcription_done"):
        check(f"сигнал {sig}", hasattr(MainWindow, sig))

    win = _build_window(history)
    win.show()
    app.processEvents()
    check("история прочитана в строку", len(win.page_home._rows) == 1)
    row = win.page_home._rows[0]
    check("строка показывает текст и длительность", "Проверочная" in row.text_full and int(row.duration_sec) == 3)
    check("_cards — совместимость с прежними тестами", win._cards is win.page_home._rows)
    check("таймер помнит длительность первой записи", win.page_home.last_seconds() == 3)

    win.set_state("recording")
    app.processEvents()
    check("recording → страница и оверлей в записи", win.page_home.state() == "recording"
          and win._overlay.mode() == "recording" and win._rec_timer.isActive())
    win._rec_started_at -= 61
    win._tick_rec_time()
    check("тик времени: 61 с → минуты активны", win.page_home.clock.seconds_value() == 61
          and win.page_home.clock.minutes.tens._tint == 1.0)
    win.set_state("processing")
    app.processEvents()
    check("processing → оверлей с отменой, таймер стоит", win._overlay.mode() == "processing"
          and not win._rec_timer.isActive())
    check("стоп: длительность записи снята на стопе", win._rec_duration == 61)
    win._rec_started_at -= 30  # «распознавание» шло ещё 30 с — в длительность не входит
    win.notify_transcription_done()
    app.processEvents()
    win.set_state("idle")
    app.processEvents()
    check("idle → длительность записи серым, без времени обработки", win.page_home.state() == "idle"
          and win.page_home.clock.minutes.tens._tint == 0.0 and win.page_home.last_seconds() == 61,
          f"{win.page_home.last_seconds()}")

    win.set_state("recording")
    win._rec_started_at -= 5
    win._tick_rec_time()
    win.set_state("processing")
    win._on_cancelled("Распознавание отменено")
    win.set_state("idle")
    app.processEvents()
    check("отмена → прежнее успешное время", win.page_home.last_seconds() == 61
          and win.page_home.clock.seconds_value() == 61, f"{win.page_home.clock.seconds_value()}")

    note = win._note_from_import(Path("C:/tmp/проба.mp3"), "текст из файла")
    meta = profile.read_note_meta(note)
    check("_note_from_import: заметка с меткой import", note.exists() and meta["source"] == profile.NOTE_SOURCE_IMPORT
          and meta["source_name"] == "проба.mp3" and profile.read_note(note).startswith("проба"))

    check("hotkey → «Ctrl + Shift + Q»", hotkey_display("<ctrl>+<shift>+q") == "Ctrl + Shift + Q")
    win._on_call_state(True)
    app.processEvents()
    check("созвон пишет, диктовка доступна (как в ядре)",
          win.page_calls.state() == "recording" and win.page_home.controls.button.isEnabled())
    win._call_rec_started -= 125
    win._on_call_processing(True, 125.0)
    win.set_state("recording")  # диктовка поверх обработки созвона
    app.processEvents()
    check("диктовка не сбивает обработку созвона", win.page_calls.state() == "processing")
    win.set_state("idle")
    win._call_rec_started -= 40  # обработка созвона шла ещё 40 с
    win._on_call_processing(False, 0.0)
    win._on_call_state(False)
    app.processEvents()
    check("созвон: в таймере длительность записи 125 с, а не 165",
          win.page_calls.last_seconds() == 125, f"{win.page_calls.last_seconds()}")
    win._quit()


def test_settings_page_keys() -> None:
    print("страница настроек:")
    _app()
    from saytype.transcribe_ui_window import load_settings_dict
    from saytype.ui.pages.settings import SettingsPage

    class _W:
        def model_busy(self):
            return False

        def navigate(self, pid):
            pass

        def emit_settings(self, new, rules=None):
            pass

        def refresh_history(self):
            pass

    page = SettingsPage(_W())
    page.reload()
    values = page.values()
    expected = set(load_settings_dict()) - {"wizard_done", "ui_language"}
    missing = expected - set(values)
    extra = set(values) - expected
    check("values() покрывает ключи load_settings_dict()", not missing, f"нет: {sorted(missing)}")
    check("values() без лишних ключей", not extra, f"лишние: {sorted(extra)}")
    for tab in ("record", "recognition", "calls", "storage", "hifi", "app"):
        page.show_tab(tab)
        check(f"вкладка {tab}", page.current_tab() == tab)
    check("replacement_rules() = None, пока редактор не открывали", page.replacement_rules() is None)


def test_stats_page() -> None:
    print("страница статистики:")
    _app()
    from saytype.transcribe_ui_window import compute_stats_aggregates, read_stats_jsonl
    from saytype.ui.pages.stats import StatsPage

    hist = Path(_TMP) / "stats-history"
    hist.mkdir(parents=True, exist_ok=True)

    class _W:
        def _history_dir_getter(self):
            return hist

    page = StatsPage(_W())
    page.refresh()
    check("пустой журнал → пустое состояние", not page._recent)

    rows = []
    for i in range(12):
        mode = "streaming" if i % 3 == 0 else "full"
        rows.append({"ts": f"2026-10-0{1 + i % 3}T10:00:{i:02d}", "duration_sec": 5.0 + i * 7,
                     "elapsed_sec": 1.0, "ratio_x": 5.0 + i, "mode": mode, "device": "cuda/int8"})
    (hist / "_stats.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    page.refresh()
    recent, oldest = read_stats_jsonl(hist / "_stats.jsonl", last_n=100)
    agg = compute_stats_aggregates(recent, oldest)
    ru = lambda v: f"{v:.1f}".replace(".", ",")  # noqa: E731
    check("записей столько же, сколько в журнале", page.m_count._readout.text() == str(agg["count"])
          if hasattr(page.m_count, "_readout") else True)
    texts = []
    for m in (page.m_count, page.m_avg, page.m_p50, page.m_p95):
        for child in m.findChildren(object):
            if hasattr(child, "text") and callable(child.text) and child.__class__.__name__ == "_Readout":
                texts.append(child.text())
    check("метрики = прежний расчёт", texts == [str(agg["count"]), f"{ru(agg['avg_ratio'])}×",
                                               f"{ru(agg['p50_ratio'])}×", f"{ru(agg['p95_ratio'])}×"],
          f"{texts}")
    page._set_mode("streaming")
    texts_s = [c.text() for m in (page.m_count,) for c in m.findChildren(object)
               if c.__class__.__name__ == "_Readout"]
    check("фильтр streaming → 4 записи", texts_s == ["4"], f"{texts_s}")
    page._set_tab("buckets")
    check("вкладка «По длительности»", page._tab == "buckets")


def test_models_page() -> None:
    print("страница моделей:")
    app = _app()
    from PySide6.QtCore import QObject, Signal
    from saytype import engine, profile
    from saytype.transcribe_ui_window import load_settings_dict, save_settings_dict
    from saytype.ui.pages import models as models_page

    class _W(QObject):
        settings_changed = Signal(dict)

        def model_busy(self):
            return False

        def apply_settings_to_pages(self, s):
            pass

        def refresh_nomodel_bar(self):
            pass

    w = _W()
    page = models_page.ModelsPage(w)
    page.refresh()
    app.processEvents()
    keys = set(page._cards)
    check("карточка на каждый пресет", set(engine.PRESET_KEYS) <= keys, f"нет: {set(engine.PRESET_KEYS) - keys}")
    check("активная = модель из настроек", page._active_spec == engine.spec_from_settings(load_settings_dict()))

    dict_path = profile.dictionary_path()
    dict_path.write_text("# комментарий: имена\nСэйТайп\nСмыслокод\n", encoding="utf-8")
    models_page._save_model_keys(load_settings_dict())
    check("выбор модели не затирает словарь", dict_path.read_text(encoding="utf-8").startswith("# комментарий"))


def test_settings_roundtrip() -> None:
    print("настройки: сохранение → перечитывание:")
    _app()
    from saytype import profile
    from saytype.transcribe_ui_window import load_settings_dict, save_settings_dict
    from saytype.ui.pages.settings import SettingsPage

    saved = {}

    class _W:
        def model_busy(self):
            return False

        def navigate(self, pid):
            pass

        def emit_settings(self, new, rules=None):
            save_settings_dict(new)  # как MainWindow.emit_settings: сначала файл
            if rules is not None:
                profile.save_replacement_rules(rules)
            saved.update(new)

        def refresh_history(self):
            pass

    page = SettingsPage(_W())
    page.reload()
    page.count_spin.setValue(7)
    page.threshold_spin.setValue(25)
    page.proxy_edit.setText("http://127.0.0.1:3065")
    page.speaker_self_edit.setText("Тест")
    page.call_keep_spin.setValue(4)
    before = page.values()
    page._win.emit_settings(before)  # тот же путь, что кнопка «Сохранить» после валидации
    after = load_settings_dict()
    diff = {k: (before[k], after.get(k)) for k in before if before[k] != after.get(k)}
    check("все поля пережили сохранение и чтение", not diff, f"{diff}")
    page2 = SettingsPage(_W())
    page2.reload()
    check("новая страница показывает сохранённое (как после перезапуска)", page2.values() == before,
          f"{ {k: (v, page2.values().get(k)) for k, v in before.items() if page2.values().get(k) != v} }")


def test_no_accumulation() -> None:
    print("нет накопления виджетов и таймеров:")
    app = _app()
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    history = Path(_TMP) / "history"
    win = _build_window(history)
    win.show()
    app.processEvents()

    def counts():
        app.processEvents()
        from PySide6.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
        widgets = len(QApplication.allWidgets())
        # сторож трея тикает всегда (раз в 15 с, см. trayguard) — он не утечка
        timers = sum(1 for tmr in win.findChildren(QTimer) if tmr.isActive() and tmr is not win._tray_guard)
        return widgets, timers

    from saytype.ui import tokens
    tokens.Motion.reduced = staticmethod(lambda: True)  # без ожидания анимаций
    for pid in ("home", "calls", "notes", "models", "stats", "settings", "about"):
        win.navigate(pid)
    win.navigate("home")
    w0, t0 = counts()
    for _ in range(20):
        for pid in ("calls", "notes", "stats", "settings", "about", "home"):
            win.navigate(pid)
        win.set_state("recording")
        win._tick_rec_time()
        win.set_state("processing")
        win.notify_transcription_done()
        app.processEvents()
        win.set_state("idle")
        win.refresh_history()
    w1, t1 = counts()
    check("число виджетов не растёт после 20 циклов", w1 <= w0, f"{w0} → {w1}")
    check("в покое активных таймеров нет", t1 == 0, f"активных: {t1}")
    check("слоя перехода не осталось", win.shell.host._layer is None)
    win._quit()


def test_incremental_history() -> None:
    print("история обновляется без пересоздания строк:")
    app = _app()
    history = Path(_TMP) / "history-incr"
    history.mkdir(parents=True, exist_ok=True)

    def add(stem: str, text: str) -> None:
        with wave.open(str(history / f"{stem}.wav"), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 16000)
        (history / f"{stem}.txt").write_text(text, encoding="utf-8")

    add("2026-10-03T10-00-00", "первая")
    win = _build_window(history)
    win.show()
    app.processEvents()
    first = list(win.page_home._rows)
    win.refresh_history()
    app.processEvents()
    check("повторное обновление не пересоздаёт строки", win.page_home._rows == first)
    import time as _t
    _t.sleep(0.05)
    add("2026-10-03T10-01-00", "вторая")
    win.refresh_history()
    app.processEvents()
    rows = win.page_home._rows
    check("новая запись → одна новая строка сверху, старая та же",
          len(rows) == 2 and rows[1] is first[0] and rows[0] not in first, f"{len(rows)}")
    (history / "2026-10-03T10-00-00.txt").write_text("первая, исправленная", encoding="utf-8")
    win.refresh_history()
    app.processEvents()
    check("изменился текст → строка пересоздана", win.page_home._rows[1] is not first[0]
          and "исправленная" in win.page_home._rows[1].text_full)
    win._quit()


def test_feedback_round1() -> None:
    """Правки после первого живого теста V5 (03.10)."""
    print("правки после живого теста:")
    app = _app()
    import time as _t
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QColor
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QToolTip
    from saytype.ui import tokens
    from saytype.ui.tokens import Color, Grid, Motion
    from saytype.ui.widgets import TextViewer

    history = Path(_TMP) / "history-round1"
    calls = history / "Calls"
    calls.mkdir(parents=True, exist_ok=True)
    long_voice = ("Слово за словом, фраза за фразой. " * 60).strip()
    (history / "2026-10-03T11-00-00.txt").write_text(long_voice, encoding="utf-8")
    with wave.open(str(history / "2026-10-03T11-00-00.wav"), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 16000 * 60)
    call_body = "\n\n".join(f"[{i // 60:02d}:{i % 60:02d}] **[Я]:** реплика номер {i}" for i in range(400))
    (calls / "2026-10-03 12-00-00.md").write_text("# Созвон\n\n" + call_body, encoding="utf-8")
    # прежние тесты подменили reduced() на «всегда без анимации»; здесь — только настройка
    # приложения, без системной настройки Windows: результат не зависит от машины
    tokens.Motion.reduced = staticmethod(lambda: tokens.Motion._user_off)
    win = _build_window(history)
    win.resize(1280, 800)
    win.show()
    QTest.qWait(150)

    check("меню всегда свёрнуто — только иконки", win.shell.sidebar.width() == Grid.SIDEBAR_COMPACT
          and all(b.accessibleName() for b in win.shell.sidebar.buttons.values()))
    row = win.page_home._rows[0]
    img = row.grab().toImage()
    x = int((row.col_text.x() + 40) * img.devicePixelRatio())
    y = int((row.height() - 1) * img.devicePixelRatio())
    check("линия под строкой не закрыта колонкой текста", QColor(img.pixel(x, y)).name() == Color.LINE.lower(),
          QColor(img.pixel(x, y)).name())

    QToolTip.showText(row.copy_btn.mapToGlobal(QPoint(5, 5)), "Копировать", row.copy_btn)
    app.processEvents()
    tips = [w for w in QApplication.topLevelWidgets() if w.metaObject().className() == "QTipLabel" and w.isVisible()]
    bg = QColor(tips[0].grab().toImage().pixel(2, 2)).name() if tips else ""
    check("подсказка тёмная, не белая", bg == Color.TOAST_BG.lower(), bg)
    QToolTip.hideText()

    h0 = row.height()
    QTest.mouseClick(row.more_btn, Qt.LeftButton)
    QTest.qWait(150)
    viewers = [w for w in win.findChildren(TextViewer) if w.isVisible()]
    check("«Весь текст» диктовки открывает лист, строка не растёт (08.10)",
          len(viewers) == 1 and row.height() == h0, f"листов {len(viewers)}, {h0} → {row.height()}")
    for v_ in viewers:
        QTest.keyClick(v_, Qt.Key_Escape)
    QTest.qWait(50)

    win.navigate("calls")
    check("переход идёт слоем из снимков", win.shell.host._layer is not None)
    QTest.qWait(Motion.PAGE_MS + 200)
    check("переход закончился, слой убран", win.shell.host._layer is None)
    call_row = win.page_calls._rows[0]
    QTest.mouseClick(call_row.more_btn, Qt.LeftButton)
    QTest.qWait(100)
    viewers = [w for w in win.findChildren(TextViewer) if w.isVisible()]
    check("созвон открывается окном поверх, а не раскрывается в строке", len(viewers) == 1)
    if viewers:
        QTest.keyClick(viewers[0], Qt.Key_Escape)
        QTest.qWait(50)
        left = [w for w in win.findChildren(TextViewer) if w.isVisible()]
        check("окно текста закрывается по Esc", not left)

    win.navigate("home")
    win.set_state("recording")
    win.notify_audio_level(0.2)  # громкая речь
    app.processEvents()
    mic = win.page_home.controls.mic_link
    check("уровень голоса доходит до точки плашки и микрофона",
          win._overlay.dot._level > 0.5 and mic.dot._level > 0.5)
    check("у кнопки нет «Идёт запись», есть микрофон", "Идёт запись" not in mic._text and "Микрофон" in mic._text)
    for _ in range(30):
        win.notify_audio_level(0.0)
    app.processEvents()
    check("тишина — ореол спадает", win._overlay.dot._level < 0.05)
    win.set_state("idle")
    win.notify_audio_level(0.2)
    app.processEvents()
    check("вне записи уровень игнорируется", mic.dot._level == 0.0)

    Motion.set_user_enabled(False)
    check("настройка «Анимации» выключает анимацию", Motion.reduced())
    win.navigate("notes")
    check("без анимации переход без слоя", win.shell.host._layer is None)
    Motion.set_user_enabled(True)
    win._quit()


def test_feedback_round2() -> None:
    """Второй круг правок (03.10): ползунок перемотки, линия в заметках, счётчик
    статистики, словарь на весь лист, режим обработки на виду."""
    print("второй круг правок:")
    app = _app()
    from PySide6.QtCore import QPoint, Qt, QTimer
    from PySide6.QtGui import QColor
    from PySide6.QtTest import QTest
    from saytype import profile
    from saytype.ui import tokens
    from saytype.ui.main_window import processing_mode_text
    from saytype.ui.tokens import Color, Motion
    from saytype.ui.widgets import TextEditorSheet

    tokens.Motion.reduced = staticmethod(lambda: tokens.Motion._user_off)
    history = Path(_TMP) / "history-round1"   # из первого круга: длинная диктовка с wav
    win = _build_window(history)
    win.resize(1280, 800)
    win.show()
    QTest.qWait(150)

    row = next(r for r in win.page_home._rows if r.audio_path is not None)
    row.set_audio_state(True, False)
    row.set_position(row.duration_sec * 0.5)
    app.processEvents()
    box = row.parentWidget()
    edge = row.mapTo(box, QPoint(row.width() // 3, row.height() - 1))
    check("край строки принадлежит полосе перемотки, а не колонке", box.childAt(edge) is row.seek)
    rest = row.seek.geometry()
    # окна прежних тестов остаются открытыми в той же точке экрана и перехватили бы мышь
    for w in QApplication.topLevelWidgets():
        if w is not win and w.isVisible():
            w.hide()
    win.page_home.scroll.ensureWidgetVisible(row.seek)
    QTest.qWait(50)
    QTest.mouseMove(win, QPoint(10, 10))   # курсор приходит на полосу снаружи, как рукой
    QTest.qWait(30)
    QTest.mouseMove(win, row.seek.mapTo(win, QPoint(row.width() // 3, row.seek.height() - 5)))
    QTest.qWait(50)
    check("наведение на полосу ловится", row.seek.active())
    img = row.seek.grab().toImage()
    dpr = img.devicePixelRatio()
    tx = int(row.seek.width() * 0.5 * dpr)

    def color_at(y):
        return QColor(img.pixel(tx, int(y * dpr))).name()

    c = row.seek.CENTER
    check("при наведении на позиции квадратный ползунок, по центру линии",
          color_at(c - 4) == color_at(c + 4) == Color.ACCENT.lower(), f"{color_at(c - 4)} {color_at(c + 4)}")
    xl = int(row.seek.width() * 0.9 * dpr)   # правее ползунка: только полоса
    check("полоса растёт в обе стороны от линии, а не прыгает вверх",
          QColor(img.pixel(xl, int((c - 2) * dpr))).name() == Color.LINE.lower()
          and QColor(img.pixel(xl, int((c + 1) * dpr))).name() == Color.LINE.lower()
          and row.seek.geometry() == rest)
    sought = []
    row.seek_requested.connect(lambda r, s: sought.append(s))
    QTest.mouseClick(row.seek, Qt.LeftButton, Qt.NoModifier, QPoint(int(row.seek.width() * 0.8), 7))
    check("клик по полосе перематывает", sought and abs(sought[-1] - row.duration_sec * 0.8) < 1.5,
          f"{sought[-1:]} из {row.duration_sec}")
    row.set_audio_state(False, False)
    check("без выбранного аудио полосы нет", not row.seek.isVisible())

    note = profile.new_note_path()
    profile.write_note(note, "Заметка\n\nтекст")
    win.open_notes(select=note)
    QTest.qWait(Motion.PAGE_MS + 200)
    notes = win.shell.host.page("notes")
    lc = notes.list_col
    img = lc.grab().toImage()
    x = int((lc.width() - 1) * img.devicePixelRatio())
    y = int(40 * img.devicePixelRatio()) if not notes._items else int(
        (list(notes._items.values())[0].y() + 30) * img.devicePixelRatio())
    check("линия между списком заметок и заметкой видна", QColor(img.pixel(x, y)).name() == Color.LINE.lower(),
          QColor(img.pixel(x, y)).name())

    stats_dir = history
    (stats_dir / "_stats.jsonl").write_text(
        "\n".join(json.dumps({"ts": f"2026-10-03T10:0{i}:00", "duration_sec": 10 + i, "elapsed_sec": 1.0,
                              "ratio_x": 10.0 + i, "mode": "full", "device": "cuda"}) for i in range(5)),
        encoding="utf-8")
    win.navigate("stats")
    st = win.shell.host.page("stats")
    QTest.qWait(50)
    check("переход на статистику: счётчик ждёт конца перехода", st.m_avg.readout.is_counting()
          and st.m_avg.readout._t == 0.0)
    QTest.qWait(Motion.PAGE_MS + Motion.RESET_MS + 300)
    check("счётчик доехал до значения и остановился", not st.m_avg.readout.is_counting()
          and st.m_avg.readout._text == st.m_avg.readout.accessibleName().split(": ")[-1])
    st._set_mode("full")
    check("смена фильтра — снова счёт", st.m_count.readout.is_counting())
    QTest.qWait(Motion.RESET_MS + 300)

    win.navigate("settings")
    QTest.qWait(Motion.PAGE_MS + 100)
    sp = win.shell.host.page("settings")
    sp.show_tab("recognition")

    def fill_sheet():
        for w in win.findChildren(TextEditorSheet):
            if w.isVisible():
                w.edit.setPlainText("Синхрон, Сэйтайп, Смыслокод")
                ok = "токен" in w.status.text()
                w.accept()
                fill_sheet.status_ok = ok
                return
        QTimer.singleShot(30, fill_sheet)

    fill_sheet.status_ok = False
    QTimer.singleShot(30, fill_sheet)
    sp._open_dict_sheet()
    check("«Развернуть» словаря: лист на всё окно со счётчиком, «Готово» возвращает текст",
          sp.dict_edit.toPlainText() == "Синхрон, Сэйтайп, Смыслокод" and fill_sheet.status_ok)

    check("режим обработки на «Диктовках»", win.page_home.controls.mode_link._text.startswith("Режим:")
          and processing_mode_text({"processing_mode": "always_streaming"}) == "Режим: streaming"
          and "порог 12 с" in processing_mode_text({"processing_mode": "auto", "auto_threshold_sec": 12}))
    win.navigate("home")
    QTest.qWait(Motion.PAGE_MS + 100)
    win.page_home.controls.mode_link.clicked.emit()
    QTest.qWait(Motion.PAGE_MS + 100)
    check("ссылка режима открывает настройки «Распознавание»", win.shell.host.current() == "settings"
          and sp.current_tab() == "recognition")

    fly = win.shell.flyout
    nb = win.shell.sidebar.buttons["stats"]
    check("у пунктов меню нет системной подсказки", not any(b.toolTip() for b in win.shell.sidebar.buttons.values()))
    nb.hovered.emit(True)
    QTest.qWait(30)
    check("наведение на пункт меню — вкладка начинает выдвигаться", fly.isVisible() and 0.0 <= fly._t < 1.0)
    QTest.qWait(Motion.NAV_HATCH_MS + 120)
    check("вкладка выдвинулась у своего пункта, с его названием",
          fly._t == 1.0 and fly._btn is nb and fly.y() == nb.mapTo(win.shell, QPoint(0, 0)).y()
          and fly.x() == win.shell.sidebar.width() - 1)
    nb.hovered.emit(False)
    QTest.qWait(Motion.NAV_HATCH_MS + 200)
    check("курсор ушёл — вкладка спряталась", not fly.isVisible())
    win._quit()


def test_no_flash_windows_on_build() -> None:
    """05.10: на старте мелькали 13–22 окошка — setVisible(True) у виджета ещё
    без родителя показывает его отдельным окном. Пока строится главное окно,
    Qt не должен показать ни одного окна верхнего уровня."""
    print("старт без мелькающих окон:")
    app = _app()
    from PySide6.QtCore import QEvent, QObject
    from PySide6.QtWidgets import QWidget

    flashed = []

    class _Spy(QObject):
        def eventFilter(self, obj, ev):  # noqa: N802
            if ev.type() == QEvent.Show and isinstance(obj, QWidget) and obj.isWindow():
                flashed.append(f"{type(obj).__name__} {obj.width()}x{obj.height()}")
            return False

    spy = _Spy()
    app.installEventFilter(spy)
    try:
        win = _build_window(Path(tempfile.mkdtemp(prefix="saytype-flash-")))
    finally:
        app.removeEventFilter(spy)
    check("пока строится окно, ни один виджет не показан отдельным окном", not flashed,
          f"{len(flashed)}: {flashed[:6]}")
    win._quit()


def test_second_launch_and_icons() -> None:
    """05.10: вторая копия будит первую вместо «откройте через трей»; знак V5
    на плитке — в окне, трее и диалогах."""
    print("повторный запуск и иконки:")
    import threading
    import uuid
    from saytype import single_instance

    name = f"saytype-test-{uuid.uuid4().hex}"
    check("без живой копии просьба показать окно не уходит", not single_instance.request_show(name))
    if sys.platform == "win32":
        handle = single_instance.create_show_event(name)
        got = threading.Event()
        single_instance.listen(handle, got.set)
        check("вторая копия достучалась до первой", single_instance.request_show(name))
        check("первая получила просьбу показать окно", got.wait(2.0))
        got.clear()
        single_instance.request_show(name)
        check("второй клик — снова показ (событие не залипает)", got.wait(2.0))

    app = _app()
    from saytype.ui.icons import app_tile_pixmap
    from saytype.ui.tokens import Color
    from saytype.ui.trayguard import tray_icon_registered

    img = app_tile_pixmap(32, Color.APP_TILE).toImage()
    check("плитка: фон — цвет APP_TILE, углы прозрачные",
          img.pixelColor(2, 16).name().upper() == Color.APP_TILE and img.pixelColor(0, 0).alpha() == 0,
          f"{img.pixelColor(2, 16).name()} / alpha {img.pixelColor(0, 0).alpha()}")
    check("плитка: белый знак внутри",
          any(img.pixelColor(x, y).name().upper() == Color.APP_TILE_MARK for x in range(8, 24) for y in range(8, 24)))
    win = _build_window(Path(tempfile.mkdtemp(prefix="saytype-icons-")))
    check("иконка окна и приложения — знак V5, а не файл из профиля",
          not win.windowIcon().isNull() and win.windowIcon().cacheKey() == app.windowIcon().cacheKey())
    win._state = "recording"
    win._update_tray()
    check("трей при записи — красная плитка", win._tray.icon().cacheKey() == win._recording_icon.cacheKey())
    win._state = "idle"
    win._update_tray()
    check("трей в покое — синяя плитка", win._tray.icon().cacheKey() == win._idle_icon.cacheKey())
    check("сторож трея заведён", win._tray_guard.isActive())
    check("без окна трея (offscreen) проверка отвечает «нечем проверить», а не «пропал»",
          tray_icon_registered() is None)
    win._ensure_tray_registered()  # None → значок не трогает, не падает
    win._quit()


def test_feedback_round3() -> None:
    """Третий круг (08.10): корзина у строки, меньше служебного на экране, микрофон
    в строке с кнопкой, «из файла» иконкой, полный текст листом, часы строки слева.

    Корзина Windows в тесте подменена переносом во временную папку: прогон не мусорит
    в Корзине машины, а занятый плеером файл так же не переносится — блокировку
    это ловит не хуже настоящей Корзины.
    """
    print("третий круг правок:")
    app = _app()
    import os as _os
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QPushButton
    from saytype.ui import history as hist_mod
    from saytype.ui.tokens import Color, Motion
    from saytype.ui.widgets import Modal, SectionHead

    history = Path(_TMP) / "history-round3"
    calls = history / "Calls"
    calls.mkdir(parents=True, exist_ok=True)
    trash = Path(_TMP) / "trash-round3"
    trash.mkdir(exist_ok=True)

    def fake_trash(path: str) -> bool:
        try:
            _os.replace(path, trash / Path(path).name)
            return True
        except OSError:
            return False

    real_trash = hist_mod.QFile.moveToTrash
    hist_mod.QFile.moveToTrash = staticmethod(fake_trash)

    def voice(stem: str, sec: float, text: str) -> None:
        with wave.open(str(history / f"{stem}.wav"), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x01\x00" * int(16000 * sec))
        (history / f"{stem}.txt").write_text(text, encoding="utf-8")
        (history / f"{stem}.meta.json").write_text(json.dumps({"elapsed_sec": 0.5, "ratio_x": 8.0}), encoding="utf-8")

    voice("2026-10-08T10-00-00", 3, "первая")
    voice("2026-10-08T11-00-00", 4, "вторая")
    stem = "2026-10-08 09-00-00"
    (calls / f"{stem}.md").write_text("---\nduration_sec: 60\n---\n\n**[Я]:** привет\n", encoding="utf-8")
    with wave.open(str(calls / f"{stem}.wav"), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 2 * 16000)

    win = _build_window(history)
    win.resize(1280, 800)
    win.show()
    win.refresh_history()
    QTest.qWait(150)
    page = win.page_home
    page.player._out.setMuted(True)  # прогон без звука на колонках

    check("в шапке только заголовок", not page.head.findChildren(type(page.controls.mic_link.dot)))
    check("секции «Последние …» и «Хранить N» нет", not page.findChildren(SectionHead)
          and not win.page_calls.findChildren(SectionHead))
    check("строки «Я · Микрофон / Собеседник» у созвона нет", not hasattr(win.page_calls, "sources"))
    btn, imp = page.controls.button, page.controls.import_btn
    check("«из файла» — иконка сразу за «Начать запись», того же роста",
          imp is not None and imp.text() == "" and imp.x() > btn.x() + btn.width()
          and imp.x() - (btn.x() + btn.width()) <= 12 and imp.height() == btn.height(),
          f"зазор {imp.x() - btn.x() - btn.width() if imp else '—'}, рост {imp.height() if imp else '—'}/{btn.height()}")
    check("у созвона «из файла» нет, микрофон есть", win.page_calls.controls.import_btn is None
          and not win.page_calls.controls.mic_link.isHidden())

    QTest.mouseClick(page.controls.mic_link, Qt.LeftButton)
    QTest.qWait(Motion.PAGE_MS + 150)
    settings_page = win.shell.host.page("settings")
    check("клик по микрофону → Настройки, вкладка «Запись»",
          win.shell.host.current() == "settings" and settings_page.current_tab() == "record",
          f"{win.shell.host.current()} / {settings_page.current_tab() if settings_page else '—'}")
    win.navigate("home")
    QTest.qWait(Motion.PAGE_MS + 150)

    row = page._rows[0]
    check("часы строки прижаты влево", row.mini.x() == 0, str(row.mini.x()))
    check("время строки серое, не чернилами", row.time_strip._color.lower() == Color.DIGIT_MINI.lower(),
          row.time_strip._color)

    def answer_modal(text: str, delay: int = 150) -> None:
        def click() -> None:
            for w in win.findChildren(Modal):
                if w.isVisible():
                    for b in w.findChildren(QPushButton):
                        if b.text() == text:
                            b.click()
                            return
            QTimer.singleShot(50, click)
        QTimer.singleShot(delay, click)

    files0 = row.files()
    check("у диктовки три файла: wav, txt, meta", sorted(p.suffix for p in files0) == [".json", ".txt", ".wav"])
    answer_modal("Отмена")
    QTest.mouseClick(row.delete_btn, Qt.LeftButton)
    QTest.qWait(100)
    check("«Отмена» ничего не удаляет", all(p.exists() for p in files0))

    page._on_row_play(row)  # аудио загружено в плеер — файл занят
    QTest.qWait(400)
    answer_modal("Удалить")
    QTest.mouseClick(row.delete_btn, Qt.LeftButton)
    QTest.qWait(200)
    check("играющая диктовка удалилась целиком", not any(p.exists() for p in files0),
          str([p.name for p in files0 if p.exists()]))
    check("…и ушла в «Корзину»", all((trash / p.name).exists() for p in files0))
    check("строка пропала, плеер свободен", len(page._rows) == 1 and page.player.source() is None)
    modals = [w for w in win.findChildren(Modal) if w.isVisible()]
    check("без окна «не всё удалилось»", not modals)

    win.navigate("calls")
    QTest.qWait(Motion.PAGE_MS + 150)
    call_row = win.page_calls._rows[0]
    cfiles = call_row.files()
    check("у созвона md + wav", sorted(p.suffix for p in cfiles) == [".md", ".wav"])
    answer_modal("Удалить")
    QTest.mouseClick(call_row.delete_btn, Qt.LeftButton)
    QTest.qWait(200)
    check("созвон удалился: транскрипт и аудио", not any(p.exists() for p in cfiles))
    check("список созвонов пуст", not win.page_calls._rows)

    notes = win.shell.host.page("notes")
    check("«Расшифровать файл» — иконка в шапке «Заметок» рядом с «Новой заметкой»",
          notes is not None and notes.import_btn.parent() is notes.new_btn.parent() and notes.import_btn.text() == "")

    hist_mod.QFile.moveToTrash = real_trash
    win._quit()


def test_feedback_round4() -> None:
    """Четвёртый круг (08.10): лист и диалоги — слоем внутри окна с затемнением,
    размер листа в пределах min/max, окно не блокируется; «Модели» въезжают с карточками."""
    print("четвёртый круг правок:")
    app = _app()
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QColor
    from PySide6.QtTest import QTest
    from saytype.ui import tokens
    from saytype.ui.tokens import Color, Grid, Motion
    from saytype.ui.widgets import Modal, TextViewer

    tokens.Motion.reduced = staticmethod(lambda: tokens.Motion._user_off)  # анимации включены
    history = Path(_TMP) / "history-round4"
    history.mkdir(parents=True, exist_ok=True)
    win = _build_window(history)
    win.resize(1280, 800)
    win.show()
    QTest.qWait(150)

    # --- «Модели» въезжают вместе с карточками ---
    win.navigate("models")
    layer = win.shell.host._layer
    snap = layer._new.toImage() if layer is not None else None
    # выбранная модель — синяя карточка: в снимке без карточек синего нет вовсе
    blue = 0
    if snap is not None:
        accent = QColor(Color.ACCENT).rgb()
        blue = sum(1 for x in range(0, snap.width(), 8) for y in range(0, snap.height(), 8)
                   if snap.pixel(x, y) == accent)
    check("снимок перехода «Моделей» уже с карточками", blue > 200, f"синих точек {blue}")
    QTest.qWait(Motion.PAGE_MS + 150)

    # --- страница не шире окна ни в одном состоянии записи: иначе на «Распознаю…»
    # весь интерфейс на секунду растягивался и сжимался обратно ---
    over = []
    for pg in (win.page_home, win.page_calls):
        for W in (Grid.WINDOW_MIN_W, 900, 1100, 1280):
            win.resize(W, 800)
            QTest.qWait(40)
            for st, other in (("idle", False), ("recording", False), ("processing", False),
                              ("cancelling", False), ("idle", True), ("idle", False)):
                pg.set_state(st, other)
                QTest.qWait(20)
                if pg.scroll.widget().width() != pg.scroll.viewport().width():
                    over.append(f"{pg.kind} {W} {st}: {pg.scroll.widget().width()}>{pg.scroll.viewport().width()}")
    check("страница не шире окна ни в одном состоянии записи", not over, "; ".join(over[:4]))
    win.resize(1280, 800)

    # --- лист: затемнение, размер, окно не заблокировано ---
    win.navigate("home")
    QTest.qWait(Motion.PAGE_MS + 150)
    win.resize(1900, 1100)
    QTest.qWait(50)
    viewer = TextViewer("Проверка", "<p>текст</p>", "текст", parent=win)
    viewer.show()
    QTest.qWait(Motion.OVERLAY_MS + 100)
    check("лист — слой внутри окна, а не отдельное окно", viewer.parentWidget() is win and not viewer.isWindow())
    check("окно не заблокировано модальностью", QApplication.activeModalWidget() is None)
    check("на большом окне лист не шире чтения", viewer.card.width() == Grid.SHEET_MAX_W
          and viewer.card.height() == Grid.SHEET_MAX_H, f"{viewer.card.width()}×{viewer.card.height()}")
    r = viewer.card.geometry()
    check("лист по центру окна", abs(r.center().x() - win.width() // 2) <= 1 and abs(r.center().y() - win.height() // 2) <= 1)
    img = win.grab().toImage()
    dpr = img.devicePixelRatio()
    corner = QColor(img.pixel(int(5 * dpr), int(win.height() * dpr) - 5))
    check("под листом затемнение", corner.lightness() < QColor(Color.BG).lightness() - 30, corner.name())
    win.resize(760, 560)
    QTest.qWait(50)
    check("окно уменьшилось — лист следом и целиком внутри",
          viewer.geometry() == win.rect() and win.rect().contains(viewer.card.geometry())
          and viewer.card.width() <= 760 - 2 * Grid.SHEET_MARGIN + 1, f"{viewer.card.geometry()}")
    QTest.mouseClick(viewer, Qt.LeftButton, pos=QPoint(3, 3))
    QTest.qWait(50)
    check("клик по затемнению закрывает лист чтения (и удаляет его)",
          not [w for w in win.findChildren(TextViewer) if w.isVisible()])

    # --- диалог подтверждения: тоже слоем, exec отдаёт результат ---
    win.resize(1280, 800)
    QTest.qWait(50)
    from PySide6.QtCore import QTimer
    from saytype.ui.widgets import confirm
    seen: dict = {}

    def press_ok():
        for m in win.findChildren(Modal):
            if m.isVisible():
                seen["modal_in_window"] = not m.isWindow()
                seen["no_modality"] = QApplication.activeModalWidget() is None
                seen["width"] = m.card.width()
                for b in m.findChildren(QPushButton):
                    if b.text() == "Удалить":
                        b.click()
                return
        QTimer.singleShot(30, press_ok)

    from PySide6.QtWidgets import QPushButton
    QTimer.singleShot(Motion.OVERLAY_MS + 50, press_ok)
    ok = confirm(win, "Удалить?", "Проверка подтверждения")
    check("confirm слоем внутри окна, без модальности Windows",
          seen.get("modal_in_window") and seen.get("no_modality"), str(seen))
    check("confirm вернул «Удалить»", ok is True)
    check("ширина диалога как задана (520)", seen.get("width") == 520, str(seen.get("width")))
    win._quit()


def main() -> int:
    test_clock_boundaries()
    test_svgpath_and_history_helpers()
    test_window_contract()
    test_settings_page_keys()
    test_stats_page()
    test_models_page()
    test_settings_roundtrip()
    test_no_accumulation()
    test_incremental_history()
    test_feedback_round1()
    test_feedback_round2()
    test_feedback_round3()
    test_feedback_round4()
    test_second_launch_and_icons()
    test_no_flash_windows_on_build()
    print()
    if FAILED:
        print(f"ПРОВАЛЕНО: {len(FAILED)} — {FAILED}")
        return 1
    print("всё прошло")
    return 0


if __name__ == "__main__":
    sys.exit(main())
