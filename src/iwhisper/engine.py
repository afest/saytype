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

    from iwhisper import engine
    model = engine.load_model("small")

Тяжёлые импорты (faster_whisper, huggingface_hub) — ленивые, чтобы
`from iwhisper import engine` из UI-слоя оставался дешёвым.
"""

from __future__ import annotations

import gc
import os
import shutil
import site
import subprocess
import sys
import threading
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

    1. Докачанный слой в профиле пользователя (`%LOCALAPPDATA%\\iwhisper\\cuda\\bin`)
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
def _snapshot_path(repo_id: str, cache_dir: Optional[Path]) -> Optional[Path]:
    """Путь к ПОЛНОМУ снапшоту в кэше или None (сеть не трогаем).

    `snapshot_download(local_files_only=True)` отдаёт папку и тогда, когда веса
    докачаться не успели (в blobs лежит `.incomplete`, в снапшоте — только
    json'ы). Ловили ровно это: оборванная закачка turbo считалась «скачано»,
    а `WhisperModel` падал с «Unable to open file 'model.bin'». Поэтому
    дополнительно проверяем сам `model.bin`.
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
    weights = path / "model.bin"
    if not weights.exists() or weights.stat().st_size == 0:
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
    total = _repo_total_bytes(repo)
    logger(f"скачиваю {repo} → {root} ({total / 1e6:.0f} МБ)" if total else f"скачиваю {repo} → {root}")

    from huggingface_hub import snapshot_download

    tqdm_cls = _make_progress_tqdm()
    with _dl_lock:
        _dl_state.update({"cb": progress_cb, "done": 0, "total": total})
    try:
        kwargs = {"allow_patterns": ALLOW_PATTERNS, "cache_dir": str(root)}
        if tqdm_cls is not None and progress_cb is not None:
            kwargs["tqdm_class"] = tqdm_cls
        snapshot_download(repo, **kwargs)
    finally:
        with _dl_lock:
            _dl_state.update({"cb": None, "done": 0, "total": 0})
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
    for where, root in (("iwhisper", models_root()), ("hf-cache", default_hf_cache())):
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
