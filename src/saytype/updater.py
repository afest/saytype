"""updater.py — проверка и установка обновлений через Velopack (T-262).

Обновления касаются только установленной версии. Запуск из исходников и
распакованная папка ничего не проверяют и в сеть не ходят: `is_available()`
отвечает «нет», и весь остальной код превращается в no-op.

Где живёт фид. Velopack принимает либо готовый источник (`GithubSource`), либо
строку — URL статического хоста или **путь к папке**. Папка используется при
проверке цикла обновления локально, до того как заведён публичный репозиторий:
это тот же кодовый путь, что и в бою, только без сети.

Про молчание. Проверка обновлений — фоновая и необязательная: нет интернета,
сломался фид, GitHub отдал 403 — всё это идёт в лог и не показывается человеку.
Диалог с ошибкой при каждом запуске оффлайн-инструмента раздражает сильнее, чем
приносит пользы; ручную проверку из настроек, наоборот, надо доводить до ответа —
человек нажал кнопку и ждёт результата.

Про заметки релиза (T-328). Текст «что изменилось» кладётся в пакет при сборке
(`vpk pack --releaseNotes`, источник — CHANGELOG.md) и приходит вместе с самим
ответом фида. Отдельного запроса к GitHub API нет намеренно: второй сетевой
вызов означал бы второе место, где текст может не прийти, и второй таймаут в
диалоге, который человек уже открыл. Нет заметок — окно показывается как
раньше, без пустого блока: это норма, а не сбой.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from typing import Callable, Optional

# Репозиторий, из релизов которого приложение берёт обновления. Пусто — значит
# публичного фида ещё нет, и автообновление просто выключено.
#
# T-314: заполнено перед первой публикацией. Пустую константу нельзя оставлять
# в релизной сборке: сборка при этом проходит, а обновления не работают у всех,
# кто уже установил, и чинится это только их ручной переустановкой.
GITHUB_REPO = "https://github.com/afest/saytype"

# Переопределение источника: путь к папке или URL статического хоста.
# Так проверяется весь цикл обновления, пока репозиторий не заведён.
ENV_FEED = "SAYTYPE_UPDATE_FEED"

_check_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"[updater] {msg}"
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        try:
            print(line.encode("ascii", "replace").decode("ascii"), file=sys.stderr, flush=True)
        except Exception:
            pass


def feed() -> str:
    """Строка источника обновлений: переменная окружения или репозиторий."""
    return (os.environ.get(ENV_FEED) or GITHUB_REPO).strip()


def is_available() -> bool:
    """Есть ли вообще смысл говорить об обновлениях.

    Нет — если запущено не из установленной версии (velopack тогда не знает, где
    искать приложение) или если фид не настроен.
    """
    if not getattr(sys, "frozen", False):
        return False
    if not feed():
        return False
    try:
        import velopack  # noqa: F401
    except Exception:
        return False
    return True


def _manager():
    """UpdateManager на текущий источник. Бросает — вызывающий гасит."""
    from velopack import GithubSource, UpdateManager

    src = feed()
    if src.startswith("https://github.com/") or src.startswith("http://github.com/"):
        return UpdateManager(GithubSource(src))
    return UpdateManager(src)  # путь к папке или URL статического хоста


def current_version() -> str:
    """Версия, из которой мы работаем. Вне установки — версия пакета."""
    from . import __version__

    if not is_available():
        return __version__
    try:
        return _manager().get_current_version()
    except Exception:
        return __version__


def check(logger: Callable[[str], None] = log, quiet: bool = True):
    """Есть ли новая версия. Возвращает UpdateInfo или None.

    `quiet=True` (фоновая проверка при старте) — ошибки уходят в лог, наружу
    отдаётся None: нет интернета не повод для диалога при каждом запуске.
    `quiet=False` (человек нажал кнопку) — ошибка пробрасывается, потому что
    молча ничего не ответить на явное действие хуже, чем показать причину.
    """
    if not is_available():
        if not quiet:
            raise RuntimeError(
                "Обновления доступны только в установленной версии приложения."
            )
        return None
    # Две проверки разом (фон при старте и кнопка в настройках) velopack не любит
    if not _check_lock.acquire(blocking=False):
        logger("проверка уже идёт — пропускаю")
        return None
    try:
        info = _manager().check_for_updates()
    except Exception as exc:
        logger(f"проверка не удалась ({exc.__class__.__name__}: {exc})"
               + (" — молчу" if quiet else ""))
        if not quiet:
            raise
        return None
    finally:
        _check_lock.release()
    if info is None:
        logger("обновлений нет")
        return None
    logger(f"доступно обновление: {version_of(info)}")
    return info


def version_of(info) -> str:
    """Номер версии из UpdateInfo — для текста уведомления."""
    try:
        return str(info.TargetFullRelease.Version)
    except Exception:
        return "?"


def release_page_url(info) -> str:
    """Страница релиза на GitHub — что человек увидит в браузере по клику.

    Не завязано на GITHUB_REPO как источник фида: T-354 может сменить его на
    статический хост или локальную папку для тестов, но страница релиза с
    заметками всё равно живёт на GitHub — адрес собирается напрямую.
    """
    version = version_of(info)
    return f"https://github.com/afest/saytype/releases/tag/v{version}"


def notes_of(info, max_lines: int = 8, max_chars: int = 700) -> str:
    """Заметки релиза обычным текстом. Нечего показать — пустая строка.

    Вызывающий сам решает, что делать с пустотой; ошибок отсюда не прилетает
    ни при каком содержимом фида. Markdown упрощается до текста, потому что
    показывается в QMessageBox: заголовок с номером версии выкидываем (он уже
    в шапке окна), список отбиваем «•», ссылку оставляем текстом.
    """
    try:
        raw = str(getattr(info.TargetFullRelease, "NotesMarkdown", "") or "")
    except Exception:
        return ""
    # Комментарии markdown — служебные пометки для того, кто ведёт CHANGELOG.md;
    # человеку в окне обновления они не адресованы. Поймано живым прогоном:
    # маркер из файла доехал до диалога и встал отдельным пунктом.
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    lines: list[str] = []
    dropped = 0
    continues = False  # предыдущая строка была началом пункта или абзаца
    for source in raw.splitlines():
        text = source.strip()
        if not text or text.startswith("#") or set(text) <= set("-=*_ "):
            continues = False  # пустая строка или заголовок разрывают абзац
            continue
        bullet = bool(re.match(r"^[-*+]\s+", text))
        text = re.sub(r"^[-*+]\s+", "", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)   # ссылка → её текст
        text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        if not text:
            continue
        # Перенос строки в исходнике — не новый пункт. В CHANGELOG.md строки
        # переносятся по ширине, и без склейки хвост длинного пункта поехал бы
        # в окне отдельной строкой без «•», как будто это другое изменение.
        if continues and not bullet and lines:
            lines[-1] += " " + text
            continue
        if len(lines) >= max_lines:
            dropped += 1
            continues = False
            continue
        lines.append(("• " + text) if bullet else text)
        continues = True
    if not lines:
        return ""
    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars].rstrip() + "…"
        dropped += 1
    if dropped:
        result += "\n…и ещё изменения — полный список на странице релиза."
    return result


def download_and_apply(
    info,
    progress_cb: Optional[Callable[[int], None]] = None,
    logger: Callable[[str], None] = log,
) -> None:
    """Скачать обновление и перезапуститься в новую версию.

    Управление сюда не возвращается: velopack завершает процесс сам. Ошибка
    скачивания, наоборот, возвращается исключением — её показывают человеку,
    потому что обновление он запустил осознанно.
    """
    manager = _manager()
    logger(f"качаю обновление {version_of(info)}")
    manager.download_updates(info, progress_cb)
    logger("применяю обновление и перезапускаюсь")
    manager.apply_updates_and_restart(info)
