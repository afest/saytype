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
"""

from __future__ import annotations

import os
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
