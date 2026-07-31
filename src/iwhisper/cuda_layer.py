"""cuda_layer.py — CUDA-рантайм, который приезжает по требованию (T-261).

Зачем отдельный слой. cuBLAS и cuDNN весят ~450 МБ — больше, чем всё остальное
приложение вместе взятое. Класть их в установщик значит раздать полгигабайта
всем, включая тех, у кого нет NVIDIA и кому эти файлы не пригодятся никогда.
Поэтому базовая сборка идёт с CPU-рантаймом CTranslate2, а CUDA докачивается
отдельным архивом при первом запуске — и только если в машине нашлась NVIDIA.

Куда кладём: ``%LOCALAPPDATA%\\iwhisper\\cuda\\bin`` (плоско, все DLL рядом —
cuDNN ищет cuBLAS обычным DLL search, так что общая папка избавляет от возни с
порядком подключения). Путь подхватывает ``engine.setup_cuda_dll_paths()``.

Откуда качаем: архив, выложенный рядом с релизом приложения. Тянуть на клиенте
напрямую с PyPI было бы хрупко — там колёса, метаданные и своя схема версий.

Скачивание возобновляемое: 450 МБ по нестабильной сети обрываются регулярно, и
начинать с нуля каждый раз — издевательство. Недокачанное лежит в
``cuda/_download/*.part``, при повторном запуске докачивается через HTTP Range.
Целостность проверяется по SHA-256, иначе оборванный или подменённый архив
превратится в «CUDA установлена, но ничего не работает».
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

from . import profile

# === Что качаем ===
# Версия слоя = версии пакетов, из которых он собран (tools/build_cuda_layer.py).
# Меняется вместе с ними: маркер в профиле хранит эту строку, и при несовпадении
# слой считается устаревшим и качается заново.
LAYER_VERSION = "cu12.9-cudnn9.21"
LAYER_FILENAME = f"iwhisper-cuda-{LAYER_VERSION}.zip"

# Публикуется в GitHub Releases рядом с релизом приложения. До первого релиза
# заполняется через переменные окружения ниже — это же используется в тестах.
LAYER_URL = (
    "https://github.com/afest/iwhisper/releases/download/"
    f"cuda-layer-{LAYER_VERSION}/{LAYER_FILENAME}"
)
# Значения от архива, собранного tools/build_cuda_layer.py --minimal 2026-07-30.
# Пересобрали слой — пересчитайте оба поля, иначе проверка целостности отвергнет
# честный файл (пустой SHA = проверку пропускаем, так делать только при отладке).
LAYER_SHA256 = "355be4a1f1b0a4b6e51d7b10d6bca5397a62ac81b7705713f89325d2ca0aed5e"
LAYER_SIZE_BYTES = 553_082_527

# Отладочные переопределения: file:///... или http://localhost/... для проверки
# всего пути до того, как архив выложен в Releases.
ENV_URL = "IWHISPER_CUDA_LAYER_URL"
ENV_SHA256 = "IWHISPER_CUDA_LAYER_SHA256"

# Минимальный набор — по нему проверяем, что распакованное годно к работе.
REQUIRED_DLLS = ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll")

# Что кладём в архив. Список не «всё, что дал pip»: пакеты nvidia-* распакованы
# в 1.9 ГБ, а замер загруженных модулей во время реальной транскрипции показал
# ровно три DLL — cuBLAS, cuBLASLt и загрузчик cuDNN (CTranslate2 считает
# свёртки Whisper через GEMM и до cuDNN-графов не доходит). Остальные модули
# cuDNN подключаются лениво, поэтому здесь идут отдельным «страховочным»
# набором: он вдвое дороже по весу, но выручит, если на чужой карте
# CTranslate2 всё-таки пойдёт в cuDNN.
LAYER_DLLS_CORE = (
    "cublas64_12.dll",
    "cublasLt64_12.dll",
    "cudnn64_9.dll",
)
LAYER_DLLS_CUDNN = (
    "cudnn_graph64_9.dll",
    "cudnn_ops64_9.dll",
    "cudnn_cnn64_9.dll",
    "cudnn_heuristic64_9.dll",
    "cudnn_engines_runtime_compiled64_9.dll",
    "cudnn_engines_tensor_ir64_9.dll",
    "nvrtc64_120_0.dll",
    "nvrtc-builtins64_129.dll",
)

MARKER_NAME = ".layer.json"
_CHUNK = 1 << 20  # 1 МБ: размер буфера чтения и шаг вызова progress_cb


def log(msg: str) -> None:
    """Тот же безопасный лог, что в engine (cp1251-stderr не должен ронять поток)."""
    line = f"[cuda_layer] {msg}"
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        try:
            print(line.encode("ascii", "replace").decode("ascii"), file=sys.stderr, flush=True)
        except Exception:
            pass


class LayerError(RuntimeError):
    """Ошибка, которую можно показать пользователю как есть."""


# === Где что лежит ===
def layer_dir() -> Path:
    return profile.cuda_dir()


def bin_dir() -> Path:
    return layer_dir() / "bin"


def _marker_path() -> Path:
    return layer_dir() / MARKER_NAME


def _download_dir() -> Path:
    return layer_dir() / "_download"


def source_url() -> str:
    return os.environ.get(ENV_URL) or LAYER_URL


def expected_sha256() -> str:
    return (os.environ.get(ENV_SHA256) or LAYER_SHA256).strip().lower()


# === Состояние ===
def installed_version() -> Optional[str]:
    """Версия установленного слоя или None.

    Проверяем не только маркер, но и сами DLL: пользователь мог почистить папку
    руками, а маркер остался — тогда «установлено» соврало бы.
    """
    marker = _marker_path()
    if not marker.exists():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = str(data.get("version") or "")
    if not version:
        return None
    if not _dlls_present(bin_dir()):
        return None
    return version


def is_installed() -> bool:
    """Слой стоит и совпадает по версии с тем, что умеет эта сборка."""
    return installed_version() == LAYER_VERSION


def _dlls_present(path: Path) -> bool:
    if not path.is_dir():
        return False
    names = {p.name.lower() for p in path.glob("*.dll")}
    return all(dll.lower() in names for dll in REQUIRED_DLLS)


def runtime_in_environment() -> bool:
    """CUDA-рантайм уже доступен помимо нашего слоя.

    Так выглядит машина разработчика (pip-пакеты nvidia-*) и машина с системным
    CUDA Toolkit в PATH. Предлагать им докачку нечего.
    """
    if sys.platform != "win32":
        return False
    from . import engine

    for path in engine.CUDA_DLL_DIRS:
        try:
            if Path(path).resolve() == bin_dir().resolve():
                continue  # это наш же слой, он считается отдельно
        except OSError:
            pass
        if _dlls_present(Path(path)):
            return True
    import ctypes.util

    return ctypes.util.find_library("cublas64_12") is not None


def gpu() -> Optional[dict]:
    """`{name, vram_mb}` найденной NVIDIA или None (через nvidia-smi, без CUDA)."""
    from . import engine

    return engine.detect_gpu()


def should_offer() -> bool:
    """Показывать ли предложение скачать слой.

    Нет NVIDIA — не показываем вообще: на CPU-пути слой бесполезен, а диалог
    про «ускорение», которое этой машине недоступно, — только шум.
    """
    if sys.platform != "win32":
        return False
    if is_installed() or runtime_in_environment():
        return False
    return gpu() is not None


def size_hint_mb() -> int:
    """Сколько примерно качать — для текста диалога."""
    return int(LAYER_SIZE_BYTES / 1e6) if LAYER_SIZE_BYTES else 450


# === Скачивание ===
def _open(url: str, offset: int = 0):
    """Открыть источник, по возможности с докачкой с `offset`.

    Возвращает `(response, resumed)`. `resumed=False` значит «сервер отдал файл
    с начала» — уже скачанное придётся выбросить. file:// Range не умеет, там
    всегда с начала (но это локальный источник, терять нечего).
    """
    req = urllib.request.Request(url, headers={"User-Agent": "iwhisper"})
    if offset and urllib.parse.urlparse(url).scheme in ("http", "https"):
        req.add_header("Range", f"bytes={offset}-")
    resp = urllib.request.urlopen(req, timeout=60)
    resumed = getattr(resp, "status", None) == 206
    return resp, resumed


def _remote_size(resp, resumed: bool, offset: int) -> int:
    """Полный размер файла из заголовков (0 = сервер не сказал)."""
    try:
        length = int(resp.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        length = 0
    if resumed and length:
        return offset + length
    return length or LAYER_SIZE_BYTES


def download(
    progress_cb: Optional[Callable[[int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    url: Optional[str] = None,
) -> Path:
    """Скачать архив слоя (с докачкой) и вернуть путь к готовому файлу.

    `progress_cb(done_bytes, total_bytes)`; total=0 — размер неизвестен.
    `should_cancel()` опрашивается на каждом мегабайте: отмена оставляет
    недокачанное на месте, следующий запуск продолжит с той же точки.
    """
    url = url or source_url()
    dl_dir = _download_dir()
    dl_dir.mkdir(parents=True, exist_ok=True)
    part = dl_dir / (LAYER_FILENAME + ".part")

    offset = part.stat().st_size if part.exists() else 0
    try:
        resp, resumed = _open(url, offset)
    except urllib.error.HTTPError as exc:
        if offset and exc.code in (416, 501):
            # Range не поддержан или диапазон уже за концом файла — начинаем заново
            part.unlink(missing_ok=True)
            offset = 0
            resp, resumed = _open(url, 0)
        else:
            raise LayerError(f"Сервер ответил {exc.code} на {url}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise LayerError(f"Не удалось подключиться: {exc}") from exc

    if offset and not resumed:
        log("сервер не поддержал докачку — качаем с начала")
        offset = 0

    total = _remote_size(resp, resumed, offset)
    mode = "ab" if offset else "wb"
    done = offset
    if progress_cb:
        progress_cb(done, total)

    try:
        with resp, open(part, mode) as f:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, total)
                if should_cancel and should_cancel():
                    log(f"скачивание отменено на {done / 1e6:.0f} МБ — прогресс сохранён")
                    raise LayerError("Скачивание отменено")
    except LayerError:
        raise
    except (urllib.error.URLError, OSError) as exc:
        # Обрыв сети — не ошибка формата: .part остаётся, следующий заход докачает
        raise LayerError(f"Скачивание прервано на {done / 1e6:.0f} МБ: {exc}") from exc

    # Оборванное соединение выглядит как «поток кончился», без исключения.
    # Отличить обрыв от битого файла можно только по размеру — и это важно:
    # обрыв должен сохранить прогресс для докачки, а битый архив, наоборот,
    # обязан быть удалён, иначе докачка увековечит мусор.
    if total and done < total:
        raise LayerError(
            f"Скачивание прервано на {done / 1e6:.0f} из {total / 1e6:.0f} МБ — "
            "соединение закрылось. Повторите: продолжим с этого места."
        )

    expected = expected_sha256()
    if expected:
        actual = _sha256(part)
        if actual != expected:
            part.unlink(missing_ok=True)
            raise LayerError(
                "Контрольная сумма архива не совпала — файл повреждён. "
                "Скачанное удалено, попробуйте ещё раз."
            )

    ready = dl_dir / LAYER_FILENAME
    ready.unlink(missing_ok=True)
    part.replace(ready)
    log(f"архив скачан: {ready} ({ready.stat().st_size / 1e6:.0f} МБ)")
    return ready


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


# === Установка ===
def install(archive: Path) -> Path:
    """Распаковать архив в `cuda/bin`, заменив прежний слой целиком.

    Распаковываем во временную папку рядом и переименовываем: половина слоя
    хуже, чем его отсутствие — на ней CTranslate2 падает уже в рантайме.
    """
    target = bin_dir()
    staging = layer_dir() / "_new"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = Path(info.filename).name
                if not name.lower().endswith(".dll"):
                    continue
                # Плоско: имя файла, а не путь из архива — защита и от zip-slip,
                # и от вложенности вида nvidia/cublas/bin/*.dll
                with zf.open(info) as src, open(staging / name, "wb") as dst:
                    shutil.copyfileobj(src, dst, _CHUNK)
    except (zipfile.BadZipFile, OSError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise LayerError(f"Архив не читается: {exc}") from exc

    if not _dlls_present(staging):
        shutil.rmtree(staging, ignore_errors=True)
        raise LayerError(
            "В архиве нет нужных библиотек ("
            + ", ".join(REQUIRED_DLLS)
            + ") — это не тот файл."
        )

    old = layer_dir() / "_old"
    shutil.rmtree(old, ignore_errors=True)
    if target.exists():
        # Заменить папку, из которой уже подгружены DLL, Windows не даст —
        # поэтому старую отодвигаем, а не удаляем: снос отложится до перезапуска.
        try:
            target.replace(old)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise LayerError(
                f"Прежний CUDA-слой занят другим процессом ({exc}). "
                "Закройте вторую копию iWhisper и повторите."
            ) from exc
    staging.replace(target)
    shutil.rmtree(old, ignore_errors=True)

    _marker_path().write_text(
        json.dumps(
            {"version": LAYER_VERSION, "source": source_url(), "files": len(list(target.glob("*.dll")))},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    log(f"слой установлен: {target}")
    return target


def activate() -> bool:
    """Подключить слой к DLL search прямо сейчас, без перезапуска.

    Работает, только пока CTranslate2 ещё не грузил свои зависимости: после
    первой загрузки модели поиск DLL уже произошёл, и новая папка ничего не
    изменит. Возвращает False, если слоя нет.
    """
    if not _dlls_present(bin_dir()):
        return False
    from . import engine

    engine.CUDA_DLL_DIRS = engine.setup_cuda_dll_paths()
    return True


def needs_restart() -> bool:
    """CTranslate2 уже в памяти — активация слоя на лету опоздала."""
    return "ctranslate2" in sys.modules


def ensure(
    progress_cb: Optional[Callable[[int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> bool:
    """Скачать (если нужно), установить и подключить слой. True = готов к работе."""
    if is_installed():
        return activate()
    archive = download(progress_cb=progress_cb, should_cancel=should_cancel)
    install(archive)
    try:
        archive.unlink()
        shutil.rmtree(_download_dir(), ignore_errors=True)
    except OSError:
        pass
    return activate()


def remove() -> tuple[bool, str]:
    """Удалить слой целиком (кнопка «убрать ускорение» в настройках)."""
    root = layer_dir()
    if not root.exists():
        return True, "CUDA-слой не установлен"
    try:
        shutil.rmtree(root)
        return True, "CUDA-слой удалён"
    except OSError as exc:
        return False, f"Не удалось удалить: {exc} (папка занята — попробуйте после перезапуска)"


def installed_bytes() -> int:
    """Сколько места занимает слой — для строки в настройках."""
    total = 0
    for path in layer_dir().rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            pass
    return total


# === Сборка архива (вызывается из tools/build_cuda_layer.py) ===
def pack_from_site_packages(
    out_path: Path, sources: list[Path], with_cudnn: bool = True
) -> tuple[Path, str, int]:
    """Собрать zip слоя из папок с DLL. Возвращает (путь, sha256, размер).

    Живёт здесь, а не в сборочном скрипте, чтобы упаковка и распаковка не
    разъехались по формату: раскладку архива знает один модуль.
    `with_cudnn=False` даёт минимальный слой — только то, что реально грузится.
    """
    wanted = {n.lower() for n in LAYER_DLLS_CORE}
    if with_cudnn:
        wanted |= {n.lower() for n in LAYER_DLLS_CUDNN}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        for src in sources:
            for dll in src.glob("*.dll"):
                key = dll.name.lower()
                if key in seen or key not in wanted:
                    continue
                seen.add(key)
                shutil.copy2(dll, staging / dll.name)
        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for dll in sorted(staging.glob("*.dll")):
                zf.write(dll, arcname=f"bin/{dll.name}")
    return out_path, _sha256(out_path), out_path.stat().st_size
