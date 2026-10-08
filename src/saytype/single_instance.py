"""Вторая копия будит первую вместо сообщения «уже запущен».

Повторный клик по ярлыку, закреплённой кнопке на панели задач или пункту меню
«Пуск» запускает ещё один процесс. Раньше он показывал «SayType уже запущен —
откройте через трей» и выходил: окно не открывалось, а если значок трея пропал
(перезапуск Проводника после сна), открыть приложение было вообще нечем.

Теперь первая копия держит именованное событие `<mutex>-show` и ждёт его в
фоновом потоке. Вторая копия разрешает ей забрать передний план
(`AllowSetForegroundWindow`: право на фокус есть у процесса, запущенного
кликом, а не у висящего в фоне), взводит событие и выходит. Первая показывает
окно.

Модуль импортируется до PySide6 и faster-whisper: вторая копия должна выйти
за доли секунды, поэтому здесь только ctypes.
"""
from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from typing import Callable

_EVENT_MODIFY_STATE = 0x0002
_SYNCHRONIZE = 0x00100000
_ASFW_ANY = 0xFFFFFFFF  # (DWORD)-1 — любой процесс
_WAIT_OBJECT_0 = 0
_INFINITE = 0xFFFFFFFF

if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _k32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.CreateEventW.restype = wintypes.HANDLE
    _k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.OpenEventW.restype = wintypes.HANDLE
    _k32.SetEvent.argtypes = [wintypes.HANDLE]
    _k32.SetEvent.restype = wintypes.BOOL
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _u32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    _u32.AllowSetForegroundWindow.restype = wintypes.BOOL
else:
    _k32 = _u32 = None


def show_event_name(mutex_name: str) -> str:
    return mutex_name + "-show"


def create_show_event(mutex_name: str):
    """Первая копия: завести событие. Auto-reset — один клик даёт один показ.

    Возвращает handle или None (не Windows / не удалось — тогда вторая копия
    откатится на прежнее сообщение).
    """
    if _k32 is None:
        return None
    handle = _k32.CreateEventW(None, False, False, show_event_name(mutex_name))
    return handle or None


def request_show(mutex_name: str) -> bool:
    """Вторая копия: попросить первую показать окно. False — первая копия
    события не держит (старая версия или ещё не успела его завести)."""
    if _k32 is None:
        return False
    handle = _k32.OpenEventW(_EVENT_MODIFY_STATE | _SYNCHRONIZE, False, show_event_name(mutex_name))
    if not handle:
        return False
    try:
        try:
            _u32.AllowSetForegroundWindow(_ASFW_ANY)
        except Exception:
            pass  # без права на фокус окно всё равно появится, только мигнёт на панели
        return bool(_k32.SetEvent(handle))
    finally:
        _k32.CloseHandle(handle)


def listen(handle, on_request: Callable[[], None]) -> threading.Thread | None:
    """Фоновый поток: на каждый сигнал зовёт `on_request` (из этого потока —
    в GUI его переносит вызывающий, через Qt-сигнал). Живёт до конца процесса."""
    if _k32 is None or not handle:
        return None

    def _worker() -> None:
        while _k32.WaitForSingleObject(handle, _INFINITE) == _WAIT_OBJECT_0:
            try:
                on_request()
            except Exception:
                pass  # поток не должен умереть из-за одного неудачного показа

    t = threading.Thread(target=_worker, daemon=True, name="show-window-request")
    t.start()
    return t
