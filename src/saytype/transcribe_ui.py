"""transcribe_ui.py — entry-point: hotkey + автопаст + интеграция с UI окном.

Архитектура:
- Тут (этот файл) — запись с микрофона, транскрипция через GPU, ротация
  истории, глобальный hotkey, точка входа QApplication.
- `transcribe_ui_window.py` — UI слой: QMainWindow + плеер + settings + tray.

Запуск: `pythonw.exe -m saytype` (без консольного окна).
"""

# === STDERR/STDOUT в UTF-8 + Crash logger ===
# pythonw.exe не имеет видимого stderr, поэтому unhandled exception молча убивает
# процесс. Чтобы после аварии остался след — пишем traceback в файл.
import sys

for _stream_name in ("stdout", "stderr"):
    _s = getattr(sys, _stream_name, None)
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Crash logger: любое необработанное исключение пишется в _crash.log в профиле
# пользователя (не рядом с кодом — установленный пакет может быть read-only).
from .profile import runtime_dir as _runtime_dir  # noqa: E402


def _crash_log_handler(exc_type, exc_value, exc_tb):
    try:
        import traceback as _tb
        from datetime import datetime as _dt
        from pathlib import Path as _P
        log_path = _runtime_dir() / "_crash.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n=== CRASH {_dt.now().isoformat()} ===\n")
            f.write(f"sys.executable: {sys.executable}\n")
            f.write(f"sys.argv: {sys.argv}\n")
            f.write(f"cwd: {_P.cwd()}\n")
            _tb.print_exception(exc_type, exc_value, exc_tb, file=f)
            f.write("\n")
    except Exception:
        pass

sys.excepthook = _crash_log_handler


# threading.excepthook отдельный — uncaught exceptions из worker threads
# (transcription, _stop_thread) НЕ идут через sys.excepthook.
def _thread_crash_log_handler(args):
    try:
        import traceback as _tb
        from datetime import datetime as _dt
        from pathlib import Path as _P
        log_path = _runtime_dir() / "_crash.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n=== THREAD CRASH {_dt.now().isoformat()} ===\n")
            f.write(f"thread: {args.thread.name if args.thread else 'unknown'}\n")
            _tb.print_exception(args.exc_type, args.exc_value, args.exc_traceback, file=f)
            f.write("\n")
    except Exception:
        pass


import threading as _threading_init
_threading_init.excepthook = _thread_crash_log_handler
del _threading_init


# Оба хука выше ловят только исключения Python. Аварии в нативном коде
# (0xC0000409 из CTranslate2 — T-268, OOM-убийца, BSOD, kill процесса) не дают
# ни трейсбека, ни строчки в лог: под pythonw окно просто исчезает. Этим
# занимается crashguard — маркер живой сессии в профиле пользователя. Сам старт
# вызывается ниже, ПОСЛЕ single-instance guard: вторая копия не должна ни
# перетирать маркер живой первой, ни оставлять свой после выхода.
from pathlib import Path as _P_init  # noqa: E402

from . import crashguard  # noqa: E402

# === SINGLE-INSTANCE CHECK ДО ИМПОРТОВ ===
# Тяжёлые импорты (PySide6 / faster-whisper / numpy) занимают 5-7 сек.
# Single-instance проверяем до них, чтобы вторая копия выходила мгновенно,
# а не ждала загрузки модулей с риском конфликта за микрофон / hotkey.
import ctypes
from ctypes import wintypes

if sys.platform == "win32":
    _k32 = ctypes.windll.kernel32
    _k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.CreateMutexW.restype = wintypes.HANDLE
    _k32.GetLastError.restype = wintypes.DWORD
    _SINGLE_INSTANCE_NAME = "faster-whisper-ui-singleton-2026-05"
    _SINGLE_INSTANCE_HANDLE = _k32.CreateMutexW(None, True, _SINGLE_INSTANCE_NAME)
    if _SINGLE_INSTANCE_HANDLE and _k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        # MessageBoxTimeoutW (undocumented, есть с XP) — auto-dismiss через 5 сек,
        # чтобы вторая копия не висела, если её никто не закроет
        try:
            ctypes.windll.user32.MessageBoxTimeoutW(
                0,
                "SayType уже запущен.\n\nОткрой окно через значок в трее "
                "(правый нижний угол, стрелка вверх, правый клик, «Открыть окно»).",
                "SayType",
                0x40,  # MB_ICONINFORMATION
                0,
                5000,
            )
        except Exception:
            pass  # fallback: silent exit
        sys.exit(0)
    # mutex держится через _SINGLE_INSTANCE_HANDLE до конца процесса (при exit ОС освободит)
else:
    _SINGLE_INSTANCE_HANDLE = None

crashguard.start(_runtime_dir())  # мы — единственная копия, маркер наш

# === SPLASH через tkinter (stdlib, быстрый импорт ~0.5с) ===
# PySide6 + faster-whisper грузятся 5-10 сек, и до появления окна непонятно,
# сработал ли ярлык вообще. Tkinter показывает заглушку мгновенно (после ~0.5 сек
# tk init); закрывается в main() прямо перед открытием основного окна.
_splash = None
try:
    import tkinter as _tk
    from pathlib import Path as _P
    _splash = _tk.Tk()
    _splash.title("SayType")
    _splash.geometry("420x140+%d+%d" % (
        (_splash.winfo_screenwidth() // 2) - 210,
        (_splash.winfo_screenheight() // 2) - 70,
    ))
    _splash.resizable(False, False)
    _splash.attributes("-topmost", True)
    # Заменяем дефолтную «перо»-иконку Tk на наш waveform .ico. Сгенерированный
    # появляется в профиле только после первого полного старта, поэтому на самом
    # первом запуске (и в собранной версии) берём эталон из поставки.
    from .profile import resource_dir as _resource_dir
    _ico = _P(_runtime_dir().parent) / "icons" / "saytype.ico"
    if not _ico.exists():
        _ico = _resource_dir() / "saytype.ico"
    if _ico.exists():
        try:
            _splash.iconbitmap(str(_ico))
        except Exception:
            pass
    _tk.Label(
        _splash,
        text="SayType",
        font=("Segoe UI", 14, "bold"),
        pady=18,
    ).pack()
    _tk.Label(
        _splash,
        text="Загрузка модели и интерфейса...\nЭто 5–10 секунд при первом старте",
        font=("Segoe UI", 10),
        fg="#666666",
        justify="center",
    ).pack()
    _splash.update_idletasks()
    _splash.update()
except Exception:
    _splash = None  # без splash, не критично

# === ТЯЖЁЛЫЕ ИМПОРТЫ ===
import json
import os
import re
import time
import threading
import wave
from collections import deque
from datetime import datetime
from pathlib import Path

# T-259: engine — единственная точка загрузки модели. Его импорт настраивает
# CUDA DLL-пути (Windows), поэтому идёт ДО всего, что тянет ctranslate2.
from . import audio_import  # T-351: декодер файла + контракт FileImportApi
from . import audio_quality  # T-418: полоса микрофона и разбор устройств записи
from . import cuda_layer
from . import engine
from . import profile
from . import sound
from . import updater

import numpy as np
import sounddevice as sd
import pyperclip
from PIL import Image, ImageDraw

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication

from .transcribe_ui_window import (
    MainWindow,
    create_autostart_shortcut,
    autostart_shortcut_exists,
    load_settings_dict,
    save_settings_dict,
)
from . import transcribe_ui_window as call_settings  # согласие на запись созвона
from .hotkeys import (
    MOD_CTRL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    to_mod_vk as hotkey_to_mod_vk,
)
from .transcribe_streaming import StreamingProcessor
from . import transcribe_call
from .transcribe_call import (
    CallRecorder,
    finalize_recording,
    transcribe_channel,
    find_orphaned_recordings,
    recover_recording_to_wav,
    cleanup_raw_log,
    RAW_META_SUFFIX,
    _wav_duration_sec,
    _resample_to_target,  # T-389: hi-fi запись → 16-кГц копия для Whisper
)

# Дай splash шанс перерисоваться после долгих импортов
if _splash is not None:
    try:
        _splash.update()
    except Exception:
        pass

# Win32 для foreground-окна (надёжный автопаст), RegisterHotKey, single-instance mutex
_user32 = ctypes.windll.user32 if sys.platform == "win32" else None
_kernel32 = ctypes.windll.kernel32 if sys.platform == "win32" else None
if _kernel32 is not None:
    _kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.GetLastError.restype = wintypes.DWORD
    _kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    _kernel32.ReleaseMutex.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
if _user32 is not None:
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _user32.AttachThreadInput.restype = wintypes.BOOL
    _user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p]
    _user32.keybd_event.restype = None

# Virtual key codes для native paste (Ctrl+V) — независимо от раскладки
_VK_CONTROL = 0x11
_VK_V = 0x56
_KEYEVENTF_KEYUP = 0x0002


def _force_foreground(target_hwnd: int) -> bool:
    """SetForegroundWindow с AttachThreadInput-обходом security restrictions.

    Windows ≥ 2000 запрещает background процессу менять foreground без
    специального хака: временно прикрепляем наш thread input к thread'у
    foreground-окна, тогда мы становимся 'foreground-related' и SetForegroundWindow
    срабатывает.
    """
    if _user32 is None or target_hwnd == 0:
        return False
    fg = _user32.GetForegroundWindow()
    if not fg:
        return bool(_user32.SetForegroundWindow(target_hwnd))
    fg_thread = _user32.GetWindowThreadProcessId(fg, None)
    our_thread = _kernel32.GetCurrentThreadId()
    if fg_thread == our_thread:
        return bool(_user32.SetForegroundWindow(target_hwnd))
    _user32.AttachThreadInput(fg_thread, our_thread, True)
    try:
        return bool(_user32.SetForegroundWindow(target_hwnd))
    finally:
        _user32.AttachThreadInput(fg_thread, our_thread, False)


def _native_paste() -> None:
    """Ctrl+V через keybd_event с virtual key codes. Не зависит от раскладки
    (на русской pyautogui.hotkey('ctrl','v') может посылать символ 'м' вместо V)."""
    if _user32 is None:
        return
    _user32.keybd_event(_VK_CONTROL, 0, 0, None)
    _user32.keybd_event(_VK_V, 0, 0, None)
    _user32.keybd_event(_VK_V, 0, _KEYEVENTF_KEYUP, None)
    _user32.keybd_event(_VK_CONTROL, 0, _KEYEVENTF_KEYUP, None)

# === Win32 RegisterHotKey: модификаторы и WM_HOTKEY ===
# Таблицы разбора hotkey-строки и константы MOD_* переехали в `hotkeys` (T-318):
# одно место правды для настроек, мастера первого запуска и регистрации.
WM_HOTKEY = 0x0312
HOTKEY_ID = 0xC001  # произвольный id регистрации (≥ 0xC000 рекомендуется для приложений)
HOTKEY_ID_CALL = 0xC002  # T-172: второй hotkey — запись созвона (ctrl+shift+E, захардкожен)


class _HotkeyEventFilter(QAbstractNativeEventFilter):
    """Ловит WM_HOTKEY из Windows message pump и вызывает callback (mgnovenно).

    Поскольку Windows кладёт WM_HOTKEY в очередь сообщений нашего hwnd, а Qt
    забирает её в main thread — никакого low-level хука system-wide. Если
    callback тормозит, тормозит только наш Qt event loop, а не вся клавиатура.
    """

    def __init__(self, hotkey_id: int, callback) -> None:
        super().__init__()
        self._id = hotkey_id
        self._cb = callback

    def nativeEventFilter(self, eventType, message):
        if eventType in (b"windows_generic_MSG", "windows_generic_MSG"):
            try:
                msg = wintypes.MSG.from_address(int(message))
            except Exception:
                return False, 0
            if msg.message == WM_HOTKEY and int(msg.wParam) == self._id:
                try:
                    self._cb()
                except Exception as exc:
                    log(f"hotkey callback fail: {exc}")
                return True, 0
        return False, 0

# === Дефолты записи/транскрипции (не настраиваются из UI) ===
SAMPLE_RATE = 16000
CHANNELS = 1
ICON_SIZE = 64
ICON_DIR = profile.icons_dir()   # генерируются кодом → в профиль, а не в пакет
# T-259: размер модели больше не константа — он в Settings (`model` / `custom_model`),
# а fallback-цепочка compute_type живёт в engine.GPU_COMPUTE_TYPES.
PASTE_DELAY_SEC = 0.15
BEAM_SIZE = 5
VAD_FILTER = True
TAIL_OVERLAP_SEC = 2.0  # T-132: tail захватывает overlap до last_confirmed_end_sec — спасает от пропусков на границах окон при нестабильном whisper. Дубли возможны (приемлемо).
# 2026-05-23 (T-159-followup, фаза 2): откат с 3.0 → 2.0. Увеличение до 3.0 дало случайное улучшение на 15 сек (tail_start = max(0, last_end-3) уходил в 0 и захватывал всё аудио, перекрывая head-проблему), НО регрессировал 60-сек прогоны: tail_audio становился 15 сек вместо 14, в combo с длинным INITIAL_PROMPT whisper ронял фрагменты в середине (см. docs/инциденты.md «2026-05-23 — регрессия в середине после фазы 1 фикса»).
HEAD_THRESHOLD_SEC = 0.2  # 2026-05-23: если first_confirmed_start_sec > этого — нужен head-merge (LCP начал confirm с середины, голова потеряна).
# Фаза 1 ставила 0.5 в расчёте «отсекать LCP подтвердивший сразу с начала», но stats после фазы 1 показал что first_start=0.0 практически всегда — head не запускался почти никогда; «фикс начала на 15 сек» обеспечивался побочным эффектом TAIL_OVERLAP=3.0, не head'ом. Снижаем до 0.2, чтобы head срабатывал даже на минимальный смещение и закрывал начало напрямую, без полагания на tail.
HEAD_OVERLAP_SEC = 1.5    # сколько секунд после first_confirmed_start захватить, чтобы head-transcribe «дотянулся» до точки confirm'а (защита от обрывов слов). На overlap'е возможен дубль с confirmed_text — приемлемо как у tail-overlap.
# T-164: pre-roll буфер — постоянно записываем последние N мс ДО hotkey-down,
# чтобы устранить head-loss (RealtimeSTT pattern, T-163 ресёрч R1). Включается
# чекбоксом в Settings (дефолт OFF). При включённом — pre_roll держит свой
# sd.InputStream постоянно и при старте записи snapshot этих 500мс
# дописывается ПЕРЕД свежим аудио. Whisper-encoder получает «прогрев»
# (тишина/фон до речи) → LA-2 не commit'ит нестабильный второй вариант.
PRE_ROLL_DURATION_SEC = 0.5
# Whisper-loop fix (2026-05-19, hotfix после wav 16-06-41 «по-русски, по-русски…»).
# При temperature=0.0 (single value) faster-whisper отключает temperature-fallback —
# decoding застревает в loop'ах. Передача кортежа активирует штатный fallback:
# если compression_ratio > 2.4 или log_prob < -1.0 — модель пробует следующую
# temperature. Это officially документированный anti-loop механизм.
TEMPERATURE_FALLBACK = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)

# === Anti-hallucination: словарь + замены (данные, а не константы) ===
# Языковой хинт ("говорит на русском") **не держим в промпте** (hotfix 2026-05-19):
# слово "русском" в prompt'е стало триггером для whisper-loop'а
# "по-русски, по-русски…" на тихих/коротких записях. Вместо него — явный
# `language="ru"` в m.transcribe(...): тот же хинт без риска попасть в loop.
#
# Словарь (`initial_prompt`) и замены в готовом тексте — **пользовательские
# данные**: из коробки пусто, наполняется в настройках и лежит в профиле
# (`profile.dictionary_path()` / `profile.replacements_path()`). До T-260 это были
# константы прямо здесь, и приложение нельзя было отдать никому другому.
#
# БЮДЖЕТ ПРОМПТА: у Whisper на initial_prompt всего 223 токена
# (max_length // 2 - 1), лишнее МОЛЧА отрезается С НАЧАЛА строки — первые слова
# словаря просто перестают работать. Один раз это уже стоило нам живучести
# артефакта, который словарь как раз и должен был лечить. Поэтому счётчик виден
# в настройках, а переполнение логируется при загрузке модели.
INITIAL_PROMPT: str = ""

# Скомпилированные правила замен: (обычные, хвостовые). Хвостовые анкерятся на
# КОНЕЦ текста и применимы только к диктовке — в транскрипте созвона реплика
# «Всем пока!» может быть настоящим прощанием, а сегменты короткие, так что
# правило стреляло бы по живой речи. transcribe_call получает post_process с
# tail_rules=False (2026-07-12).
_BODY_RULES: list = []
_TAIL_RULES: list = []


def reload_user_dictionary() -> None:
    """Перечитать словарь и замены из профиля. Зовётся при старте и после настроек."""
    global INITIAL_PROMPT, _BODY_RULES, _TAIL_RULES
    INITIAL_PROMPT = profile.load_dictionary()
    _BODY_RULES, _TAIL_RULES = profile.compile_rules()


def _merge_with_dedupe(left: str, right: str, max_overlap_words: int = 12) -> str:
    """Склеить left+right с удалением максимального суффикс-префикс перекрытия.

    Используется на стыках head-confirmed и confirmed-tail в streaming-режиме
    (T-159-followup фаза 3, 2026-05-23). До фазы 3 склейка через " ".join
    давала видимые дубли: head «Смотри, у меня сейчас» + confirmed «Смотри, у
    меня сейчас такое чувство» = повтор начала; tail «работу фигми и точно
    скажу» + similar в confirmed = дублированная фраза в середине.

    Алгоритм: ищем наибольшее N (1..max), такое что последние N слов left
    (case-insensitive, без хвостовой пунктуации) равны первым N словам right.
    Если найдено — обрезаем right на N слов спереди.

    NB: не идеален при искажениях whisper'а — если в head «Смотри, у меня
    сет...» а в confirmed «Смотри, у меня сейчас» — overlap не найдётся
    (последнее слово «сет» != «сейчас») и останутся оба. Это компромисс vs
    риском обрезать осмысленный текст.
    """
    if not left:
        return right
    if not right:
        return left

    def _norm(w: str) -> str:
        return w.lower().strip(".,!?;:—-\"'`«»()[]")

    left_words = left.split()
    right_words = right.split()
    upper = min(max_overlap_words, len(left_words), len(right_words))
    for overlap in range(upper, 0, -1):
        left_suffix = [_norm(w) for w in left_words[-overlap:]]
        right_prefix = [_norm(w) for w in right_words[:overlap]]
        if left_suffix and left_suffix == right_prefix:
            return " ".join(left_words + right_words[overlap:])
    return left + " " + right


def post_process(text: str, tail_rules: bool = True) -> str:
    """Применить пользовательские замены. Пустой список правил = текст как есть."""
    for regex, replacement in _BODY_RULES:
        try:
            text = regex.sub(replacement, text)
        except re.error:
            continue  # битая ссылка на группу в замене — правило пропускаем
    if tail_rules:
        for regex, replacement in _TAIL_RULES:
            try:
                text = regex.sub(replacement, text)
            except re.error:
                continue
    return text


# === Runtime state ===
SETTINGS: dict = {}  # заполняется в main() из QSettings
APP_HWND: int = 0  # hwnd главного окна — для RegisterHotKey
HOTKEY_FILTER: "_HotkeyEventFilter | None" = None  # установленный native event filter
CALL_HOTKEY_FILTER: "_HotkeyEventFilter | None" = None  # T-172: фильтр второго hotkey (созвон)
HOTKEY_REGISTERED: bool = False  # текущий hotkey зарегистрирован в Win32?
MODEL_DEVICE: str = ""  # "cuda" | "cpu" — реальное устройство после load_model()
MODEL_COMPUTE_TYPE: str = ""  # "int8_float16" | ... — реальный compute_type после load_model()

recording = False
busy = False
audio_buffer: list = []
audio_stream = None
state_lock = threading.Lock()
model = None
window: "MainWindow | None" = None
captured_hwnd: int = 0  # foreground-окно на момент начала записи (для автопаста обратно)
via_ui_request: bool = False  # True если последняя запись запущена через UI-кнопку (без автопаста)
note_dictation: bool = False  # T-352: True — текст уходит в открытую заметку, а не в историю/автопаст
streaming_processor: "StreamingProcessor | None" = None  # T-131: каркас фонового LA-2 worker'а
pre_roll: "PreRollBuffer | None" = None  # T-164: rolling deque pre-roll буфер
_pre_roll_used: bool = False  # True если последняя запись начата с pre-roll snapshot'ом

# T-172: запись созвона (второй режим). CallRecorder держит mic + WASAPI loopback
# всё время созвона; транскрипт делается НА СТОПЕ (модель свободна во время созвона).
call_recorder: "CallRecorder | None" = None
call_active: bool = False
call_dictation_start_idx: "int | None" = None  # курсор начала среза диктовки в mic-буфере CallRecorder
call_busy: bool = False  # финализация созвона идёт в worker'е — защита от двойного стопа

# T-351: идёт импорт аудиофайла (декодирование + транскрипция). Отдельный флаг, а
# не `busy`: тот принадлежит циклу записи с микрофона и сбрасывается `_stop_thread`.
import_busy: bool = False

# T-165: hybrid streaming/batch — snapshot режима на момент hotkey-down
# (изменение в Settings во время записи не применяется к текущей, только к следующей).
_active_processing_mode: str = "auto"  # "auto" | "always_batch" | "always_streaming"
_active_auto_threshold_sec: int = 10
# T-389: частота текущей записи. Snapshot на hotkey-down, как и режим обработки:
# SAMPLE_RATE (16000) в обычной диктовке, 44100/48000 — когда включена hi-fi.
_active_rate: int = SAMPLE_RATE
_active_hifi: bool = False  # True — запись этой сессии идёт в папку-карантин датасета
# T-284: поколение auto-таймера вместо ссылки на сам QTimer. Отменяет отсчёт
# worker-поток (`_stop_thread`), а QTimer живёт в GUI-потоке — трогать его
# оттуда нельзя: `stop()` Qt молча игнорирует («Timers cannot be stopped from
# another thread»), а снятие последней Python-ссылки разрушает C++-объект,
# оставляя диспетчер главного потока с висячим указателем → access violation
# внутри Qt6Core на следующем тике. Целое число безопасно из любого потока:
# отмена = инкремент, устаревший колбэк себя узнаёт и ничего не делает.
_auto_timer_generation: int = 0
_streaming_worker_started: bool = False  # True если в текущей записи реально стартанул LA-2 worker

# T-405: «Отменить» во время распознавания. Флаг ставит GUI-поток (кнопка в окне,
# пункт tray, кнопка в toast'е), читает worker — между сегментами и в ожидании
# загрузки модели в VRAM. Событие, а не bool: `wait(timeout)` даёт ожидание,
# которое прерывается сразу, без опроса в цикле со sleep.
_cancel_event = threading.Event()


class PreRollBuffer:
    """Постоянно работающий sd.InputStream, пишет последние duration_sec аудио
    в rolling deque. snapshot() возвращает текущий буфер как numpy float32.

    Решает head-loss (T-163 ресёрч M2+M6): даёт Whisper-encoder тишину/фон
    ДО реальной речи → LA-2 не commit'ит нестабильный второй вариант,
    cross-attention не переоценивает начало.

    Отдельный stream от recording-stream — на Windows WASAPI shared mode
    PortAudio даёт два параллельных потока на один микрофон без конфликта.
    """

    def __init__(self, duration_sec: float = PRE_ROLL_DURATION_SEC) -> None:
        self.duration_sec = duration_sec
        self._maxlen = int(duration_sec * SAMPLE_RATE)
        # deque(maxlen=N) автоматически выталкивает старые элементы при append
        self._deque: deque = deque(maxlen=self._maxlen)
        self._stream: "sd.InputStream | None" = None
        self._lock = threading.Lock()

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log(f"pre_roll sd status: {status}")
        # mono float32 1D — append каждый sample отдельно; deque(maxlen) ротирует
        with self._lock:
            self._deque.extend(indata[:, 0])

    def start(self) -> None:
        if self._stream is not None:
            return  # уже работает
        try:
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                device=mic_device(),  # T-263: выбранный микрофон или системный
                callback=self._callback,
            )
            stream.start()
            self._stream = stream
            log(f"pre_roll buffer started ({self.duration_sec * 1000:.0f}ms)")
        except Exception as exc:
            log(f"pre_roll start fail: {exc}")
            self._stream = None

    def stop(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as exc:
            log(f"pre_roll stop fail: {exc}")
        self._stream = None
        with self._lock:
            self._deque.clear()
        log("pre_roll buffer stopped")

    def is_running(self) -> bool:
        return self._stream is not None

    def snapshot(self) -> np.ndarray:
        """Текущий буфер как np.ndarray float32. Пустой → np.zeros(0)."""
        with self._lock:
            if not self._deque:
                return np.zeros(0, dtype=np.float32)
            return np.fromiter(self._deque, dtype=np.float32, count=len(self._deque))


def mic_device() -> "int | None":
    """Устройство записи для sounddevice: индекс выбранного или None (системное).

    В настройках хранится **имя**, а не индекс: индексы sounddevice меняются от
    подключения наушников и перезапуска службы звука, и вчерашняя «двойка»
    сегодня оказывается другим микрофоном. Имя не нашлось (устройство отключили)
    — молча возвращаемся к системному: остаться без записи хуже, чем записать
    не тем микрофоном, а строчка в логе объяснит, что произошло.
    """
    name = (SETTINGS.get("mic_device") or "").strip()
    if not name:
        return None
    try:
        idx = audio_quality.resolve_device_index(name)
    except Exception as exc:
        log(f"не смог перечислить устройства записи ({exc!r}) — беру системное")
        return None
    if idx is None:
        log(f"микрофон «{name}» не найден — беру системный")
    return idx


def current_mic_name() -> str:
    """С какого микрофона реально пишем — для лога, полосы и настроек (T-418).

    Выбранное имя показываем как есть; при «системном» спрашиваем Windows, что
    у неё сейчас системное. Разница между этими двумя ответами и была слепой
    зоной: настройка не менялась, а устройство за ней — да.
    """
    name = (SETTINGS.get("mic_device") or "").strip()
    if name:
        return name
    try:
        return audio_quality.default_input_name() or "системный"
    except Exception:
        return "системный"


def log(msg: str) -> None:
    """Безопасный лог в stderr. В pythonw на русской Windows stderr идёт через
    cp1251, который не покрывает многие unicode-символы. Если падает на encode —
    fallback на ascii с заменой непечатных. Никогда не должен убить процесс.
    """
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, file=sys.stderr)
    except (UnicodeEncodeError, OSError, AttributeError):
        try:
            safe = line.encode("ascii", errors="replace").decode("ascii")
            print(safe, file=sys.stderr)
        except Exception:
            pass  # тихо игнорируем — нельзя ронять процесс из-за лога


def _draw_waveform_app(draw, color, size, with_plate: bool = False):
    """App icon AppMark из icons.jsx (Claude Design).
    5 баров: [x=2,h=6], [x=6,h=12], [x=10,h=18], [x=14,h=10], [x=18,h=4] в viewBox 22x22.

    with_plate=True добавляет светлую плашку со скруглением (для .ico на рабочий
    стол — иначе waveform теряется на тёмных обоях). Без plate — для tray-иконок
    в трее (там плашка не нужна).
    """
    k = size / 22.0
    if with_plate:
        # Светлая плашка с тонкой границей — как у primary button «Записать»
        # Скругление 22% от size (Win11 app-tile стиль)
        radius = int(size * 0.22)
        draw.rounded_rectangle(
            (0, 0, size - 1, size - 1),
            radius=radius,
            fill=(255, 255, 255, 255),
            outline=(199, 199, 204, 255),
            width=max(1, int(size * 0.012)),
        )
    bars = [(2, 6), (6, 12), (10, 18), (14, 10), (18, 4)]
    bar_w = 2 * k
    for x_v, h_v in bars:
        x = x_v * k
        h = h_v * k
        y = (11 - h_v / 2) * k
        draw.rounded_rectangle(
            (x, y, x + bar_w, y + h),
            radius=max(1, int(k)),
            fill=color,
        )


def ensure_icons() -> dict:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    paths = {
        "idle": ICON_DIR / "idle.png",
        "recording": ICON_DIR / "recording.png",
        "processing": ICON_DIR / "processing.png",
        "app": ICON_DIR / "app.png",
        "app_ico": ICON_DIR / "saytype.ico",  # для ярлыка на рабочем столе
    }
    colors = {
        "idle": (140, 140, 140, 255),
        "recording": (220, 50, 50, 255),
        "processing": (240, 180, 40, 255),
    }
    for state in ("idle", "recording", "processing"):
        path = paths[state]
        if path.exists():
            continue
        img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((4, 4, ICON_SIZE - 4, ICON_SIZE - 4), fill=colors[state])
        img.save(path)
    # App-иконка для окна / taskbar — waveform без плашки (там фон есть)
    if not paths["app"].exists():
        img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
        _draw_waveform_app(ImageDraw.Draw(img), (31, 31, 35, 255), ICON_SIZE, with_plate=False)
        img.save(paths["app"])
    # Multi-resolution .ico для ярлыка на рабочем столе — С плашкой,
    # чтобы читалось на любых обоях (тёмные/светлые)
    if not paths["app_ico"].exists():
        big = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        _draw_waveform_app(ImageDraw.Draw(big), (31, 31, 35, 255), 256, with_plate=True)
        try:
            big.save(paths["app_ico"], format="ICO",
                     sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        except Exception:
            pass
    return paths


def _set_taskbar_app_id() -> None:
    """AppUserModelID — Windows связывает иконку с этим ID, чтобы в taskbar
    показывался наш waveform, а не дефолтная иконка pythonw.exe."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("saytype.app.1")
    except Exception:
        pass


def _sync_desktop_shortcut(ico_path: Path) -> None:
    """Обновить ярлык на рабочем столе:
    - Переименовать `faster-whisper UI.lnk` → `SayType.lnk` (если ещё старое имя)
    - Установить IconLocation на наш waveform .ico
    - Уведомить Windows об изменении (SHChangeNotify) — иначе Explorer кеширует
    """
    if sys.platform != "win32" or not ico_path.exists():
        return
    desktop = Path.home() / "Desktop"
    old_lnk = desktop / "faster-whisper UI.lnk"
    new_lnk = desktop / "SayType.lnk"
    target_lnk = new_lnk if new_lnk.exists() else (old_lnk if old_lnk.exists() else None)
    if target_lnk is None:
        return  # ярлыка нет — не создаём сами (это работа T-123)
    if target_lnk == old_lnk:
        try:
            target_lnk.rename(new_lnk)
            target_lnk = new_lnk
        except OSError as exc:
            log(f"rename shortcut fail: {exc}")
    # Перезаписываем ярлык целиком (Save() + удаление/recreate). Если просто
    # менять IconLocation на том же ярлыке — Explorer часто игнорирует и берёт
    # из icon-кеша. Чтобы Windows точно перечитал, удаляем и создаём заново
    # с теми же параметрами.
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$lnk = $ws.CreateShortcut('{target_lnk}'); "
        "$prevTarget = $lnk.TargetPath; "
        "$prevArgs = $lnk.Arguments; "
        "$prevWD = $lnk.WorkingDirectory; "
        "$prevWindowStyle = $lnk.WindowStyle; "
        f"Remove-Item '{target_lnk}' -Force -ErrorAction SilentlyContinue; "
        f"$lnk2 = $ws.CreateShortcut('{target_lnk}'); "
        "$lnk2.TargetPath = $prevTarget; "
        "$lnk2.Arguments = $prevArgs; "
        "$lnk2.WorkingDirectory = $prevWD; "
        "$lnk2.WindowStyle = $prevWindowStyle; "
        f"$lnk2.IconLocation = '{ico_path},0'; "
        "$lnk2.Save()"
    )
    try:
        import subprocess as _sp
        # CREATE_NO_WINDOW (0x08000000) — иначе PowerShell мелькает синим окном
        # на долю секунды между splash и главным окном
        _sp.run(
            ["powershell", "-NoProfile", "-Command", ps],
            check=True, capture_output=True, text=True, timeout=15,
            creationflags=0x08000000,
        )
        log(f"desktop shortcut updated: {target_lnk}")
    except Exception as exc:
        log(f"update shortcut icon fail: {exc}")
        return

    # Уведомить Windows Explorer об изменении ярлыка — иначе старая иконка
    # из icon-кеша останется ещё долго.
    try:
        SHCNE_UPDATEITEM = 0x00002000
        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNF_PATHW = 0x0005
        shell32 = ctypes.windll.shell32
        # обновить именно этот ярлык
        shell32.SHChangeNotify(
            SHCNE_UPDATEITEM, SHCNF_PATHW, ctypes.c_wchar_p(str(target_lnk)), None
        )
        # глобально перечитать icon-ассоциации
        shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, 0, None, None)
    except Exception as exc:
        log(f"SHChangeNotify fail: {exc}")


def _check_prompt_budget(m) -> None:
    """Бюджет initial_prompt у Whisper — 223 токена (max_length//2 - 1), лишнее
    молча отрезается С НАЧАЛА строки (первые слова словаря перестают работать).
    Ловим переполнение один раз при загрузке модели, а не молча на записях
    (2026-07-12: промпт дорос до 240 токенов, и начало словаря выпало)."""
    if not INITIAL_PROMPT:
        return  # словарь пуст — нечего проверять
    budget = profile.PROMPT_TOKEN_BUDGET
    try:
        ids = m.hf_tokenizer.encode(INITIAL_PROMPT, add_special_tokens=False).ids
        if len(ids) > budget:
            log(
                f"[WARN] словарь = {len(ids)} токенов > бюджета {budget} — "
                f"whisper ОТРЕЖЕТ НАЧАЛО промпта, первые слова словаря не работают! Сократите."
            )
        else:
            log(f"словарь: {len(ids)}/{budget} токенов")
    except Exception as exc:
        log(f"prompt budget check skip: {exc}")


def active_model_spec() -> str:
    """Что грузить по текущим Settings: пресет / HF repo id / путь к папке (T-259)."""
    return engine.spec_from_settings(SETTINGS)


def no_model_reason() -> str:
    """Почему диктовать нечем, либо "" — модель на месте (T-405).

    Один ответ на два вопроса: что писать в полосе главного окна и чем
    отвечать на хоткей. Пока проверки не было, нажатие по хоткею на пустом
    кэше запускало запись, и человек узнавал правду уже наговорив — в конце.
    """
    spec = active_model_spec()
    try:
        if engine.is_loaded(spec) or engine.is_cached(spec):
            return ""
    except Exception:
        return ""
    return f"Модель {engine.spec_display(spec)} не скачана — распознавать нечем."


def report_error(title: str, text: str, hint: str = "") -> None:
    """Показать сбой человеку (T-404) — окно + toast, а не только `_crash.log`.

    Зовётся из worker-потоков: у окна для этого есть thread-safe `notify_error`.
    Если окна нет вообще (batch-режим, ранний старт) — остаётся лог, но тогда
    и смотреть некому.
    """
    log(f"[ERROR] {title}: {text}" + (f" | {hint}" if hint else ""))
    if window is None:
        return
    try:
        window.notify_error(title, text, hint)
    except Exception as exc:
        log(f"notify_error fail: {exc}")


def load_model(spec: "str | None" = None, progress_cb=None, force_reload: bool = False,
               allow_download: bool = True, should_cancel=None):
    """Обёртка над engine.load_model — кэш модели, double-checked locking и
    fallback-цепочка compute_type живут в engine (T-259, одна точка на проект).

    `progress_cb(done_bytes, total_bytes)` пробрасывается в скачивание модели.

    `allow_download=False` — «грузи только то, что уже на диске». Ставится во
    всех путях, где показать прогресс негде: диктовка, созвон, импорт файла,
    preload. T-404: без этого стоп записи уходил качать 1,6 ГБ внутри своего
    потока — молча, без шкалы и без отмены, и на мёртвом маршруте выглядел как
    «транскрибирую» до конца сессии. Качаем там, где есть окно с прогрессом:
    раздел «Модели» и старт приложения.
    """
    global model, MODEL_DEVICE, MODEL_COMPUTE_TYPE
    spec = spec or active_model_spec()
    if not allow_download and not engine.is_loaded(spec) and not engine.is_cached(spec):
        raise engine.ModelDownloadError(
            f"Модель {engine.spec_display(spec)} не скачана — распознавать нечем.",
            hint="Открой раздел «Модели» и нажми «Скачать»: там видно прогресс "
                 "и понятно, чем закончилось.",
        )
    m = engine.load_model(spec, logger=log, progress_cb=progress_cb, force_reload=force_reload,
                          should_cancel=should_cancel)
    if m is not model:
        _check_prompt_budget(m)
    model = m
    MODEL_DEVICE = engine.current_device()
    MODEL_COMPUTE_TYPE = engine.current_compute_type()
    # T-278: на чём реально считаем — попадёт в отчёт об аварии рядом с действием.
    crashguard.set_context(
        model=f"{engine.spec_display(spec)} ({MODEL_DEVICE}/{MODEL_COMPUTE_TYPE})"
    )
    return m


def load_model_background(spec: "str | None" = None, reason: str = "") -> None:
    """Поднять модель в фоне так, чтобы сбой не потерялся (T-404).

    Цель для `threading.Thread(target=...)`: раньше туда отдавали сам
    `load_model`, и любое исключение (нет весов, нет сети, не собрался CUDA)
    улетало в `threading.excepthook` → `_crash.log`. Пользователь при этом
    видел прежнее состояние UI и ничего не знал.
    """
    try:
        load_model(spec, allow_download=False)
    except Exception as exc:
        report_error(
            "Модель не загрузилась",
            f"{exc}" + (f"\n\n({reason})" if reason else ""),
            getattr(exc, "hint", ""),
        )


def switch_model(spec: str, progress_cb=None, should_cancel=None):
    """Смена модели на лету: выгрузить старую из VRAM → загрузить новую.

    Глобальную ссылку `model` обнуляем ДО engine.unload_model() — чтобы никто
    не начал транскрибировать по выгруженной модели. Само освобождение VRAM от
    наших ссылок больше не зависит: engine отдаёт память явным CT2-unload и
    объект не разрушает (T-268 — разрушение после sampling убивало процесс).
    """
    global model
    crashguard.mark(f"смена модели → {engine.spec_display(spec)}")
    model = None
    engine.unload_model(logger=log)
    m = load_model(spec, progress_cb=progress_cb, force_reload=True, should_cancel=should_cancel)
    crashguard.mark(f"модель сменена на {engine.spec_display(spec)}")
    return m


class _TranscriptionCancelled(Exception):
    """Распознавание прервал сам пользователь (T-405) — не сбой, окна с ошибкой нет."""


def cancel_transcription() -> bool:
    """Прервать идущее распознавание диктовки. False — прерывать нечего.

    Зовётся из GUI-потока: кнопка в окне, пункт tray-меню, кнопка в toast'е.
    Само прерывание делает worker — между сегментами и в ожидании модели.
    """
    if not busy:
        return False
    if not _cancel_event.is_set():
        _cancel_event.set()
        log("отмена распознавания запрошена")
        crashguard.mark("отмена распознавания")
    if window is not None:
        try:
            window.notify_cancelling()
        except Exception as exc:
            log(f"notify_cancelling fail: {exc}")
    return True


def transcription_cancelling() -> bool:
    """Идёт ли уже остановка (для UI: кнопку второй раз нажимать незачем)."""
    return bool(busy and _cancel_event.is_set())


def _load_model_cancellable():
    """Загрузить модель так, чтобы «Отмена» работала и на прогреве CUDA.

    `WhisperModel(...)` — блокирующий вызов внутри CTranslate2: прервать его
    нечем, а разрушать наполовину поднятую модель нельзя (T-268, процесс умрёт
    мимо `try/except`). Поэтому грузим в отдельном потоке и ждём его с оглядкой
    на флаг отмены: по нажатию мы просто перестаём ЖДАТЬ. Поток догружает
    модель до конца и кладёт её в кэш engine — следующая диктовка стартует уже
    прогретой, VRAM не течёт, ничего не разрушено.

    Первый прогон после старта — те самые ~18 секунд, ради которых это и
    сделано: без этого отмена доходила бы до worker'а только после прогрева.
    """
    box: dict = {}

    def _work() -> None:
        try:
            box["model"] = load_model(allow_download=False)
        except BaseException as exc:  # noqa: BLE001 — отдаём вызывающему как есть
            box["error"] = exc

    thread = threading.Thread(target=_work, daemon=True, name="model-load-for-dictation")
    thread.start()
    while thread.is_alive():
        thread.join(0.2)
        if thread.is_alive() and _cancel_event.is_set():
            log("отмена на этапе загрузки модели — догрузка продолжится в фоне")
            raise _TranscriptionCancelled()
    if "error" in box:
        raise box["error"]
    return box.get("model")


def _collect_segments(segments) -> str:
    """Текст из ленивого генератора faster-whisper, с проверкой отмены.

    Модель считает не на вызове `transcribe()`, а на каждой итерации — поэтому
    выход из цикла и есть остановка работы (тот же приём, что в импорте файла,
    T-351: иначе длинную запись нечем было бы прервать).
    """
    parts: list[str] = []
    for seg in segments:
        if _cancel_event.is_set():
            raise _TranscriptionCancelled()
        text = (seg.text or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def set_state(state: str) -> None:
    """Thread-safe смена UI state. Внутри MainWindow эмитится Qt-сигнал,
    который доставляется в main thread (Qt widgets — main-thread-only)."""
    if window is not None:
        try:
            window.notify_set_state(state)
        except Exception as exc:
            log(f"set_state fail: {exc}")


def history_dir() -> Path:
    return Path(SETTINGS.get("history_dir", str(profile.default_history_dir())))


def rotation_count() -> int:
    return int(SETTINGS.get("rotation_count", 5))


def rotation_minutes() -> float:
    # T-386: опциональный порог по суммарной длительности (датасет клона голоса).
    # 0 (дефолт) = выключено, ротация только по rotation_count, как раньше.
    return float(SETTINGS.get("rotation_minutes", 0))


def hifi_enabled() -> bool:
    # T-389: hi-fi надиктовка — материал для клона голоса пишется на 44.1/48 кГц
    # в отдельную папку, минуя ротацию истории. Whisper при этом получает
    # 16-кГц копию, распознавание не меняется.
    return bool(SETTINGS.get("hifi_enabled", False))


def hifi_rate() -> int:
    rate = int(SETTINGS.get("hifi_sample_rate", 44100))
    return rate if rate in (44100, 48000) else 44100


def hifi_dir() -> Path:
    """Папка-карантин hi-fi записей. `rotate_history()` сканит только корень
    истории и сюда не заглядывает — накопленное не удаляется само."""
    return history_dir() / profile.HIFI_SUBDIR


def call_audio_keep() -> int:
    # T-175: размер буфера WAV созвонов (Settings → ротация папки Calls). Минимум 1.
    return max(1, int(SETTINGS.get("call_audio_keep", 2)))


_last_level_ms: int = 0


def _start_streaming_worker_now() -> bool:
    """T-165: запуск StreamingProcessor задним числом — используется как из
    start_recording (always_streaming-режим), так и из _on_auto_threshold_fire
    (auto-режим, при достижении порога). Возвращает True если worker стартанул.

    Не reentrant: если worker уже создан или модель не загружена — no-op.
    """
    global streaming_processor, _streaming_worker_started
    if streaming_processor is not None:
        return False
    if model is None:
        log("streaming worker skipped: model not loaded yet (preload still in progress)")
        return False
    try:
        streaming_processor = StreamingProcessor(
            model=model,
            get_audio_snapshot=lambda: (
                np.concatenate(list(audio_buffer), axis=0)
                if audio_buffer
                else np.zeros(0, dtype=np.float32)
            ),
            sample_rate=SAMPLE_RATE,
            initial_prompt=INITIAL_PROMPT or None,
            log=log,
            # debug_log_path=Path(__file__).parent / "_streaming_debug.log",
            # ↑ раскомментировать для дампа всего worker-лога в файл (для отладки
            # из pythonw, когда стдерр невидим). Параметр оставлен в API
            # StreamingProcessor как hook на будущее (T-133 замеры / weekly ritual).
        )
        streaming_processor.start()
        _streaming_worker_started = True
        # T-278: с этого момента модель работает на GPU параллельно записи —
        # смерть здесь и смерть в простое выглядят в логе по-разному.
        crashguard.mark("идёт запись диктовки, streaming-worker считает на GPU")
        return True
    except Exception as exc:
        log(f"streaming start fail: {exc}")
        streaming_processor = None
        return False


def _on_auto_threshold_fire(generation: int) -> None:
    """T-165 auto-режим: QTimer выстрелил — порог пересечён, время поднимать
    streaming-worker. Если запись успела остановиться раньше или worker уже
    стартанул (защита от двойного срабатывания) — no-op.

    Worker'у в качестве initial-buffer ничего отдельно передавать не нужно:
    `get_audio_snapshot` — лямбда, возвращает свежий `np.concatenate(audio_buffer)`,
    при первом tick'е worker подберёт всё накопленное за порог.

    T-284: `generation` — номер записи, под который таймер армили. Стоп записи
    его инкрементирует, поэтому таймер от прошлой (уже остановленной) записи
    молча отваливается здесь, а не через `QTimer.stop()` из чужого потока.
    """
    if generation != _auto_timer_generation:
        return  # запись, под которую армили таймер, давно остановлена
    if not recording:
        log(
            f"auto-threshold fire ignored: recording already stopped "
            f"(threshold={_active_auto_threshold_sec}s)"
        )
        return
    if streaming_processor is not None:
        return  # уже стартанул как-то иначе (теоретически не должно случиться)
    log(
        f"auto-threshold {_active_auto_threshold_sec}s reached — "
        f"streaming worker starting retroactively on accumulated audio"
    )
    _start_streaming_worker_now()


def start_recording() -> None:
    global recording, audio_buffer, audio_stream, _last_level_ms, _pre_roll_used
    global _active_processing_mode, _active_auto_threshold_sec, _streaming_worker_started
    global _auto_timer_generation, _active_rate, _active_hifi
    audio_buffer = []
    recording = True
    _last_level_ms = 0
    _pre_roll_used = False
    _streaming_worker_started = False

    # T-165: snapshot processing-режима НА МОМЕНТ hotkey-down. Изменения в
    # Settings во время записи не применяются к текущей — иначе будет race
    # с QTimer'ом и пограничные стартовавшие worker'ы зависнут как сироты.
    _active_processing_mode = SETTINGS.get("processing_mode", "auto")
    if _active_processing_mode not in ("auto", "always_batch", "always_streaming"):
        _active_processing_mode = "auto"
    _active_auto_threshold_sec = int(SETTINGS.get("auto_threshold_sec", 10))
    _active_auto_threshold_sec = max(1, min(60, _active_auto_threshold_sec))

    # T-389: частота записи — тем же snapshot'ом, что и режим. Hi-fi форсит
    # always_batch: streaming-worker режет чанки, считая всю цепочку 16-кГц
    # (`transcribe_streaming.py`), и второй sample rate там пришлось бы
    # протаскивать сквозь LCP-логику ради режима, который включают на время
    # набора датасета. На стопе отработает обычный full-pass.
    _active_hifi = hifi_enabled()
    _active_rate = hifi_rate() if _active_hifi else SAMPLE_RATE
    if _active_hifi and _active_processing_mode != "always_batch":
        log(
            f"hi-fi: processing_mode {_active_processing_mode} → always_batch "
            f"на эту запись (streaming-путь работает только на {SAMPLE_RATE} Гц)"
        )
        _active_processing_mode = "always_batch"

    # T-164: pre-roll — дописываем последние ~500мс из rolling deque ПЕРЕД
    # свежим аудио. Whisper-encoder получает «прогрев» (тишина/фон до речи),
    # LA-2 не commit'ит нестабильный второй вариант на первых токенах.
    #
    # T-389: при hi-fi snapshot НЕ приклеиваем. PreRollBuffer держит свой поток
    # на 16 кГц (его не трогаем — он про head-loss, а не про датасет), и эти
    # 500 мс в 44.1-кГц буфере растянулись бы в полторы секунды замедленного
    # звука — и в файле датасета, и в том, что услышит Whisper.
    if _active_hifi and pre_roll is not None and pre_roll.is_running():
        log("hi-fi: pre-roll snapshot пропущен (буфер 16 кГц, запись на другой частоте)")
    elif pre_roll is not None and pre_roll.is_running():
        snap = pre_roll.snapshot()
        if snap.size > 0:
            audio_buffer.append(snap)
            _pre_roll_used = True
            log(
                f"pre_roll prepended: {snap.size / SAMPLE_RATE * 1000:.0f}ms "
                f"({snap.size} samples)"
            )

    # Foreground захватываем НЕ здесь, а в toggle_recording при втором нажатии
    # (момент финиша) — как у Handy. Пользователь может за время записи переключиться
    # в другое окно и кликнуть в чат именно тогда когда заканчивает мысль.

    def cb(indata, frames, time_info, status):
        global _last_level_ms
        if status:
            log(f"sd status: {status}")
        # mono float32 1D — streaming worker'у нужен 1D (faster_whisper VAD и
        # transcribe ждут shape=(N,), не (N, 1)). Для wav-записи в OFF-режиме
        # tobytes() даёт идентичные байты — регрессия OFF не нарушается.
        audio_buffer.append(indata[:, 0].copy())
        # Эквалайзер — отправляем RMS в overlay (throttle 20Hz чтобы не перегружать Qt)
        if window is not None:
            now_ms = time.monotonic_ns() // 1_000_000
            if now_ms - _last_level_ms >= 50:
                _last_level_ms = now_ms
                try:
                    level = float(np.sqrt(np.mean(indata**2)))  # RMS, 0..~1
                    window.notify_audio_level(level)
                except Exception:
                    pass  # эквалайзер не должен ронять запись

    def _open_stream(rate: int):
        stream = sd.InputStream(
            samplerate=rate, channels=CHANNELS, dtype="float32",
            device=mic_device(),  # T-263: выбранный микрофон или системный
            callback=cb,
        )
        stream.start()
        return stream

    try:
        audio_stream = _open_stream(_active_rate)
    except Exception as exc:
        # T-389: микрофон может не отдать выбранную частоту (драйвер, занятое
        # устройство). Датасет — дело наживное, а вот потерянная надиктовка
        # невосполнима: откатываемся на 16 кГц и пишем как обычно.
        if not _active_hifi:
            raise
        log(f"hi-fi: {_active_rate} Гц не открылись ({exc!r}) — пишу обычную диктовку 16 кГц")
        _active_hifi = False
        _active_rate = SAMPLE_RATE
        audio_stream = _open_stream(_active_rate)
    set_state("recording")  # overlay покажет «● Запись» + эквалайзер
    log(f"recording started ({_active_rate} Hz{', hi-fi' if _active_hifi else ''})")
    if SETTINGS.get("sound_notifications_dictation", True):
        sound.play_start()  # T-355: main thread, QSoundEffect уже прогрет — не задерживает старт
    # T-278: раньше маркер молчал от hotkey-down до стопа — десятки секунд, в
    # которые смерть выглядела как смерть в простое. Ветки ниже могут уточнить
    # запись («streaming-worker считает на GPU»).
    crashguard.mark(f"идёт запись диктовки (режим {_active_processing_mode})")

    # T-165: 3-ветка по processing_mode (snapshot на hotkey-down):
    # - always_batch: streaming-worker не запускается совсем (full-pass на стопе)
    # - always_streaming: запускаем сразу — точно так же как ON-чекбокс раньше
    # - auto: QTimer на _active_auto_threshold_sec; по срабатыванию (если запись ещё идёт)
    #   стартуем worker задним числом — get_audio_snapshot подберёт уже накопленные сэмплы.
    #
    # Граничный кейс elapsed == threshold (например ровно 10.0с): singleShot(threshold * 1000)
    # выстрелит ПОСЛЕ threshold секунд; если hotkey-up попал ровно на 10.0с — таймер ещё не
    # сработал → streaming_processor остаётся None → fallback на full-pass → batch_short.
    # Это совпадает с acceptance «elapsed <= threshold → batch_short».
    if _active_processing_mode == "always_streaming":
        _start_streaming_worker_now()
    elif _active_processing_mode == "auto":
        # T-284: одноразовый QTimer.singleShot вместо хранимого QTimer. Отменять
        # его не нужно (и нечем — объект принадлежит Qt и живёт в GUI-потоке):
        # стоп записи инкрементирует поколение, и колбэк отваливается сам.
        _auto_timer_generation += 1
        _generation = _auto_timer_generation
        QTimer.singleShot(
            _active_auto_threshold_sec * 1000,
            lambda: _on_auto_threshold_fire(_generation),
        )
        log(
            f"auto-mode armed: streaming worker fires at +{_active_auto_threshold_sec}s "
            f"if recording still active"
        )
    # else always_batch — ничего не делаем, worker не появится в этой записи


def stop_recording_and_transcribe() -> None:
    """Worker-поток (`_stop_thread`). Qt-объекты отсюда трогать НЕЛЬЗЯ —
    только через `window.notify_*` (Qt-сигналы с queued-доставкой). См. T-284."""
    global recording, audio_buffer, audio_stream, streaming_processor
    global _auto_timer_generation
    stream, buf = audio_stream, audio_buffer
    audio_stream, audio_buffer = None, []
    recording = False
    if SETTINGS.get("sound_notifications_dictation", True) and window is not None:
        # T-355: QSoundEffect — main-thread-only, отсюда (worker-поток) только
        # через сигнал, как и остальные Qt-обращения в этой функции (см. докстринг).
        window.notify_stop_sound()
    # T-165: отменить ARMED auto-timer ДО закрытия потока — иначе после стопа
    # timer ещё может выстрелить и стартануть worker на уже пустой audio_buffer.
    # T-284: отмена = инкремент поколения. Сам QTimer не трогаем: мы в чужом
    # потоке, а он принадлежит GUI-потоку (раньше здесь были stop() + снятие
    # последней ссылки — это и роняло процесс в Qt6Core через секунды).
    _auto_timer_generation += 1
    try:
        stream.stop()
        stream.close()
    except Exception as exc:
        log(f"stream close fail: {exc}")
    # Глушим streaming worker до начала транскрипции. Worker должен join'нуться
    # раньше чем модель начнёт работу, чтобы не было параллельных вызовов
    # m.transcribe (faster-whisper не reentrant на одной WhisperModel).
    streaming_result: "tuple[str, float, int] | None" = None
    if streaming_processor is not None:
        try:
            streaming_result = streaming_processor.stop()
        except Exception as exc:
            log(f"streaming stop fail: {exc}")
            streaming_result = None
        streaming_processor = None
    set_state("processing")  # жёлтая иконка + кнопка disabled
    log("recording stopped, transcribing")

    if not buf:
        log("empty buffer — nothing recorded")
        crashguard.mark("простой (пустая запись)")
        return

    audio_np = np.concatenate(buf, axis=0)
    # T-389: длительность и путь к модели считаются от частоты ЭТОЙ записи —
    # в hi-fi буфер приходит 44.1/48 кГц, и деление на 16000 завысило бы её втрое.
    dur_sec = len(audio_np) / _active_rate
    crashguard.mark(f"транскрипция диктовки {dur_sec:.0f} сек")

    # T-389: при hi-fi оригинал на native rate уходит в датасет ОТДЕЛЬНЫМ файлом,
    # а всё остальное (история, карточки в окне, ротация, транскрипция) работает
    # с привычной 16-кГц версией. Иначе включённый режим уносил бы надиктовки из
    # окна целиком — ровно это и вылезло на живом прогоне.
    hifi_pcm16 = None
    if _active_hifi:
        hifi_pcm16 = (np.clip(audio_np, -1.0, 1.0) * 32767).astype(np.int16)
        # Тот же ресемплер, что сводит канал собеседника в созвонах.
        audio_np = _resample_to_target(hifi_pcm16, _active_rate, SAMPLE_RATE).astype(np.float32) / 32767.0

    hdir = history_dir()
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")

    # T-352: диктовка в заметку не пишется в ротируемую историю вообще (ни wav,
    # ни txt/meta ниже) — заметка не «последние N, потом сотрётся», а обратное.
    if not note_dictation:
        hdir.mkdir(parents=True, exist_ok=True)
        wav_path = hdir / f"{ts}.wav"
        txt_path = hdir / f"{ts}.txt"

        pcm16 = (np.clip(audio_np, -1.0, 1.0) * 32767).astype(np.int16)
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm16.tobytes())
        log(f"saved {wav_path.name} ({dur_sec:.1f}s)")

        if hifi_pcm16 is not None:
            # Копия для клона голоса: своя папка вне ротации (`rotate_history()`
            # сканирует только корень). Сбой записи датасета не должен уносить
            # уже сохранённую надиктовку — отсюда отдельный try.
            try:
                hdir_hifi = hifi_dir()
                hdir_hifi.mkdir(parents=True, exist_ok=True)
                hifi_wav = hdir_hifi / f"{ts}.wav"
                with wave.open(str(hifi_wav), "wb") as wf:
                    wf.setnchannels(CHANNELS)
                    wf.setsampwidth(2)
                    wf.setframerate(_active_rate)
                    wf.writeframes(hifi_pcm16.tobytes())
                log(f"saved hi-fi {hifi_wav.name} ({dur_sec:.1f}s, {_active_rate} Hz)")
            except Exception as exc:
                log(f"hi-fi save fail: {exc}")
            # T-418: узкополосный источник (мик вебки, Bluetooth-гарнитура в
            # режиме Hands-Free) открывается на 48 кГц без ошибки и отдаёт
            # передискретизованные 16. Настройки при этом честно показывают
            # «hi-fi 48 000 Гц» — отличить можно только по самому сигналу.
            _report_hifi_band(hifi_pcm16, _active_rate)

    t0 = time.time()
    # T-404: модель грузим только из кэша. Скачивание отсюда запрещено —
    # это поток стопа записи, в нём нет ни шкалы, ни отмены, а на плохом
    # маршруте оно висит минутами и выглядит как зависшая транскрипция.
    try:
        # T-405: ждём модель через отдельный поток — иначе прогрев CUDA (~18 сек
        # на первом прогоне) держал бы кнопку «Отменить» бесполезной.
        m = _load_model_cancellable()
    except _TranscriptionCancelled:
        raise
    except Exception as exc:
        hint = getattr(exc, "hint", "")
        where = f"\n\nЗапись сохранена: {wav_path}" if not note_dictation else ""
        report_error(
            "Не удалось распознать запись",
            f"{exc}{where}",
            hint,
        )
        return
    info_lang = "ru"

    # T-132: развилка по streaming. Tail-merge только если worker накопил
    # непустой confirmed_text — иначе fallback на полный full-pass (точно как
    # OFF-режим), чтобы короткие записи (≤ window_sec) и кейсы с пустым
    # confirmed (worker не успел LCP-стабилизировать) не теряли качество.
    use_tail_merge = streaming_result is not None and bool(streaming_result[0])
    # T-133: замер времени tail `m.transcribe` (только в streaming-режиме с tail-merge).
    # Записывается в _stats.jsonl как tail_elapsed_sec. В fallback / OFF — остаётся 0.0.
    tail_elapsed_sec: float = 0.0
    head_elapsed_sec: float = 0.0
    if use_tail_merge:
        (
            confirmed_text,
            last_confirmed_end_sec,
            chunks_processed,
            first_confirmed_start_sec,
        ) = streaming_result
        # 2026-05-23 head-merge (T-159-followup): LCP может начать confirm только
        # с середины записи (на коротких 15-сек записях 3/3 теряли начало,
        # 1/3 потерял ~18% chars). Если first_confirmed_start_sec > HEAD_THRESHOLD_SEC —
        # transcribe audio[0..first_start + HEAD_OVERLAP_SEC] и приклеить ПЕРЕД
        # confirmed_text. На overlap'е возможен дубль (как в tail) — приемлемо.
        head_raw = ""
        if (
            first_confirmed_start_sec is not None
            and first_confirmed_start_sec > HEAD_THRESHOLD_SEC
        ):
            head_end_sec = first_confirmed_start_sec + HEAD_OVERLAP_SEC
            head_end_sample = min(int(head_end_sec * SAMPLE_RATE), audio_np.size)
            head_audio = (
                audio_np[:head_end_sample]
                if head_end_sample > 0
                else np.zeros(0, dtype=audio_np.dtype)
            )
            if head_audio.size > 0:
                _head_t0 = time.time()
                head_segments, _head_info = m.transcribe(
                    head_audio,
                    beam_size=1,
                    vad_filter=False,
                    language="ru",
                    initial_prompt=INITIAL_PROMPT or None,  # head — начало записи, prev confirmed нет
                    condition_on_previous_text=False,
                    # T-165 followup 2026-05-25: temperature fallback против whisper-loop
                    # «вот, вот, вот…». В head-merge до этого фикса temperature=0 (default),
                    # на коротком head-audio это иногда срывалось в loop. Логика та же
                    # что в full-pass ветке (см. TEMPERATURE_FALLBACK на module-level).
                    temperature=TEMPERATURE_FALLBACK,
                )
                head_raw = _collect_segments(head_segments)
                # 2026-05-23 фаза 3: whisper на head_audio оборванном на границе
                # фонетического слова часто ставит «...» — последнее слово ломаное
                # («сет...» вместо «сейчас»). Обрезаем чтобы dedupe-склейка нашла
                # overlap с confirmed_text по целым словам. Без этого v3.2 кейс
                # «Смотри, у меня сет... | Смотри, у меня сейчас такое чувство»
                # давал двойной старт.
                if head_raw.endswith("...") or head_raw.endswith("…"):
                    _head_words = head_raw.rstrip(".… \t").split()
                    if _head_words:
                        head_raw = " ".join(_head_words[:-1])
                head_elapsed_sec = time.time() - _head_t0
                log(
                    f"streaming head-merge: first_start={first_confirmed_start_sec:.2f}s, "
                    f"head_end={head_end_sec:.2f}s, head_chars={len(head_raw)}, "
                    f"head_elapsed={head_elapsed_sec:.2f}s"
                )

        # T-132: overlap 2 сек чтобы захватить слова, которые
        # whisper нестабильно распознал между окнами и LCP их не сматчила.
        # Возможны лёгкие дубли на границе — осознанный trade-off.
        # 2026-05-23 (followup): пробовали 3.0 — регрессировало 60 сек (см. константу).
        tail_start_sec = max(0.0, last_confirmed_end_sec - TAIL_OVERLAP_SEC)
        tail_start_sample = max(0, int(tail_start_sec * SAMPLE_RATE))
        tail_audio = (
            audio_np[tail_start_sample:]
            if tail_start_sample < audio_np.size
            else np.zeros(0, dtype=audio_np.dtype)
        )
        tail_dur = tail_audio.size / SAMPLE_RATE if tail_audio.size else 0.0
        log(
            f"streaming tail-merge: confirmed={len(confirmed_text)} chars, "
            f"chunks={chunks_processed}, last_end={last_confirmed_end_sec:.2f}s, "
            f"tail_start={tail_start_sec:.2f}s, tail={tail_dur:.1f}s"
        )
        if tail_audio.size > 0:
            tail_prompt = (INITIAL_PROMPT + " " + confirmed_text[-150:]).strip() or None
            _tail_t0 = time.time()
            segments, info = m.transcribe(
                tail_audio,
                beam_size=1,
                vad_filter=False,
                language="ru",
                initial_prompt=tail_prompt,
                condition_on_previous_text=False,
                # T-165 followup 2026-05-25: temperature fallback против whisper-loop.
                # 2026-05-25T17-28-31.txt: «вот, вот, вот, вот, вот, вот…» в tail —
                # на нестабильном tail-audio с temperature=0 модель залипает на
                # одном токене. Подача кортежа активирует штатный fallback
                # (compression_ratio > 2.4 → следующая temperature).
                temperature=TEMPERATURE_FALLBACK,
            )
            tail_raw = _collect_segments(segments)
            tail_elapsed_sec = time.time() - _tail_t0
            info_lang = info.language
        else:
            tail_raw = ""
        # Склейка с dedupe на стыках (фаза 3, 2026-05-23): head_audio имеет
        # overlap с confirmed_text на ~HEAD_OVERLAP_SEC, tail_audio — на
        # ~TAIL_OVERLAP_SEC. До фазы 3 раскинутая " ".join давала видимые
        # дубли «Смотри, у меня сейчас… Смотри, у меня сейчас…» в начале
        # и «работу фигми и точно скажу… работу фигми и точно скажу…» в
        # середине. _merge_with_dedupe ищет longest matching suffix-prefix
        # и обрезает right.
        raw = _merge_with_dedupe(head_raw, confirmed_text.strip())
        raw = _merge_with_dedupe(raw, tail_raw)
        raw = raw.strip()
    else:
        if streaming_result is not None:
            log(
                f"streaming fallback to full-pass "
                f"(chunks={streaming_result[2]}, confirmed empty — короткая запись или silence)"
            )
        # Массив, а не путь к файлу. Путь заставляет faster-whisper декодировать
        # wav через PyAV, а тот тянет за собой libx264/libx265 — кодеки под
        # GPLv2+, которые в поставке распространили бы GPL на всё приложение.
        # Данные те же: `audio_np` — исходный float32 16 kHz mono, из которого
        # только что записан этот самый wav. Остальные три вызова так и работали.
        segments, info = m.transcribe(
            audio_np,
            beam_size=BEAM_SIZE,
            vad_filter=VAD_FILTER,
            initial_prompt=INITIAL_PROMPT or None,
            temperature=TEMPERATURE_FALLBACK,
            condition_on_previous_text=False,  # не подавать loop как контекст следующему сегменту
            language="ru",  # явный язык вместо хинта в initial_prompt (см. INITIAL_PROMPT)
        )
        raw = _collect_segments(segments)
        info_lang = info.language

    text = post_process(raw)
    elapsed = time.time() - t0
    ratio = dur_sec / elapsed if elapsed > 0 else 0
    log(f"transcribed: {len(text)} chars, {elapsed:.1f}s, {ratio:.1f}x, lang={info_lang}")

    if note_dictation:
        # T-352: ни файла, ни ротации, ни автопаста — текст уходит прямо в
        # редактор открытой заметки. `_stop_thread.finally` сбросит флаг.
        crashguard.mark("после транскрипции (заметка): доставка текста в редактор")
        if window is not None:
            window.notify_note_text(text)
        return

    # T-284: раньше здесь стояло «простой (транскрипция завершена)», хотя дальше
    # идёт ещё полдюжины шагов — авария на любом из них читалась в `_crash.log`
    # как смерть в простое. Отметки ниже разрезают этот участок: следующая
    # авария сразу называет шаг.
    crashguard.mark("после транскрипции: сохраняю txt/meta/stats")

    txt_path.write_text(text, encoding="utf-8")
    if _active_hifi:
        # Текст рядом с hi-fi копией: при сборке датасета видно, что на записи
        # сказано, без обращения к истории (её ротация к тому времени уже сотрёт).
        try:
            (hifi_dir() / f"{ts}.txt").write_text(text, encoding="utf-8")
        except Exception as exc:
            log(f"hi-fi txt save fail: {exc}")

    # Sidecar meta — длительность аудио, время транскрипции, скорость x realtime.
    # Статистика видна в таблице истории, а не всплывающим тостом.
    meta_path = hdir / f"{ts}.meta.json"
    try:
        meta = {
            "duration_sec": round(float(dur_sec), 2),
            "elapsed_sec": round(float(elapsed), 2),
            "ratio_x": round(float(ratio), 1),
            "chars": len(text),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        log(f"meta save fail: {exc}")

    # _stats.jsonl — неротируемый журнал всех транскрипций (для тренда скорости работы
    # модели). В отличие от .meta.json (sidecar к wav, ротируется вместе) — кладётся
    # одной строкой в общий файл, который остаётся даже когда wav/txt/meta удаляются
    # по rotation_count. Append однострочного JSON < 4КБ — атомарен и потокобезопасен
    # на Windows; в этом приложении писатель в любом случае один (state_lock+busy).
    stats_path = hdir / "_stats.jsonl"
    try:
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "duration_sec": round(float(dur_sec), 2),
            "elapsed_sec": round(float(elapsed), 2),
            "ratio_x": round(float(ratio), 1),
            "chars": len(text),
            "device": f"{MODEL_DEVICE}/{MODEL_COMPUTE_TYPE}" if MODEL_DEVICE else "unknown",
            "model_size": engine.current_label() or active_model_spec(),
            "beam_size": BEAM_SIZE,
            "vad": VAD_FILTER,
        }
        # T-133: режим транскрипции для последующего анализа «streaming vs full» по
        # категориям длительности. mode=streaming когда worker реально стартовал
        # (streaming_result is not None) — даже если потом fallback на full-pass из-за
        # пустого confirmed_text. chunks/tail_elapsed дают разрезать «worker
        # отработал и сократил время» vs «worker отработал но всё ушло в full-pass».
        # T-164: pre_roll_used — true если pre-roll snapshot был приклеен
        # в начало audio_buffer (включён в Settings И deque был непустой).
        rec["pre_roll_used"] = bool(_pre_roll_used)
        # T-165: processing_mode для последующего анализа hybrid-режима.
        # 4 значения: batch_short (auto, ≤порог) / streaming_long (auto, >порог) /
        # batch_forced (always_batch) / streaming_forced (always_streaming).
        # Решение по snapshot'у режима на hotkey-down + флагу _streaming_worker_started.
        if _active_processing_mode == "always_batch":
            rec["processing_mode"] = "batch_forced"
        elif _active_processing_mode == "always_streaming":
            rec["processing_mode"] = "streaming_forced"
        elif _streaming_worker_started:
            rec["processing_mode"] = "streaming_long"
        else:
            rec["processing_mode"] = "batch_short"
        rec["auto_threshold_sec"] = int(_active_auto_threshold_sec)
        # T-389: частота записи. Журнал общий для обоих режимов (он про скорость
        # модели, а она работает на 16 кГц в любом случае) — но по этому полю
        # видно, какие прогоны шли в датасет.
        rec["sample_rate"] = int(_active_rate)
        rec["hifi"] = bool(_active_hifi)
        if streaming_result is not None:
            rec["mode"] = "streaming"
            rec["streaming_chunks"] = int(streaming_result[2])
            rec["tail_elapsed_sec"] = round(float(tail_elapsed_sec), 2)
            # 2026-05-23 (T-159-followup): head-merge диагностика — first_confirmed_start_sec
            # позволит на следующих ritual'ах автоматически ловить «head-потеря» (если
            # на коротких записях значение > 0.5s, голова терялась без head-merge).
            # head_elapsed_sec = 0.0 если head-merge не понадобился (LCP сразу с начала
            # или first_start is None) или короткой записи.
            first_start = streaming_result[3]
            rec["first_confirmed_start_sec"] = (
                round(float(first_start), 2) if first_start is not None else None
            )
            rec["head_elapsed_sec"] = round(float(head_elapsed_sec), 2)
        else:
            rec["mode"] = "full"
        with open(stats_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:
        log(f"stats append fail: {exc}")

    crashguard.mark("после транскрипции: ротация истории")
    rotate_history()

    if not text:
        log("empty transcription")
        if window is not None:
            crashguard.mark("после транскрипции: обновляю окно (пустой текст)")
            window.notify_transcription_done()
        return

    crashguard.mark("после транскрипции: буфер обмена + автопаст")
    pyperclip.copy(text)
    # запись через UI-кнопку — автопаст пропускаем (фокус на нашем окне, вставится в text_view)
    if not (via_ui_request or captured_hwnd == 0):
        try:
            # AttachThreadInput-trick — пробивает Windows security restrictions
            # на SetForegroundWindow из background-процесса
            _force_foreground(captured_hwnd)
            time.sleep(PASTE_DELAY_SEC)
            # Native keybd_event с VK_CONTROL/VK_V — не зависит от раскладки
            _native_paste()
        except Exception as exc:
            log(f"paste fail: {exc}")

    if window is not None:
        crashguard.mark("после транскрипции: обновляю окно (карточки истории)")
        window.notify_transcription_done()


def rotate_history() -> None:
    """Ротация надиктовок в корне папки истории.

    По умолчанию — по количеству (`rotation_count`), как раньше. Если задан
    `rotation_minutes` (T-386, > 0) — буфер держит и по суммарной длительности:
    держим новые записи, пока накопленная длительность (от самой свежей) не
    превысит порог; `rotation_count` при этом работает как пол (не меньше N
    файлов в любом случае).
    """
    hdir = history_dir()
    max_count = rotation_count()
    keep_minutes = rotation_minutes()
    wavs = sorted(hdir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
    if keep_minutes > 0:
        newest_first = list(reversed(wavs))
        keep_set = set()
        total_sec = 0.0
        for i, p in enumerate(newest_first):
            if i < max_count or total_sec < keep_minutes * 60:
                keep_set.add(p)
                total_sec += _wav_duration_sec(p)
            else:
                break
        to_remove = [p for p in wavs if p not in keep_set]
    else:
        to_remove = wavs[: max(0, len(wavs) - max_count)]
    for oldest in to_remove:
        meta = oldest.parent / f"{oldest.stem}.meta.json"
        for p in (oldest, oldest.with_suffix(".txt"), meta):
            if p.exists():
                try:
                    p.unlink()
                except OSError as exc:
                    log(f"rotate fail {p}: {exc}")


def _transcribe_failure_hint(exc: BaseException) -> str:
    """Подсказка под конкретный сбой транскрипции (T-404)."""
    text = str(exc)
    if "Invalid input features shape" in text:
        # Ровно то, что даёт модель, скачанная без `preprocessor_config.json`:
        # веса на диске, модель грузится, а каждая транскрипция падает.
        return ("Похоже, модель скачана не полностью: без preprocessor_config.json "
                "faster-whisper считает, что у модели 80 мел-каналов вместо 128. "
                "Открой раздел «Модели» и нажми «Скачать» — недостающие файлы "
                "докачаются, уже скачанное не пропадёт.")
    return "Запись сохранена в истории — можно попробовать ещё раз после разбора причины."


def _stop_thread() -> None:
    global busy, via_ui_request, note_dictation
    was_note = note_dictation
    try:
        stop_recording_and_transcribe()
    except _TranscriptionCancelled:
        # T-405: нажали «Отменить». Запись при этом уже лежит на диске (wav
        # пишется ДО транскрипции) — сообщаем это, а не «не удалось распознать».
        log("распознавание отменено пользователем")
        crashguard.mark("распознавание отменено")
        if window is not None:
            try:
                window.notify_cancelled(
                    "Распознавание отменено"
                    + ("" if was_note else " — запись осталась в истории")
                )
            except Exception as exc:
                log(f"notify_cancelled fail: {exc}")
    except Exception as exc:
        # T-404: раньше любая авария после стопа записи уходила только в
        # `_crash.log` — иконка возвращалась в серую, и это выглядело как «текст
        # куда-то делся сам». Причина обязана дойти до человека.
        report_error(
            "Не удалось распознать запись",
            f"{exc.__class__.__name__}: {exc}",
            getattr(exc, "hint", "") or _transcribe_failure_hint(exc),
        )
    finally:
        _cancel_event.clear()  # T-405: флаг живёт ровно один прогон
        set_state("idle")  # гарантия возврата к серой иконке независимо от исходов
        busy = False
        via_ui_request = False
        note_dictation = False
        # T-352 (доработка после ревью): happy path уже дёрнул notify_note_text
        # из stop_recording_and_transcribe — но пустой буфер (`if not buf: return`)
        # и любое исключение до записи файлов туда не доходят, и кнопка «Диктовать»
        # в NoteEditorDialog залипала на «Распознаю…» навсегда. `finally` — гарантированная
        # точка после ЛЮБОГО исхода, поэтому уведомление шлём отсюда, а не с happy path.
        # На успехе сигнал придёт вторым: `_on_note_text_ready` уже очистит
        # `_note_dictation_target`, и этот — no-op (см. MainWindow._on_note_dictation_ended).
        if was_note and window is not None:
            window.notify_note_dictation_ended()
        # T-284: «простой» ставим ЗДЕСЬ — когда worker действительно закончил.
        # Раньше отметка стояла сразу после m.transcribe, и всё, что шло дальше
        # (сохранение, ротация, автопаст, обновление окна), выглядело простоем.
        crashguard.mark("простой (транскрипция завершена)")


def _report_hifi_band(pcm16: "np.ndarray", rate: int) -> None:
    """Померить полосу сохранённой hi-fi записи и записать вывод в лог.

    Проверка стоит после сохранения, а не до: испортить надиктовку из-за сбоя в
    измерении нельзя, а сам вывод нужен всё равно постфактум. Тишина и слишком
    короткая запись вывода не дают — тревога на них была бы ложной.

    В окно вывод больше не идёт: полоса hi-fi из главного окна убрана перед
    выпуском 0.3.0. Замер остаётся в логе — по нему видно, чем писалась
    конкретная запись, — а спросить про микрофон можно кнопкой «Проверить» в
    настройках.
    """
    try:
        check = audio_quality.check_samples(pcm16.astype(np.float32) / 32767.0, rate)
    except Exception as exc:
        log(f"полоса hi-fi не измерена: {exc!r}")
        return
    if not check.measured:
        return
    mic = current_mic_name()
    text = audio_quality.describe(check, hifi=True)
    log(f"hi-fi полоса ({mic}): провал ВЧ {check.drop_db:.0f} дБ — {text}")


def toggle_recording(via_ui: bool = False, to_note: bool = False) -> None:
    """Старт/стоп записи. via_ui=True для записи через UI-кнопку (без автопаста).
    to_note=True (T-352) — диктовка в открытую заметку: без автопаста, без
    записи в ротируемую историю, результат уходит в редактор заметки."""
    global busy, via_ui_request, note_dictation, captured_hwnd
    # T-172: во время записи созвона CallRecorder держит mic-поток. Обычная
    # диктовка НЕ может открыть свой sd.InputStream на том же устройстве (два
    # MME-потока конфликтуют), поэтому диктуем СРЕЗОМ из mic-буфера CallRecorder.
    # Этот путь полностью отдельный — не трогает audio_stream/recording/streaming.
    # Диктовка в заметку поверх активного созвона не поддержана (редкий edge-case
    # вне acceptance T-352) — уйдёт по обычной ветке созвона, to_note игнорируется.
    if call_active:
        _toggle_call_dictation(via_ui=via_ui)
        return
    with state_lock:
        if busy:
            return
        if not recording:
            # T-405: отказ ДО записи, а не после. Раньше пустой кэш моделей
            # обнаруживался в конце — когда человек уже наговорил.
            reason = no_model_reason()
            if reason:
                report_error(
                    "Модель не скачана",
                    reason,
                    "Открой раздел «Модели» (кнопка с чипом в шапке окна) и нажми "
                    "«Скачать» — там видно прогресс и понятно, чем закончилось.",
                )
                return
            via_ui_request = via_ui or to_note
            note_dictation = to_note
            captured_hwnd = 0
            start_recording()
        else:
            # === Capture foreground именно НА МОМЕНТ СТОПА (как Handy). ===
            # Пользователь может за время записи переключаться (смотреть картинки,
            # листать), и кликает в чат именно когда заканчивает мысль —
            # автопаст должен пойти туда, куда он кликнул последним, а не туда
            # откуда начал. Если via_ui — фокус на нашем окне, paste бессмысленен.
            if _user32 is not None and not via_ui_request:
                try:
                    captured_hwnd = _user32.GetForegroundWindow()
                    log(f"foreground captured at stop: hwnd={captured_hwnd}")
                except Exception as exc:
                    log(f"capture fg at stop fail: {exc}")
                    captured_hwnd = 0
            else:
                captured_hwnd = 0
            busy = True
            threading.Thread(target=_stop_thread, daemon=True).start()


def _on_hotkey_activated() -> None:
    """Callback из Win32 WM_HOTKEY через Qt nativeEventFilter. Main thread.

    Должен быть БЫСТРЫМ — нельзя блокировать Qt event loop. Вся тяжёлая работа
    (transcription) уходит в отдельный thread через _stop_thread.
    """
    toggle_recording(via_ui=False)


def register_hotkey(hotkey_str: str) -> bool:
    """Регистрирует глобальный hotkey через Win32 RegisterHotKey (layout-independent).

    hotkey_str — в pynput-style формате '<ctrl>+<shift>+q'. Конвертируется в
    (modifiers, virtual_key) для Win32 API. Возвращает True если успех.
    """
    global HOTKEY_REGISTERED
    if _user32 is None or APP_HWND == 0:
        log("register_hotkey: win32 unavailable or hwnd=0")
        return False
    mods, vk = hotkey_to_mod_vk(hotkey_str)
    if mods == 0 or vk == 0:
        log(f"register_hotkey: cannot parse '{hotkey_str}' -> mods={mods} vk={vk}")
        return False
    # MOD_NOREPEAT — игнорируем auto-repeat пока клавиша удерживается
    ok = _user32.RegisterHotKey(APP_HWND, HOTKEY_ID, mods | MOD_NOREPEAT, vk)
    if ok:
        HOTKEY_REGISTERED = True
        log(f"hotkey registered (Win32): {hotkey_str} -> mods=0x{mods:04x} vk=0x{vk:02x}")
        return True
    err = ctypes.get_last_error() if _kernel32 else 0
    log(f"RegisterHotKey FAIL for '{hotkey_str}' (err={err})")
    return False


def unregister_hotkey() -> None:
    global HOTKEY_REGISTERED
    if not HOTKEY_REGISTERED or _user32 is None or APP_HWND == 0:
        return
    try:
        _user32.UnregisterHotKey(APP_HWND, HOTKEY_ID)
    except Exception as exc:
        log(f"unregister_hotkey fail: {exc}")
    HOTKEY_REGISTERED = False


def reload_hotkey(new_hotkey: str) -> None:
    """Снять текущий hotkey и зарегистрировать новый. При ошибке — восстановить старый."""
    old = SETTINGS.get("hotkey")
    unregister_hotkey()
    if register_hotkey(new_hotkey):
        SETTINGS["hotkey"] = new_hotkey
    else:
        log(f"register new hotkey '{new_hotkey}' fail -> rollback to {old}")
        if old and old != new_hotkey:
            register_hotkey(old)
            SETTINGS["hotkey"] = old


def trigger_recording_via_ui() -> None:
    """Вызывается из UI-кнопки записи. Без автопаста (фокус на нашем окне)."""
    toggle_recording(via_ui=True)


def trigger_note_recording_via_ui() -> None:
    """T-352: вызывается кнопкой диктовки внутри открытой заметки."""
    toggle_recording(via_ui=True, to_note=True)


# === T-172: запись созвона (второй режим) ===

def _call_silence_alert(silent: bool) -> None:
    """Watchdog CallRecorder'а: loopback тих / возобновился. Прокидываем в UI
    через thread-safe Qt-сигнал (вызывается из watchdog-потока, не main)."""
    if window is not None:
        try:
            window.notify_call_alert(silent)
        except Exception as exc:
            log(f"call alert notify fail: {exc}")


def _call_finalize_thread(recording_obj) -> None:
    """Worker: тяжёлый finalize_recording (WAV→MP3→2 прохода транскрипта).

    Запускается ПОСЛЕ стопа созвона. Модель в этот момент свободна (транскрипт
    созвона делается на стопе, не вживую). UI трогаем только через сигналы окна.
    """
    global call_busy
    # T-173 C: индикатор обработки. Сигнал шлём ДО тяжёлого load_model()/транскрипта,
    # чтобы «идёт обработка» было видно сразу на стопе (раньше тут была «тишина» 5-6 мин).
    total_sec = 0.0
    try:
        total_sec = float(getattr(recording_obj, "duration_sec", 0.0) or 0.0)
    except Exception:
        total_sec = 0.0
    _dur = f"{total_sec:.0f} сек" if total_sec < 60 else f"{total_sec / 60:.0f} мин"
    crashguard.mark(f"финализация созвона ({_dur}): WAV + два прохода транскрипта")
    if window is not None:
        try:
            window.notify_call_processing(True, total_sec)
        except Exception:
            pass

    def _progress(frac: float) -> None:
        if window is not None:
            try:
                window.notify_call_progress(float(frac))
            except Exception:
                pass

    try:
        # T-174: транскрипт → подпапка Calls папки истории (вне ротации
        # надиктовок), аудио по умолчанию не храним; MP3 — только если включено
        # «хранить аудио созвона» в Settings.
        result = finalize_recording(
            recording_obj,
            # T-404: только из кэша — качать 1,6 ГБ внутри финализации созвона
            # нельзя, там на руках несохранённый транскрипт.
            model=load_model(allow_download=False),
            keep_audio=SETTINGS.get("keep_call_audio", False),
            call_audio_keep=call_audio_keep(),
            progress_cb=_progress,
            speaker_l=SETTINGS.get("speaker_self"),
            speaker_r=SETTINGS.get("speaker_other"),
            # 2026-07-12: словарь и замены не доходили до созвонов вообще —
            # артефакты, вылеченные в диктовке, жили в .md созвонов.
            # tail_rules=False: $-правила диктовки в сегментах созвона стреляли
            # бы по живой речи («Всем пока!» может быть настоящим прощанием).
            initial_prompt=INITIAL_PROMPT or None,
            postproc=lambda t: post_process(t, tail_rules=False),
            # 2026-07-28: явно отдаём защищённый от кодировки логгер UI
            # (дефолтный печатал в stderr «как есть» и уронил транскрипт).
            logger=log,
        )
        if window is not None:
            if result.get("transcribe_error"):
                wav = result.get("wav_path")
                window.notify_call_transcript(
                    f"Транскрипт созвона не удался:\n{result['transcribe_error']}\n\n"
                    f"WAV сохранён для повторной попытки:\n{wav}\n\n"
                    f"Дотранскрибировать позже можно по этому WAV-файлу."
                )
            else:
                md_path = result.get("md_path")
                try:
                    body = md_path.read_text(encoding="utf-8") if md_path else "(нет .md)"
                except Exception as exc:
                    body = f"(не смог прочитать {md_path}: {exc})"
                window.notify_call_transcript(body)
    except Exception as exc:
        log(f"call finalize fail: {exc!r}")
        if window is not None:
            window.notify_call_transcript(f"Финализация созвона упала: {exc!r}")
    finally:
        call_busy = False
        crashguard.mark("простой (созвон обработан)")
        if window is not None:
            try:
                window.notify_call_processing(False)
            except Exception:
                pass
            try:
                window.notify_call_state(False)
            except Exception:
                pass


def _confirm_call_consent() -> bool:
    """Показать предупреждение о согласии перед самой первой записью.

    True — можно писать (уже подтверждали раньше либо подтвердили сейчас).
    Вызывается из GUI-потока: и hotkey (через native event filter), и кнопка в
    окне приходят сюда в главном потоке.
    """
    if call_settings.call_consent_acknowledged():
        return True
    if window is None:
        # Окна нет — молча не блокируем запись, но и согласия не записываем:
        # спросим при следующем запуске, когда интерфейс будет.
        return True
    from PySide6.QtWidgets import QMessageBox

    box = QMessageBox(window)
    box.setIcon(QMessageBox.Information)
    box.setWindowTitle("Запись разговора")
    box.setText("Предупредите собеседника о записи.")
    box.setInformativeText(
        "В части стран и штатов запись разговора без предупреждения второй "
        "стороны незаконна. Приложение не знает, где вы находитесь, и не может "
        "решить это за вас.\n\n"
        "Показывается один раз."
    )
    box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
    box.button(QMessageBox.Ok).setText("Понятно, записывать")
    box.button(QMessageBox.Cancel).setText("Отмена")
    box.setDefaultButton(QMessageBox.Ok)
    if box.exec() != QMessageBox.Ok:
        return False
    call_settings.set_call_consent_acknowledged(True)
    return True


def toggle_call_recording() -> None:
    """Старт/стоп записи созвона (hotkey ctrl+shift+E или UI-кнопка)."""
    global call_recorder, call_active, call_busy, call_dictation_start_idx
    if call_busy:
        log("call toggle ignored: финализация предыдущего созвона ещё идёт")
        return
    if not call_active:
        # T-314: один раз перед первой записью показываем то же предупреждение,
        # что и в README. Запись разговора без предупреждения собеседника в
        # части стран и штатов незаконна, а приложение не знает, где находится
        # пользователь. Спрашиваем один раз, а не каждый раз: вопрос на каждом
        # созвоне перестают читать со второго.
        if not _confirm_call_consent():
            log("call recording: отменено на предупреждении о согласии")
            return
        try:
            call_recorder = CallRecorder(on_silence_alert=_call_silence_alert)
            call_recorder.start()
        except Exception as exc:
            log(f"[ERR] старт записи созвона не удался: {exc!r}")
            call_recorder = None
            if window is not None:
                window.notify_call_transcript(
                    f"Не удалось начать запись созвона:\n{exc!r}\n\n"
                    f"Частая причина: микрофон занят (закрой другое приложение, "
                    f"использующее микрофон) или нет WASAPI loopback-устройства."
                )
            return
        call_active = True
        call_dictation_start_idx = None
        log("call recording: STARTED (ctrl+shift+E to stop)")
        if SETTINGS.get("sound_notifications_call", True):
            sound.play_start()
        # T-278: созвон длится десятки минут — без отметки вся эта дыра
        # читалась в _crash.log как «простой».
        crashguard.mark("идёт запись созвона")
        if window is not None:
            window.notify_call_state(True)
    else:
        # Стоп: останавливаем рекордер синхронно (быстро), финализацию — в worker.
        call_active = False
        call_dictation_start_idx = None
        call_busy = True
        rec = call_recorder
        call_recorder = None
        log("call recording: STOPPING, финализирую в фоне…")
        if SETTINGS.get("sound_notifications_call", True):
            sound.play_stop()
        try:
            recording_obj = rec.stop() if rec is not None else None
        except Exception as exc:
            log(f"[ERR] stop записи созвона: {exc!r}")
            call_busy = False
            if window is not None:
                window.notify_call_state(False)
                window.notify_call_transcript(f"Стоп записи созвона упал: {exc!r}")
            return
        if recording_obj is None:
            call_busy = False
            if window is not None:
                window.notify_call_state(False)
            return
        threading.Thread(
            target=_call_finalize_thread, args=(recording_obj,), daemon=True,
            name="call-finalize",
        ).start()


def _do_call_dictation_thread(start_idx: int, captured: int, via_ui: bool) -> None:
    """Worker: срез mic-буфера CallRecorder'а → transcribe_channel → автопаст.

    Используется ТОЛЬКО во время созвона (call_active). Не трогает audio_stream/
    streaming — отдельный путь, переиспользует ту же модель и тот же native-paste,
    что обычный стоп диктовки.
    """
    global busy, via_ui_request, captured_hwnd
    try:
        if call_recorder is None:
            log("call dictation: рекордер исчез до среза — пропуск")
            return
        snap = call_recorder.mic_snapshot()
        clip = snap[start_idx:] if start_idx < snap.size else np.zeros(0, dtype=np.int16)
        if clip.size == 0:
            log("call dictation: пустой срез — нечего вставлять")
            return
        # 2026-07-12: INITIAL_PROMPT сюда тоже не передавался — диктовка во время
        # созвона шла без словаря, в отличие от обычной диктовки.
        try:
            m_call = load_model(allow_download=False)  # T-404: качать посреди созвона нельзя
        except Exception as exc:
            report_error("Диктовка в созвоне не распознана", str(exc), getattr(exc, "hint", ""))
            return
        segs = transcribe_channel(m_call, clip, initial_prompt=INITIAL_PROMPT)
        raw = " ".join(t for (_s, _e, t) in segs).strip()
        text = post_process(raw)
        log(f"call dictation: {clip.size / SAMPLE_RATE:.1f}s -> {len(text)} chars")
        if not text:
            return
        pyperclip.copy(text)
        # Автопаст тем же механизмом, что и обычный стоп диктовки (см.
        # stop_recording_and_transcribe): _force_foreground(captured_hwnd) + native Ctrl+V.
        if not (via_ui or captured == 0):
            try:
                _force_foreground(captured)
                time.sleep(PASTE_DELAY_SEC)
                _native_paste()
            except Exception as exc:
                log(f"call dictation paste fail: {exc}")
    finally:
        set_state("idle")
        busy = False
        via_ui_request = False


def _toggle_call_dictation(via_ui: bool = False) -> None:
    """Старт/стоп диктовки ВО ВРЕМЯ созвона (срез mic-буфера CallRecorder'а).

    Первое нажатие — запоминаем курсор в mic-буфере, показываем «диктую».
    Второе — берём срез [start:] и транскрибируем в worker'е с автопастом.
    """
    global busy, via_ui_request, captured_hwnd, call_dictation_start_idx
    with state_lock:
        if busy:
            return
        if call_recorder is None:
            return
        if call_dictation_start_idx is None:
            call_dictation_start_idx = call_recorder.mic_sample_count()
            via_ui_request = via_ui
            captured_hwnd = 0
            set_state("recording")  # overlay «● Запись» (без эквалайзера — у нас нет своего callback'а)
            log(f"call dictation: START at mic_idx={call_dictation_start_idx}")
            if SETTINGS.get("sound_notifications_dictation", True):
                sound.play_start()  # T-355: тот же hotkey Q, та же проблема с незамеченным стартом
        else:
            # Захват foreground на момент стопа (как обычная диктовка)
            captured = 0
            if _user32 is not None and not via_ui_request:
                try:
                    captured = _user32.GetForegroundWindow()
                except Exception as exc:
                    log(f"call dictation capture fg fail: {exc}")
                    captured = 0
            start_idx = call_dictation_start_idx
            call_dictation_start_idx = None
            busy = True
            if SETTINGS.get("sound_notifications_dictation", True):
                sound.play_stop()
            set_state("processing")
            threading.Thread(
                target=_do_call_dictation_thread,
                args=(start_idx, captured, via_ui_request),
                daemon=True, name="call-dictation",
            ).start()


def _on_call_hotkey() -> None:
    """Callback ctrl+shift+E (WM_HOTKEY через Qt nativeEventFilter, main thread)."""
    toggle_call_recording()


def trigger_call_via_ui() -> None:
    """Вызывается из UI-кнопки записи созвона."""
    toggle_call_recording()


# === T-351: импорт готового аудиофайла ===
# Декодирование живёт в audio_import (GUI-поток, Qt), транскрипция — здесь, в
# worker'е окна. Модель берём через load_model() — тот же engine, что у диктовки
# и созвона, второго WhisperModel в проекте нет (T-259).

# Пауза между сегментами, с которой начинается новый абзац. Длинный файл одной
# простынёй нечитаем, а сегменты whisper'а — это фразы, не абзацы.
IMPORT_PARAGRAPH_GAP_SEC = 1.5


def begin_file_import() -> bool:
    """Занять движок под импорт. False — занят записью, созвоном или другим импортом.

    Вызывается из GUI-потока ДО декодирования: смена или выгрузка модели во время
    импорта — это разрушение WhisperModel после генерации, то есть `0xC0000409`
    (T-268). Пока флаг стоит, `model_busy()` держит раздел «Модели» закрытым.
    """
    global import_busy
    with state_lock:
        if model_busy():
            return False
        import_busy = True
    set_state("processing")
    crashguard.mark("импорт файла: декодирование")
    return True


def end_file_import() -> None:
    """Освободить движок. Зовётся в `finally` на любом исходе, включая отмену."""
    global import_busy
    import_busy = False
    set_state("idle")
    crashguard.mark("простой (импорт файла завершён)")


def _segments_to_text(segments: list) -> str:
    """Сегменты (start, end, text) → текст с абзацами по паузам."""
    parts: list[str] = []
    prev_end = None
    for start, end, text in segments:
        if prev_end is not None and start - prev_end >= IMPORT_PARAGRAPH_GAP_SEC:
            parts.append("\n\n")
        elif parts:
            parts.append(" ")
        parts.append(text)
        prev_end = end
    return "".join(parts).strip()


def run_file_import(
    path: Path,
    audio_np: np.ndarray,
    *,
    progress_cb=None,
    should_cancel=None,
) -> dict:
    """Worker-поток: готовый numpy → текст рядом с исходником.

    Qt-объекты отсюда трогать НЕЛЬЗЯ (T-284) — только `window.notify_*` и
    переданные колбэки, которые окно доставляет к себе сигналами.

    Язык не задаём (`language=None`) — файл чужой, «ru» тут был бы вредным
    хардкодом: английская речь распозналась бы как русские звуки. Своя диктовка
    остаётся на явном «ru».
    """
    t0 = time.time()
    dur_sec = len(audio_np) / SAMPLE_RATE
    crashguard.mark(f"импорт файла: транскрипция {dur_sec:.0f} сек")
    # T-404: только из кэша — у импорта своя шкала прогресса по аудио, и
    # скачивание весов внутри неё выглядело бы как «файл обрабатывается».
    m = load_model(allow_download=False)
    info_out: dict = {}
    segments = transcribe_call.transcribe_channel(
        m,
        audio_np,
        language=None,
        progress_cb=progress_cb,
        initial_prompt=INITIAL_PROMPT or None,
        # Хвостовые правила рассчитаны на одну надиктовку («точка» в конце) —
        # в сегментах чужого файла они стреляли бы по живой речи (как в созвонах).
        postproc=lambda text: post_process(text, tail_rules=False),
        should_cancel=should_cancel,
        info_out=info_out,
    )
    cancelled = bool(should_cancel is not None and should_cancel())
    text = _segments_to_text(segments)
    elapsed = time.time() - t0
    ratio = dur_sec / elapsed if elapsed > 0 else 0.0
    log(
        f"file import: {path.name} · {dur_sec:.1f}s audio · {len(text)} chars · "
        f"{elapsed:.1f}s · {ratio:.1f}x · lang={info_out.get('language', '?')}"
        + (" · ОТМЕНЁН" if cancelled else "")
    )

    txt_path = None
    save_error = ""
    if text and not cancelled:
        crashguard.mark("импорт файла: сохраняю txt рядом с исходником")
        try:
            candidate = audio_import.output_txt_path(path)
            candidate.write_text(text, encoding="utf-8")
            txt_path = candidate
        except OSError as exc:
            # Файл может лежать на CD, сетевом диске или в папке без записи —
            # текст при этом уже есть, и терять его из-за этого нельзя.
            save_error = str(exc)
            log(f"file import: txt save fail: {exc}")

    return {
        "text": text,
        "txt_path": txt_path,
        "save_error": save_error,
        "duration_sec": dur_sec,
        "elapsed_sec": elapsed,
        "ratio_x": ratio,
        "language": info_out.get("language", ""),
        "cancelled": cancelled,
        # Пик громкости — чтобы отличить «речь не разобрали» от «дорожка молчит»
        # (у скринкастов и видео с выключенным микрофоном она почти нулевая).
        "peak": float(np.abs(audio_np).max()) if audio_np.size else 0.0,
    }


FILE_IMPORT_API = audio_import.FileImportApi(
    begin=begin_file_import,
    run=run_file_import,
    end=end_file_import,
)


# === T-259: смена / первая загрузка модели с видимым прогрессом ===
def model_busy() -> bool:
    """Идёт запись или транскрипция (диктовка, созвон либо импорт файла) —
    модель трогать нельзя."""
    return bool(recording or busy or call_active or call_busy or import_busy)


class _ModelSwitchBridge(QObject):
    """Мост worker-поток → GUI: прогресс скачивания и финал загрузки модели.

    Скачивание/загрузка (10-15 сек в VRAM, минуты на закачку) обязаны идти в
    worker'е — в главном потоке это выглядело бы как фриз UI.
    """

    progress = Signal(int, int)   # done_bytes, total_bytes (total=0 — размер неизвестен)
    finished = Signal(str, str)   # spec, error ("" — успех)


_model_switch_bridge: "_ModelSwitchBridge | None" = None
_model_switch_dialog = None
# Сигнал «пользователь нажал Отмена» через то же поле, что и текст ошибки:
# заводить второй Signal ради одного состояния — больше кода, чем смысла.
_CANCEL_MARK = "\x00cancelled"


def start_model_switch(spec: str, prev_settings: "dict | None" = None) -> None:
    """Скачать (если надо) и загрузить модель, показывая прогресс. GUI thread."""
    global _model_switch_bridge, _model_switch_dialog
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtWidgets import QMessageBox, QProgressDialog

    if _model_switch_dialog is not None:
        log("model switch already in progress — ignore")
        return

    need_download = not engine.is_cached(spec)
    label = engine.spec_display(spec)
    head = f"Скачиваю модель {label}…" if need_download else f"Загружаю модель {label}…"
    # T-405: скачивание можно прервать. Кнопка есть только когда качаем: прогрев
    # уже скачанной модели в VRAM длится секунды и прерывать там нечего.
    dlg = QProgressDialog(head, "Отменить" if need_download else None, 0, 0, window)
    dlg.setWindowTitle("SayType — модель")
    dlg.setWindowModality(_Qt.ApplicationModal)
    if not need_download:
        dlg.setCancelButton(None)
    dlg.setMinimumDuration(0)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    dlg.setMinimumWidth(460)

    bridge = _ModelSwitchBridge()
    cancel_flag = {"on": False}
    tracker = engine.DownloadProgress()
    # Таймер тикает независимо от байтов: застой видно только так (см. T-405).
    tick = QTimer(dlg)
    tick.setInterval(1000)

    def _cancel() -> None:
        if cancel_flag["on"]:
            return
        cancel_flag["on"] = True
        log("скачивание модели отменено пользователем")
        if _model_switch_dialog is not None:
            _model_switch_dialog.setLabelText(f"Останавливаю загрузку {label}…")

    def _tick() -> None:
        if _model_switch_dialog is None or cancel_flag["on"] or not need_download:
            return
        _model_switch_dialog.setLabelText(f"Скачиваю модель {label}…\n{tracker.text()}")

    dlg.canceled.connect(_cancel)
    tick.timeout.connect(_tick)
    tick.start()

    @Slot(int, int)
    def _on_progress(done: int, total: int) -> None:
        if _model_switch_dialog is None:
            return
        tracker.feed(done, total)
        if total > 0:
            _model_switch_dialog.setRange(0, 100)
            _model_switch_dialog.setValue(int(done * 100 / total))
        if not cancel_flag["on"]:
            _model_switch_dialog.setLabelText(f"Скачиваю модель {label}…\n{tracker.text()}")

    @Slot(str, str)
    def _on_finished(done_spec: str, error: str) -> None:
        global _model_switch_dialog, _model_switch_bridge
        tick.stop()
        if _model_switch_dialog is not None:
            _model_switch_dialog.close()
            _model_switch_dialog = None
        _model_switch_bridge = None
        if error == _CANCEL_MARK:
            # Отмена — не сбой: окна «не удалось» быть не должно, но и молчать
            # нельзя, иначе непонятно, осталось ли что-то на диске.
            done_mb = tracker.done / 1e6
            QMessageBox.information(
                window, "Скачивание отменено",
                f"Загрузка модели {label} остановлена."
                + (f"\n\nСкачано {done_mb:.0f} МБ — они остались на диске, "
                   "повторный запуск продолжит с этого места." if done_mb >= 1 else ""),
            )
            return
        if not error:
            log(f"model switched: {done_spec} ({MODEL_DEVICE}/{MODEL_COMPUTE_TYPE})")
            return
        log(f"model switch FAIL ({done_spec}): {error}")
        # Откат настроек — иначе следующая транскрипция снова упрётся в битую
        # модель. T-404: откатываемся ТОЛЬКО на модель, которая реально лежит на
        # диске и отличается от упавшей. Раньше откат шёл всегда, и на старте с
        # пустым кэшем (prev == та же несуществующая модель) он запускал вторую
        # закачку в фоновом потоке — она падала так же и уходила в `_crash.log`.
        rolled_back = ""
        if prev_settings is not None:
            prev_spec = engine.spec_from_settings(prev_settings)
            if prev_spec != done_spec and engine.is_cached(prev_spec):
                SETTINGS["model"] = prev_settings.get("model", engine.FALLBACK_SPEC)
                SETTINGS["custom_model"] = prev_settings.get("custom_model", "")
                try:
                    save_settings_dict(SETTINGS)
                except Exception as exc:
                    log(f"settings rollback save fail: {exc}")
                threading.Thread(
                    target=lambda: load_model_background(reason="откат на прежнюю модель"),
                    daemon=True, name="model-rollback",
                ).start()
                rolled_back = f"\n\nВернул прежнюю модель ({engine.spec_display(prev_spec)})."
            else:
                log(f"откат не нужен: prev={prev_spec} (в кэше: {engine.is_cached(prev_spec)})")
        QMessageBox.warning(
            window,
            "Модель не загрузилась",
            f"{error}{rolled_back}",
        )

    bridge.progress.connect(_on_progress)
    bridge.finished.connect(_on_finished)
    _model_switch_bridge = bridge
    _model_switch_dialog = dlg
    dlg.show()

    def _worker() -> None:
        try:
            switch_model(
                spec,
                progress_cb=lambda d, t: bridge.progress.emit(int(d), int(t)),
                should_cancel=lambda: cancel_flag["on"],
            )
            bridge.finished.emit(spec, "")
        except engine.DownloadCancelled:
            bridge.finished.emit(spec, _CANCEL_MARK)
        except Exception as exc:
            # T-404: у ошибок скачивания есть человеческий текст с причиной и
            # следующим шагом — показываем его, а не `ClassName: repr`.
            text = exc.full_text() if isinstance(exc, engine.ModelDownloadError) else (
                f"{engine.spec_display(spec)} не удалось загрузить:\n\n"
                f"{exc.__class__.__name__}: {exc}"
            )
            bridge.finished.emit(spec, text)

    threading.Thread(target=_worker, daemon=True, name="model-switch").start()


def run_first_run_wizard(force: bool = False) -> bool:
    """Мастер первого запуска. Возвращает True, если человек его прошёл.

    Зовётся до загрузки модели и до предложения CUDA-слоя: в мастере оба выбора
    уже сделаны, и повторно спрашивать про то же самое было бы издевательством.
    `force=True` — повторный запуск из настроек.
    """
    global SETTINGS

    if not force and SETTINGS.get("wizard_done", False):
        return False
    from .wizard import FirstRunWizard

    dialog = FirstRunWizard(dict(SETTINGS), window)
    if dialog.exec() != dialog.Accepted:
        # Закрыли крестиком — не считаем пройденным, спросим в следующий раз.
        log("мастер первого запуска закрыт без завершения")
        return False

    values = dialog.values()
    SETTINGS.update(values)
    try:
        save_settings_dict(SETTINGS)
    except Exception as exc:
        log(f"мастер: не смог сохранить настройки ({exc})")
    log(
        "мастер пройден: mic=%s hotkey=%s model=%s autostart=%s"
        % (values["mic_device"] or "системный", values["hotkey"],
           values["model"], values["autostart"])
    )
    reload_hotkey(values["hotkey"])
    _sync_autostart_on_start()
    if dialog.wants_cuda_layer():
        download_cuda_layer(then_start_model=True)
    return True


def start_startup_model() -> None:
    """Поднять модель на старте: из кэша — молча в фоне, иначе с прогрессом.

    T-132: preload нужен streaming-worker'у с первой же записи. T-259: если
    весов ещё нет на диске — качаем видимо, а не 480 МБ в тишине под видом
    «зависло». T-261: вызывается либо сразу, либо после докачки CUDA-слоя.
    """
    spec = active_model_spec()
    if engine.is_cached(spec):
        threading.Thread(
            target=lambda: load_model_background(reason="прогрев модели на старте"),
            daemon=True, name="model-preload",
        ).start()
    else:
        log(f"модель {spec} не найдена на диске — качаю с прогрессом")
        QTimer.singleShot(0, lambda: start_model_switch(spec, dict(SETTINGS)))


# === T-261: докачка CUDA-слоя ===
class _CudaLayerBridge(QObject):
    """Мост worker → GUI для скачивания CUDA-слоя (тот же приём, что у модели)."""

    progress = Signal(int, int)  # done_bytes, total_bytes
    finished = Signal(bool, str)  # ok, error ("" — успех)


_cuda_bridge: "_CudaLayerBridge | None" = None
_cuda_dialog = None
_cuda_cancel = threading.Event()


def download_cuda_layer(parent=None, then_start_model: bool = False) -> None:
    """Скачать и установить CUDA-слой с прогрессом и отменой. GUI thread.

    Отмена не выбрасывает скачанное: недокачанный файл остаётся в профиле, и
    следующий заход продолжает с той же точки (450 МБ по плохой сети иначе
    превращаются в бесконечный цикл «начали — оборвалось — начали заново»).

    `then_start_model` — вариант со старта приложения: модель ждёт итога, чтобы
    подняться уже на GPU (иначе ускорение включилось бы только через перезапуск).
    """
    global _cuda_bridge, _cuda_dialog
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtWidgets import QMessageBox, QProgressDialog

    if _cuda_dialog is not None:
        log("CUDA layer download already in progress — ignore")
        return

    parent = parent or window
    _cuda_cancel.clear()
    dlg = QProgressDialog(
        f"Скачиваю ускорение GPU… ~{cuda_layer.size_hint_mb()} МБ", "Отмена", 0, 0, parent
    )
    dlg.setWindowTitle("SayType — ускорение GPU")
    dlg.setWindowModality(_Qt.ApplicationModal)
    dlg.setMinimumDuration(0)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    dlg.setMinimumWidth(460)
    dlg.canceled.connect(_cuda_cancel.set)

    bridge = _CudaLayerBridge()

    @Slot(int, int)
    def _on_progress(done: int, total: int) -> None:
        if _cuda_dialog is None:
            return
        if total > 0:
            _cuda_dialog.setRange(0, 100)
            _cuda_dialog.setValue(int(done * 100 / total))
            _cuda_dialog.setLabelText(
                f"Скачиваю ускорение GPU… {done / 1e6:.0f} / {total / 1e6:.0f} МБ"
            )
        else:
            _cuda_dialog.setLabelText(f"Скачиваю ускорение GPU… {done / 1e6:.0f} МБ")

    @Slot(bool, str)
    def _on_finished(ok: bool, error: str) -> None:
        global _cuda_dialog, _cuda_bridge
        if _cuda_dialog is not None:
            _cuda_dialog.close()
            _cuda_dialog = None
        _cuda_bridge = None
        if ok:
            log("CUDA-слой установлен")
            QMessageBox.information(
                parent, "Ускорение GPU установлено",
                "Готово. Ускорение включится после перезапуска SayType."
                if cuda_layer.needs_restart()
                else "Готово — транскрипция пойдёт на видеокарте.",
            )
        elif _cuda_cancel.is_set():
            log("CUDA-слой: скачивание отменено пользователем")  # молча: отмену нажали сами
        else:
            log(f"CUDA-слой FAIL: {error}")
            QMessageBox.warning(
                parent, "Ускорение GPU не установилось",
                f"{error}\n\nПриложение продолжит работать на процессоре. "
                "Повторить можно в настройках.",
            )
        # Модель ждала итога — поднимаем её в любом случае, хоть на GPU, хоть на CPU
        if then_start_model:
            start_startup_model()

    bridge.progress.connect(_on_progress)
    bridge.finished.connect(_on_finished)
    _cuda_bridge = bridge
    _cuda_dialog = dlg
    dlg.show()

    def _worker() -> None:
        try:
            ok = cuda_layer.ensure(
                progress_cb=lambda d, t: bridge.progress.emit(int(d), int(t)),
                should_cancel=_cuda_cancel.is_set,
            )
            bridge.finished.emit(ok, "" if ok else "Слой скачан, но библиотеки не подхватились.")
        except cuda_layer.LayerError as exc:
            bridge.finished.emit(False, str(exc))
        except Exception as exc:
            bridge.finished.emit(False, f"{exc.__class__.__name__}: {exc}")

    threading.Thread(target=_worker, daemon=True, name="cuda-layer").start()


# === T-262: фоновая проверка обновлений ===
class _UpdateBridge(QObject):
    """Мост из фонового потока проверки обновлений и скачивания в GUI."""

    found = Signal(object)  # UpdateInfo
    progress = Signal(int)  # процент 0-100
    failed = Signal(str)  # текст ошибки


_update_bridge: "_UpdateBridge | None" = None
_update_box = None
_update_progress_dialog = None


def _show_update_offer(info) -> None:
    """Немодальное предложение обновиться. GUI thread.

    Немодальное сознательно: человек мог запустить приложение, чтобы прямо
    сейчас надиктовать мысль, и модалка поперёк этого — худшее, что может
    сделать апдейтер. Окно висит, работать не мешает, закрывается без ответа.
    """
    global _update_box
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtWidgets import QMessageBox

    if _update_box is not None:
        return
    version = updater.version_of(info)
    # T-328: «что изменилось» перед кнопкой «Обновить». Заметок нет (старый
    # релиз, фид без них) — окно остаётся ровно таким, каким было до T-328.
    #
    # T-443: NotesMarkdown из живого UpdateInfo (не из тестовых данных — там
    # всё разбирается верно) на реальном апдейте приходил пустым в трёх
    # прогонах подряд, причину поймать не удалось. Полный список изменений
    # ссылкой на страницу релиза — не зависит от того, почему это поле пустое.
    notes = updater.notes_of(info)
    box = QMessageBox(window)
    box.setWindowTitle("Доступно обновление")
    box.setIcon(QMessageBox.Information)
    box.setText(f"Вышла версия {version}.")
    box.setTextFormat(_Qt.RichText)
    release_url = updater.release_page_url(info)
    box.setInformativeText(
        (notes + "\n\n" if notes else "")
        + f'Что изменилось: <a href="{release_url}">страница релиза</a>.\n\n'
        + "Записи, настройки, словарь и скачанные модели останутся на месте."
    )
    update_btn = box.addButton("Обновить и перезапустить", QMessageBox.AcceptRole)
    box.addButton("Позже", QMessageBox.RejectRole)
    box.setDefaultButton(update_btn)

    def _on_done(_button) -> None:
        global _update_box
        _update_box = None
        if box.clickedButton() is not update_btn:
            log("обновление отложено пользователем")
            return
        _start_update_download(info)

    box.buttonClicked.connect(_on_done)
    _update_box = box
    box.show()


def _start_update_download(info) -> None:
    """Качать с видимым прогрессом — тот же приём, что у CUDA-слоя и модели.

    T-443: раньше здесь просто уходил фоновый поток без единого UI-хука, и
    человек 10-15 секунд не видел вообще ничего, пока velopack качал и
    распаковывал пакет — не мог понять, сработало ли нажатие. Ошибка тоже
    терялась молча: исключение в daemon-потоке без обработчика просто пропадало.
    """
    global _update_progress_dialog
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtWidgets import QMessageBox, QProgressDialog

    if _update_progress_dialog is not None:
        return
    # T-443: до первого вызова progress_cb velopack сам резолвит путь
    # обновления (полный пакет или delta, какой из них) — замерено до 70 сек
    # молчания на реальном апдейте, обратного вызова на этом этапе нет.
    # Неопределённая полоса (setRange(0, 0)) — единственный честный способ
    # сказать «идёт процесс», не обещая процент, которого ещё нет.
    # cancelButtonText принимает пустую строку как «нет кнопки», а не None —
    # PySide6 здесь требует str, а не Optional[str] (проверено живым TypeError).
    dlg = QProgressDialog("Готовлюсь к обновлению…", "", 0, 0, window)
    dlg.setWindowTitle("Обновление SayType")
    dlg.setWindowModality(_Qt.ApplicationModal)
    dlg.setMinimumDuration(0)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    dlg.setMinimumWidth(420)
    _update_progress_dialog = dlg

    bridge = _update_bridge or _UpdateBridge()

    @Slot(int)
    def _on_progress(percent: int) -> None:
        if _update_progress_dialog is None:
            return
        if _update_progress_dialog.maximum() == 0:
            _update_progress_dialog.setLabelText("Скачиваю обновление…")
            _update_progress_dialog.setRange(0, 100)
        _update_progress_dialog.setValue(max(0, min(100, percent)))

    @Slot(str)
    def _on_failed(error: str) -> None:
        global _update_progress_dialog
        if _update_progress_dialog is not None:
            _update_progress_dialog.close()
            _update_progress_dialog = None
        QMessageBox.warning(
            window, "Обновление не установилось",
            f"Не получилось скачать или применить обновление:\n{error}\n\n"
            "Попробуйте ещё раз позже.",
        )

    bridge.progress.connect(_on_progress)
    bridge.failed.connect(_on_failed)
    dlg.show()
    # На быстром апдейте (маленький delta, хороший канал) скачивание могло
    # завершиться и убить процесс раньше, чем Qt успевал отрисовать первый
    # кадр диалога — .show() только планирует показ, реальная прорисовка
    # ждёт следующего прохода event loop. Форсируем его синхронно.
    QApplication.processEvents()

    def _run() -> None:
        # Управление сюда не возвращается при успехе: velopack перезапускает
        # процесс сам. Ошибка — единственный путь, которым функция возвращается.
        try:
            updater.download_and_apply(
                info, progress_cb=lambda pct: bridge.progress.emit(int(pct)),
            )
        except Exception as exc:
            bridge.failed.emit(str(exc))

    threading.Thread(target=_run, daemon=True, name="update-apply").start()


def start_update_check() -> None:
    """Спросить про обновления в фоне, если это разрешено настройками."""
    global _update_bridge

    if not SETTINGS.get("check_updates", True):
        log("автопроверка обновлений выключена в настройках")
        return
    if not updater.is_available():
        return  # не установленная версия или фид не настроен — молча

    bridge = _UpdateBridge()
    bridge.found.connect(_show_update_offer)
    _update_bridge = bridge

    def _worker() -> None:
        info = updater.check()  # тихая: сетевые ошибки только в лог
        if info is not None:
            bridge.found.emit(info)

    threading.Thread(target=_worker, daemon=True, name="update-check").start()


def maybe_offer_cuda_layer() -> None:
    """Найдена NVIDIA, а CUDA-рантайма нет — предложить докачать. GUI thread.

    Зовётся ДО загрузки модели: после первого импорта CTranslate2 подключить
    новые DLL уже нельзя, понадобился бы перезапуск.
    """
    from PySide6.QtWidgets import QMessageBox

    if SETTINGS.get("cuda_layer_declined", False):
        return
    try:
        if not cuda_layer.should_offer():
            return
        gpu = cuda_layer.gpu() or {}
    except Exception as exc:
        log(f"cuda layer probe fail: {exc!r}")
        return

    name = gpu.get("name") or "видеокарта NVIDIA"
    answer = QMessageBox.question(
        window,
        "Найдена видеокарта NVIDIA",
        f"Обнаружена {name}.\n\n"
        f"Скачать ускорение? ~{cuda_layer.size_hint_mb()} МБ, транскрипция станет "
        "заметно быстрее.\n\nБез него всё работает на процессоре — просто медленнее. "
        "Передумать можно в настройках.",
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.Yes,
    )
    if answer == QMessageBox.Yes:
        download_cuda_layer(then_start_model=True)
        return
    SETTINGS["cuda_layer_declined"] = True
    try:
        save_settings_dict(SETTINGS)
    except Exception as exc:
        log(f"cuda decline save fail: {exc}")
    log("CUDA-слой: пользователь отказался — работаем на CPU")


class _Bridge(QObject):
    """Мост между сигналами MainWindow и hotkey-логикой (Qt main thread)."""

    @Slot(dict)
    def on_settings_changed(self, new: dict) -> None:
        old_hotkey = SETTINGS.get("hotkey")
        old_pre_roll = bool(SETTINGS.get("pre_roll_enabled", False))
        prev_settings = dict(SETTINGS)
        old_spec = active_model_spec()
        SETTINGS.update(new)
        if new["hotkey"] != old_hotkey:
            reload_hotkey(new["hotkey"])
        # Словарь и замены — файлы в профиле: перечитываем, чтобы правки
        # применились без перезапуска (как и остальные настройки).
        reload_user_dictionary()
        log(f"словарь: {len(INITIAL_PROMPT)} знаков, замен: "
            f"{len(_BODY_RULES) + len(_TAIL_RULES)}")
        # Папка созвонов следует за папкой истории.
        transcribe_call.set_history_dir(history_dir())
        # T-404: маршрут скачивания весов — применяется сразу, без перезапуска:
        # его правят как раз в момент, когда модель не качается.
        engine.set_download_proxy(SETTINGS.get("download_proxy", ""), logger=log)
        # T-164: hot-reload pre-roll буфера без перезапуска приложения.
        # OFF→ON: запустить фоновый sd.InputStream. ON→OFF: остановить.
        new_pre_roll = bool(new.get("pre_roll_enabled", False))
        if new_pre_roll != old_pre_roll and pre_roll is not None:
            if new_pre_roll:
                pre_roll.start()
            else:
                pre_roll.stop()
        # T-418: pre-roll держит постоянный поток на прежнем устройстве. Без
        # перезапуска буфера первые 500 мс каждой надиктовки продолжали бы
        # приезжать со старого микрофона — как раз с того, от которого ушли.
        if (new.get("mic_device", "") or "") != (prev_settings.get("mic_device", "") or ""):
            log(f"микрофон: {prev_settings.get('mic_device') or 'системный'} → "
                f"{new.get('mic_device') or 'системный'}")
            if pre_roll is not None and pre_roll.is_running():
                pre_roll.stop()
                pre_roll.start()
        # T-165: hot-reload processing_mode и auto_threshold_sec не требует side-эффектов —
        # значения снапшотятся в start_recording на каждом hotkey-down. Просто логируем
        # текущие, чтобы по логу было видно, что изменения сохранились.
        log(
            f"settings applied: hotkey={SETTINGS['hotkey']} hist={SETTINGS['history_dir']} "
            f"count={SETTINGS['rotation_count']} autostart={SETTINGS['autostart']} "
            f"pre_roll={new_pre_roll} mode={SETTINGS.get('processing_mode', 'auto')} "
            f"threshold={SETTINGS.get('auto_threshold_sec', 10)}s "
            f"model={SETTINGS.get('model')}/{SETTINGS.get('custom_model', '')}"
        )
        # T-278: режим и путь к статистике живут в контексте аварии — после
        # правки настроек они не должны остаться прошлыми.
        crashguard.set_context(
            mode=f"{SETTINGS.get('processing_mode', 'auto')}"
                 f"/{SETTINGS.get('auto_threshold_sec', 10)}с",
            stats_path=str(history_dir() / "_stats.jsonl"),
        )
        # T-259: смена модели на лету. Диалог настроек блокирует поле модели, пока
        # идёт запись/транскрипция, но проверяем и здесь — состояние могло
        # поменяться, пока диалог был открыт (hotkey работает и при нём).
        new_spec = active_model_spec()
        if new_spec != old_spec:
            if model_busy():
                log(f"model switch отклонён (идёт запись/транскрипция): {old_spec} → {new_spec}")
                SETTINGS["model"] = prev_settings.get("model", engine.FALLBACK_SPEC)
                SETTINGS["custom_model"] = prev_settings.get("custom_model", "")
                try:
                    save_settings_dict(SETTINGS)
                except Exception as exc:
                    log(f"settings rollback save fail: {exc}")
                from PySide6.QtWidgets import QMessageBox

                QMessageBox.information(
                    window,
                    "Модель не сменена",
                    "Идёт запись или транскрипция — смена модели заблокирована.\n"
                    "Останови процесс и поменяй модель ещё раз.",
                )
            else:
                start_model_switch(new_spec, prev_settings)

    @Slot()
    def on_stop_sound(self) -> None:
        sound.play_stop()

    @Slot()
    def on_quit_requested(self) -> None:
        log("quit requested")
        if audio_stream is not None:
            try:
                audio_stream.stop()
                audio_stream.close()
            except Exception:
                pass
        # T-164: graceful shutdown pre-roll stream'а (без orphan-thread'а)
        if pre_roll is not None:
            pre_roll.stop()
        # T-172: если идёт запись созвона — остановить рекордер (освободить mic/loopback)
        global call_recorder, call_active
        if call_active and call_recorder is not None:
            try:
                call_recorder.stop()
            except Exception as exc:
                log(f"call recorder stop on quit fail: {exc}")
            call_recorder = None
            call_active = False
        unregister_hotkey()
        if _user32 is not None and APP_HWND != 0:
            try:
                _user32.UnregisterHotKey(APP_HWND, HOTKEY_ID_CALL)
            except Exception:
                pass


def _sync_autostart_on_start() -> None:
    """При старте: если settings.autostart=True а ярлыка нет — создать."""
    want = SETTINGS.get("autostart", False)
    have = autostart_shortcut_exists()
    if want and not have:
        ok, msg = create_autostart_shortcut(Path(__file__).resolve())
        log(f"autostart create: ok={ok} {msg}")


# === T-176: recovery прерванных созвонов (краш ВО ВРЕМЯ записи) ===
class _RecoveryBridge(QObject):
    """Мост worker-поток → GUI-поток для показа предложения восстановить.

    Сборку WAV (тяжёлую) делает worker; QMessageBox обязан жить в GUI-потоке,
    поэтому worker эмитит сигнал, слот ловит его в main thread (queued connection).
    """

    offer = Signal(object)  # list[dict]: {stem, recording, started_at, dur_min, wav}

    @Slot(object)
    def on_offer(self, recovered) -> None:
        if not recovered:
            return
        try:
            from PySide6.QtWidgets import QMessageBox

            n = len(recovered)
            lines = "\n".join(
                f"• {r['started_at']} (~{r['dur_min']} мин) → {r['wav']}" for r in recovered
            )
            head = (
                "Найден прерванный созвон (запись оборвалась — краш/закрытие)."
                if n == 1
                else f"Найдено прерванных созвонов: {n} (записи оборвались)."
            )
            text = (
                f"{head}\n\nАудио уже восстановлено и сохранено в Calls\\:\n{lines}\n\n"
                f"Транскрибировать сейчас? (можно и позже — WAV уже на диске)"
            )
            btn = QMessageBox.question(
                window,
                "SayType — восстановление созвона",
                text,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if btn == QMessageBox.Yes:
                threading.Thread(
                    target=_recovery_finalize_thread,
                    args=(recovered,),
                    daemon=True,
                    name="call-recovery-finalize",
                ).start()
            else:
                log(f"recovery: транскрипт {n} восстановл. созвонов отложен (WAV в Calls)")
        except Exception as exc:
            log(f"recovery offer fail: {exc!r}")


_recovery_bridge: "_RecoveryBridge | None" = None


def _recovery_finalize_thread(recovered) -> None:
    """Дотранскрибировать восстановленные созвоны ПОСЛЕДОВАТЕЛЬНО (одна
    WhisperModel не reentrant — нельзя два m.transcribe разом)."""
    for r in recovered:
        try:
            _call_finalize_thread(r["recording"])
        except Exception as exc:
            log(f"recovery finalize {r.get('stem')} fail: {exc!r}")


def _recovery_scan_worker() -> None:
    """Старт: вычистить устаревший raw (WAV уже есть) + собрать WAV из осиротевших
    raw-логов прерванных записей, затем предложить транскрипт.

    Тяжёлая часть (сборка/ресэмпл) — здесь, в фоне, чтобы не морозить старт UI.
    Никогда не роняет процесс (всё в try)."""
    try:
        # 1) stale raw: у записи уже есть WAV → raw устарел, чистим молча
        try:
            for meta_p in transcribe_call.calls_dir().glob(f"*{RAW_META_SUFFIX}"):
                stem = meta_p.name[: -len(RAW_META_SUFFIX)]
                if (transcribe_call.calls_dir() / f"{stem}.wav").exists():
                    cleanup_raw_log(stem, logger=log)
        except Exception as exc:
            log(f"recovery stale-cleanup fail: {exc!r}")

        # 2) осиротевшие записи → собрать WAV (аудио durable) + предложить транскрипт
        recovered = []
        for stem in find_orphaned_recordings():
            try:
                rec = recover_recording_to_wav(stem, logger=log)
            except Exception as exc:
                log(f"recovery {stem} fail: {exc!r}")
                continue
            if rec is None:
                continue
            recovered.append(
                {
                    "stem": stem,
                    "recording": rec,
                    "started_at": rec.started_at.strftime("%Y-%m-%d %H:%M"),
                    "dur_min": max(1, int(round(rec.duration_sec / 60))),
                    "wav": rec.wav_path.name if rec.wav_path else f"{stem}.wav",
                }
            )
        if recovered and _recovery_bridge is not None:
            log(f"recovery: восстановлено {len(recovered)} прерванных созвонов — предлагаю транскрипт")
            _recovery_bridge.offer.emit(recovered)
    except Exception as exc:
        log(f"recovery scan worker fail: {exc!r}")


def main() -> None:
    global window, SETTINGS, APP_HWND, HOTKEY_FILTER, CALL_HOTKEY_FILTER

    # single-instance guard уже выполнен на module-level до импортов
    _set_taskbar_app_id()  # Windows taskbar показывает наш микрофон, не pythonw

    icon_paths = ensure_icons()
    _sync_desktop_shortcut(icon_paths["app_ico"])

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("SayType")
    app.setApplicationDisplayName("SayType")
    app.setQuitOnLastWindowClosed(False)  # tray держит процесс живым после close окна

    # T-355: QSoundEffect создаётся здесь (main thread, после QApplication) и живёт
    # всю сессию — держит audio session процесса "тёплой", короткие редкие сигналы
    # не обрываются (см. sound.py, история двух неудачных попыток в docstring).
    sound.init_player()

    SETTINGS = load_settings_dict()
    # Словарь и замены — данные пользователя, а не константы кода (T-260).
    reload_user_dictionary()
    # Подпапка созвонов живёт внутри папки истории из настроек.
    transcribe_call.set_history_dir(history_dir())
    # T-404: свой маршрут для huggingface.co — до первой попытки скачать веса.
    engine.set_download_proxy(SETTINGS.get("download_proxy", ""), logger=log)
    log(
        f"start · hotkey={SETTINGS['hotkey']} · history={SETTINGS['history_dir']} · "
        f"count={SETTINGS['rotation_count']} · "
        f"mode={SETTINGS.get('processing_mode', 'auto')}/"
        f"{SETTINGS.get('auto_threshold_sec', 10)}s · "
        f"pre_roll={SETTINGS.get('pre_roll_enabled', False)} · "
        f"model={engine.spec_display(engine.spec_from_settings(SETTINGS))} · py={sys.executable}"
    )
    # T-278: постоянный контекст сессии для отчёта об аварии. Модель здесь —
    # из настроек (загрузка идёт ниже в фоне), load_model уточнит её на
    # реальные device/compute_type.
    crashguard.set_context(
        model=engine.spec_display(engine.spec_from_settings(SETTINGS)),
        mode=f"{SETTINGS.get('processing_mode', 'auto')}"
             f"/{SETTINGS.get('auto_threshold_sec', 10)}с",
        stats_path=str(history_dir() / "_stats.jsonl"),
    )
    _sync_autostart_on_start()

    window = MainWindow(
        idle_icon_path=icon_paths["idle"],
        recording_icon_path=icon_paths["recording"],
        processing_icon_path=icon_paths["processing"],
        app_icon_path=icon_paths["app"],
        history_dir_getter=history_dir,
        rotation_count_getter=rotation_count,
        entry_script=Path(__file__).resolve(),
        toggle_recording_via_ui=trigger_recording_via_ui,
        toggle_call_via_ui=trigger_call_via_ui,
        toggle_note_recording_via_ui=trigger_note_recording_via_ui,  # T-352
        model_busy_getter=model_busy,  # T-259: блокировка смены модели во время работы
        file_import_api=FILE_IMPORT_API,  # T-351: импорт готового аудиофайла
        cancel_transcription=cancel_transcription,  # T-405: выход из ожидания
        model_missing_getter=no_model_reason,       # T-405: полоса «модель не скачана»
    )
    APP_HWND = int(window.winId())  # hwnd для RegisterHotKey

    bridge = _Bridge()
    window.settings_changed.connect(bridge.on_settings_changed)
    window.quit_requested.connect(bridge.on_quit_requested)
    window.stop_sound_requested.connect(bridge.on_stop_sound)

    # T-132: preload модели в фоне — нужна для streaming worker'а с первой записи.
    # OFF-режим грузит лениво при первом стопе (5-7 сек ожидания), preload только
    # ускоряет. Lock внутри engine.load_model() защищает от race c lazy-loader'ом.
    # T-259: если модели ещё нет на диске — качаем с прогрессом (модалка), а не
    # молча 480 МБ / 1.6 ГБ в тишине под видом «зависло».
    # T-263: первый запуск — мастер. Он сам спрашивает и про модель, и про
    # ускорение, поэтому обычные предложения ниже при пройденном мастере
    # оказываются пустыми: выбор уже сделан и сохранён.
    wizard_passed = run_first_run_wizard()

    # T-261: сначала CUDA-слой, потом модель. Обратный порядок означал бы, что
    # CTranslate2 уже загрузился без CUDA, и скачанные DLL подхватились бы
    # только со следующего запуска.
    if not wizard_passed:
        maybe_offer_cuda_layer()
    if _cuda_dialog is None:
        start_startup_model()
    else:
        log("загрузка модели отложена: идёт докачка CUDA-слоя")

    # T-164: pre-roll singleton + старт если включён в Settings (дефолт OFF).
    # Hot-reload в Settings dialog → _Bridge.on_settings_changed start/stop.
    global pre_roll
    pre_roll = PreRollBuffer(duration_sec=PRE_ROLL_DURATION_SEC)
    if SETTINGS.get("pre_roll_enabled", False):
        pre_roll.start()

    # Установить native event filter для ловли WM_HOTKEY
    HOTKEY_FILTER = _HotkeyEventFilter(HOTKEY_ID, _on_hotkey_activated)
    app.installNativeEventFilter(HOTKEY_FILTER)

    if not register_hotkey(SETTINGS["hotkey"]):
        log(f"hotkey '{SETTINGS['hotkey']}' could not be registered (busy or invalid)")

    # T-172: ВТОРОЙ hotkey — запись созвона (ctrl+shift+E, захардкожен).
    # Отдельный native event filter + отдельный HOTKEY_ID_CALL; первый фильтр/
    # хоткей не трогаем. Сохраняем ссылку на фильтр чтобы Qt его не собрал GC.
    CALL_HOTKEY_FILTER = _HotkeyEventFilter(HOTKEY_ID_CALL, _on_call_hotkey)
    app.installNativeEventFilter(CALL_HOTKEY_FILTER)
    if _user32 is not None and APP_HWND != 0:
        ok_call = _user32.RegisterHotKey(
            APP_HWND, HOTKEY_ID_CALL, MOD_CTRL | MOD_SHIFT | MOD_NOREPEAT, ord("E")
        )
        if ok_call:
            log("call hotkey registered (Win32): <ctrl>+<shift>+e -> запись созвона")
        else:
            err = ctypes.get_last_error() if _kernel32 else 0
            log(f"RegisterHotKey FAIL for call hotkey ctrl+shift+e (err={err})")

    # Закрываем splash прямо перед показом основного окна
    global _splash
    if _splash is not None:
        try:
            _splash.destroy()
        except Exception:
            pass
        _splash = None

    if not SETTINGS.get("start_minimized", False):
        window.show_window()  # showNormal + raise_ + activateWindow

    # T-262: обновления спрашиваем последними и в фоне — до них уже поднялись
    # модель и хоткеи, так что задержка сети ничего не задерживает.
    start_update_check()

    # T-176: проверка прерванных созвонов (recovery) — в фоне, не морозит старт.
    # Сборка WAV в worker'е, предложение транскрипта — через сигнал в GUI-поток.
    global _recovery_bridge
    _recovery_bridge = _RecoveryBridge()
    _recovery_bridge.offer.connect(_recovery_bridge.on_offer)
    threading.Thread(target=_recovery_scan_worker, daemon=True, name="call-recovery-scan").start()

    exit_code = app.exec()
    unregister_hotkey()
    # T-172: снять второй hotkey (созвон) рядом с обычным
    if _user32 is not None and APP_HWND != 0:
        try:
            _user32.UnregisterHotKey(APP_HWND, HOTKEY_ID_CALL)
        except Exception as exc:
            log(f"unregister call hotkey fail: {exc}")
    # mutex освободится автоматом при выходе процесса (Windows OS)
    crashguard.clean_exit()
    # T-268: не sys.exit — на финализации интерпретатор разрушил бы объекты
    # моделей CT2, а после генерации с sampling это убивает процесс с
    # 0xC0000409. Штатный выход не должен выглядеть как краш.
    engine.hard_exit(exit_code)


def run() -> None:
    """Точка входа пакета — то, что зовёт ``python -m saytype``.

    Сам выход из процесса делает `main()` через `engine.hard_exit` (T-268), сюда
    управление не возвращается.
    """
    main()


if __name__ == "__main__":
    run()
