"""Тесты апдейтера (T-262).

Главное, что здесь проверяется, — что апдейтер молчит там, где должен молчать.
Запуск из исходников, ненастроенный фид и мёртвая сеть не должны ни ронять
приложение, ни показывать человеку диалог с ошибкой при каждом старте: это
оффлайн-инструмент, и «нет интернета» для него нормальное состояние.

Обратная сторона тоже проверяется: когда человек сам нажал «Проверить
обновления», молчать нельзя — ошибка обязана долететь до вызывающего.
"""

from __future__ import annotations

import pytest

from saytype import __version__, updater


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(updater.ENV_FEED, raising=False)
    monkeypatch.setattr(updater, "GITHUB_REPO", "")
    monkeypatch.setattr(updater.sys, "frozen", False, raising=False)


def test_feed_prefers_env_over_constant(monkeypatch):
    monkeypatch.setattr(updater, "GITHUB_REPO", "https://github.com/someone/repo")
    assert updater.feed() == "https://github.com/someone/repo"
    monkeypatch.setenv(updater.ENV_FEED, r"C:\feed")
    assert updater.feed() == r"C:\feed", "переменная окружения должна перебивать константу"


def test_not_available_from_sources():
    """Запуск из исходников: апдейтер выключен целиком."""
    assert updater.is_available() is False
    assert updater.check() is None, "фоновая проверка обязана молчать"


def test_not_available_without_feed(monkeypatch):
    """Собранная версия без настроенного фида в сеть не ходит."""
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    assert updater.feed() == ""
    assert updater.is_available() is False
    assert updater.check() is None


def test_manual_check_reports_reason():
    """Человек нажал кнопку — ответ обязателен, пусть и отрицательный."""
    with pytest.raises(RuntimeError):
        updater.check(quiet=False)


def test_version_falls_back_to_package():
    """Вне установки версия берётся из пакета, а не выдумывается."""
    assert updater.current_version() == __version__


def test_broken_feed_is_silent_in_background(monkeypatch):
    """Мёртвый источник в фоне даёт None и строчку в лог, а не исключение."""
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setenv(updater.ENV_FEED, r"Z:\нет-такой-папки")
    monkeypatch.setattr(updater, "is_available", lambda: True)
    lines: list[str] = []

    assert updater.check(logger=lines.append) is None
    assert any("не удалась" in line for line in lines), "причина должна остаться в логе"


def test_broken_feed_raises_on_manual_check(monkeypatch):
    """Тот же мёртвый источник при ручной проверке — исключение наружу."""
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setenv(updater.ENV_FEED, r"Z:\нет-такой-папки")
    monkeypatch.setattr(updater, "is_available", lambda: True)

    with pytest.raises(Exception):
        updater.check(logger=lambda _: None, quiet=False)


def test_version_of_survives_garbage():
    """Номер версии для текста уведомления не должен ронять UI."""
    assert updater.version_of(object()) == "?"


# === T-328: заметки релиза ===


class _Info:
    """UpdateInfo ровно в том объёме, в каком его читает notes_of."""

    def __init__(self, notes):
        self.TargetFullRelease = type("Asset", (), {"NotesMarkdown": notes})()


def test_notes_absent_give_empty_string():
    """Старый релиз без заметок и мусор вместо UpdateInfo — пусто, не ошибка.

    От этого зависит поведение окна: пустая строка = показать его как до T-328,
    а исключение здесь уронило бы предложение обновиться целиком.
    """
    assert updater.notes_of(object()) == ""
    assert updater.notes_of(_Info(None)) == ""
    assert updater.notes_of(_Info("")) == ""
    assert updater.notes_of(_Info("## 0.3.0\n\n---\n")) == "", "одно оформление — нечего показывать"


def test_notes_markdown_becomes_plain_text():
    notes = updater.notes_of(_Info(
        "## 0.3.0 — 12 августа\n"
        "\n"
        "- Модель переключается **без перезапуска**\n"
        "- Полный список — [на странице релиза](https://example.com/r)\n"
    ))
    assert notes == (
        "• Модель переключается без перезапуска\n"
        "• Полный список — на странице релиза"
    )


def test_notes_join_wrapped_lines():
    """Перенос по ширине в CHANGELOG.md — не новый пункт."""
    notes = updater.notes_of(_Info(
        "- Мастер первого запуска спрашивает микрофон, потом\n"
        "  скачивает выбранную модель\n"
        "- Вторая строчка\n"
    ))
    assert notes.splitlines() == [
        "• Мастер первого запуска спрашивает микрофон, потом скачивает выбранную модель",
        "• Вторая строчка",
    ]


def test_notes_drop_markdown_comments():
    """Служебная пометка из CHANGELOG.md человеку не адресована.

    Поймано живым прогоном T-328: маркер `<!-- ... -->` доехал до окна
    обновления и встал в списке изменений отдельной строкой.
    """
    notes = updater.notes_of(_Info(
        "<!-- временно, убрать после прогона -->\n"
        "- Настоящее изменение\n"
        "<!-- /временно -->\n"
    ))
    assert notes == "• Настоящее изменение"


def test_notes_are_capped():
    """Длинный список не должен растягивать окно на весь экран."""
    notes = updater.notes_of(_Info("\n".join(f"- изменение {i}" for i in range(30))))
    body, tail = notes.rsplit("\n", 1)
    assert len(body.splitlines()) == 8
    assert "и ещё изменения" in tail
