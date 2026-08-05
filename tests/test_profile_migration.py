"""Переезд папки профиля со старого имени на новое (T-318).

Запуск:  python -m tests.test_profile_migration   (из корня репозитория)

Проверяется не «файлы оказались на месте», а поведение на отказе: переезд
делается переименованием и только им. Первая реализация держала в запасе
`shutil.move`, тот при занятом файле деградировал в «скопировать и удалить», и
на профиле с весами моделей это дало вторую копию на 2.4 ГБ рядом с первой.
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def fresh_profile(tmp: str):
    """Модуль profile, смотрящий в свежую временную папку."""
    os.environ["LOCALAPPDATA"] = tmp
    os.environ["XDG_DATA_HOME"] = tmp
    os.environ.pop("APPDATA", None)
    from saytype import profile

    return importlib.reload(profile)


def make_legacy(base: Path, profile) -> Path:
    legacy = base / profile.LEGACY_APP_DIR_NAME
    (legacy / "models").mkdir(parents=True)
    (legacy / "dictionary.txt").write_text("Фигма, промпт", encoding="utf-8")
    (legacy / "models" / "weights.bin").write_bytes(b"x" * 1024)
    return legacy


def test_rename_happens_once() -> None:
    print("переезд переименованием:")
    tmp = tempfile.mkdtemp(prefix="saytype-mig-")
    profile = fresh_profile(tmp)
    base = Path(tmp)
    legacy = make_legacy(base, profile)

    resolved = profile.resolve_profile_dir()
    check("отдан путь под новым именем", resolved.name == profile.APP_DIR_NAME, str(resolved))
    check("старой папки не осталось", not legacy.exists())
    check("словарь переехал",
          (resolved / "dictionary.txt").read_text(encoding="utf-8") == "Фигма, промпт")
    check("веса переехали", (resolved / "models" / "weights.bin").stat().st_size == 1024)

    again = profile.resolve_profile_dir()
    check("повторный вызов ничего не трогает", again == resolved)


def test_falls_back_when_rename_impossible() -> None:
    print("переименование не удалось:")
    tmp = tempfile.mkdtemp(prefix="saytype-mig-")
    profile = fresh_profile(tmp)
    base = Path(tmp)
    legacy = make_legacy(base, profile)

    original_rename = Path.rename

    def refuse(self, target):  # имитируем занятый файл внутри папки
        if self == legacy:
            raise OSError(32, "The process cannot access the file")
        return original_rename(self, target)

    Path.rename = refuse
    try:
        resolved = profile.resolve_profile_dir()
    finally:
        Path.rename = original_rename

    check("остаёмся на старой папке", resolved == legacy, str(resolved))
    check("данные на месте",
          (resolved / "dictionary.txt").read_text(encoding="utf-8") == "Фигма, промпт")
    check("копии рядом не появилось", not (base / profile.APP_DIR_NAME).exists())
    check("оригинал не тронут", legacy.exists())


def test_new_install_needs_no_migration() -> None:
    print("чистая установка:")
    tmp = tempfile.mkdtemp(prefix="saytype-mig-")
    profile = fresh_profile(tmp)
    resolved = profile.profile_dir()
    check("создана папка под новым именем", resolved.name == profile.APP_DIR_NAME)
    check("папка существует", resolved.is_dir())
    check("старой не создано", not (Path(tmp) / profile.LEGACY_APP_DIR_NAME).exists())


def main() -> int:
    for fn in (
        test_rename_happens_once,
        test_falls_back_when_rename_impossible,
        test_new_install_needs_no_migration,
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
