"""Микрофон виден и проверяем (T-418): полоса сигнала, выбор устройства, полоса hi-fi.

Что здесь ловится — то, из-за чего появилась задача. После переустановки Windows
системным микрофоном стала вебка, приложение продолжало писать «hi-fi 48 000 Гц»,
и 108 минут материала для клона голоса ушли с полосой, обрезанной на 8 кГц.

1. `audio_quality` отличает живой широкополосный источник от передискретизованных
   16 кГц — и молчит там, где вывода нет (тишина, короткая запись, низкая частота).
2. Устройство хранится **именем**: индексы sounddevice переставляются от
   подключения наушников, и вчерашняя «двойка» сегодня другой микрофон.
3. Настройки показывают микрофон и умеют его сменить — раньше это жило только в
   мастере первого запуска, куда заходят один раз.
4. Полоса hi-fi называет микрофон, а на узкополосном источнике краснеет.

Запуск:  python -m tests.test_mic_quality   (из корня репозитория)
"""

import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_TMP = tempfile.mkdtemp(prefix="saytype-mic-test-")
os.environ["LOCALAPPDATA"] = _TMP
os.environ.pop("APPDATA", None)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402

from saytype import audio_quality as aq  # noqa: E402
from saytype import profile  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


# === Синтетика ================================================================

def _voice(rate: int, seconds: float = 3.0, seed: int = 7) -> np.ndarray:
    """Похожий на речь сигнал: гармоники основного тона + шумовые шипящие.

    Ровный белый шум для проверки не годится — у него полоса широкая по
    построению, и любой порог на нём срабатывает. Здесь энергия распределена
    как у голоса: основное в 0,3–3 кГц, наверху заметно тише.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(int(rate * seconds)) / rate
    sig = np.zeros_like(t)
    for k, amp in enumerate((0.5, 0.3, 0.18, 0.1, 0.05), start=1):
        sig += amp * np.sin(2 * np.pi * 130 * k * t)
    hiss = rng.normal(0, 0.02, t.size)  # шипящие и шум комнаты — вся полоса
    env = (np.sin(2 * np.pi * 2.5 * t) > -0.3).astype(np.float32)  # паузы между фразами
    return ((sig + hiss) * env * 0.4).astype(np.float32)


def _narrowband(rate: int, seconds: float = 3.0) -> np.ndarray:
    """Тот же голос, но прошедший через источник с потолком 8 кГц.

    Именно это делает Windows с миком вебки или гарнитурой в режиме
    Hands-Free: файл остаётся 48-кГц, а выше 8 кГц — цифровой ноль. Режем
    спектром, а не интерполяцией: линейная интерполяция оставляет зеркальные
    образы и моделировала бы плохой ресемплер, а не узкий источник.
    """
    src = _voice(rate, seconds)
    spec = np.fft.rfft(src)
    spec[np.fft.rfftfreq(src.size, 1.0 / rate) > 8000] = 0
    return np.fft.irfft(spec, n=src.size).astype(np.float32)


def test_band_check() -> None:
    print("полоса сигнала:")
    wide = aq.check_samples(_voice(48000), 48000)
    narrow = aq.check_samples(_narrowband(48000), 48000)
    check("широкополосный источник — не тревога", wide.measured and not wide.narrowband,
          f"drop={wide.drop_db:.1f} дБ")
    check("передискретизованные 16 кГц — тревога", narrow.measured and narrow.narrowband,
          f"drop={narrow.drop_db:.1f} дБ")
    check("между ними разрыв больше 20 дБ", narrow.drop_db - wide.drop_db > 20,
          f"{wide.drop_db:.1f} → {narrow.drop_db:.1f}")
    check("порог лежит между ними",
          wide.drop_db < aq.NARROWBAND_DROP_DB < narrow.drop_db)


def test_says_nothing_when_it_cannot_tell() -> None:
    print("там, где вывода нет:")
    silence = aq.check_samples(np.zeros(48000 * 3, dtype=np.float32), 48000)
    check("тишина — не тревога, а «сигнала нет»",
          not silence.measured and not silence.narrowband and "тишина" in silence.reason,
          silence.reason)
    low = aq.check_samples(_voice(16000), 16000)
    check("16-кГц запись не объявляется браком",
          not low.measured and not low.narrowband, low.reason)
    short = aq.check_samples(_voice(48000, 0.2), 48000)
    check("короткая запись — без вывода", not short.measured, short.reason)
    quiet = aq.check_samples(_voice(48000) * 0.0005, 48000)
    check("почти неслышный сигнал — без вывода", not quiet.measured, quiet.reason)


def test_describe_speaks_about_consequence() -> None:
    print("формулировки:")
    narrow = aq.check_samples(_narrowband(48000), 48000)
    hifi_text = aq.describe(narrow, hifi=True)
    plain_text = aq.describe(narrow, hifi=False)
    check("для hi-fi сказано про датасет", "датасет" in hifi_text, hifi_text)
    check("для обычной диктовки — про распознавание", "распознавание" in plain_text, plain_text)
    ok_text = aq.describe(aq.check_samples(_voice(48000), 48000))
    check("нормальный сигнал не пугает", "обрезана" not in ok_text, ok_text)


# === Устройства ===============================================================

class _FakeSd(types.ModuleType):
    """Подмена sounddevice: два имени в трёх хост-API, как на живой машине."""

    DEVICES = [
        {"name": "Микрофон (DJI MIC MINI)", "max_input_channels": 2},
        {"name": "Динамики", "max_input_channels": 0},
        {"name": "Микрофон (USB Microphone)", "max_input_channels": 1},
        {"name": "Микрофон (DJI MIC MINI)", "max_input_channels": 2},  # тот же, другой хост-API
    ]

    def __init__(self, default_index=2):
        super().__init__("sounddevice")
        self.default = types.SimpleNamespace(device=[default_index, 0])

    def query_devices(self, device=None, kind=None):
        if kind == "input":
            return self.DEVICES[self.default.device[0]]
        return list(self.DEVICES)


def test_device_list_and_resolution() -> None:
    print("список устройств:")
    saved = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = _FakeSd()
    try:
        devices = aq.input_devices()
        check("дубли по хост-API схлопнуты", [d["name"] for d in devices] == [
            "Микрофон (DJI MIC MINI)", "Микрофон (USB Microphone)"], str(devices))
        check("выходы не попали в список", all(d["index"] != 1 for d in devices))
        check("системный помечен", [d["default"] for d in devices] == [False, True], str(devices))
        check("имя разрешается в индекс",
              aq.resolve_device_index("Микрофон (USB Microphone)") == 2)
        check("отключённое имя — None, а не чужой индекс",
              aq.resolve_device_index("Микрофон (которого нет)") is None)
        check("пустая настройка — системное устройство",
              aq.resolve_device_index("") is None)
        check("имя системного видно", aq.default_input_name() == "Микрофон (USB Microphone)")
    finally:
        if saved is not None:
            sys.modules["sounddevice"] = saved
        else:
            sys.modules.pop("sounddevice", None)


def test_survives_dead_audio_stack() -> None:
    print("звуковой слой отвалился:")
    saved = sys.modules.get("sounddevice")
    broken = types.ModuleType("sounddevice")
    broken.query_devices = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("PortAudio упал"))
    broken.default = types.SimpleNamespace(device=[None, None])
    sys.modules["sounddevice"] = broken
    try:
        check("список пуст, без исключения", aq.input_devices() == [])
        check("имя системного пусто, без исключения", aq.default_input_name() == "")
        check("разрешение имени не падает", aq.resolve_device_index("хоть что") is None)
    finally:
        if saved is not None:
            sys.modules["sounddevice"] = saved
        else:
            sys.modules.pop("sounddevice", None)


# === Настройки и полоса =======================================================

def test_settings_dialog_and_round_trip() -> None:
    print("настройки:")
    from PySide6.QtWidgets import QApplication

    from saytype import transcribe_ui_window as win

    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
    saved = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = _FakeSd()
    try:
        current = win.load_settings_dict()
        current["mic_device"] = ""
        dlg = win.SettingsDialog(None, current)
        check("первый пункт называет системный микрофон",
              "Микрофон (USB Microphone)" in dlg.mic_combo.itemText(0), dlg.mic_combo.itemText(0))
        check("устройства в списке", dlg.mic_combo.count() == 3, str(dlg.mic_combo.count()))
        check("выбран системный (пустое значение)", dlg.mic_combo.currentData() == "")
        dlg.mic_combo.setCurrentIndex(dlg.mic_combo.findData("Микрофон (DJI MIC MINI)"))
        values = dlg.values()
        check("values() отдаёт имя устройства",
              values["mic_device"] == "Микрофон (DJI MIC MINI)", str(values["mic_device"]))
        check("regression: остальные поля на месте",
              values["rotation_count"] == current["rotation_count"]
              and values["hotkey"] == current["hotkey"])
        win.save_settings_dict(values)
        check("настройка пережила запись и чтение",
              win.load_settings_dict()["mic_device"] == "Микрофон (DJI MIC MINI)")
        dlg.close()

        # Устройство отключили — выбор человека остаётся видимым, а не подменяется
        current["mic_device"] = "Микрофон (которого нет)"
        dlg2 = win.SettingsDialog(None, current)
        check("отключённое устройство помечено, а не забыто",
              dlg2.mic_combo.currentData() == "Микрофон (которого нет)"
              and "не подключён" in dlg2.mic_combo.currentText(), dlg2.mic_combo.currentText())
        dlg2.close()
    finally:
        if saved is not None:
            sys.modules["sounddevice"] = saved
        else:
            sys.modules.pop("sounddevice", None)


def main() -> int:
    test_band_check()
    test_says_nothing_when_it_cannot_tell()
    test_describe_speaks_about_consequence()
    test_device_list_and_resolution()
    test_survives_dead_audio_stack()
    test_settings_dialog_and_round_trip()
    print()
    if FAILED:
        print(f"ПРОВАЛЕНО: {len(FAILED)} — {', '.join(FAILED)}")
        return 1
    print("все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
