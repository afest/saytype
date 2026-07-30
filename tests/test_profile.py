"""Профиль пользователя: словарь, замены, пути. Без модели, без Qt, без сети.

Запуск:  python -m tests.test_profile   (из корня репозитория)
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Профиль резолвится по переменным окружения — уводим его во временную папку,
# чтобы тест не трогал настоящие настройки того, кто его запускает.
_TMP = tempfile.mkdtemp(prefix="iwhisper-test-")
os.environ["LOCALAPPDATA"] = _TMP
os.environ["XDG_DATA_HOME"] = _TMP
os.environ.pop("APPDATA", None)  # чтобы не подхватилась миграция старых настроек

from iwhisper import profile  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def test_defaults_are_empty() -> None:
    print("профиль из коробки пуст:")
    check("словарь пуст", profile.load_dictionary() == "")
    check("правил нет", profile.load_replacement_rules() == [])
    body, tail = profile.compile_rules()
    check("скомпилированных правил нет", body == [] and tail == [])


def test_dictionary_roundtrip() -> None:
    print("словарь:")
    profile.save_dictionary("Фигма: Фигмы, Фигме.\n# коммент\nkanban, оркестратор.")
    loaded = profile.load_dictionary()
    check("комментарии выброшены", "#" not in loaded, repr(loaded))
    check("строки склеены в одну", "\n" not in loaded, repr(loaded))
    check("содержимое на месте", "Фигме" in loaded and "kanban" in loaded, repr(loaded))
    profile.save_dictionary("")
    check("пустой словарь читается как пустой", profile.load_dictionary() == "")


def test_replacements_roundtrip() -> None:
    print("замены:")
    rules = [
        {"pattern": r"\bкамбан\b", "replacement": "kanban", "tail_only": False},
        {"pattern": r"(?<=[.!?])\s+Удачи", "replacement": "", "tail_only": True},
        {"pattern": "   ", "replacement": "x", "tail_only": False},
    ]
    profile.save_replacement_rules(rules)
    loaded = profile.load_replacement_rules()
    check("пустой шаблон не сохранён", len(loaded) == 2, f"len={len(loaded)}")
    body, tail = profile.compile_rules(loaded)
    check("правила разложены по группам", len(body) == 1 and len(tail) == 1)
    check("хвостовое заанкерено на конец", tail[0][0].pattern.endswith("$"),
          tail[0][0].pattern)


def test_broken_rules_do_not_kill() -> None:
    print("устойчивость к битым данным:")
    profile.replacements_path().write_text("{ это не json", encoding="utf-8")
    check("битый json → пустой список", profile.load_replacement_rules() == [])

    body, tail = profile.compile_rules([
        {"pattern": "[", "replacement": "x", "tail_only": False},          # не компилируется
        {"pattern": r"\bок\b", "replacement": "ОК", "tail_only": False},
    ])
    check("битая регулярка пропущена, живая осталась", len(body) == 1 and not tail)

    ok, _ = profile.validate_pattern("[")
    check("validate_pattern ловит битую", not ok)
    ok, _ = profile.validate_pattern(r"\bок\b")
    check("validate_pattern пропускает живую", ok)


def test_backreference_replacement() -> None:
    print("ссылки на группы в замене:")
    body, _ = profile.compile_rules([
        {"pattern": r"\bпром[тТ]([уаеоё]|ом|ы)?\b", "replacement": r"промпт\1",
         "tail_only": False},
    ])
    regex, repl = body[0]
    check("окончание сохраняется", regex.sub(repl, "промту") == "промпту")
    check("группа не участвовала → пусто", regex.sub(repl, "промт") == "промпт")


def test_paths_are_relative_to_profile() -> None:
    print("пути:")
    root = profile.profile_dir()
    check("профиль во временной папке теста", str(root).startswith(_TMP), str(root))
    for name, path in (
        ("settings.ini", profile.settings_file()),
        ("dictionary.txt", profile.dictionary_path()),
        ("replacements.json", profile.replacements_path()),
        ("history", profile.default_history_dir()),
        ("icons", profile.icons_dir()),
        ("runtime", profile.runtime_dir()),
    ):
        check(f"{name} внутри профиля", str(path).startswith(str(root)), str(path))


def test_token_estimate() -> None:
    print("оценка бюджета промпта:")
    check("пустая строка → 0", profile.estimate_prompt_tokens("") == 0)
    n = profile.estimate_prompt_tokens("Фигма: Фигмы, Фигме, Фигму, Фигмой.")
    check("кириллица считается", 5 < n < 40, f"n={n}")
    check("бюджет объявлен", profile.PROMPT_TOKEN_BUDGET == 223)


def main() -> int:
    for fn in (
        test_defaults_are_empty,
        test_dictionary_roundtrip,
        test_replacements_roundtrip,
        test_broken_rules_do_not_kill,
        test_backreference_replacement,
        test_paths_are_relative_to_profile,
        test_token_estimate,
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
