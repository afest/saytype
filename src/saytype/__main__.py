"""Точка входа: ``pythonw.exe -m saytype``.

Тяжёлая часть — в ``transcribe_ui``: там на module-level сидит защита от второй
копии и splash, которые обязаны отработать ДО импорта PySide6 и faster-whisper.
Поэтому здесь только импорт и вызов main() — порядок держит сам модуль.
"""

from .transcribe_ui import run

run()
