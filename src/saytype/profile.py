"""profile.py — где приложение хранит данные пользователя.

Один модуль отвечает на вопрос «куда писать», чтобы в остальном коде не было ни
одного пути с буквой диска. Всё лежит под ``%LOCALAPPDATA%\\saytype`` (Windows)
или ``~/.local/share/saytype`` (остальные платформы):

    settings.ini        настройки приложения (QSettings IniFormat)
    dictionary.txt      словарь: слова, имена и термины, которые модель путает
    replacements.json   правила замен в готовом тексте
    models/             скачанные веса (владелец — engine.py)
    cuda/               CUDA-рантайм, докачанный по требованию (владелец — cuda_layer.py)
    icons/              иконки трея, генерируются при первом запуске
    runtime/            маркер живой сессии и журнал аварий
    history/            история записей по умолчанию (меняется в настройках)

Словарь и замены — **данные, а не константы**: из коробки пусты, наполняет
пользователь. Модуль не тянет ни PySide6, ни faster-whisper: его импортируют и
UI-слой, и CLI.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

APP_DIR_NAME = "saytype"

# Как папка называлась до переименования продукта (T-318). Переезд делается
# один раз и переименованием папки целиком: скачанные веса и CUDA-слой весят
# гигабайты, а переименование в пределах тома — мгновенная операция.
LEGACY_APP_DIR_NAME = "iwhisper"

# Бюджет `initial_prompt` у Whisper — max_length // 2 - 1 токенов. Лишнее
# отрезается МОЛЧА и С НАЧАЛА строки: первые слова словаря просто перестают
# работать, без ошибки и без следа в логе.
PROMPT_TOKEN_BUDGET = 223

# Подпапка истории под транскрипты созвонов. Регистр как был: на Windows он не
# влияет на разрешение пути, а у существующих пользователей папка уже создана.
CALLS_SUBDIR = "Calls"

# T-389: подпапка истории под hi-fi надиктовки для датасета клона голоса.
# Отдельная папка, а не флаг у файлов в корне: `rotate_history()` сканирует
# только корень истории — значит эти записи не удаляются вообще, и весь
# эксперимент сносится одной папкой, не задевая обычный архив надиктовок.
HIFI_SUBDIR = "_hi-fi-voice-dataset"

_REPLACEMENTS_VERSION = 1


def _profile_base() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base)


def resolve_profile_dir() -> Path:
    """Где лежат данные — с учётом переезда со старого имени папки.

    Переезжаем один раз: если папки под новым именем ещё нет, а старая есть —
    переименовываем её. Не получилось (файл занят, права, антивирус) —
    **остаёмся на старой** и пробуем в следующий раз. Создать вместо неё пустую
    новую значило бы, что словарь, замены и гигабайты скачанных весов молча
    исчезли, хотя лежат рядом.

    Только `rename`, никакого `shutil.move` в запасе. Move при занятом файле
    внутри деградирует в «скопировать целиком и удалить оригинал», и на профиле
    с весами моделей это молча съедает лишние гигабайты, а оригинал остаётся:
    старая копия и новая начинают расходиться. Переименование либо срабатывает
    целиком, либо не делает ничего — здесь нужно ровно это.
    """
    base = _profile_base()
    target = base / APP_DIR_NAME
    if target.exists():
        return target
    legacy = base / LEGACY_APP_DIR_NAME
    if legacy.is_dir():
        try:
            legacy.rename(target)
        except OSError:
            return legacy
    return target


def profile_dir() -> Path:
    """Корень пользовательских данных. Создаётся при первом обращении."""
    path = resolve_profile_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_file() -> Path:
    return profile_dir() / "settings.ini"


def default_history_dir() -> Path:
    """Куда писать записи, пока пользователь не выбрал свою папку."""
    return profile_dir() / "history"


def icons_dir() -> Path:
    """Иконки трея. Генерируются кодом, поэтому лежат в профиле, а не в пакете:
    установленный пакет может быть в папке без права записи."""
    path = profile_dir() / "icons"
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_dir() -> Path:
    """Маркер живой сессии и журнал аварий — по той же причине не в пакете."""
    path = profile_dir() / "runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sounds_dir() -> Path:
    """T-355: сгенерированные звуковые сигналы старт/стоп. Как icons_dir — код
    создаёт их сам, QSoundEffect нужен реальный файл (не байты в памяти)."""
    path = profile_dir() / "sounds"
    path.mkdir(parents=True, exist_ok=True)
    return path


# === Заметки (T-352) ===
# Отдельная сущность с другим сроком жизни, чем история диктовок: не ротируются,
# живут до ручного удаления. Файлами, не одним JSON (T-350, оценка новых фич):
# правится чем угодно снаружи, битая заметка не уносит остальные. Класть в
# history/ нельзя — rotate_history() сканит её корень по `*.wav` и не должен
# видеть соседей.

def notes_dir() -> Path:
    path = profile_dir() / "notes"
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_note_path() -> Path:
    """Свободное имя файла заметки — и сразу занятое, пустым файлом.

    Одного времени, даже с микросекундами, для уникальности не хватает:
    системный таймер Windows тикает раз в ~15 мс, и `datetime.now()` внутри
    тика возвращает то же значение до последней цифры (замер: 2000 вызовов
    подряд → 5 разных имён). Две заметки, рождённые в одном тике, получали
    один путь, и вторая запись молча затирала первую вместе с её `.meta.json`.

    Поэтому имя не угадывается, а занимается: `O_CREAT | O_EXCL` создаёт файл
    и падает, если он уже есть — проверка и захват в одной операции, между
    ними нельзя вклиниться. Занято — пробуем следующий суффикс `-2`, `-3`.
    Точек в имени нет, поэтому `with_suffix` в `note_meta_path` по-прежнему
    даёт правильный sidecar.
    """
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S-%f")
    base = notes_dir()
    for n in range(1, 1000):
        path = base / (f"{ts}.md" if n == 1 else f"{ts}-{n}.md")
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            continue
        except OSError:
            # Диск недоступен или права — тем же способом падёт и write_note,
            # который сообщит об этом вызывающему. Отдаём путь как раньше.
            return path
        return path
    # Тысяча заметок в одну микросекунду — такого не бывает, но молчать нельзя.
    raise OSError(f"не удалось подобрать свободное имя заметки в {base}")


# Откуда взялся текст заметки (T-351): надиктовал, загрузил файл, набрал руками.
# Живёт в sidecar-файле рядом с заметкой, а не во frontmatter: заметку человек
# правит в обычном текстовом поле, и служебная шапка в ней мешалась бы.
NOTE_SOURCE_MANUAL = "manual"
NOTE_SOURCE_DICTATION = "dictation"
NOTE_SOURCE_IMPORT = "import"


def note_meta_path(path: Path) -> Path:
    return path.with_suffix(".meta.json")


def read_note_meta(path: Path) -> dict:
    """Метка источника заметки. Нет файла (заметки до T-351) — «набрано руками»."""
    meta_path = note_meta_path(path)
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"source": NOTE_SOURCE_MANUAL, "source_name": ""}
    source = str(data.get("source", NOTE_SOURCE_MANUAL))
    if source not in (NOTE_SOURCE_MANUAL, NOTE_SOURCE_DICTATION, NOTE_SOURCE_IMPORT):
        source = NOTE_SOURCE_MANUAL
    return {"source": source, "source_name": str(data.get("source_name", ""))}


def write_note_meta(path: Path, *, source: str, source_name: str = "") -> None:
    """Сам аудиофайл не храним — только имя, чтобы через месяц было понятно,
    из чего эта расшифровка (T-351, прямое пожелание владельца продукта)."""
    try:
        note_meta_path(path).write_text(
            json.dumps({"source": source, "source_name": source_name}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def list_notes() -> list:
    """Заметки, новые сверху. Первая строка файла — заголовок (пусто → «Без названия»)."""
    out = []
    for path in notes_dir().glob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
            mtime = path.stat().st_mtime
        except OSError:
            continue
        lines = text.splitlines()
        title = (lines[0].strip() if lines else "") or "Без названия"
        preview = " ".join(lines[1:]).strip()[:120]
        meta = read_note_meta(path)
        out.append({
            "path": path, "title": title, "preview": preview, "mtime": mtime,
            "source": meta["source"], "source_name": meta["source_name"],
        })
    out.sort(key=lambda n: n["mtime"], reverse=True)
    return out


def read_note(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_note(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass


def delete_note(path: Path) -> None:
    for target in (path, note_meta_path(path)):
        try:
            target.unlink()
        except OSError:
            pass


def resource_dir() -> Path:
    """Ресурсы, которые едут вместе с кодом (эталонная .ico для splash и ярлыка).

    В собранном виде PyInstaller распаковывает их в ``sys._MEIPASS``; при запуске
    из исходников это папка ``assets/`` репозитория. Путь может не существовать
    (установка пакета через pip ассеты не копирует) — вызывающий проверяет сам.
    """
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled) / "assets"
    return Path(__file__).resolve().parents[2] / "assets"


def licenses_dir() -> Path:
    """Тексты лицензий, которые едут вместе с поставкой (T-318).

    Кладутся в сборку через `datas` в спеке, поэтому в замороженном виде лежат
    рядом с остальными ресурсами, а при запуске из исходников — в папке
    ``licenses/`` репозитория. Путь может не существовать (установка пакета
    через pip их не копирует) — вызывающий проверяет сам.
    """
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled) / "licenses"
    return Path(__file__).resolve().parents[2] / "licenses"


def cuda_dir() -> Path:
    """CUDA-рантайм, докачиваемый по требованию (~450 МБ, см. cuda_layer.py).

    В профиле, а не в папке приложения: установленное в Program Files приложение
    не пишет в свою папку, а слой у каждого пользователя качается отдельно.
    Папку НЕ создаём: её наличие — часть ответа на «слой уже стоит?».
    """
    return profile_dir() / "cuda"


# === Миграция с прежнего расположения настроек ===

_LEGACY_SETTINGS_SUBDIR = "faster-whisper-ui"


def legacy_settings_file() -> "Path | None":
    """``%APPDATA%\\faster-whisper-ui\\settings.ini`` — где настройки жили до
    того, как у приложения появилось имя. Возвращает путь, если файл есть."""
    if sys.platform != "win32":
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    candidate = Path(appdata) / _LEGACY_SETTINGS_SUBDIR / "settings.ini"
    return candidate if candidate.exists() else None


def migrate_legacy_settings() -> "str | None":
    """Скопировать старый settings.ini в профиль, если своего ещё нет.

    Копия, а не перемещение: пока новая версия не подтверждена, старый файл
    остаётся рабочим откатом. Возвращает описание для лога либо None.
    """
    target = settings_file()
    if target.exists():
        return None
    legacy = legacy_settings_file()
    if legacy is None:
        return None
    try:
        shutil.copy2(legacy, target)
    except OSError as exc:
        return f"миграция настроек не удалась: {exc}"
    return f"настройки перенесены из {legacy}"


# === Словарь (initial_prompt) ===

def dictionary_path() -> Path:
    return profile_dir() / "dictionary.txt"


def load_dictionary() -> str:
    """Словарь пользователя одной строкой. Пусто — если файла нет.

    Файл читается как обычный текст: переводы строк схлопываются в пробелы,
    потому что Whisper принимает `initial_prompt` одной строкой, а человеку
    удобнее держать словарь в несколько строк. Строки, начинающиеся с `#`, —
    комментарии.
    """
    path = dictionary_path()
    if not path.exists():
        return ""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = [ln.strip() for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    return " ".join(lines).strip()


def save_dictionary(text: str) -> None:
    path = dictionary_path()
    try:
        path.write_text((text or "").strip() + "\n", encoding="utf-8")
    except OSError:
        pass


def estimate_prompt_tokens(text: str) -> int:
    """Оценка длины промпта в токенах без загрузки модели.

    Нужна только для счётчика в настройках, когда модель ещё не в памяти.
    Точное число даёт `engine.count_prompt_tokens` через токенизатор модели;
    здесь — приближение по эмпирике мультиязычного BPE Whisper: на русском
    ~2.5 знака на токен, на латинице и цифрах ~4.
    """
    if not text:
        return 0
    cyrillic = sum(1 for ch in text if "Ѐ" <= ch <= "ӿ")
    other = len(text) - cyrillic
    return int(cyrillic / 2.5 + other / 4.0 + 0.5)


# === Замены в готовом тексте ===

def replacements_path() -> Path:
    return profile_dir() / "replacements.json"


def default_replacement_rules() -> list:
    """Из коробки — пусто. Приложение не знает, какие слова путает именно у вас."""
    return []


def load_replacement_rules() -> list:
    """Список правил ``{"pattern", "replacement", "tail_only"}``.

    ``tail_only`` — правило применяется только к концу текста (`$`-анкер
    дописывается автоматически). Битый файл не должен ронять транскрипцию,
    поэтому любая ошибка чтения = пустой список.
    """
    path = replacements_path()
    if not path.exists():
        return default_replacement_rules()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default_replacement_rules()
    raw = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return default_replacement_rules()
    rules = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern", "")).strip()
        if not pattern:
            continue
        rules.append({
            "pattern": pattern,
            "replacement": str(item.get("replacement", "")),
            "tail_only": bool(item.get("tail_only", False)),
        })
    return rules


def save_replacement_rules(rules: list) -> None:
    payload = {
        "version": _REPLACEMENTS_VERSION,
        "rules": [
            {
                "pattern": r.get("pattern", ""),
                "replacement": r.get("replacement", ""),
                "tail_only": bool(r.get("tail_only", False)),
            }
            for r in rules
            if str(r.get("pattern", "")).strip()
        ],
    }
    try:
        replacements_path().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def validate_pattern(pattern: str) -> "tuple[bool, str]":
    """Компилируется ли регулярка. Сообщение — для показа в редакторе."""
    if not pattern.strip():
        return False, "пустой шаблон"
    try:
        re.compile(pattern)
    except re.error as exc:
        return False, str(exc)
    return True, ""


def compile_rules(rules: "list | None" = None) -> "tuple[list, list]":
    """Скомпилировать правила в две группы: обычные и хвостовые.

    Возвращает ``(body, tail)``, где элемент — ``(compiled_regex, replacement)``.
    Невалидные шаблоны пропускаются молча: пользователь мог править файл руками,
    и одна опечатка не должна оставить его без транскрипта.
    """
    if rules is None:
        rules = load_replacement_rules()
    body, tail = [], []
    for rule in rules:
        pattern = rule.get("pattern", "")
        replacement = rule.get("replacement", "")
        target = tail if rule.get("tail_only") else body
        # Хвостовое правило анкерится на конец текста, если автор не сделал это сам.
        if rule.get("tail_only") and not pattern.rstrip().endswith("$"):
            pattern = pattern + r"\s*$"
        try:
            target.append((re.compile(pattern), replacement))
        except re.error:
            continue
    return body, tail
