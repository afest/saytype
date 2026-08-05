"""Разбор и валидация hotkey-строк. Без Qt, без модели, без сети.

Запуск:  python -m tests.test_hotkeys   (из корня репозитория)

Тесты появились вместе с отказом от pynput (T-318): раньше проверку сочетания
делала библиотека, и своего покрытия у этого места не было.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from saytype import hotkeys  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def test_canonical_form() -> None:
    print("канонический вид:")
    for src in ("Ctrl+Shift+Q", "ctrl+shift+q", "<ctrl>+<shift>+q", "CTRL + SHIFT + Q"):
        got = hotkeys.to_canonical(src)
        check(f"{src!r} → <ctrl>+<shift>+q", got == "<ctrl>+<shift>+q", got)
    check("meta и win сводятся к cmd",
          hotkeys.to_canonical("Meta+E") == "<cmd>+e"
          and hotkeys.to_canonical("Win+E") == "<cmd>+e")
    check("пустая строка остаётся пустой", hotkeys.to_canonical("") == "")


def test_display_form() -> None:
    print("вид для интерфейса:")
    check("модификаторы с большой буквы",
          hotkeys.to_qt("<ctrl>+<shift>+q") == "Ctrl+Shift+Q",
          hotkeys.to_qt("<ctrl>+<shift>+q"))
    check("cmd показывается как Win",
          hotkeys.to_qt("<cmd>+e") == "Win+E", hotkeys.to_qt("<cmd>+e"))
    check("именованная клавиша не ломается в одну букву",
          hotkeys.to_qt("<ctrl>+<alt>+f5") == "Ctrl+Alt+F5",
          hotkeys.to_qt("<ctrl>+<alt>+f5"))
    check("туда-обратно без потерь",
          hotkeys.to_canonical(hotkeys.to_qt("<ctrl>+<shift>+q")) == "<ctrl>+<shift>+q")


def test_mod_vk() -> None:
    print("перевод в (modifiers, virtual key):")
    mods, vk = hotkeys.to_mod_vk("<ctrl>+<shift>+q")
    check("ctrl+shift", mods == hotkeys.MOD_CTRL | hotkeys.MOD_SHIFT, str(mods))
    check("буква Q → 0x51", vk == 0x51, hex(vk))
    check("цифра", hotkeys.to_mod_vk("<alt>+1")[1] == 0x31)
    check("F5 как имя", hotkeys.to_mod_vk("<ctrl>+f5")[1] == 0x74)
    check("space как имя", hotkeys.to_mod_vk("<ctrl>+space")[1] == 0x20)
    check("win-клавиша", hotkeys.to_mod_vk("<cmd>+e")[0] == hotkeys.MOD_WIN)
    check("мусор → (0, 0)", hotkeys.to_mod_vk("абракадабра") == (0, 0))
    check("пусто → (0, 0)", hotkeys.to_mod_vk("") == (0, 0))


def test_validate_accepts() -> None:
    print("валидация пропускает рабочие сочетания:")
    for h in ("<ctrl>+<shift>+q", "<ctrl>+<alt>+f5", "<alt>+space", "<cmd>+<shift>+1"):
        ok, msg = hotkeys.validate(h)
        check(f"{h} принято", ok, msg)


def test_validate_rejects() -> None:
    print("валидация отсекает нерабочее:")
    cases = [
        ("", "пустая строка"),
        ("<ctrl>+<shift>", "только модификаторы"),
        ("q", "без модификатора"),
        ("<ctrl>+q+w", "две основные клавиши"),
        ("<ctrl>+абв", "неизвестная клавиша"),
    ]
    for value, name in cases:
        ok, msg = hotkeys.validate(value)
        check(f"{name} отклонено", not ok and bool(msg), f"ok={ok} msg={msg!r}")


def test_no_pynput() -> None:
    print("зависимости больше нет:")
    source = (Path(__file__).resolve().parent.parent / "src" / "saytype").rglob("*.py")
    hits = []
    for path in source:
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("import pynput", "from pynput")):
                hits.append(f"{path.name}:{i}")
    check("в пакете нет импорта pynput", not hits, ", ".join(hits))


def main() -> int:
    for fn in (
        test_canonical_form,
        test_display_form,
        test_mod_vk,
        test_validate_accepts,
        test_validate_rejects,
        test_no_pynput,
    ):
        fn()
    print()
    if FAILED:
        print(f"ПРОВАЛЕНО: {len(FAILED)} — {', '.join(FAILED)}")
        return 1
    print("все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
