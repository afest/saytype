"""Формат hotkey-строки, разбор и валидация.

Одно место правды на три модуля: настройки (`transcribe_ui_window`), мастер
первого запуска (`wizard`) и регистрация в Windows (`transcribe_ui`). Раньше
таблицы жили в двух файлах, а проверка вводимого сочетания шла через
`pynput.keyboard.HotKey.parse`.

Почему без pynput (T-318). Библиотека под LGPL-3.0, и в замороженной сборке её
байткод оказывается внутри PYZ-архива exe — заменить её пользователь не может,
то есть §4(d) не выполняется. Отдавать pynput отдельными файлами ради одной
функции разбора смысла нет: перехват клавиш у нас свой (`RegisterHotKey`), а
формат строки — просто текст. Проверка ниже вдобавок точнее прежней: она
пропускает ровно то, что примет `RegisterHotKey`, тогда как pynput принимал и
сочетания, которые Windows зарегистрировать не даст.

Канонический формат — модификаторы в угловых скобках, всё в нижнем регистре:
``<ctrl>+<shift>+q``. Он же лежит в `settings.ini`.
"""

# === Модификаторы Win32 (значения из winuser.h, нужны RegisterHotKey) ===
MOD_ALT = 0x0001
MOD_CTRL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

# Написания модификатора, которые встречаются в Qt, в старых настройках и в
# каноническом формате. `cmd` — исторический синоним `win` из формата строки.
MODIFIER_NAMES = {"ctrl", "shift", "alt", "meta", "cmd", "win"}

MOD_MAP = {
    "<ctrl>": MOD_CTRL,
    "<shift>": MOD_SHIFT,
    "<alt>": MOD_ALT,
    "<cmd>": MOD_WIN,
    "<win>": MOD_WIN,
    "<meta>": MOD_WIN,
}

# Клавиши, у которых имя длиннее одного символа: Qt отдаёт их словом, а
# RegisterHotKey требует virtual-key code вторым аргументом.
SPECIAL_KEY_VK = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B,
    "escape": 0x1B, "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}

_MOD_TITLE = {
    "ctrl": "Ctrl", "shift": "Shift", "alt": "Alt",
    "cmd": "Win", "win": "Win", "meta": "Win",
}


def _split(s: str) -> list[str]:
    """'Ctrl+Shift+Q' / '<ctrl>+<shift>+q' → ['ctrl', 'shift', 'q'] (без скобок)."""
    if not s:
        return []
    cleaned = s.replace("<", "").replace(">", "")
    return [p.strip().lower() for p in cleaned.split("+") if p.strip()]


def to_canonical(s: str) -> str:
    """Произвольная hotkey-строка → канонический вид '<ctrl>+<shift>+q'.

    Принимает 'Ctrl+Shift+Q' (Qt), 'ctrl+shift+q' (плоский вид) и уже
    канонический '<ctrl>+<shift>+q'. Модификаторы уходят в угловые скобки,
    обычные клавиши остаются в нижнем регистре без скобок.
    """
    out = []
    for p in _split(s):
        mod = "cmd" if p in ("meta", "win") else p
        out.append(f"<{mod}>" if mod in MODIFIER_NAMES else p)
    return "+".join(out)


def to_qt(s: str) -> str:
    """Канонический вид → 'Ctrl+Shift+Q' для показа в интерфейсе."""
    out = []
    for p in _split(s):
        if p in MODIFIER_NAMES:
            out.append(_MOD_TITLE.get(p, p.capitalize()))
        else:
            out.append(p.upper() if len(p) == 1 else p.capitalize())
    return "+".join(out)


def to_mod_vk(hotkey: str) -> tuple[int, int]:
    """Канонический вид → (modifiers, virtual_key) для RegisterHotKey.

    (0, 0), если разобрать не удалось.
    """
    mods = 0
    vk = 0
    for p in _split(hotkey):
        if p in MODIFIER_NAMES:
            mods |= MOD_MAP[f"<{'cmd' if p in ('meta', 'win') else p}>"]
        elif p in SPECIAL_KEY_VK:
            vk = SPECIAL_KEY_VK[p]
        elif len(p) == 1 and p.isalnum():
            vk = ord(p.upper())  # A=0x41…Z=0x5A, 0=0x30…9=0x39
    return mods, vk


def validate(hotkey: str) -> tuple[bool, str]:
    """(ok, сообщение об ошибке) — принимаем ровно то, что примет Windows.

    Требования: хотя бы один модификатор и ровно одна основная клавиша из
    известных. Сочетание без модификатора отклоняем сознательно: Windows его
    зарегистрирует, и клавиша перестанет работать во всех программах сразу.
    """
    parts = _split(hotkey)
    if not parts:
        return False, "сочетание пустое"

    mods: list[str] = []
    keys: list[str] = []
    for p in parts:
        if p in MODIFIER_NAMES:
            mods.append(p)
        elif p in SPECIAL_KEY_VK or (len(p) == 1 and p.isalnum()):
            keys.append(p)
        else:
            return False, f"неизвестная клавиша: {p}"

    if not mods:
        return False, "нужен хотя бы один модификатор: Ctrl, Shift, Alt или Win"
    if not keys:
        return False, "нужна основная клавиша помимо модификаторов"
    if len(keys) > 1:
        return False, "основная клавиша должна быть одна, а не " + ", ".join(keys)
    return True, ""
