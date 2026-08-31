"""Кнопки шапки открывают свои разделы — проверка кликом, а не вызовом метода.

Зачем отдельный тест: `clicked` передаёт слоту `checked: bool`. Метод с
необязательным параметром (`open_notes(select=None)`) получает в него `False`, и
раздел молча перестаёт открываться — вызов того же метода из кода при этом
работает, поэтому обычный smoke такую регрессию не видит (T-382).

Запуск:  python -m tests.test_window_buttons   (из корня репозитория)
"""

import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_TMP = tempfile.mkdtemp(prefix="saytype-buttons-test-")
os.environ["LOCALAPPDATA"] = _TMP
os.environ.pop("APPDATA", None)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # CI и прогон без монитора

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from saytype import audio_import, profile  # noqa: E402
from saytype.transcribe_ui_window import MainWindow  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def _build_window(app):
    icons = profile.profile_dir() / "icons"
    api = audio_import.FileImportApi(
        begin=lambda: True, run=lambda *a, **k: {}, end=lambda: None
    )
    return MainWindow(
        idle_icon_path=icons / "idle.png",
        recording_icon_path=icons / "recording.png",
        processing_icon_path=icons / "processing.png",
        app_icon_path=icons / "app.png",
        history_dir_getter=lambda: Path(_TMP),
        rotation_count_getter=lambda: 5,
        entry_script=Path(__file__).resolve().parent.parent / "src" / "saytype" / "transcribe_ui.py",
        model_busy_getter=lambda: False,
        file_import_api=api,
    )


def test_header_buttons_open_sections() -> None:
    print("кнопки шапки открывают свои разделы:")
    app = QApplication.instance() or QApplication(sys.argv)
    errors: list[str] = []
    sys.excepthook = lambda k, v, tb: errors.append(
        "".join(traceback.format_exception(k, v, tb))
    )
    win = _build_window(app)
    win.show()
    app.processEvents()

    note = profile.new_note_path()
    profile.write_note(note, "Заметка для проверки\n\nтекст")

    opened: list[str] = []

    def close_top_dialog() -> None:
        for w in app.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible():
                opened.append(w.windowTitle())
                w.close()

    for name, button in (
        ("Заметки", win.notes_btn),
        ("Статистика", win.stats_btn),
        ("Модели", win.models_btn),
    ):
        opened.clear()
        QTimer.singleShot(120, close_top_dialog)
        QTimer.singleShot(600, app.quit)
        button.click()
        app.exec()
        check(f"«{name}» открылся", bool(opened), "диалог не появился")

    check("без исключений в слотах", not errors, errors[0] if errors else "")
    win.close()
    profile.delete_note(note)


def test_batch_import_keeps_every_note() -> None:
    """T-442: заметки, рождённые пачкой при импорте, не затирают друг друга.

    Импорт — тот путь, где интервал между заметками задаёт цикл, а не человек:
    в UI файлы приходят по одному (`_dragged_audio_path` отбивает drop с
    несколькими), но сама заметка и её `.meta.json` создаются в `_note_from_import`
    подряд, и на прежнем коде две расшифровки в одном тике часов делили один
    путь — второй текст затирал первый вместе с меткой источника.
    """
    print("пакетный импорт не теряет заметки:")
    app = QApplication.instance() or QApplication(sys.argv)
    win = _build_window(app)

    count = 200
    made = []
    for i in range(count):
        src = Path(_TMP) / f"интервью-{i}.mp3"
        note = win._note_from_import(src, f"расшифровка {i}")
        if note is None:
            break
        made.append((note, i, src.name))

    check("создались все заметки", len(made) == count, f"создано {len(made)} из {count}")
    check("пути не повторяются", len({n for n, _, _ in made}) == len(made))

    texts_ok = sum(1 for n, i, name in made
                   if n.read_text(encoding="utf-8") == f"{Path(name).stem}\n\nрасшифровка {i}\n")
    check("ни одна расшифровка не затёрта", texts_ok == len(made),
          f"уцелело {texts_ok} из {len(made)}")

    metas_ok = sum(1 for n, _, name in made
                   if profile.read_note_meta(n) == {
                       "source": profile.NOTE_SOURCE_IMPORT, "source_name": name})
    check("у каждой заметки своя метка источника", metas_ok == len(made),
          f"совпало {metas_ok} из {len(made)}")

    win.close()
    for note, _, _ in made:
        profile.delete_note(note)


def main() -> int:
    test_header_buttons_open_sections()
    test_batch_import_keeps_every_note()
    print()
    if FAILED:
        print(f"ПРОВАЛЕНО: {len(FAILED)} — {', '.join(FAILED)}")
        return 1
    print("все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
