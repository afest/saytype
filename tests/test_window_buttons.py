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


def main() -> int:
    test_header_buttons_open_sections()
    print()
    if FAILED:
        print(f"ПРОВАЛЕНО: {len(FAILED)} — {', '.join(FAILED)}")
        return 1
    print("все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
