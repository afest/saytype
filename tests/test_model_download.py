"""Скачивание модели: обрыв не равен провалу, а провал обязан дойти до окна (T-404).

Три вещи, которые ломались молча и стоили рабочего дня после
переустановки Windows:

1. Оборванная закачка оставляла в профиле огрызок (`refs/main` без `snapshots`),
   и следующий запуск пытался работать по ссылке на несуществующий снапшот.
2. Флапающий маршрут (VPN, системный прокси) рвал закачку на полпути, и
   единственная попытка объявлялась провалом — хотя hub умеет продолжить с места.
3. Исключение из фонового потока уходило в `_crash.log`. Пользователь видел
   «Транскрибирую…», которое не кончится, и не знал ни причины, ни что делать.

Запуск:  python -m tests.test_model_download   (из корня репозитория)
"""

import os
import sys
import tempfile
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_TMP = tempfile.mkdtemp(prefix="saytype-dl-test-")
os.environ["LOCALAPPDATA"] = _TMP
os.environ.pop("APPDATA", None)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # прогон без монитора

from saytype import engine  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


class _FakeSSLError(Exception):
    """Похоже на httpcore-обрыв TLS: узнаём по имени класса, как это делает engine."""


_FakeSSLError.__name__ = "ConnectError"


def _network_exc() -> Exception:
    """Цепочка исключений в том же виде, в каком её отдаёт huggingface_hub."""
    try:
        try:
            raise _FakeSSLError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF in violation of protocol")
        except Exception as inner:
            raise RuntimeError("Got: ConnectError: не смог достать метаданные") from inner
    except Exception as exc:
        return exc


# --- 1. Огрызок кэша ---
def test_partial_cache_cleanup() -> None:
    print("огрызок кэша (refs без snapshots) сносится, живая папка — нет:")
    root = Path(_TMP) / "models-cleanup"
    repo = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    broken = engine._repo_cache_dir(repo, root)
    (broken / "refs").mkdir(parents=True)
    (broken / "refs" / "main").write_text("0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf")
    check("огрызок найден и снесён", engine.clean_partial_cache(repo, root) is True)
    check("папки модели больше нет", not broken.exists())

    # Та же папка, но закачка реально началась: `blobs` с недокачанным файлом —
    # это материал для докачки с места, его снос стоил бы гигабайта трафика.
    (broken / "blobs").mkdir(parents=True)
    (broken / "blobs" / "abc.incomplete").write_bytes(b"x" * 1024)
    (broken / "refs").mkdir(parents=True, exist_ok=True)
    (broken / "refs" / "main").write_text("0a363e9")
    check("частичная закачка не тронута", engine.clean_partial_cache(repo, root) is False)
    check("blobs на месте", (broken / "blobs" / "abc.incomplete").exists())
    check("байты на диске посчитаны", engine._downloaded_bytes(repo, root) >= 1024)


def _fake_snapshot(root: Path, repo: str, files: dict) -> Path:
    """Кэш HF руками: refs/main → ревизия, snapshots/<ревизия>/<файлы>.
    Именно такой раскладкой hub отдаёт модель офлайн — на ней и проверяем."""
    rev = "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"
    base = engine._repo_cache_dir(repo, root)
    (base / "refs").mkdir(parents=True, exist_ok=True)
    (base / "refs" / "main").write_text(rev)
    snap = base / "snapshots" / rev
    snap.mkdir(parents=True, exist_ok=True)
    for name, size in files.items():
        (snap / name).write_bytes(b"x" * size)
    return snap


def test_incomplete_snapshot_is_not_cached() -> None:
    """Живой прогон T-404: 1,6 ГБ весов доехали, `preprocessor_config.json` — нет.
    Модель при этом ГРУЗИЛАСЬ, а каждая транскрипция падала на
    «Invalid input features shape (1, 80, 3000)»: без этого json faster-whisper
    берёт дефолтные 80 мел-бинов, а turbo/large-v3 нужны 128."""
    print("снапшот без preprocessor_config.json не считается скачанным:")
    root = Path(_TMP) / "models-complete"
    repo = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    snap = _fake_snapshot(root, repo, {
        "model.bin": 4096, "config.json": 64, "tokenizer.json": 64,
    })
    check("названо, чего не хватает",
          engine.missing_snapshot_files(snap, repo) == ["preprocessor_config.json"],
          str(engine.missing_snapshot_files(snap, repo)))
    check("огрызок с весами не сносится", engine.clean_partial_cache(repo, root) is False)

    (snap / "preprocessor_config.json").write_bytes(b"{}")
    check("полный снапшот претензий не вызывает", engine.missing_snapshot_files(snap, repo) == [])

    (snap / "model.bin").write_bytes(b"")  # нулевой файл = не скачан
    check("пустой model.bin считается отсутствующим",
          "model.bin" in engine.missing_snapshot_files(snap, repo))

    # Обратная сторона: Systran/faster-whisper-tiny…medium этот json НЕ публикуют
    # (80 мел-каналов — дефолт). Общее требование объявило бы их «скачанными не
    # полностью» и гоняло бы по кругу — ловилось живым прогоном на tiny.
    tiny_repo = "Systran/faster-whisper-tiny"
    tiny_snap = _fake_snapshot(root, tiny_repo, {
        "model.bin": 4096, "config.json": 64, "tokenizer.json": 64, "vocabulary.txt": 64,
    })
    check("у tiny preprocessor_config.json не спрашиваем",
          engine.missing_snapshot_files(tiny_snap, tiny_repo) == [],
          str(engine.missing_snapshot_files(tiny_snap, tiny_repo)))
    check("состав требований зависит от модели",
          engine.required_snapshot_files(repo) != engine.required_snapshot_files(tiny_repo))


# --- 2. Текст ошибки ---
def test_network_error_becomes_human_text() -> None:
    print("сетевой обрыв превращается в текст с причиной и следующим шагом:")
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:3067"
    try:
        check("сеть распознана", engine._is_network_error(_network_exc()) is True)
        err = engine._download_error(_network_exc(), "Systran/faster-whisper-tiny")
        check("тип ошибки — ModelDownloadError", isinstance(err, engine.ModelDownloadError))
        check("названа причина", "huggingface.co" in str(err))
        check("названо, что делать", "VPN" in err.hint and "прокси" in err.hint.lower())
        check("назван реальный прокси", "127.0.0.1:3067" in err.hint,
              "без этого сутки ищут прокси в переменных окружения, а он в реестре")
        check("полный текст склеен", err.full_text().startswith(str(err)))

        not_network = engine._download_error(ValueError("совсем другое"), "Systran/faster-whisper-tiny")
        check("не-сетевая ошибка не врёт про сеть", "huggingface.co" not in str(not_network))
    finally:
        os.environ.pop("HTTPS_PROXY", None)


def test_system_proxy_prefers_env() -> None:
    print("прокси ищется в обоих источниках:")
    os.environ["HTTPS_PROXY"] = "http://proxy.example:8080"
    try:
        check("переменная окружения видна", "proxy.example:8080" in engine.system_proxy())
    finally:
        os.environ.pop("HTTPS_PROXY", None)
    # Без переменных остаётся реестр Windows (там его держит Karing) — значение
    # зависит от машины, поэтому проверяем только что не падает и отдаёт строку.
    check("без переменных не падает", isinstance(engine.system_proxy(), str))


# --- 3. Повтор с докачкой ---
def _closed_client_exc() -> Exception:
    """Ровно то, что прилетело на живом прогоне: общий httpx-клиент hub'а закрыт."""
    return RuntimeError("Cannot send a request, as the client has been closed.")


def _install_fake_hub(fail_times: int, calls: list, exc_factory=_network_exc) -> None:
    """Подменить huggingface_hub: N обрывов, потом успех. engine импортирует его
    внутри функции, поэтому подмены в sys.modules достаточно."""

    def snapshot_download(repo_id, **kwargs):
        calls.append(repo_id)
        if len(calls) <= fail_times:
            raise exc_factory()
        return str(engine._repo_cache_dir(repo_id, engine.models_root()))

    sys.modules["huggingface_hub"] = types.SimpleNamespace(
        snapshot_download=snapshot_download,
        constants=types.SimpleNamespace(HF_HUB_CACHE=str(Path(_TMP) / "hf")),
    )


def _run_ensure(fail_times: int, exc_factory=_network_exc) -> tuple[list, Exception | None]:
    calls: list = []
    real_hub = sys.modules.get("huggingface_hub")
    real_total, real_cached = engine._repo_total_bytes, engine.is_cached
    real_pause, real_reset = engine.RETRY_PAUSE_SEC, engine._reset_hub_session
    resets: list = []
    engine._repo_total_bytes = lambda repo: 1_600_000_000
    engine.is_cached = lambda spec: bool(calls) and len(calls) > fail_times  # «появилась после успеха»
    engine.RETRY_PAUSE_SEC = 0        # пауза между попытками в тесте не нужна
    engine._reset_hub_session = lambda *_a, **_k: resets.append(1)
    _install_fake_hub(fail_times, calls, exc_factory)
    err: Exception | None = None
    try:
        engine.ensure_downloaded("tiny", logger=lambda *_a: None)
    except Exception as exc:
        err = exc
    finally:
        engine._repo_total_bytes, engine.is_cached = real_total, real_cached
        engine.RETRY_PAUSE_SEC, engine._reset_hub_session = real_pause, real_reset
        if real_hub is not None:
            sys.modules["huggingface_hub"] = real_hub
        else:
            sys.modules.pop("huggingface_hub", None)
    return calls, err, len(resets)


def test_download_retries_then_succeeds() -> None:
    print("два обрыва подряд — не провал, а продолжение с места:")
    calls, err, resets = _run_ensure(fail_times=2)
    check("попыток было три", len(calls) == 3, f"было {len(calls)}")
    check("ошибки наверх не ушло", err is None, repr(err))
    check("перед повтором клиент сбрасывался", resets == 2, f"сбросов {resets}")


def test_download_gives_up_with_human_error() -> None:
    print("маршрут мёртв целиком — отказ с причиной, не сырой трейсбек:")
    calls, err, _ = _run_ensure(fail_times=99)
    check("исчерпал попытки", len(calls) == engine.DOWNLOAD_ATTEMPTS, f"было {len(calls)}")
    check("тип ошибки — ModelDownloadError", isinstance(err, engine.ModelDownloadError), repr(err))
    check("причина названа", err is not None and "huggingface.co" in str(err))
    check("сохранена исходная причина", err is not None and err.cause is not None)


def test_closed_hub_client_is_retried() -> None:
    """Живой прогон T-404: 1,6 ГБ доехали, а последний json упал в
    «Cannot send a request, as the client has been closed» — общий httpx-клиент
    hub'а закрылся после обрыва TLS. Без повтора со сбросом клиента это
    требовало перезапуска приложения (acceptance 4 — как раз про это)."""
    print("закрытый httpx-клиент hub'а — повод повторить, а не сдаться:")
    calls, err, resets = _run_ensure(fail_times=1, exc_factory=_closed_client_exc)
    check("повтор состоялся", len(calls) == 2, f"попыток {len(calls)}")
    check("клиент сброшен перед повтором", resets == 1, f"сбросов {resets}")
    check("ошибки наверх не ушло", err is None, repr(err))


# --- 4. Ошибка доходит до окна ---
def test_error_reaches_window() -> None:
    print("сбой из фонового потока показывается окном, а не только в логе:")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from saytype import audio_import, profile
    from saytype.transcribe_ui_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    icons = profile.profile_dir() / "icons"
    win = MainWindow(
        idle_icon_path=icons / "idle.png",
        recording_icon_path=icons / "recording.png",
        processing_icon_path=icons / "processing.png",
        app_icon_path=icons / "app.png",
        history_dir_getter=lambda: Path(_TMP),
        rotation_count_getter=lambda: 5,
        entry_script=Path(__file__).resolve().parent.parent / "src" / "saytype" / "transcribe_ui.py",
        model_busy_getter=lambda: False,
        file_import_api=audio_import.FileImportApi(
            begin=lambda: True, run=lambda *a, **k: {}, end=lambda: None
        ),
    )
    seen: list[str] = []

    def collect() -> None:
        for w in app.topLevelWidgets():
            if isinstance(w, QMessageBox) and w.isVisible():
                seen.append(w.text())
                w.close()
        app.quit()

    # Именно из другого потока: путь `notify_error` существует ради worker'ов,
    # а Qt-виджеты трогать оттуда нельзя (T-284) — сигнал доставит в main thread.
    threading.Thread(
        target=lambda: win.notify_error(
            "Модель не загрузилась",
            "Нет связи с huggingface.co — модель large-v3-turbo не скачалась.",
            "Проверь VPN и прокси.",
        ),
        daemon=True,
    ).start()
    QTimer.singleShot(400, collect)
    QTimer.singleShot(1500, app.quit)
    app.exec()

    check("окно с ошибкой появилось", bool(seen), "ни одного QMessageBox")
    check("в тексте есть причина", any("huggingface.co" in t for t in seen))
    check("в тексте есть следующий шаг", any("VPN" in t for t in seen))

    # Тот же сбой сразу вторым заходом не должен плодить второе окно.
    seen.clear()
    win.notify_error(
        "Модель не загрузилась",
        "Нет связи с huggingface.co — модель large-v3-turbo не скачалась.",
        "Проверь VPN и прокси.",
    )
    QTimer.singleShot(300, collect)
    QTimer.singleShot(1200, app.quit)
    app.exec()
    check("дубль подавлен", not seen, f"второе окно: {seen}")
    win.close()


def test_download_proxy_setting() -> None:
    """Настройка «прокси загрузки» доезжает от ini до самого запроса.

    Обход системного прокси (`NO_PROXY`) в этой ситуации не помогает вообще:
    VPN-туннель перехватывает трафик по IP, и запрос уходит через ту же ноду.
    Поэтому настройка задаёт явный адрес, а не «не использовать прокси»."""
    print("прокси для скачивания моделей: настройка → окружение запроса:")
    from saytype.transcribe_ui_window import load_settings_dict, save_settings_dict

    before = load_settings_dict()
    check("по умолчанию пусто", before.get("download_proxy") == "")
    updated = dict(before)
    updated["download_proxy"] = "http://127.0.0.1:3065"
    save_settings_dict(updated)
    check("значение сохранилось в ini",
          load_settings_dict().get("download_proxy") == "http://127.0.0.1:3065")

    resets: list = []
    real_reset = engine._reset_hub_session
    engine._reset_hub_session = lambda *_a, **_k: resets.append(1)
    real_env = os.environ.get("HTTPS_PROXY")
    os.environ["HTTPS_PROXY"] = "http://был-системный:1"
    try:
        engine.set_download_proxy("http://127.0.0.1:3065")
        check("клиент сброшен при смене маршрута", len(resets) == 1)
        with engine._DownloadRoute():
            check("внутри запроса подставлен наш прокси",
                  os.environ.get("HTTPS_PROXY") == "http://127.0.0.1:3065")
            check("http тоже подставлен", os.environ.get("HTTP_PROXY") == "http://127.0.0.1:3065")
        check("после запроса окружение вернулось",
              os.environ.get("HTTPS_PROXY") == "http://был-системный:1")

        engine.set_download_proxy("")
        with engine._DownloadRoute():
            check("пустая настройка окружение не трогает",
                  os.environ.get("HTTPS_PROXY") == "http://был-системный:1")
    finally:
        engine._reset_hub_session = real_reset
        if real_env is None:
            os.environ.pop("HTTPS_PROXY", None)
        else:
            os.environ["HTTPS_PROXY"] = real_env
        save_settings_dict(before)


def test_zz_no_failures() -> None:
    """Под pytest сами check'и молчаливы — этот тест делает их провал видимым."""
    assert not FAILED, "провалено: " + ", ".join(FAILED)


def main() -> int:
    for fn in (
        test_partial_cache_cleanup,
        test_incomplete_snapshot_is_not_cached,
        test_network_error_becomes_human_text,
        test_system_proxy_prefers_env,
        test_download_retries_then_succeeds,
        test_download_gives_up_with_human_error,
        test_closed_hub_client_is_retried,
        test_download_proxy_setting,
        test_error_reaches_window,
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
