"""Проверка того, что микрофон действительно отдаёт широкую полосу (T-418).

Зачем отдельный модуль. Драйвер про полосу врёт: узкополосный источник (мик
вебки, Bluetooth-гарнитура в режиме Hands-Free) отдаётся Windows как обычное
48-кГц устройство, `sd.InputStream(samplerate=48000)` открывается без ошибки, и
сверху приезжает передискретизованные 16 кГц. Ни настройки, ни лог отличить это
не могут — только сам сигнал.

Случай, из-за которого модуль появился: после переустановки Windows системным
микрофоном стала вебка, и три дня hi-fi надиктовок (108 минут материала для
клона голоса) писались с обрезанной на 8 кГц полосой. Приложение всё это время
показывало «hi-fi 48 000 Гц».

Метрика — **провал ВЧ**: насколько верхняя полоса (9,5–14 кГц) тише речевой
(0,3–3 кГц) на самых громких кадрах записи. У живого микрофона там шум комнаты
и шипящие, у передискретизованных 16 кГц — цифровой ноль.

Пороги выставлены по размеченной выборке (167 нормальных записей DJI MIC MINI
против 253 записей вебки, 18–21.08.2026):

    провал, дБ     min    p5    med    p95    max
    нормальные    12,2  23,9   31,6   38,3   44,5
    вебка         30,5  59,4   68,5   72,5   74,2

Порог 48 дБ: ложных тревог на нормальных 0 %, пропущенного брака 0,4 %.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Полосы сравнения. Речевая — где сигнал есть всегда; верхняя начинается выше
# 8 кГц (граница узкополосного источника) с запасом на плавный спад фильтра.
SPEECH_BAND_HZ = (300.0, 3000.0)
HIGH_BAND_HZ = (9500.0, 14000.0)

#: Провал ВЧ больше этого — источник узкополосный (см. таблицу в docstring).
NARROWBAND_DROP_DB = 48.0

#: Ниже этой частоты дискретизации проверять нечего: 16-кГц запись сама по себе
#: не содержит полосы выше 8 кГц, и «провал» ничего не сказал бы об источнике.
MIN_RATE_HZ = 32000

#: Тише этого на громких кадрах — говорить не о чем, вывод был бы про тишину.
MIN_LOUD_RMS_DBFS = -55.0

_FRAME = 2048  # окно анализа; на 48 кГц это 43 мс — кадр речи целиком


@dataclass(frozen=True)
class BandCheck:
    """Результат проверки. `measured=False` — данных не хватило, вывода нет."""

    rate: int
    measured: bool
    narrowband: bool
    drop_db: float
    peak_dbfs: float
    loud_rms_dbfs: float
    reason: str  # почему не измеряли (при measured=False) — словами, для лога


def _dbfs(value: float) -> float:
    return float(20.0 * np.log10(max(float(value), 1e-12)))


def check_samples(samples: np.ndarray, rate: int) -> BandCheck:
    """Измерить провал ВЧ на массиве float32 в диапазоне −1..1.

    Считаем по 20 % самых громких кадров, а не по всей записи: пауза между
    фразами тише ВЧ-шума, и на записи с длинными паузами усреднение по всему
    сигналу занижало бы речевую полосу.
    """
    rate = int(rate)
    a = np.asarray(samples, dtype=np.float32).reshape(-1)
    none = lambda reason: BandCheck(rate, False, False, 0.0, _dbfs(np.max(np.abs(a)) if a.size else 0.0), 0.0, reason)
    if rate < MIN_RATE_HZ:
        return none(f"частота {rate} Гц — полосы выше 8 кГц там нет по определению")
    if a.size < _FRAME * 8:
        return none("запись короче секунды — мерить нечего")

    n = a.size // _FRAME
    frames = a[: n * _FRAME].reshape(n, _FRAME)
    energy = (frames.astype(np.float64) ** 2).mean(axis=1)
    loud_idx = np.argsort(energy)[-max(4, n // 5) :]
    loud_rms_dbfs = _dbfs(float(np.sqrt(energy[loud_idx].mean())))
    peak_dbfs = _dbfs(float(np.max(np.abs(a))))
    if loud_rms_dbfs < MIN_LOUD_RMS_DBFS:
        return BandCheck(rate, False, False, 0.0, peak_dbfs, loud_rms_dbfs,
                         "сигнала почти нет — тишина или выключенный микрофон")

    spec = (np.abs(np.fft.rfft(frames[loud_idx] * np.hanning(_FRAME), axis=1)) ** 2).mean(axis=0)
    freq = np.fft.rfftfreq(_FRAME, 1.0 / rate)
    band = lambda lo, hi: float(spec[(freq >= lo) & (freq < hi)].sum())
    speech = band(*SPEECH_BAND_HZ)
    high = band(*HIGH_BAND_HZ)
    drop_db = float(10.0 * np.log10((speech + 1e-30) / (high + 1e-30)))
    return BandCheck(rate, True, drop_db > NARROWBAND_DROP_DB, drop_db,
                     peak_dbfs, loud_rms_dbfs, "")


def describe(check: BandCheck, *, hifi: bool = False) -> str:
    """Одна строка для человека: что с сигналом и что это значит.

    Формулировки разные для hi-fi и обычной диктовки: на распознавание
    узкая полоса влияет мало, а датасет голоса портит целиком — и человек
    должен видеть в подписи именно своё последствие.
    """
    if not check.measured:
        return check.reason or "проверить не удалось"
    level = f"уровень {check.loud_rms_dbfs:.0f} dBFS, пик {check.peak_dbfs:.0f} dBFS"
    if check.narrowband:
        tail = ("для датасета голоса такие записи непригодны"
                if hifi else "на распознавание влияет мало, но голос звучит глухо")
        return (f"полоса обрезана около 8 кГц — источник узкополосный "
                f"(вебка или Bluetooth-гарнитура в режиме Hands-Free); {tail}. {level}")
    quiet = ", но тихо — говорите ближе или добавьте усиление" if check.loud_rms_dbfs < -32 else ""
    return f"полоса широкая, микрофон живой{quiet}. {level}"


def input_devices() -> list[dict]:
    """Устройства записи: `{index, name, default}`, без дублей по имени.

    sounddevice показывает одно физическое устройство несколько раз (MME,
    DirectSound, WASAPI). Человеку в списке нужен микрофон, а не хост-API,
    поэтому оставляем первое вхождение каждого имени.
    """
    import sounddevice as sd  # локально: модуль обязан импортироваться и без звукового слоя

    out: list[dict] = []
    seen: set[str] = set()
    try:
        default_idx = sd.default.device[0]
    except Exception:
        default_idx = None
    try:
        devices = sd.query_devices()
    except Exception:
        return out
    for idx, dev in enumerate(devices):
        if int(dev.get("max_input_channels", 0)) <= 0:
            continue
        # Драйверные имена бывают многострочными (WDM-KS отдаёт путь к .sys с
        # переводом строки внутри) — в выпадающем списке такая строка ломает ряд.
        name = " ".join(str(dev.get("name", "")).split())
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"index": idx, "name": name, "default": idx == default_idx})
    return out


def default_input_name() -> str:
    """Имя микрофона, который Windows считает системным. Пусто — не определился.

    Именно эта строка отвечает на вопрос «а куда сейчас говорят», когда в
    настройках выбран «системный»: после переустановки Windows системным стало
    другое устройство, а настройка не изменилась и выглядела правильной.
    """
    import sounddevice as sd

    try:
        return str(sd.query_devices(kind="input").get("name", "")).strip()
    except Exception:
        return ""


def resolve_device_index(name: str) -> "int | None":
    """Индекс устройства записи по имени; None — не нашли (или имя пустое).

    В настройках хранится **имя**, а не индекс: индексы sounddevice меняются от
    подключения наушников и перезапуска службы звука, и вчерашняя «двойка»
    сегодня оказывается другим микрофоном.
    """
    name = (name or "").strip()
    if not name:
        return None
    for dev in input_devices():
        if dev["name"] == name:
            return int(dev["index"])
    return None
