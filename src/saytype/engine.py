"""engine.py — единая точка загрузки модели faster-whisper (T-259).

До этого модуля логика жила в трёх копиях — в UI, в модуле записи созвона и в
batch-CLI. Здесь она одна.

Что умеет:
  * настройка CUDA DLL путей (Windows, pip-пакеты nvidia-*) — на импорте модуля,
    ДО того как кто-либо импортирует faster_whisper / ctranslate2;
  * пресеты моделей (tiny … large-v3) + «своя модель» (HF repo id или папка);
  * скачивание в подпапку `models` профиля пользователя, с прогрессом;
  * fallback-цепочка compute_type (float16 → int8_float16 → int8_float32 →
    float32 → cpu/int8);
  * кэш загруженной модели + выгрузка из VRAM (смена модели на лету);
  * валидация чужих моделей (CT2 vs safetensors) и учёт занятого места.

Из всего пакета модуль знает только про `profile` (где лежат веса) — ни PySide6,
ни UI-слоя здесь нет, поэтому его можно использовать отдельно для batch-расшифровки::

    from saytype import engine
    model = engine.load_model("small")

Тяжёлые импорты (faster_whisper, huggingface_hub) — ленивые, чтобы
`from saytype import engine` из UI-слоя оставался дешёвым.
"""

from __future__ import annotations

import gc
import os
import shutil
import site
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional


def log(msg: str) -> None:
    """Лог, который не может уронить вызывающий код (см. инцидент 2026-07-28:
    `print` в cp1251-stderr бросил UnicodeEncodeError внутри рабочего try и
    стоил транскрипта 45-минутного созвона)."""
    line = f"[engine] {msg}"
    try:
        print(line, file=sys.stderr, flush=True)
    except (UnicodeEncodeError, OSError, AttributeError, ValueError):
        try:
            print(line.encode("ascii", errors="replace").decode("ascii"),
                  file=sys.stderr, flush=True)
        except Exception:
            pass
    except Exception:
        pass


# === CUDA DLL (Windows) — должно выполниться ДО импорта faster_whisper ===
def setup_cuda_dll_paths() -> list[Path]:
    """Подключить папки с CUDA-DLL к DLL search (Windows).

    Два источника, в таком порядке:

    1. Докачанный слой в профиле пользователя (`%LOCALAPPDATA%\\saytype\\cuda\\bin`)
       — так CUDA приезжает к собранному приложению, внутри которого никакого
       site-packages нет вообще (см. `cuda_layer.py`).
    2. pip-пакеты nvidia-cublas-cu12 / nvidia-cudnn-cu12 в site-packages — путь
       разработчика, у которого всё поставлено через pip.

    CTranslate2 грузит эти DLL классическим Windows DLL search, который не
    смотрит ни туда, ни туда. Добавляем и через `os.add_dll_directory()`, и в PATH.
    Функция идемпотентна: `cuda_layer.activate()` зовёт её повторно после
    скачивания слоя.
    """
    if sys.platform != "win32":
        return []
    candidates: list[Path] = []
    seen: set[str] = set()

    try:
        from . import profile

        layer_bin = profile.cuda_dir() / "bin"
        if layer_bin.is_dir():
            seen.add(str(layer_bin).lower())
            candidates.append(layer_bin)
    except Exception:
        pass

    site_paths: list[str] = []
    try:
        site_paths.extend(site.getsitepackages())
    except Exception:
        pass
    try:
        site_paths.append(site.getusersitepackages())
    except Exception:
        pass
    for sp in site_paths:
        nvidia_root = Path(sp) / "nvidia"
        if not nvidia_root.exists():
            continue
        for sub in nvidia_root.iterdir():
            bin_path = sub / "bin"
            key = str(bin_path).lower()
            if bin_path.exists() and key not in seen:
                seen.add(key)
                candidates.append(bin_path)
    added: list[Path] = []
    extra_path: list[str] = []
    for path in candidates:
        try:
            os.add_dll_directory(str(path))
            added.append(path)
        except OSError:
            pass
        extra_path.append(str(path))
    if extra_path:
        current = os.environ.get("PATH", "")
        # не дублировать при повторном вызове (модуль может импортироваться дважды)
        missing = [p for p in extra_path if p not in current]
        if missing:
            os.environ["PATH"] = os.pathsep.join(missing) + os.pathsep + current
    return added


CUDA_DLL_DIRS = setup_cuda_dll_paths()

# === Пресеты ===
# Вес — float16-сборка CT2 с HuggingFace (то, что реально качается).
PRESETS: tuple[dict, ...] = (
    {"key": "tiny", "size_mb": 75, "note": "слабый CPU",
     "title": "Whisper Tiny", "desc": "Самая быстрая, качество низкое.",
     "accuracy": 1, "speed": 5},
    {"key": "base", "size_mb": 145, "note": "дефолт без NVIDIA",
     "title": "Whisper Base", "desc": "Быстрая, для машин без NVIDIA.",
     "accuracy": 2, "speed": 5},
    {"key": "small", "size_mb": 480, "note": "баланс, прежний дефолт",
     "title": "Whisper Small", "desc": "Баланс скорости и точности.",
     "accuracy": 3, "speed": 4},
    {"key": "medium", "size_mb": 1500, "note": "лучше, но медленно",
     "title": "Whisper Medium", "desc": "Точнее small, заметно медленнее.",
     "accuracy": 4, "speed": 2},
    {"key": "large-v3-turbo", "size_mb": 1600, "note": "~4× быстрее large-v3, WER −1-2%",
     "title": "Whisper Large v3 Turbo", "desc": "Почти как large-v3, но в разы быстрее.",
     "accuracy": 4, "speed": 3},
    {"key": "large-v3", "size_mb": 3100, "note": "максимум качества",
     "title": "Whisper Large v3", "desc": "Максимум качества, самая медленная.",
     "accuracy": 5, "speed": 1},
)


def preset_meta(key: str) -> dict:
    """Карточка пресета для UI (title/desc/accuracy/speed/size_mb) или заглушка."""
    for p in PRESETS:
        if p["key"] == key:
            return dict(p)
    return {"key": key, "size_mb": 0, "note": "", "title": key,
            "desc": "Своя модель.", "accuracy": 0, "speed": 0}
PRESET_KEYS = tuple(p["key"] for p in PRESETS)
CUSTOM_KEY = "custom"  # значение settings.model для «своей модели»
FALLBACK_SPEC = "small"  # что грузим, если настройки пустые/битые

GPU_COMPUTE_TYPES = ("float16", "int8_float16", "int8_float32", "float32")
CPU_COMPUTE_TYPE = "int8"

# Файлы CT2-модели (тот же список, что у faster_whisper.utils.download_model)
ALLOW_PATTERNS = [
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
]
REQUIRED_LOCAL_FILES = ("model.bin", "config.json", "tokenizer.json")

# Что обязано лежать в скачанном снапшоте, чтобы считать модель скачанной.
CACHE_REQUIRED_FILES = ("model.bin", "config.json", "tokenizer.json")

# А это — тот самый файл в 340 байт, без которого модель «скачана» и не работает:
# faster-whisper молча берёт дефолт 80 мел-бинов, WhisperModel поднимается
# успешно, а КАЖДАЯ транскрипция падает на «Invalid input features shape:
# expected (1, 128, 3000), got (1, 80, 3000)». Поймано на живом прогоне T-404:
# 1,6 ГБ весов turbo доехали, а этот json — нет.
# Требуем его не у всех: 128 бинов только у large-v3 и turbo-производных, а
# Systran/faster-whisper-tiny…medium его вообще не публикуют (80 бинов — дефолт),
# и общее требование объявило бы их всех «скачанными не полностью».
MEL128_MARKERS = ("large-v3", "turbo")
PREPROCESSOR_FILE = "preprocessor_config.json"


def required_snapshot_files(repo_id: str) -> tuple[str, ...]:
    """Обязательный состав снапшота для конкретного репозитория."""
    name = (repo_id or "").lower()
    if any(mark in name for mark in MEL128_MARKERS):
        return CACHE_REQUIRED_FILES + (PREPROCESSOR_FILE,)
    return CACHE_REQUIRED_FILES

CT2_CONVERT_HINT = (
    "Модель не в формате CTranslate2 (нет model.bin, лежат веса transformers).\n\n"
    "Сконвертируй её один раз:\n"
    "    pip install --user transformers[torch] ctranslate2\n"
    "    ct2-transformers-converter --model <repo-или-папка> "
    "--output_dir <папка-вывода> --copy_files tokenizer.json preprocessor_config.json "
    "--quantization float16\n\n"
    "Дока: https://opennmt.net/CTranslate2/guides/transformers.html#whisper"
)

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def preset_size_mb(key: str) -> int:
    for p in PRESETS:
        if p["key"] == key:
            return int(p["size_mb"])
    return 0


def preset_label(key: str) -> str:
    for p in PRESETS:
        mb = int(p["size_mb"])
        size = f"{mb} МБ" if mb < 1000 else f"{mb / 1000:.1f} ГБ".replace(".0 ", " ")
        if p["key"] == key:
            return f"{key} · {size} · {p['note']}"
    return key


# === Пути ===
def models_root() -> Path:
    """Подпапка `models` в профиле пользователя — своя папка вместо общего HF-кэша."""
    from . import profile

    return profile.profile_dir() / "models"


def default_hf_cache() -> Optional[Path]:
    """Общий HF-кэш (`~/.cache/huggingface/hub`) — там лежат модели, скачанные до T-259."""
    try:
        from huggingface_hub import constants

        return Path(constants.HF_HUB_CACHE)
    except Exception:
        return None


def is_local_path(spec: str) -> bool:
    """Спека — путь к папке на диске (а не размер/repo id)."""
    if not spec:
        return False
    if "\\" in spec or spec.count("/") > 1 or ":" in spec:
        return True
    return Path(spec).is_dir()


def repo_for_spec(spec: str) -> Optional[str]:
    """`small` → `Systran/faster-whisper-small`; repo id → как есть; папка → None."""
    if is_local_path(spec):
        return None
    if "/" in spec:
        return spec
    try:
        from faster_whisper.utils import _MODELS

        return _MODELS.get(spec)
    except Exception:
        return None


def spec_from_settings(settings: dict) -> str:
    """Из dict настроек (`model` + `custom_model`) — что грузить."""
    key = (settings or {}).get("model") or FALLBACK_SPEC
    if key == CUSTOM_KEY:
        custom = ((settings or {}).get("custom_model") or "").strip()
        return custom or FALLBACK_SPEC
    if key not in PRESET_KEYS:
        return FALLBACK_SPEC
    return key


def spec_display(spec: str) -> str:
    """Короткое имя для UI / frontmatter / статистики.

    Для папки внутри HF-кэша отдаём имя репозитория, а не sha снапшота:
    `…/models--Systran--faster-whisper-tiny/snapshots/d90ca5fe…` → `Systran/faster-whisper-tiny`.
    """
    if not spec:
        return "?"
    if not is_local_path(spec):
        return spec
    path = Path(spec)
    for part in path.parts:
        if part.startswith("models--"):
            return part[len("models--"):].replace("--", "/")
    return path.name or spec


# === Железо ===
def detect_gpu() -> Optional[dict]:
    """`{name, vram_mb}` через nvidia-smi или None, если NVIDIA не видно."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8, creationflags=_CREATE_NO_WINDOW,
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    first = proc.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in first.split(",")]
    if len(parts) < 2:
        return None
    try:
        return {"name": parts[0], "vram_mb": int(float(parts[1]))}
    except ValueError:
        return None


def cuda_runtime_available() -> bool:
    """Есть ли на машине cuBLAS/cuDNN, которые сможет открыть CTranslate2.

    Три источника: докачанный слой и pip-пакеты (их подключил
    `setup_cuda_dll_paths`) плюс системный CUDA Toolkit в PATH. Без этой
    проверки `device="auto"` на машине без CUDA перебирает четыре compute_type,
    каждый раз ловит исключение из нативного кода и сыпет в лог «GPU не
    запустился» — пугающе и незаслуженно (T-261).
    """
    if sys.platform != "win32":
        return True  # на Linux/macOS решает сам CTranslate2, там DLL search другой
    for path in CUDA_DLL_DIRS:
        try:
            if any(p.name.lower().startswith("cublas64") for p in Path(path).glob("*.dll")):
                return True
        except OSError:
            continue
    try:
        import ctypes.util

        return ctypes.util.find_library("cublas64_12") is not None
    except Exception:
        return False


def gpu_usable() -> bool:
    """И карта видна, и рантайм есть — только тогда есть смысл идти на CUDA."""
    return detect_gpu() is not None and cuda_runtime_available()


def recommended_preset() -> str:
    """Дефолт для ПЕРВОГО запуска: NVIDIA с 6+ ГБ → turbo, иначе base."""
    gpu = detect_gpu()
    if gpu and gpu.get("vram_mb", 0) >= 6000:
        return "large-v3-turbo"
    return "base"


# === Кэш на диске ===
def missing_snapshot_files(path: Path, repo_id: str = "") -> list[str]:
    """Какие обязательные файлы отсутствуют (или пусты) в папке снапшота.

    `repo_id` решает, спрашивать ли `preprocessor_config.json` (см.
    `required_snapshot_files`). Без него — только базовый состав.
    """
    out: list[str] = []
    for name in required_snapshot_files(repo_id):
        f = path / name
        try:
            if not f.exists() or f.stat().st_size == 0:
                out.append(name)
        except OSError:
            out.append(name)
    return out


def _snapshot_path(repo_id: str, cache_dir: Optional[Path]) -> Optional[Path]:
    """Путь к ПОЛНОМУ снапшоту в кэше или None (сеть не трогаем).

    `snapshot_download(local_files_only=True)` отдаёт папку и тогда, когда веса
    докачаться не успели (в blobs лежит `.incomplete`, в снапшоте — только
    json'ы). Ловили ровно это: оборванная закачка turbo считалась «скачано»,
    а `WhisperModel` падал с «Unable to open file 'model.bin'». Поэтому
    дополнительно проверяем состав снапшота — T-404 добавил в проверку остальные
    обязательные файлы (см. `CACHE_REQUIRED_FILES`): одного `model.bin` мало,
    без `preprocessor_config.json` модель грузится и не работает.
    """
    try:
        from huggingface_hub import snapshot_download

        path = Path(
            snapshot_download(
                repo_id,
                allow_patterns=ALLOW_PATTERNS,
                local_files_only=True,
                cache_dir=str(cache_dir) if cache_dir else None,
            )
        )
    except Exception:
        return None
    missing = missing_snapshot_files(path, repo_id)
    if missing:
        log(f"снапшот {repo_id} неполный, нет: {', '.join(missing)}")
        return None
    return path


def is_cached(spec: str) -> bool:
    """Модель уже на диске (наша папка или старый общий HF-кэш) — качать не надо."""
    if is_local_path(spec):
        return Path(spec).is_dir()
    repo = repo_for_spec(spec)
    if not repo:
        return False
    if _snapshot_path(repo, models_root()) is not None:
        return True
    return _snapshot_path(repo, default_hf_cache()) is not None


def _cache_dir_for(spec: str) -> Optional[Path]:
    """Какой `download_root` отдать WhisperModel.

    Наша папка, если модель там уже есть ИЛИ её нет нигде (свежая закачка идёт
    к нам). None (общий HF-кэш) — если модель лежит только там: не перекачивать
    те же 480 МБ ради переезда.
    """
    repo = repo_for_spec(spec)
    if not repo:
        return None
    root = models_root()
    if _snapshot_path(repo, root) is not None:
        return root
    if _snapshot_path(repo, default_hf_cache()) is not None:
        return None
    return root


# === Скачивание: ошибки, которые обязаны дойти до окна (T-404) ===
DOWNLOAD_ATTEMPTS = 3       # обрыв на полпути — норма под VPN; hub докачивает с места
RETRY_PAUSE_SEC = 3         # пауза перед повтором, растёт линейно с номером попытки

# Имена классов исключений, по которым узнаём «сеть, а не логика». Сверяем по
# имени, чтобы не тащить httpx/huggingface_hub в импорт ради одного isinstance.
_NETWORK_EXC_NAMES = (
    "ConnectError", "ConnectTimeout", "ConnectionError", "ReadError", "ReadTimeout",
    "WriteTimeout", "PoolTimeout", "ProxyError", "RemoteProtocolError", "SSLError",
    "SSLEOFError", "TimeoutError", "gaierror", "LocalEntryNotFoundError",
    "OfflineModeIsEnabled", "HfHubHTTPError", "socket.timeout",
)


class ModelDownloadError(RuntimeError):
    """Веса не скачались. Несёт человеческую причину и следующий шаг.

    До T-404 сюда прилетал сырой `LocalEntryNotFoundError` с абзацем
    английского текста про snapshot folder — и уходил в `_crash.log`, а
    пользователь смотрел на «идёт работа» вечно. Теперь у ошибки есть `hint`
    («что делать»), и оба поля показывает окно.
    """

    def __init__(self, message: str, *, hint: str = "", cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.hint = hint
        self.cause = cause

    def full_text(self) -> str:
        return f"{self}\n\n{self.hint}" if self.hint else str(self)


def _exc_chain(exc: BaseException) -> list[BaseException]:
    """Исключение и вся его цепочка причин (`__cause__` / `__context__`)."""
    out: list[BaseException] = []
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
    return out


def _is_network_error(exc: BaseException) -> bool:
    return any(
        type(e).__name__ in _NETWORK_EXC_NAMES
        or f"{type(e).__module__}.{type(e).__name__}" in _NETWORK_EXC_NAMES
        for e in _exc_chain(exc)
    )


# huggingface_hub 1.x держит ОДИН общий httpx.Client на процесс. Обрыв TLS на
# полпути умеет закрыть его насовсем, и все дальнейшие запросы падают с этим
# текстом — до перезапуска приложения. Поймано на живом прогоне T-404: 1,6 ГБ
# весов доехали, а маленький `preprocessor_config.json` уже не смог.
_CLOSED_CLIENT_MARK = "client has been closed"


def _is_retryable(exc: BaseException) -> bool:
    if _is_network_error(exc):
        return True
    return any(_CLOSED_CLIENT_MARK in str(e).lower() for e in _exc_chain(exc))


_download_proxy: str = ""


def set_download_proxy(value: str, logger: Callable[[str], None] = log) -> None:
    """Прокси, через который качать веса (T-404). Пусто — как настроена система.

    Зачем отдельная настройка, а не «не использовать системный прокси»: обход
    системного прокси маршрут не меняет — VPN-туннель перехватывает трафик по
    IP, и запрос всё равно уходит через ту же ноду, которая теряет
    huggingface.co. Помогает только явный адрес маршрута, который работает.
    """
    global _download_proxy
    value = (value or "").strip()
    if value == _download_proxy:
        return
    _download_proxy = value
    _reset_hub_session(logger)  # клиент httpx читает прокси при создании
    logger(f"прокси для скачивания моделей: {value or 'как в системе'}")


def download_proxy() -> str:
    return _download_proxy


class _DownloadRoute:
    """Контекст: на время сетевой работы hub'а подставить свой прокси в env.

    Через env, а не через `set_client_factory`, намеренно: фабрика клиента у
    hub'а несёт свои event-хуки (user-agent, телеметрия), и переписывать её
    заново — расходиться с библиотекой на каждом её обновлении. httpx читает
    `HTTPS_PROXY` при создании клиента, поэтому клиент сбрасываем на входе и
    на выходе.
    """

    _KEYS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")

    def __init__(self, logger: Callable[[str], None] = log) -> None:
        self._logger = logger
        self._saved: dict[str, Optional[str]] = {}

    def __enter__(self) -> "_DownloadRoute":
        if not _download_proxy:
            return self
        self._saved = {k: os.environ.get(k) for k in self._KEYS}
        for key in self._KEYS:
            os.environ.pop(key, None)
        os.environ["HTTPS_PROXY"] = _download_proxy
        os.environ["HTTP_PROXY"] = _download_proxy
        _reset_hub_session(self._logger)
        return self

    def __exit__(self, *_exc) -> None:
        if not self._saved:
            return
        for key, val in self._saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self._saved = {}
        _reset_hub_session(self._logger)


def _reset_hub_session(logger: Callable[[str], None] = log) -> None:
    """Выбросить общий httpx-клиент hub'а: следующий запрос создаст новый."""
    try:
        from huggingface_hub.utils import close_session

        close_session()
    except Exception as exc:  # старые версии hub клиент не кэшируют — и не надо
        logger(f"сброс HTTP-клиента hub пропущен: {exc.__class__.__name__}")


def system_proxy() -> str:
    """Прокси, через который пойдёт скачивание, или "" — как строка для человека.

    Смотрим ОБА источника, потому что httpx берёт оба: переменные окружения и
    настройку Windows в реестре. Второй источник — та самая яма T-404: Karing
    прописывает `127.0.0.1:3067` в WinINET, `HTTPS_PROXY` при этом пуст, и по
    переменным окружения проблему не видно вообще.
    """
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        val = (os.environ.get(var) or "").strip()
        if val:
            return f"{val} (переменная {var})"
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not int(enabled):
                return ""
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        server = (server or "").strip()
        return f"{server} (системный прокси Windows)" if server else ""
    except OSError:
        return ""


def _download_error(exc: BaseException, repo: str) -> ModelDownloadError:
    """Перевести исключение скачивания в текст, который не стыдно показать."""
    label = spec_display(repo)
    if _is_retryable(exc):
        proxy = system_proxy()
        where = f"через {proxy}" if proxy else "напрямую"
        detail = next(
            (f"{type(e).__name__}: {e}".strip() for e in reversed(_exc_chain(exc)) if str(e).strip()),
            type(exc).__name__,
        )
        return ModelDownloadError(
            f"Нет связи с huggingface.co — модель {label} не скачалась.",
            hint=(
                f"Запрос шёл {where}. Так бывает, когда VPN уводит huggingface.co "
                "в маршрут, который его теряет: остальные сайты работают, а этот "
                "нет. Два выхода — правило «прямое соединение» для huggingface.co "
                "и hf.co в VPN-клиенте либо поле «Прокси для загрузки моделей» в "
                f"настройках (адрес маршрута, который работает).\nПодробно: {detail[:300]}"
            ),
            cause=exc,
        )
    if type(exc).__name__ in ("OSError", "PermissionError") or "No space" in str(exc):
        return ModelDownloadError(
            f"Не удалось записать веса {label} на диск.",
            hint=f"Проверь место и права на {models_root()}.\nПодробно: {exc}",
            cause=exc,
        )
    return ModelDownloadError(
        f"Модель {label} не скачалась: {type(exc).__name__}: {exc}",
        hint="Попробуй ещё раз; если повторяется — смотри runtime\\saytype.log в профиле.",
        cause=exc,
    )


def _repo_cache_dir(repo_id: str, root: Path) -> Path:
    """Папка репозитория в HF-кэше: `models--автор--название`."""
    return root / ("models--" + repo_id.replace("/", "--"))


def _downloaded_bytes(repo_id: str, root: Path) -> int:
    """Сколько байт репо уже лежит на диске (для честного старта прогресса)."""
    path = _repo_cache_dir(repo_id, root)
    return _dir_size(path) if path.is_dir() else 0


def clean_partial_cache(repo_id: str, root: Optional[Path] = None,
                        logger: Callable[[str], None] = log) -> bool:
    """Снести огрызок кэша, в котором нет ни одного файла весов.

    Состояние с живой машины (T-404): в папке модели остался только
    `refs\\main` — ни `blobs`, ни `snapshots`. Такого на здоровой машине не
    бывает: `refs` появляется первым, а всё остальное не доехало. Огрызок
    оставляет ссылку на ревизию, снапшота которой нет, поэтому чистим целиком —
    качать всё равно с нуля. Частично скачанные веса (`blobs/*.incomplete`)
    НЕ трогаем: на них держится докачка с места.
    """
    root = root or models_root()
    path = _repo_cache_dir(repo_id, root)
    if not path.is_dir():
        return False
    blobs = path / "blobs"
    has_blobs = blobs.is_dir() and any(blobs.iterdir())
    snapshots = path / "snapshots"
    has_snapshots = snapshots.is_dir() and any(snapshots.iterdir())
    if has_blobs or has_snapshots:
        return False
    try:
        shutil.rmtree(path)
    except OSError as exc:
        logger(f"огрызок кэша {path.name} не удалился: {exc}")
        return False
    logger(f"снесён огрызок кэша без весов: {path.name}")
    return True


def _repo_total_bytes(repo_id: str) -> int:
    """Сумма размеров CT2-файлов репо (для процентов прогресса). 0 = неизвестно."""
    try:
        from huggingface_hub import HfApi

        info = HfApi().model_info(repo_id, files_metadata=True, timeout=20)
        total = 0
        for f in info.siblings or []:
            name = f.rfilename
            if name.endswith((".json", ".bin", ".txt")) and (
                name in ("config.json", "preprocessor_config.json", "model.bin", "tokenizer.json")
                or name.startswith("vocabulary.")
            ):
                total += int(f.size or 0)
        return total
    except Exception:
        return 0


_dl_lock = threading.Lock()
_dl_state: dict = {"cb": None, "done": 0, "total": 0}


def _make_progress_tqdm():
    """tqdm-подкласс, который скармливает байты нашему progress_cb."""
    try:
        from tqdm.auto import tqdm as _tqdm
    except Exception:
        return None

    class _ProgressTqdm(_tqdm):  # type: ignore[misc]
        def update(self, n=1):
            res = super().update(n)
            if getattr(self, "unit", "") == "B" and n:
                with _dl_lock:
                    cb = _dl_state["cb"]
                    if cb is None:
                        return res
                    _dl_state["done"] += int(n)
                    done, total = _dl_state["done"], _dl_state["total"]
                try:
                    cb(done, total)
                except Exception:
                    pass
            return res

    return _ProgressTqdm


def ensure_downloaded(
    spec: str,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    logger: Callable[[str], None] = log,
) -> None:
    """Скачать модель, если её нет ни в нашей папке, ни в общем HF-кэше.

    `progress_cb(done_bytes, total_bytes)` — total=0 значит «размер неизвестен»
    (показывай бесконечный прогресс). Локальные папки не качаются.

    T-404: обрыв не равен провалу. Флапающий маршрут (VPN, мобильная сеть)
    рвёт закачку на полпути, а hub умеет продолжить с места — поэтому
    `DOWNLOAD_ATTEMPTS` попыток с паузой, и только потом отказ. Любой отказ
    уходит наверх как `ModelDownloadError` с причиной и следующим шагом:
    вызывающий обязан показать это в окне, а не оставить «идёт работа».
    """
    if is_local_path(spec):
        if not Path(spec).is_dir():
            raise FileNotFoundError(f"Папка модели не найдена: {spec}")
        return
    repo = repo_for_spec(spec)
    if not repo:
        raise ValueError(f"Неизвестная модель: {spec}")
    if is_cached(spec):
        return

    root = models_root()
    root.mkdir(parents=True, exist_ok=True)
    # Огрызок от прошлой оборванной попытки (папка есть, весов нет) — снести до
    # старта, иначе он остаётся висеть после успеха и путает следующий запуск.
    clean_partial_cache(repo, root, logger)
    with _DownloadRoute(logger):
        _download_snapshot(repo, spec, root, progress_cb, logger)


def _download_snapshot(repo: str, spec: str, root: Path,
                       progress_cb: Optional[Callable[[int, int], None]],
                       logger: Callable[[str], None]) -> None:
    """Тело скачивания: метаданные, попытки с докачкой, проверка состава."""
    total = _repo_total_bytes(repo)
    logger(f"скачиваю {repo} → {root} ({total / 1e6:.0f} МБ)" if total else f"скачиваю {repo} → {root}")
    if _download_proxy:
        logger(f"маршрут скачивания: {_download_proxy}")

    from huggingface_hub import snapshot_download

    tqdm_cls = _make_progress_tqdm()
    kwargs = {"allow_patterns": ALLOW_PATTERNS, "cache_dir": str(root)}
    if tqdm_cls is not None and progress_cb is not None:
        kwargs["tqdm_class"] = tqdm_cls
    try:
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            # Прогресс начинаем не с нуля, а с того, что уже лежит на диске:
            # после обрыва докачка продолжает файл, и счётчик tqdm считает
            # только новые байты — иначе шкала после повтора врала бы вдвое.
            with _dl_lock:
                _dl_state.update({
                    "cb": progress_cb, "done": _downloaded_bytes(repo, root), "total": total,
                })
            try:
                snapshot_download(repo, **kwargs)
                break
            except Exception as exc:
                last = attempt >= DOWNLOAD_ATTEMPTS
                if last or not _is_retryable(exc):
                    raise _download_error(exc, repo) from exc
                logger(f"скачивание {repo}: попытка {attempt}/{DOWNLOAD_ATTEMPTS} "
                       f"оборвалась ({type(exc).__name__}) — продолжу с места")
                # Клиент мог остаться закрытым после обрыва — тогда повтор без
                # сброса упал бы мгновенно и «повторов» было бы три пустых.
                _reset_hub_session(logger)
                time.sleep(RETRY_PAUSE_SEC * attempt)
    finally:
        with _dl_lock:
            _dl_state.update({"cb": None, "done": 0, "total": 0})
    if not is_cached(spec):
        # snapshot_download отработал, но состав неполный: так выглядит и
        # выкачка одних json'ов, и потерянный `preprocessor_config.json` при
        # доехавших весах (см. `CACHE_REQUIRED_FILES`).
        snap_dir = _repo_cache_dir(repo, root) / "snapshots"
        missing: list[str] = []
        if snap_dir.is_dir():
            for rev in snap_dir.iterdir():
                missing = missing_snapshot_files(rev, repo)
                if missing:
                    break
        raise ModelDownloadError(
            f"Модель {spec_display(repo)} скачалась не полностью"
            + (f" — не хватает: {', '.join(missing)}." if missing else "."),
            hint="Нажми «Скачать» ещё раз — недостающие файлы докачаются, "
                 f"уже скачанное не пропадёт. Папка: {_repo_cache_dir(repo, root)}",
        )
    logger(f"скачано: {repo}")


# === Валидация чужих моделей ===
def validate_local_model(path_str: str) -> tuple[bool, str]:
    """Папка похожа на CT2-модель? (ok, сообщение для пользователя)."""
    path = Path(path_str.strip().strip('"'))
    if not path.exists():
        return False, f"Папка не найдена: {path}"
    if not path.is_dir():
        return False, f"Это файл, а нужна папка с моделью: {path}"
    names = {p.name for p in path.iterdir() if p.is_file()}
    missing = [f for f in REQUIRED_LOCAL_FILES if f not in names]
    if not missing:
        return True, f"CT2-модель найдена: {path.name}"
    if "model.bin" in missing and any(
        n.endswith(".safetensors") or n == "pytorch_model.bin" for n in names
    ):
        return False, CT2_CONVERT_HINT
    return False, "В папке нет файлов " + ", ".join(missing) + " — это не CT2-модель faster-whisper."


def validate_repo_id(repo_id: str, timeout: int = 20) -> tuple[bool, str]:
    """Репо существует и это CT2-модель? Сеть трогаем, скачивание не запускаем."""
    repo_id = repo_id.strip()
    if "/" not in repo_id:
        return False, "Ожидается id вида `автор/название` (например deepdml/faster-whisper-large-v3-turbo-ct2)."
    try:
        from huggingface_hub import HfApi
        from huggingface_hub.utils import (
            GatedRepoError,
            RepositoryNotFoundError,
        )
    except Exception as exc:
        return False, f"huggingface_hub недоступен: {exc}"
    try:
        # Тот же маршрут, что у скачивания (T-404): иначе «своя модель» не
        # проверяется на машине, где HF доступен только через свой прокси.
        with _DownloadRoute():
            info = HfApi().model_info(repo_id, timeout=timeout)
    except RepositoryNotFoundError:
        return False, f"Репозиторий `{repo_id}` не найден на HuggingFace (404). Проверь написание."
    except GatedRepoError:
        return False, f"Репозиторий `{repo_id}` закрытый (gated) — нужен токен HuggingFace."
    except Exception as exc:
        return False, f"Не смог проверить `{repo_id}`: {exc.__class__.__name__}: {exc}"
    names = {s.rfilename for s in (info.siblings or [])}
    if "model.bin" in names:
        return True, f"CT2-модель найдена: {repo_id}"
    if any(n.endswith(".safetensors") or n == "pytorch_model.bin" for n in names):
        return False, CT2_CONVERT_HINT
    return False, f"В `{repo_id}` нет model.bin — не похоже на CT2-модель faster-whisper."


def validate_spec(spec: str) -> tuple[bool, str]:
    """Универсальная проверка: папка / repo id / имя пресета."""
    spec = (spec or "").strip()
    if not spec:
        return False, "Пустое значение модели."
    if spec in PRESET_KEYS:
        return True, spec
    if is_local_path(spec):
        return validate_local_model(spec)
    return validate_repo_id(spec)


# === Учёт места на диске ===
def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def list_cached_models() -> list[dict]:
    """Все whisper-модели в кэшах: `{repo, path, bytes, where}` (наша папка + общий HF)."""
    out: list[dict] = []
    seen: set[str] = set()
    for where, root in (("saytype", models_root()), ("hf-cache", default_hf_cache())):
        if root is None or not root.exists():
            continue
        for d in root.glob("models--*"):
            if not d.is_dir() or "whisper" not in d.name.lower():
                continue
            repo = d.name[len("models--"):].replace("--", "/")
            key = str(d).lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({"repo": repo, "path": d, "bytes": _dir_size(d), "where": where})
    out.sort(key=lambda e: -e["bytes"])
    return out


def cached_total_bytes() -> int:
    return sum(e["bytes"] for e in list_cached_models())


def delete_cached_model(path: Path) -> tuple[bool, str]:
    try:
        shutil.rmtree(path)
        return True, f"удалено: {path.name}"
    except Exception as exc:
        return False, f"не удалось удалить {path.name}: {exc}"


def fmt_bytes(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f} ГБ"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.0f} МБ"
    return f"{n / 1000:.0f} КБ"


# === Загрузка / выгрузка ===
_model = None
_model_spec: str = ""
_model_device: str = ""
_model_compute_type: str = ""
_model_device_req: str = "auto"  # что просили: auto / cuda / cpu (влияет на попадание в кэш)
_load_lock = threading.RLock()

# T-268. CTranslate2 4.7.1 + CUDA: если модель хоть раз генерировала с sampling
# (temperature > 0 — тот самый anti-loop fallback faster-whisper), то РАЗРУШЕНИЕ
# объекта WhisperModel убивает процесс с 0xC0000409 (STATUS_STACK_BUFFER_OVERRUN,
# fail-fast из нативного кода — через try/except не ловится, трейсбека нет).
# Отсечки: на device=cpu не воспроизводится; при temperature=0.0 не
# воспроизводится; лёгкий файл + принудительная temperature=1.0 — крашится;
# пауза перед выгрузкой и предварительный CT2-unload не спасают.
# Обход: VRAM освобождаем штатным ctranslate2 `unload_model()`, а пустую
# оболочку кладём сюда и НИКОГДА не разрушаем — деструктор не запускается.
# Стоит это ~килобайты хоста на смену модели (веса уже освобождены).
_retired: list = []
RETIRED_SOFT_LIMIT = 8  # с этого числа смен подряд предупреждаем про VRAM


def hard_exit(code: int = 0):
    """Завершить процесс, не давая интерпретатору разрушать объекты моделей.

    Нужно по той же причине, что и `_retired` (T-268): на финализации Python
    разрушил бы отставленные оболочки — и процесс умер бы с 0xC0000409 уже
    ПОСЛЕ полезной работы (батч писал txt и «падал», UI мигал кодом краха при
    штатном выходе). Деструкторы не запускаются ни в одной из веток ниже;
    буферы потоков сбрасываем руками — после выхода это уже некому сделать.

    T-285: на Windows выходим через `TerminateProcess`, а не `os._exit`.
    `os._exit` зовёт CRT `_exit` → `ExitProcess`: тот убивает чужие потоки,
    но ПОТОМ дёргает DllMain(DLL_PROCESS_DETACH) у всех загруженных DLL — и
    Qt/CT2/аудио-обвязка иногда ловит там access violation по памяти, которую
    под ней уже разобрали. Штатный выход попадал в журнал Windows как APPCRASH
    (`pythonw.exe` / `Qt6Core.dll` / 0xC0000005) и был ложным следом при
    разборе настоящих аварий. `TerminateProcess` останавливает все потоки и
    завершает процесс без DLL_PROCESS_DETACH — код возврата сохраняется.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    if sys.platform == "win32":
        try:
            import ctypes

            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            k32.TerminateProcess(k32.GetCurrentProcess(), ctypes.c_uint(code & 0xFFFFFFFF))
        except Exception:
            pass  # не вышло — падаем на общий путь ниже
    os._exit(code)


def _release(m, device: str, logger: Callable[[str], None]) -> None:
    """Освободить память модели. На CUDA — не разрушая объект (см. `_retired`)."""
    if device != "cuda":
        return  # на CPU деструктор безопасен: просто отпускаем ссылку
    try:
        m.model.unload_model()  # ctranslate2.models.Whisper — отдаёт VRAM
    except Exception as exc:
        logger(f"CT2 unload_model не сработал ({exc.__class__.__name__}: {exc}) — "
               "модель остаётся в VRAM; разрушать её нельзя (T-268)")
    _retired.append(m)


def current_model():
    return _model


def current_spec() -> str:
    return _model_spec


def current_label() -> str:
    return spec_display(_model_spec) if _model_spec else ""


def current_device() -> str:
    return _model_device


def current_compute_type() -> str:
    return _model_compute_type


def is_loaded(spec: Optional[str] = None) -> bool:
    if _model is None:
        return False
    return True if spec is None else (_model_spec == spec)


def count_prompt_tokens(text: str) -> Optional[int]:
    """Длина `initial_prompt` в токенах текущей модели, либо None.

    None означает «модель ещё не в памяти» — тогда счётчик в настройках берёт
    оценку из `profile.estimate_prompt_tokens`. Своего токенизатора модуль не
    поднимает: у Whisper он одинаковый для всех мультиязычных чекпойнтов, но
    тянуть его отдельно ради счётчика — лишние секунды и лишняя зависимость.
    """
    m = _model
    if m is None or not text:
        return 0 if m is not None else None
    try:
        return len(m.hf_tokenizer.encode(text, add_special_tokens=False).ids)
    except Exception:
        return None


def unload_model(logger: Callable[[str], None] = log) -> None:
    """Выгрузить модель из VRAM.

    Память отдаёт `_release()` — явно, а не через деструктор (T-268), поэтому
    чужие ссылки на модель больше не мешают освобождению VRAM. Но работать по
    такой ссылке дальше нельзя: веса выгружены, следующий `transcribe` упадёт.
    """
    global _model, _model_spec, _model_device, _model_compute_type, _model_device_req
    with _load_lock:
        if _model is None:
            return
        prev, prev_device, m = _model_spec, _model_device, _model
        _model = None
        _model_spec = _model_device = _model_compute_type = ""
        _model_device_req = "auto"
        _release(m, prev_device, logger)
        del m
        gc.collect()
        logger(f"модель выгружена: {prev}"
               + (f" (оболочка отставлена, всего {len(_retired)})" if prev_device == "cuda" else ""))
        # Веса освобождены, но обвязка CT2 (поток, хендлы cublas/cudnn) остаётся
        # с оболочкой — замерено ~15 МБ VRAM на смену. Для пары смен за сессию
        # это ничто, для двух десятков — уже заметно.
        if len(_retired) >= RETIRED_SOFT_LIMIT:
            logger(f"[WARN] отставленных оболочек моделей: {len(_retired)} "
                   f"(~{15 * len(_retired)} МБ VRAM) — на долгой сессии стоит перезапустить приложение")


def load_model(
    spec: Optional[str] = None,
    *,
    device: str = "auto",
    logger: Callable[[str], None] = log,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    force_reload: bool = False,
):
    """Загрузить (или вернуть из кэша) WhisperModel по спеке.

    Спека — размер из PRESETS, HF repo id (`автор/модель`) или путь к папке с
    CT2-моделью. `device`: "auto" (GPU с откатом на CPU), "cuda" (только GPU,
    иначе RuntimeError), "cpu". Fallback-цепочка compute_type — здесь, в одном
    месте. Потокобезопасно (double-checked locking): preload-поток и ленивый
    загрузчик со стопа записи могут гнаться за одной моделью, а конструктор
    WhisperModel не reentrant.
    """
    global _model, _model_spec, _model_device, _model_compute_type, _model_device_req

    spec = (spec or _model_spec or FALLBACK_SPEC).strip()

    def _hit() -> bool:
        return _model is not None and _model_spec == spec and _model_device_req == device

    if _hit() and not force_reload:
        return _model

    with _load_lock:
        if _hit() and not force_reload:
            return _model
        if _model is not None:
            unload_model(logger=logger)

        ensure_downloaded(spec, progress_cb=progress_cb, logger=logger)
        target = str(Path(spec)) if is_local_path(spec) else spec
        cache_dir = _cache_dir_for(spec)
        root_kw = {"download_root": str(cache_dir)} if cache_dir else {}
        # Модель уже на диске → грузим офлайн. Иначе faster_whisper на каждом
        # старте ходит на HF сверять ревизию: лишние секунды при живой сети и
        # зависание при мёртвой (или под VPN — ловили при скачивании turbo).
        if not is_local_path(spec):
            root_kw["local_files_only"] = True

        from faster_whisper import WhisperModel

        last_err: Optional[Exception] = None
        if device == "auto" and not cuda_runtime_available():
            # Машина без CUDA-рантайма (собранная версия без докачанного слоя).
            # Молча идём на CPU: перебирать GPU-варианты тут нечего, а четыре
            # исключения подряд в логе выглядят как поломка.
            logger("CUDA-рантайм не найден — работаем на CPU")
            device = "cpu"
        if device in ("auto", "cuda"):
            for ct in GPU_COMPUTE_TYPES:
                try:
                    m = WhisperModel(target, device="cuda", compute_type=ct, **root_kw)
                except Exception as exc:
                    last_err = exc
                    continue
                _model, _model_spec, _model_device_req = m, spec, device
                _model_device, _model_compute_type = "cuda", ct
                logger(f"model loaded: {spec_display(spec)} · cuda/{ct}")
                return m
            if device == "cuda":
                raise RuntimeError(f"GPU не запустился ни на одном compute_type: {last_err}")
            logger(f"GPU не запустился ({last_err}), fallback на CPU")

        m = WhisperModel(target, device="cpu", compute_type=CPU_COMPUTE_TYPE, **root_kw)
        _model, _model_spec, _model_device_req = m, spec, device
        _model_device, _model_compute_type = "cpu", CPU_COMPUTE_TYPE
        logger(f"model loaded: {spec_display(spec)} · cpu/{CPU_COMPUTE_TYPE}")
        return m
