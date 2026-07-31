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

from iwhisper import __version__, updater


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
