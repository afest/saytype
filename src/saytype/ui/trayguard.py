"""Видит ли Windows наш значок в трее на самом деле.

Значок живёт в Проводнике, а не в приложении. Когда Проводник перезапускается
(после сна так бывает), Windows рассылает «TaskbarCreated», и Qt ставит значок
заново. Если панель в этот момент ещё не готова, повторная регистрация молча не
проходит: `QSystemTrayIcon.isVisible()` по-прежнему True, а в трее пусто
(04.10.2026: V5 остался без значка после выхода из сна, рабочая версия,
запущенная позже, — со значком).

Проверка — `Shell_NotifyIconGetRect` по скрытому окну, через которое Qt
регистрирует значок: S_OK и для видимого значка, и для убранного под стрелку ^,
ошибка — если Проводник о нём не знает.
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

_TRAY_WINDOW_CLASS = "TrayIconMessageWindow"  # Qt6<ver>TrayIconMessageWindowClass


class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort),
                ("d4", ctypes.c_ubyte * 8)]


class _NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("guidItem", _GUID)]


if sys.platform == "win32":
    _u32 = ctypes.WinDLL("user32")
    _shell32 = ctypes.WinDLL("shell32")
    _EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _u32.EnumWindows.argtypes = [_EnumProc, wintypes.LPARAM]
    _u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _shell32.Shell_NotifyIconGetRect.argtypes = [ctypes.POINTER(_NOTIFYICONIDENTIFIER),
                                                 ctypes.POINTER(wintypes.RECT)]
    _shell32.Shell_NotifyIconGetRect.restype = ctypes.c_long
else:
    _u32 = _shell32 = None


def _tray_windows(pid: int) -> list[int]:
    found: list[int] = []

    @_EnumProc
    def _cb(hwnd, _lparam):
        owner = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            buf = ctypes.create_unicode_buffer(128)
            _u32.GetClassNameW(hwnd, buf, 128)
            if _TRAY_WINDOW_CLASS in buf.value:
                found.append(hwnd)
        return True

    _u32.EnumWindows(_cb, 0)
    return found


def tray_icon_registered() -> bool | None:
    """True — значок есть у Проводника; False — Qt его создал, а Проводник не
    знает; None — проверить нечем (не Windows или у процесса нет окна трея)."""
    if _u32 is None:
        return None
    hwnds = _tray_windows(os.getpid())
    if not hwnds:
        return None
    for hwnd in hwnds:
        for uid in range(4):  # Qt регистрирует с uID 0; запас — на смену версии
            nii = _NOTIFYICONIDENTIFIER(cbSize=ctypes.sizeof(_NOTIFYICONIDENTIFIER), hWnd=hwnd, uID=uid)
            rect = wintypes.RECT()
            if _shell32.Shell_NotifyIconGetRect(ctypes.byref(nii), ctypes.byref(rect)) == 0:
                return True
    return False
