"""crashguard.py — след от аварий, которые Python-хуки не видят (T-268, T-278).

`sys.excepthook` / `threading.excepthook` в `transcribe_ui.py` ловят только
исключения Python. Авария в нативном коде (0xC0000409 из CTranslate2 — см.
инцидент 2026-07-29), OOM-убийца, BSOD или `taskkill` не дают ни трейсбека, ни
строчки в лог: под `pythonw.exe` окно просто исчезает. Именно из-за этого
инцидент 2026-05-25 остался с пометкой «краши без stack trace, `_crash.log` пуст».

Механика простая: пока сессия жива, в профиле пользователя лежит файл-маркер с тем,
чем она занята. Штатный выход его удаляет. Если следующий старт маркер находит —
предыдущая сессия умерла, и мы пишем в `_crash.log` её pid, время старта и
последнее действие. Плюс `faulthandler` — он добирает то, что приходит сигналом
(SIGSEGV / SIGABRT); 0xC0000409 мимо него проходит, это fail-fast.

T-278 закрыл две дыры первого разбора:
  * у действия появилось **время начала** (`note_started`) — «идёт запись, начата
    в 16:33:40» читается вместе с дампом потоков, а не вместо него;
  * рестарт больше не съедает улику: при обнаружении аварии рядом с фактом
    пишется собранный контекст (модель, режим, mtime кода, свободная VRAM,
    хвост `_stats.jsonl`) и сырое содержимое маркера. Сбор — best-effort и
    строго ПОСЛЕ записи самого факта: сломанный сборщик не может её отменить.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

MARKER_NAME = "_session.alive"
LOG_NAME = "_crash.log"
STATS_TAIL_LINES = 5  # сколько последних транскрипций показать в отчёте

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_marker: Optional[Path] = None
_started: str = ""
_note: str = ""
_note_started: str = ""
_context: dict = {}
_lock = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write_marker() -> None:
    """Записать текущее состояние сессии в маркер. Вызывать под `_lock`."""
    if _marker is None:
        return
    try:
        _marker.write_text(
            json.dumps({"pid": os.getpid(), "started": _started, "note": _note,
                        "note_started": _note_started, "ts": _now(),
                        "context": _context},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def mark(note: str) -> None:
    """Чем сессия занята сейчас. Попадёт в `_crash.log`, если она не доживёт.

    Каждый вызов — новое действие: запоминается и его текст, и время начала.
    Пишет файл целиком, поэтому внутрь циклов (чанки streaming'а, колбэки
    аудио) тащить нельзя — только на смену состояния.
    """
    global _note, _note_started
    if _marker is None:
        return
    with _lock:
        _note = note
        _note_started = _now()
        _write_marker()


def set_context(**fields) -> None:
    """Постоянные поля сессии (модель, режим обработки, путь к `_stats.jsonl`).

    В отличие от `mark` не меняет «последнее действие» — только дополняет
    контекст, который попадёт в отчёт об аварии. `None` игнорируется, чтобы
    неудачное определение значения не затирало уже известное.
    """
    if _marker is None:
        return
    with _lock:
        _context.update({k: v for k, v in fields.items() if v is not None})
        _write_marker()


def _free_vram() -> str:
    """Свободная VRAM через nvidia-smi. Best-effort: нет NVIDIA / висит → строка
    с причиной, а не исключение (паттерн `engine.detect_gpu`)."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8, creationflags=_CREATE_NO_WINDOW,
        )
    except Exception as exc:
        return f"не удалось собрать ({exc!r})"
    if proc.returncode != 0 or not proc.stdout.strip():
        return "не удалось собрать (nvidia-smi молчит)"
    parts = [p.strip() for p in proc.stdout.strip().splitlines()[0].split(",")]
    if len(parts) < 2:
        return f"не удалось собрать (ответ {proc.stdout.strip()!r})"
    return f"свободно {parts[0]} МБ из {parts[1]}"


def _stats_tail(path_str: str, lines: int = STATS_TAIL_LINES) -> list[str]:
    """Последние N записей `_stats.jsonl` — что модель успела сделать до смерти."""
    path = Path(path_str)
    if not path.exists():
        return [f"  (нет файла {path})"]
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        tail = f.readlines()[-lines:]
    return ["  " + line.strip() for line in tail if line.strip()] or ["  (пусто)"]


def collect_context(prev: dict, base_dir: Path | None = None) -> list[str]:
    """Улики о том, в каких условиях умерла сессия. Каждый блок — отдельный try:
    отвалившийся сборщик забирает с собой только свою строку.

    Вынесено отдельной функцией сознательно — так регрессия может подменить её
    заведомо сломанной и проверить, что запись о самой аварии всё равно есть.
    """
    ctx = prev.get("context") or {}
    out: list[str] = []

    out.append(f"модель: {ctx.get('model', 'не записана')}")
    out.append(f"режим обработки: {ctx.get('mode', 'не записан')}")

    try:
        # Код и маркер лежат в разных местах (пакет vs профиль пользователя),
        # поэтому mtime берём у своего модуля, а не относительно base_dir.
        code = Path(__file__).resolve().parent / "transcribe_ui.py"
        st = code.stat()
        mtime = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
        out.append(f"transcribe_ui.py: изменён {mtime}, {st.st_size} байт")
    except Exception as exc:
        out.append(f"transcribe_ui.py: не удалось прочитать ({exc!r})")

    try:
        out.append(f"VRAM: {_free_vram()}")
    except Exception as exc:
        out.append(f"VRAM: не удалось собрать ({exc!r})")

    stats_path = ctx.get("stats_path")
    if stats_path:
        try:
            out.append(f"последние транскрипции ({stats_path}):")
            out.extend(_stats_tail(stats_path))
        except Exception as exc:
            out.append(f"  (не удалось прочитать: {exc!r})")

    return out


def _report_crash(log_path: Path, raw: str, prev: dict, base_dir: Path) -> None:
    """Записать блок об аварии: сначала факт, потом — что удалось собрать."""
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n=== АВАРИЙНОЕ ЗАВЕРШЕНИЕ ПРЕДЫДУЩЕЙ СЕССИИ "
                f"(обнаружено {_started}) ===\n")
        f.write(f"pid: {prev.get('pid', '?')}, старт сессии: {prev.get('started', '?')}\n")
        f.write(f"последнее действие: {prev.get('note', '?')}\n")
        f.write(f"  начато: {prev.get('note_started', prev.get('ts', '?'))}, "
                f"отмечено в маркере: {prev.get('ts', '?')}\n")
        f.flush()  # факт зафиксирован — дальше идёт то, что может не собраться

        try:
            for line in collect_context(prev, base_dir):
                f.write(line + "\n")
        except Exception as exc:
            f.write(f"контекст собрать не удалось целиком: {exc!r}\n")

        f.write(f"сырой маркер: {raw.strip()}\n")
        f.write("Трейсбека нет — авария в нативном коде либо процесс убит извне.\n")


def start(base_dir: Path, enable_faulthandler: bool = True) -> None:
    """Поднять маркер. Вызывать ПОСЛЕ single-instance guard: вторая копия не
    должна ни перетирать маркер живой первой, ни оставлять свой после выхода."""
    global _marker, _started
    try:
        base_dir = Path(base_dir)
        log_path = base_dir / LOG_NAME
        _marker = base_dir / MARKER_NAME
        _started = _now()

        if _marker.exists():  # прошлый запуск не дошёл до штатного выхода
            try:
                raw = _marker.read_text(encoding="utf-8")
            except Exception as exc:
                raw = f"(не прочитан: {exc!r})"
            try:
                prev = json.loads(raw)
                if not isinstance(prev, dict):
                    prev = {}
            except Exception:
                prev = {}
            try:
                _report_crash(log_path, raw, prev, base_dir)
            except Exception:
                pass

        if enable_faulthandler:
            try:
                import faulthandler

                faulthandler.enable(
                    file=open(log_path, "a", encoding="utf-8", buffering=1)
                )
            except Exception:
                pass

        mark("старт")
    except Exception:
        _marker = None  # что угодно сломалось — просто работаем без маркера


def clean_exit() -> None:
    """Штатный выход: снять маркер, чтобы следующий старт не счёл его крашем."""
    if _marker is None:
        return
    try:
        _marker.unlink(missing_ok=True)
    except Exception:
        pass
