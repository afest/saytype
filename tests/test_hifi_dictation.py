"""Hi-fi диктовка (T-389): запись на 44.1/48 кГц мимо ротации, Whisper — на 16 кГц.

Проверяем три вещи, которые ломаются молча:
1. Файл ложится в подпапку `profile.HIFI_SUBDIR` на выбранной частоте, а `rotate_history()`
   её не видит — иначе накопленный датасет вытеснится по `rotation_count`.
2. Модели уходит 16-кГц копия и длительность считается от частоты записи — деление
   на 16000 при hi-fi завысило бы её втрое, и это видно только в meta/статистике.
3. Выключенный режим не меняет ничего: корень истории, 16 кГц, streaming не продавлен.

`transcribe_ui` — точка входа: на импорте она берёт single-instance mutex (при живом
SayType показывает MessageBox и выходит), ставит crash-маркер и рисует tkinter-splash.
Всё три обезвреживаются ДО импорта, иначе тест либо молча выходит с кодом 0, либо
портит журнал аварий работающего приложения.

Запуск:  python -m tests.test_hifi_dictation   (из корня репозитория)
"""

import ctypes
import json
import os
import sys
import tempfile
import types
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_TMP = tempfile.mkdtemp(prefix="saytype-hifi-test-")
os.environ["LOCALAPPDATA"] = _TMP
os.environ.pop("APPDATA", None)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

if sys.platform == "win32":  # single-instance: handle 0 = «мы первые»
    ctypes.windll.kernel32.CreateMutexW = lambda *a, **k: 0

from saytype import crashguard  # noqa: E402

crashguard.start = lambda *a, **k: None
crashguard.mark = lambda *a, **k: None

_fake_tk = types.ModuleType("tkinter")  # splash ловит исключение своим except
_fake_tk.Tk = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("splash отключён в тесте"))
sys.modules["tkinter"] = _fake_tk

import numpy as np  # noqa: E402

from saytype import profile  # noqa: E402
from saytype import transcribe_ui as ui  # noqa: E402
from saytype import transcribe_ui_window as win  # noqa: E402
from saytype.transcribe_call import _resample_to_target  # noqa: E402

FAILED = []
CLIP_SEC = 1.5


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def write_wav(path: Path, seconds: float, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(seconds * rate)
    data = (np.sin(np.arange(n) * 2 * np.pi * 220 / rate) * 9000).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(data.tobytes())


# === Подмена микрофона и модели ===============================================

_seen_by_model: dict = {}


class _FakeStream:
    """Отдаёт CLIP_SEC синуса одним колбэком на запрошенной частоте."""

    def __init__(self, samplerate, channels, dtype, device, callback):
        self.rate = int(samplerate)
        self.cb = callback

    def start(self):
        n = int(self.rate * CLIP_SEC)
        t = np.arange(n) / self.rate
        data = (np.sin(2 * np.pi * 300 * t) * 0.5).astype(np.float32).reshape(-1, 1)
        self.cb(data, n, None, None)

    def stop(self):
        pass

    def close(self):
        pass


class _PickyStream(_FakeStream):
    """Микрофон, который отдаёт только 16 кГц — проверка отката."""

    def __init__(self, samplerate, channels, dtype, device, callback):
        if int(samplerate) != 16000:
            raise RuntimeError("Invalid sample rate")
        super().__init__(samplerate, channels, dtype, device, callback)


class _FakeSegment:
    text = "проверка"


class _FakeInfo:
    language = "ru"


class _FakeModel:
    def transcribe(self, audio, **kwargs):
        arr = np.asarray(audio)
        _seen_by_model["samples"] = int(arr.size)
        _seen_by_model["peak"] = float(np.abs(arr).max())
        return [_FakeSegment()], _FakeInfo()


def _install_fakes() -> None:
    ui.sd.InputStream = _FakeStream
    ui.load_model = lambda *a, **k: _FakeModel()
    ui.pyperclip.copy = lambda text: None  # буфер обмена пользователя не трогаем
    ui.window = None
    ui.pre_roll = None


_install_fakes()  # на уровне модуля: pytest зовёт тесты напрямую, минуя main()


def _record(hifi: bool, rate: int, hist: Path) -> None:
    """Одна диктовка от hotkey-down до записанного файла."""
    _seen_by_model.clear()
    ui.SETTINGS = {
        "history_dir": str(hist),
        "rotation_count": 5,
        "rotation_minutes": 0,
        "hifi_enabled": hifi,
        "hifi_sample_rate": rate,
        "processing_mode": "always_streaming",  # именно его hi-fi обязана продавить в batch
        "auto_threshold_sec": 10,
        "sound_notifications_dictation": False,
    }
    ui.captured_hwnd = 0
    ui.via_ui_request = True  # без автопаста в чужое окно
    ui.start_recording()
    ui.stop_recording_and_transcribe()


# === Проверки =================================================================


def test_hifi_writes_native_rate_to_quarantine() -> None:
    print("hi-fi: история как обычно + оригинал в папку-карантин, модели 16 кГц:")
    for rate in (44100, 48000):
        hist = Path(_TMP) / f"hist-{rate}"
        _record(True, rate, hist)
        wavs = sorted((hist / profile.HIFI_SUBDIR).glob("*.wav"))
        roots = sorted(hist.glob("*.wav"))
        check(f"{rate}: один wav в {profile.HIFI_SUBDIR}", len(wavs) == 1, str([p.name for p in wavs]))
        # Запись обязана остаться и в истории — иначе она пропадает из карточек окна
        check(f"{rate}: запись есть и в корне истории", len(roots) == 1, str([p.name for p in roots]))
        with wave.open(str(roots[0]), "rb") as wf:
            check(f"{rate}: история осталась 16-кГц", wf.getframerate() == ui.SAMPLE_RATE,
                  str(wf.getframerate()))
        with wave.open(str(wavs[0]), "rb") as wf:
            check(f"{rate}: частота файла", wf.getframerate() == rate, str(wf.getframerate()))
            check(f"{rate}: длительность цела",
                  abs(wf.getnframes() / wf.getframerate() - CLIP_SEC) < 0.02)
            check(f"{rate}: моно 16 бит", wf.getnchannels() == 1 and wf.getsampwidth() == 2)
        check(f"{rate}: txt в истории", roots[0].with_suffix(".txt").exists())
        check(f"{rate}: txt рядом с hi-fi копией", wavs[0].with_suffix(".txt").exists())
        meta_path = roots[0].parent / f"{roots[0].stem}.meta.json"
        check(f"{rate}: meta в истории", meta_path.exists())
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        check(f"{rate}: длительность в meta не завышена",
              abs(meta["duration_sec"] - CLIP_SEC) < 0.02, str(meta["duration_sec"]))
        check(f"{rate}: модель получила 16-кГц копию",
              abs(_seen_by_model["samples"] / ui.SAMPLE_RATE - CLIP_SEC) < 0.02,
              f"{_seen_by_model['samples']} сэмплов")
        check(f"{rate}: копия не искажена по амплитуде",
              0.4 < _seen_by_model["peak"] < 0.6, f"peak={_seen_by_model['peak']:.2f}")
        check(f"{rate}: streaming-worker не поднимался", ui._streaming_worker_started is False)
        check(f"{rate}: режим продавлен в always_batch", ui._active_processing_mode == "always_batch")
        stats = [json.loads(s) for s in (hist / "_stats.jsonl").read_text(encoding="utf-8").splitlines()]
        check(f"{rate}: в статистике помечено hifi",
              stats[-1]["hifi"] is True and stats[-1]["sample_rate"] == rate)
        check(f"{rate}: в статистике batch_forced", stats[-1]["processing_mode"] == "batch_forced")


def test_hifi_entries_visible_in_window() -> None:
    print("карточки истории показывают hi-fi запись и полосу накопления:")
    from PySide6.QtWidgets import QApplication

    from saytype import audio_import
    from saytype.transcribe_ui_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
    hist = Path(_TMP) / "hist-window"
    _record(True, 48000, hist)

    icons = profile.profile_dir() / "icons"
    api = audio_import.FileImportApi(begin=lambda: True, run=lambda *a, **k: {}, end=lambda: None)
    settings = win.get_settings()
    settings.setValue("hifi_enabled", True)
    settings.sync()
    window = MainWindow(
        idle_icon_path=icons / "idle.png",
        recording_icon_path=icons / "recording.png",
        processing_icon_path=icons / "processing.png",
        app_icon_path=icons / "app.png",
        history_dir_getter=lambda: hist,
        rotation_count_getter=lambda: 5,
        entry_script=Path(__file__).resolve().parent.parent / "src" / "saytype" / "transcribe_ui.py",
        model_busy_getter=lambda: False,
        file_import_api=api,
    )
    window.refresh_history()
    check("надиктовка видна карточкой", len(window._cards) == 1, str(len(window._cards)))

    # Полосы hi-fi в главном окне больше нет (убрана перед выпуском 0.3.0):
    # включённый режим карточек истории не меняет, и выключение — тоже.
    settings.setValue("hifi_enabled", False)
    settings.sync()
    window.refresh_history()
    check("выключенный режим не трогает карточки", len(window._cards) == 1,
          str(len(window._cards)))
    window.close()


def test_rotation_does_not_touch_quarantine() -> None:
    print("ротация истории не видит папку-карантин:")
    hist = Path(_TMP) / "hist-rotate"
    for i in range(8):
        write_wav(hist / f"2026-08-12T09-0{i}-00.wav", 5, 16000)
        (hist / f"2026-08-12T09-0{i}-00.txt").write_text("x", encoding="utf-8")
    for i in range(4):
        write_wav(hist / profile.HIFI_SUBDIR / f"hifi-{i}.wav", 5, 44100)
    ui.SETTINGS = {"history_dir": str(hist), "rotation_count": 5, "rotation_minutes": 0}
    ui.rotate_history()
    check("корень обрезан до rotation_count", len(list(hist.glob("*.wav"))) == 5)
    check("подпапка цела", len(list((hist / profile.HIFI_SUBDIR).glob("*.wav"))) == 4)


def test_disabled_hifi_keeps_old_behaviour() -> None:
    print("выключенный режим ничего не меняет:")
    hist = Path(_TMP) / "hist-off"
    _record(False, 44100, hist)
    roots = sorted(hist.glob("*.wav"))
    check("wav в корне истории", len(roots) == 1, str([p.name for p in roots]))
    check("папка-карантин не создана", not (hist / profile.HIFI_SUBDIR).exists())
    with wave.open(str(roots[0]), "rb") as wf:
        check("частота 16000", wf.getframerate() == 16000, str(wf.getframerate()))
    check("режим обработки не тронут", ui._active_processing_mode == "always_streaming")
    stats = [json.loads(s) for s in (hist / "_stats.jsonl").read_text(encoding="utf-8").splitlines()]
    check("статистика: hifi=false, 16000",
          stats[-1]["hifi"] is False and stats[-1]["sample_rate"] == 16000)


def test_falls_back_when_microphone_refuses_rate() -> None:
    print("микрофон не отдал частоту — диктовка не теряется:")
    ui.sd.InputStream = _PickyStream
    try:
        hist = Path(_TMP) / "hist-fallback"
        _record(True, 48000, hist)
        check("запись легла в корень истории", len(list(hist.glob("*.wav"))) == 1)
        check("карантин пуст", not (hist / profile.HIFI_SUBDIR).exists())
        with wave.open(str(sorted(hist.glob("*.wav"))[0]), "rb") as wf:
            check("откат на 16000", wf.getframerate() == 16000, str(wf.getframerate()))
    finally:
        ui.sd.InputStream = _FakeStream


def test_note_dictation_writes_nothing() -> None:
    print("диктовка в заметку при hi-fi не создаёт файлов:")
    hist = Path(_TMP) / "hist-note"
    ui.note_dictation = True
    try:
        _record(True, 44100, hist)
    finally:
        ui.note_dictation = False
    check("ни одного wav", not list(hist.rglob("*.wav")) if hist.exists() else True)
    check("модель всё равно получила 16-кГц копию",
          abs(_seen_by_model["samples"] / ui.SAMPLE_RATE - CLIP_SEC) < 0.02)


def test_resample_matches_call_channel_path() -> None:
    print("ресемпл 44.1/48 → 16 кГц:")
    for rate in (44100, 48000):
        n = rate * 2
        t = np.arange(n) / rate
        src = (np.sin(2 * np.pi * 440 * t) * 20000).astype(np.int16)
        out = _resample_to_target(src, rate, ui.SAMPLE_RATE)
        check(f"{rate}: длительность сохранена", abs(out.size / ui.SAMPLE_RATE - 2.0) < 0.01)
        check(f"{rate}: амплитуда цела", 17000 < int(np.abs(out).max()) < 23000)
        check(f"{rate}: тип int16", out.dtype == np.int16)


def test_settings_round_trip_and_counter() -> None:
    print("настройки и счётчик накопления:")
    check("мусор в частоте → дефолт", win._valid_hifi_rate("абв") == 44100)
    check("чужая частота → дефолт", win._valid_hifi_rate(22050) == 44100)
    check("48000 проходит", win._valid_hifi_rate("48000") == 48000)

    base = win.load_settings_dict()
    check("дефолт выключен", base["hifi_enabled"] is False)
    base["hifi_enabled"] = True
    base["hifi_sample_rate"] = 48000
    win.save_settings_dict(base)
    again = win.load_settings_dict()
    check("round-trip через QSettings",
          again["hifi_enabled"] is True and again["hifi_sample_rate"] == 48000)

    hist = Path(_TMP) / "hist-counter"
    write_wav(hist / "2026-08-12T10-00-00.wav", 30, 16000)  # обычная запись в корне не считается
    write_wav(hist / profile.HIFI_SUBDIR / "a.wav", 60, 44100)
    write_wav(hist / profile.HIFI_SUBDIR / "b.wav", 120, 48000)
    (hist / profile.HIFI_SUBDIR / "broken.wav").write_bytes(b"not a wav")
    minutes = win.hifi_accumulated_minutes(hist)
    check("3 минуты, битый файл не уронил счёт", abs(minutes - 3.0) < 0.01, f"{minutes:.2f}")
    check("нет папки → 0", win.hifi_accumulated_minutes(Path(_TMP) / "нет-такой") == 0.0)


def test_settings_dialog_row() -> None:
    print("строка hi-fi в настройках (клик по галке, а не вызов метода):")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
    hist = Path(_TMP) / "hist-dialog"
    write_wav(hist / profile.HIFI_SUBDIR / "a.wav", 240, 44100)

    current = win.load_settings_dict()
    current["history_dir"] = str(hist)
    current["hifi_enabled"] = False
    current["hifi_sample_rate"] = 44100
    dlg = win.SettingsDialog(None, current)
    check("частота заблокирована при выключенном режиме",
          not dlg.hifi_rate_44_radio.isEnabled() and not dlg.hifi_rate_48_radio.isEnabled())
    check("счётчик прочитал диск", "Накоплено: 4 из 180 мин" in dlg.hifi_counter.text(),
          dlg.hifi_counter.text()[:60])
    dlg.hifi_box.setChecked(True)
    check("галка разблокировала частоту",
          dlg.hifi_rate_44_radio.isEnabled() and dlg.hifi_rate_48_radio.isEnabled())
    dlg.hifi_rate_48_radio.setChecked(True)
    values = dlg.values()
    check("values(): включено и 48000",
          values["hifi_enabled"] is True and values["hifi_sample_rate"] == 48000)
    check("regression: остальные поля на месте",
          values["rotation_count"] == current["rotation_count"]
          and values["processing_mode"] in win._VALID_PROCESSING_MODES)
    dlg.close()


def main() -> int:
    test_hifi_writes_native_rate_to_quarantine()
    test_hifi_entries_visible_in_window()
    test_rotation_does_not_touch_quarantine()
    test_disabled_hifi_keeps_old_behaviour()
    test_falls_back_when_microphone_refuses_rate()
    test_note_dictation_writes_nothing()
    test_resample_matches_call_channel_path()
    test_settings_round_trip_and_counter()
    test_settings_dialog_row()
    print()
    if FAILED:
        print(f"ПРОВАЛЕНО: {len(FAILED)} — {', '.join(FAILED)}")
        return 1
    print("все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
