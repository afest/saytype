"""Выход из ожидания: отмена и честная шкала (T-405).

Что здесь проверяется — ровно те места, где приложение раньше молчало:

1. Отмена распознавания. Флаг ставит GUI-поток, читает worker между сегментами
   и в ожидании модели. Модель при этом НЕ разрушается (правило 10 проектного
   CLAUDE.md, T-268: разрушение WhisperModel на CUDA убивает процесс).
2. Отмена скачивания. `huggingface_hub` API прерывания не даёт — единственная
   точка внутри его сетевого цикла — колбэк прогресса, и исключение оттуда
   обязано доехать наверх как «отменено», а не как сбой.
3. Шкала: скорость, остаток, застой словами. Скорость считается до «сейчас», а
   не до последнего замера — иначе на вставшей закачке она оставалась бы бодрой.
4. Откат маршрута: мёртвый прокси из настроек не должен уносить с собой все
   попытки, если системный маршрут рабочий.
5. Карточка модели: «выбрана» и «готова к работе» — разные подписи.

Живьём (сеть, GPU) проверялось отдельно на `large-v3` 17.08 — здесь всё на
подменах, чтобы прогон был быстрым и не зависел от маршрута.

Запуск:  python -m tests.test_cancel_and_progress   (из корня репозитория)
"""

import ctypes
import io
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_TMP = tempfile.mkdtemp(prefix="saytype-cancel-test-")
os.environ["LOCALAPPDATA"] = _TMP
os.environ.pop("APPDATA", None)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # прогон без монитора

if sys.platform == "win32":  # single-instance: handle 0 = «мы первые»
    ctypes.windll.kernel32.CreateMutexW = lambda *a, **k: 0

from saytype import crashguard  # noqa: E402

crashguard.start = lambda *a, **k: None
crashguard.mark = lambda *a, **k: None

_fake_tk = types.ModuleType("tkinter")  # splash ловит исключение своим except
_fake_tk.Tk = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("splash отключён в тесте"))
sys.modules["tkinter"] = _fake_tk

from saytype import engine  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


# === 1. Шкала: скорость, остаток, застой =====================================
def test_progress_text() -> None:
    print("шкала говорит про скорость, остаток и застой:")
    p = engine.DownloadProgress()
    now = time.monotonic()
    # Замеры подставляем руками: 10 МБ за 2 секунды = 5 МБ/с.
    p._samples = [(now - 2.0, 100_000_000), (now, 110_000_000)]
    p.done, p.total = 110_000_000, 1_600_000_000
    p._last_done, p._last_change = 110_000_000, now
    check("скорость посчитана", 4e6 < p.speed_bps() < 6e6, f"{p.speed_bps():.0f} Б/с")
    text = p.text()
    check("в строке объём", "110 / 1600 МБ" in text, text)
    check("в строке скорость", "МБ/с" in text, text)
    check("в строке остаток", "осталось" in text, text)

    # Байты перестали идти: новых замеров нет вообще, и скорость обязана падать
    # сама — иначе шкала показывает бодрые мегабайты на вставшей закачке.
    p._samples = [(now - 12.0, 100_000_000), (now - 10.0, 110_000_000)]
    check("скорость падает без новых байт", p.speed_bps() < 1.5e6, f"{p.speed_bps():.0f} Б/с")

    p._last_change = time.monotonic() - (engine.STALL_AFTER_SEC + 3)
    check("застой распознан", p.stalled() is True)
    stall_text = p.text()
    check("застой назван словами", "нет данных" in stall_text, stall_text)
    check("вместо скорости — застой", "МБ/с" not in stall_text, stall_text)

    p.feed(120_000_000, 1_600_000_000)
    check("свежие байты снимают застой", p.stalled() is False)

    unknown = engine.DownloadProgress()
    unknown.feed(5_000_000, 0)
    check("неизвестный размер не врёт про остаток", "осталось" not in unknown.text(), unknown.text())

    check("длительность по-человечески",
          (engine.fmt_duration(45), engine.fmt_duration(150), engine.fmt_duration(4900))
          == ("45 сек", "2 мин", "1 ч 22 мин"))


# === 2. Отмена скачивания ====================================================
def _install_hub(on_download) -> None:
    sys.modules["huggingface_hub"] = types.SimpleNamespace(
        snapshot_download=on_download,
        constants=types.SimpleNamespace(
            HF_HUB_CACHE=str(Path(_TMP) / "hf"),
            DOWNLOAD_CHUNK_SIZE=10 * 1024 * 1024,
            HF_HUB_DISABLE_XET=False,
        ),
    )


class _FakeConnectError(Exception):
    """Обрыв соединения в том виде, в каком его узнаёт engine — по имени класса."""


_FakeConnectError.__name__ = "ConnectError"


def test_download_cancel() -> None:
    print("скачивание прерывается нажатием и не выглядит сбоем:")
    cancel = {"on": False}
    chunks = {"n": 0}
    # Прогрев: первый `repo_for_spec` тянет за собой faster_whisper (~1.4 с), а
    # первый `_make_progress_tqdm` — сам tqdm. Без прогрева отмена успевает
    # сработать ещё до входа в сетевой цикл, и тест мерил бы не то.
    engine.repo_for_spec("small")
    engine._make_progress_tqdm()

    def snapshot_download(repo_id, **kwargs):
        # Имитируем сетевой цикл hub: прогресс идёт через tqdm_class, и отмена
        # обязана вылететь именно оттуда — другого входа внутрь у нас нет.
        bar = kwargs["tqdm_class"](unit="B", total=100, file=io.StringIO())
        for _ in range(100):
            chunks["n"] += 1
            bar.update(1_000_000)
            time.sleep(0.01)
        return str(engine._repo_cache_dir(repo_id, engine.models_root()))

    real_total, real_cached, real_reset = (
        engine._repo_total_bytes, engine.is_cached, engine._reset_hub_session,
    )
    real_hub = sys.modules.get("huggingface_hub")
    engine._repo_total_bytes = lambda r: 100_000_000
    engine.is_cached = lambda spec: False
    engine._reset_hub_session = lambda *a, **k: None
    _install_hub(snapshot_download)
    seen: list = []
    try:
        threading.Timer(0.3, lambda: cancel.__setitem__("on", True)).start()
        t0 = time.monotonic()
        err = None
        try:
            engine.ensure_downloaded(
                "small",
                progress_cb=lambda d, t: seen.append(d),
                should_cancel=lambda: cancel["on"],
            )
        except BaseException as exc:  # noqa: BLE001
            err = exc
        dt = time.monotonic() - t0
        check("вылетела именно отмена", isinstance(err, engine.DownloadCancelled), repr(err))
        check("отмена — не ошибка скачивания",
              not isinstance(err, engine.ModelDownloadError), type(err).__name__)
        check(f"вышли быстро ({dt:.2f} с)", dt < 2.0, f"{dt:.2f} с")
        check("успели что-то скачать до отмены", bool(seen) and chunks["n"] < 100,
              f"кусков {chunks['n']}")
        check("в отмене видно, сколько скачано", err.done_bytes > 0, str(err.done_bytes))
        check("прогресс отвязан после выхода", engine._dl_state["cb"] is None)
        check("флаг отмены не остался в состоянии", engine._dl_state["cancel"] is None)
    finally:
        engine._repo_total_bytes, engine.is_cached = real_total, real_cached
        engine._reset_hub_session = real_reset
        if real_hub is not None:
            sys.modules["huggingface_hub"] = real_hub
        else:
            sys.modules.pop("huggingface_hub", None)


# === 3. Откат маршрута =======================================================
def test_route_fallback() -> None:
    print("мёртвый прокси не уносит с собой рабочий системный маршрут:")
    routes: list = []

    def snapshot_download(repo_id, **kwargs):
        # Через прокси — обрыв, напрямую — успех. Маршрут узнаём по env, который
        # выставляет _DownloadRoute.
        via_proxy = bool(os.environ.get("HTTPS_PROXY"))
        routes.append("proxy" if via_proxy else "system")
        if via_proxy:
            raise _FakeConnectError("не смог достать метаданные")
        return str(engine._repo_cache_dir(repo_id, engine.models_root()))

    real_total, real_cached, real_reset, real_pause = (
        engine._repo_total_bytes, engine.is_cached, engine._reset_hub_session, engine.RETRY_PAUSE_SEC,
    )
    real_hub = sys.modules.get("huggingface_hub")
    engine._repo_total_bytes = lambda r: 480_000_000
    engine.is_cached = lambda spec: "system" in routes
    engine._reset_hub_session = lambda *a, **k: None
    engine.RETRY_PAUSE_SEC = 0
    engine.set_download_proxy("http://127.0.0.1:9")
    _install_hub(snapshot_download)
    try:
        engine.ensure_downloaded("small")
        check("через прокси пробовали", routes.count("proxy") == engine.DOWNLOAD_ATTEMPTS,
              str(routes))
        check("после него зашли по системному маршруту", routes[-1] == "system", str(routes))

        # Тот же сценарий, но системный маршрут тоже мёртв: в тексте ошибки
        # обязаны быть названы оба — иначе «нет связи» не отличить от «прокси лёг».
        routes.clear()
        engine.is_cached = lambda spec: False

        def always_fail(repo_id, **kwargs):
            routes.append("x")
            raise _FakeConnectError("не смог достать метаданные")

        _install_hub(always_fail)
        err = None
        try:
            engine.ensure_downloaded("small")
        except engine.ModelDownloadError as exc:
            err = exc
        check("отказ дошёл как ModelDownloadError", err is not None)
        check("маршруты перечислены", "Пробовал:" in (err.hint if err else ""), err.hint if err else "")
        check("назван и прокси, и системный маршрут",
              "127.0.0.1:9" in err.hint and "системный" in err.hint, err.hint if err else "")
    finally:
        engine.set_download_proxy("")
        engine._repo_total_bytes, engine.is_cached = real_total, real_cached
        engine._reset_hub_session, engine.RETRY_PAUSE_SEC = real_reset, real_pause
        if real_hub is not None:
            sys.modules["huggingface_hub"] = real_hub
        else:
            sys.modules.pop("huggingface_hub", None)


# === 4. Отмена распознавания =================================================
def test_transcription_cancel() -> None:
    print("распознавание прерывается между сегментами и в ожидании модели:")
    from saytype import transcribe_ui as ui

    class _Seg:
        def __init__(self, text):
            self.text = text

    def slow_segments(n=50):
        for i in range(n):
            time.sleep(0.05)
            yield _Seg(f"кусок {i}")

    ui._cancel_event.clear()
    threading.Timer(0.3, ui._cancel_event.set).start()
    t0 = time.monotonic()
    err = None
    try:
        ui._collect_segments(slow_segments())
    except BaseException as exc:  # noqa: BLE001
        err = exc
    dt = time.monotonic() - t0
    check("проход прерван", isinstance(err, ui._TranscriptionCancelled), repr(err))
    check(f"вышли между сегментами ({dt:.2f} с)", dt < 1.5, f"{dt:.2f} с")
    check("до конца записи не дошли", dt < 50 * 0.05)

    # Полный проход без отмены отдаёт склеенный текст.
    ui._cancel_event.clear()
    text = ui._collect_segments(iter([_Seg(" раз "), _Seg("два"), _Seg("  ")]))
    check("текст собран и обрезан", text == "раз два", repr(text))

    # Ожидание модели: прогрев CUDA прервать нельзя, но ЖДАТЬ его — можно.
    real_load = ui.load_model
    loaded = {"done": False}

    def slow_load(*a, **kw):
        time.sleep(2.0)
        loaded["done"] = True
        return "модель"

    ui.load_model = slow_load
    try:
        ui._cancel_event.clear()
        threading.Timer(0.3, ui._cancel_event.set).start()
        t0 = time.monotonic()
        err = None
        try:
            ui._load_model_cancellable()
        except BaseException as exc:  # noqa: BLE001
            err = exc
        dt = time.monotonic() - t0
        check("ожидание модели прервано", isinstance(err, ui._TranscriptionCancelled), repr(err))
        check(f"вышли, не дожидаясь прогрева ({dt:.2f} с)", dt < 1.5, f"{dt:.2f} с")
        check("загрузка при этом НЕ убита", loaded["done"] is False, "рано, она ещё идёт")
        time.sleep(2.2)
        check("модель догрузилась в фоне (её не разрушали, T-268)", loaded["done"] is True)
    finally:
        ui.load_model = real_load
        ui._cancel_event.clear()

    # Кнопка «Отменить» бессмысленна, когда ничего не идёт.
    check("нечего отменять — говорим об этом", ui.cancel_transcription() is False)


# === 5. «Выбрана» ≠ «готова к работе» ========================================
def test_model_card_states() -> None:
    print("карточка модели не выдаёт не скачанную за активную:")
    from PySide6.QtWidgets import QApplication, QLabel

    from saytype.transcribe_ui_window import ModelCard

    _app = QApplication.instance() or QApplication([])
    base = {"title": "Whisper Large v3", "size_mb": 3100, "meta": "", "desc": ""}
    ready = ModelCard({**base, "spec": "large-v3", "downloaded": True, "active": True})
    pending = ModelCard({**base, "spec": "large-v3", "downloaded": False, "active": True})

    def labels(card):
        return [lbl.text() for lbl in card.findChildren(QLabel)]

    check("скачанная активная несёт бейдж «Активная»",
          any("Активная" in t for t in labels(ready)))
    check("не скачанная активная бейдж «Активная» НЕ несёт",
          not any("✓ Активная" in t for t in labels(pending)), str(labels(pending)))
    check("вместо него — «Выбрана · не скачана»",
          any("не скачана" in t for t in labels(pending)), str(labels(pending)))
    check("у не скачанной есть кнопка скачивания", hasattr(pending, "dl_btn"))
    check("у скачанной кнопки скачивания нет", not hasattr(ready, "dl_btn"))

    # Итог скачивания остаётся на экране во всех трёх исходах.
    for kind, text, want_btn in (
        ("ok", "Модель готова к работе.", "Скачать"),
        ("cancelled", "Отменено — скачано 400 из 1600 МБ.", "Докачать"),
        ("error", "Нет связи с huggingface.co", "Скачать"),
    ):
        pending.show_result(kind, text)
        check(f"итог «{kind}» виден", not pending.result_label.isHidden() and text in
              pending.result_label.text())
        check(f"кнопка после «{kind}» — «{want_btn}»", want_btn in pending.dl_btn.text(),
              pending.dl_btn.text())

    # Застой: шкала замирает, а не бежит бесконечной анимацией.
    stalled = engine.DownloadProgress()
    stalled.feed(50_000_000, 0)
    stalled._last_change -= engine.STALL_AFTER_SEC + 5
    pending.show_progress(50_000_000, 0, stalled.text(), stalled.stalled())
    check("на застое сказано словами", "нет данных" in pending.progress.format(),
          pending.progress.format())
    check("бесконечной анимации нет", pending.progress.maximum() != 0)


if __name__ == "__main__":
    test_progress_text()
    test_download_cancel()
    test_route_fallback()
    test_transcription_cancel()
    test_model_card_states()
    print()
    if FAILED:
        print(f"ПРОВАЛЕНО {len(FAILED)}: " + ", ".join(FAILED))
        sys.exit(1)
    print("все проверки пройдены")
