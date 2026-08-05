"""transcribe_call.py — запись созвона: микрофон + системный звук (T-172).

Пишет два канала одновременно — микрофон (вы) и WASAPI loopback (собеседник) —
и на стопе собирает из них транскрипт с разметкой по говорящему.

**Записывайте созвон только с согласия собеседника.** В части стран и штатов
запись без предупреждения незаконна; приложение об этом не знает.

Что решено внутри:
  * **watchdog тишины WASAPI loopback.** Фоновый поток мониторит время с
    последнего loop-callback'а; >3 c → warning, >5 c → ``on_silence_alert(True)``
    (UI рисует «Системный звук не обнаружен»). Re-init стрима / silent-playback
    не делаем: callback молчит только при ПОЛНОЙ тишине endpoint'а, на живом
    созвоне он fire'ит — warning + alert достаточно.
  * **сон машины.** ``SetThreadExecutionState`` (ES_CONTINUOUS|
    ES_SYSTEM_REQUIRED|ES_DISPLAY_REQUIRED) на время записи, сброс при стопе.
    Держится из watchdog-потока — execution state привязан к потоку, а тот живёт
    ровно длину записи.
  * **аудио-архив.** stereo WAV → MP3 128 kbps через внешний ffmpeg, если он
    есть в PATH. ffmpeg под GPL, поэтому в поставку не входит: без него WAV
    остаётся, MP3 просто не создаётся (см. `ffmpeg_available`).
  * **транскрипт.** Каналы транскрибируются раздельно (L=микрофон, R=системный
    звук) с сегментными таймстампами и сливаются в один ``.md`` с репликами по
    говорящему. Имена говорящих задаются в настройках.

Standalone::

    python -m saytype.transcribe_call --duration 60

Программно::

    rec = CallRecorder(on_silence_alert=...)
    rec.start()
    ...                       # второй hotkey → rec.stop()
    result = rec.stop()       # CallRecording
    finalize_recording(result, model=shared_model)   # → .md + буфер последних N WAV
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from math import gcd
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pyaudiowpatch as pyaudio
import sounddevice as sd
from scipy.signal import resample_poly

from . import profile
from .transcribe_loopback import find_loopback_device, open_loopback_stream

# Ensure stderr handles cyrillic device names on Windows (cp866 default kills прочёт).
try:
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass


# === Константы записи (зеркало transcribe_ui.py) ===
MIC_SAMPLE_RATE = 16000          # стандарт saytype
MIC_CHANNELS = 1
PRE_ROLL_MS = 100                # тишина-прогрев в начало обоих буферов
MIC_BLOCKSIZE = 1024
LOOP_CHUNK_SIZE = 2048

# === Пути ===
# Папка истории задаётся пользователем в настройках; модуль работает и без них
# (standalone-CLI), поэтому держит своё значение и отдаёт его через функции.
# Раньше здесь стоял абсолютный путь, и подпапка созвонов не следовала за
# настройкой «Папка истории» — теперь следует.
_history_dir: Path = profile.default_history_dir()
CALL_AUDIO_KEEP = 2   # T-175: сколько последних WAV созвонов держать в Calls как буфер «вернуться»


def set_history_dir(path) -> None:
    """Переключить папку истории (вызывает UI при старте и при смене настройки)."""
    global _history_dir
    _history_dir = Path(path)


def history_dir() -> Path:
    return _history_dir


def calls_dir() -> Path:
    """Подпапка транскриптов созвонов — вне ротации надиктовок (T-174)."""
    return _history_dir / profile.CALLS_SUBDIR

# === T-176: потоковая запись сырья на диск (durability ВО ВРЕМЯ записи) ===
RAW_FLUSH_SEC = 1.0              # writer-поток сбрасывает накопленные чанки на диск раз в N сек (окно потери при краше)
RAW_MIC_SUFFIX = ".mic.f32raw"   # сырьё mic: float32 little-endian, 16kHz mono
RAW_LOOP_SUFFIX = ".loop.raw"    # сырьё loopback: int16 как пришло из WASAPI (rate/channels — в meta)
RAW_META_SUFFIX = ".callmeta.json"  # параметры сборки (loop_rate/channels/started_at) для recovery

# === Watchdog / транскрипт ===
WATCHDOG_POLL_SEC = 0.5
SILENCE_WARN_SEC = 3.0           # лог-warning (задача Part 1 Risk #2)
SILENCE_ALERT_SEC = 5.0          # on_silence_alert(True) → UI (задача Part 2)
# 2026-07-12: созвоны 03.07/05.07 — R-канал вышел ЦИФРОВЫМ нулём (peak=0), но
# watchdog молчал: активная-но-беззвучная аудиосессия на endpoint'е (звук
# созвона ушёл на BT-гарнитуру, а default-выход — монитор) шлёт callbacks с
# нулями. Ловим и это: нет РЕАЛЬНОГО сигнала дольше порога → тот же alert.
# Порог длинный (60с), чтобы не мигать на естественных паузах собеседника.
SIGNAL_ALERT_SEC = 60.0          # callbacks идут, но сплошные нули > N сек → alert
LOOP_SIGNAL_PEAK_MIN = 50        # |int16| >= порога — считаем «реальный звук», не dither
MP3_BITRATE = "128k"
WHISPER_MODEL = "small"
WHISPER_LANG = "ru"
# Имена в транскрипте. Значения по умолчанию нейтральные — свои задаются в
# настройках записи созвона и приходят сюда параметрами `speaker_l` / `speaker_r`.
DEFAULT_SPEAKER_L = "Я"
DEFAULT_SPEAKER_R = "Собеседник"

# === SetThreadExecutionState (Risk #4) ===
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

CREATE_NO_WINDOW = 0x08000000    # не мигать консолью при вызове ffmpeg из pythonw


def log(msg: str) -> None:
    """Лог в stderr, который НЕ МОЖЕТ уронить вызывающий код.

    2026-07-28: 45-минутный созвон остался без транскрипта из-за строки
    «⏳ транскрибирую…»: stderr процесса был в cp1251, `print` бросил
    UnicodeEncodeError ВНУТРИ try-блока транскрипции, и весь транскрипт
    свалился в except. Лог — вспомогательная вещь, ронять из-за него работу
    нельзя: сначала как есть, потом ascii-фолбэк, потом молча.
    """
    try:
        print(msg, file=sys.stderr, flush=True)
    except (UnicodeEncodeError, OSError, AttributeError, ValueError):
        try:
            print(msg.encode("ascii", errors="replace").decode("ascii"),
                  file=sys.stderr, flush=True)
        except Exception:
            pass
    except Exception:
        pass


def _safe_logger(logger: Callable[[str], None]) -> Callable[[str], None]:
    """Обернуть ЛЮБОЙ переданный логгер так, чтобы его падение не всплывало.

    Вызывающий может передать свой `log` (UI) — он тоже пишет в stderr и тоже
    может упасть на кодировке. Точка сборки одна, здесь."""

    def _log(msg: str) -> None:
        try:
            logger(msg)
        except Exception:
            try:
                log(msg)
            except Exception:
                pass

    return _log


def _prevent_sleep(logger: Callable[[str], None]) -> bool:
    """ES_CONTINUOUS|ES_SYSTEM_REQUIRED|ES_DISPLAY_REQUIRED — не давать idle-сон.

    Вызывать из потока, который живёт всю запись (execution state привязан к
    потоку и сбрасывается при его завершении).
    """
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )
        return True
    except Exception as exc:
        logger(f"[WARN] SetThreadExecutionState(prevent) fail: {exc}")
        return False


def _allow_sleep(logger: Callable[[str], None]) -> None:
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception as exc:
        logger(f"[WARN] SetThreadExecutionState(allow) fail: {exc}")


def _resample_to_target(arr: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate:
        return arr
    g = gcd(src_rate, dst_rate)
    out = resample_poly(arr.astype(np.float32), dst_rate // g, src_rate // g)
    return np.clip(out, -32768, 32767).astype(np.int16)


def build_call_channels(
    mic_f32: np.ndarray,
    loop_bytes: bytes,
    loop_rate: int,
    loop_channels: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Собрать L/R int16 @16k из сырья mic(float32) + loop(int16 bytes).

    T-176: **единая** логика для ``CallRecorder.stop()`` (сырьё из RAM) и
    ``recover_recording_to_wav`` (сырьё с диска) — чтобы каналы не разъехались.
    Включает pre-roll-тишину в начало обоих каналов (артефакт §4 skeleton'а).
    Устойчива к обрыву raw на полуслове: выравнивает байты по int16/каналам.
    """
    mic_f32 = np.asarray(mic_f32, dtype=np.float32).reshape(-1)
    mic_int16 = np.clip(mic_f32 * 32767.0, -32768, 32767).astype(np.int16)
    if not loop_bytes:
        sys_int16 = np.zeros(len(mic_int16), dtype=np.int16)
    else:
        loop_bytes = loop_bytes[: len(loop_bytes) // 2 * 2]  # выравнивание int16
        loop_int16 = np.frombuffer(loop_bytes, dtype=np.int16)
        if loop_channels > 1:
            usable = (len(loop_int16) // loop_channels) * loop_channels
            loop_int16 = (
                loop_int16[:usable].reshape(-1, loop_channels).mean(axis=1).astype(np.int16)
            )
        sys_int16 = _resample_to_target(loop_int16, loop_rate, MIC_SAMPLE_RATE)
    pre_roll = np.zeros(int(MIC_SAMPLE_RATE * PRE_ROLL_MS / 1000), dtype=np.int16)
    return np.concatenate([pre_roll, mic_int16]), np.concatenate([pre_roll, sys_int16])


@dataclass
class CallRecording:
    mic_int16: np.ndarray            # L, 16 kHz mono
    sys_int16: np.ndarray            # R, 16 kHz mono
    sample_rate: int
    started_at: datetime
    wav_path: Optional[Path] = None
    stats: dict = field(default_factory=dict)
    raw_stem: Optional[str] = None   # T-176: stem raw-лога записи (для очистки после WAV)

    @property
    def duration_sec(self) -> float:
        return max(len(self.mic_int16), len(self.sys_int16)) / self.sample_rate


class CallRecorder:
    """Синхронный двух-потоковый рекордер созвона (mic + WASAPI loopback).

    Lifecycle: ``CallRecorder()`` → ``start()`` → (живёт до) ``stop()``.
    ``stop()`` возвращает :class:`CallRecording`. Объект одноразовый.

    ``on_silence_alert(silent: bool)`` (опц.) — watchdog: loopback молчит >
    порога / возобновился (Risk #2). Level meter обоих каналов — Part 2 (UI).
    """

    def __init__(
        self,
        mic_device=None,
        on_silence_alert: Optional[Callable[[bool], None]] = None,
        logger: Callable[[str], None] = log,
    ) -> None:
        self._mic_device = mic_device
        self._on_silence_alert = on_silence_alert
        self._log = logger

        self._mic_chunks: list[np.ndarray] = []
        self._loop_chunks: list[bytes] = []
        self._mic_first_cb: Optional[float] = None
        self._loop_first_cb: Optional[float] = None
        self._last_loop_cb: float = 0.0
        self._last_loop_signal: float = 0.0  # 2026-07-12: последний callback с НЕнулевым звуком
        self._mic_errors: list[str] = []
        self._loop_errors: list[int] = []
        self._mic_start_error: Optional[Exception] = None
        self._silence_active = False

        self._start_event = threading.Event()
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._pa: Optional[pyaudio.PyAudio] = None
        self._mic_stream = None
        self._loop_stream = None
        self._loop_dev: Optional[dict] = None
        self._mic_info: Optional[dict] = None
        self._record_start: float = 0.0
        self._started_at: Optional[datetime] = None

        # T-176: потоковая запись сырья на диск (durability во время записи)
        self._stem: Optional[str] = None
        self._mic_raw_f = None
        self._loop_raw_f = None
        self._mic_written = 0      # курсор: сколько mic-чанков уже на диске
        self._loop_written = 0     # курсор: сколько loop-чанков уже на диске
        self._raw_ok = False       # True пока raw-лог пишется (False = деградация в RAM-only)

    # --- callbacks ---
    def _mic_cb(self, indata, frames, time_info, status):
        if status:
            self._mic_errors.append(str(status))
        if self._mic_first_cb is None:
            self._mic_first_cb = time.perf_counter()
        self._mic_chunks.append(indata[:, 0].copy())

    def _loop_cb(self, in_data, frame_count, time_info, status):
        if status:
            self._loop_errors.append(int(status))
        now = time.perf_counter()
        if self._loop_first_cb is None:
            self._loop_first_cb = now
        self._last_loop_cb = now
        self._loop_chunks.append(in_data)
        # 2026-07-12: факт callback'а ≠ факт звука — беззвучная сессия шлёт нули.
        # peak-чек на 2048 фреймов — микросекунды, callback-латентность не страдает.
        try:
            a = np.frombuffer(in_data, dtype=np.int16)
            if a.size and int(np.abs(a).max()) >= LOOP_SIGNAL_PEAK_MIN:
                self._last_loop_signal = now
        except Exception:
            pass
        return (None, pyaudio.paContinue)

    # --- threads ---
    def _mic_runner(self) -> None:
        self._start_event.wait()
        try:
            self._mic_stream.start()
        except Exception as exc:
            self._mic_start_error = exc
            self._log(f"[ERR] mic-поток не стартовал: {exc!r}")
            return
        self._stop_event.wait()

    def _loop_runner(self) -> None:
        self._start_event.wait()
        try:
            self._loop_stream = open_loopback_stream(
                self._pa, self._loop_dev, self._loop_cb, chunk_size=LOOP_CHUNK_SIZE
            )
        except Exception as exc:
            self._log(f"[ERR] loopback stream open fail: {exc}")
            return
        self._stop_event.wait()

    def _watchdog_runner(self) -> None:
        """Risk #4: держит anti-sleep весь срок записи. Risk #2: следит за тишиной."""
        held = _prevent_sleep(self._log)
        if held:
            self._log("anti-sleep ON (ES_SYSTEM_REQUIRED|ES_DISPLAY_REQUIRED)")
        try:
            self._start_event.wait()
            # last_loop_cb/last_loop_signal инициализируем стартом, чтобы alert
            # сработал и если callback/звук не пришёл НИ разу (звук созвона идёт
            # не на то устройство вывода).
            self._last_loop_cb = time.perf_counter()
            self._last_loop_signal = self._last_loop_cb
            while not self._stop_event.wait(timeout=WATCHDOG_POLL_SEC):
                now = time.perf_counter()
                cb_quiet = now - self._last_loop_cb        # callbacks не приходят
                sig_quiet = now - self._last_loop_signal   # callbacks идут, но нули
                if not self._silence_active and (
                    cb_quiet >= SILENCE_ALERT_SEC or sig_quiet >= SIGNAL_ALERT_SEC
                ):
                    self._silence_active = True
                    reason = (
                        f"callback'и не приходят {cb_quiet:.1f}c"
                        if cb_quiet >= SILENCE_ALERT_SEC
                        else f"callback'и идут, но сплошные НУЛИ {sig_quiet:.0f}c "
                             f"(звук созвона на другом устройстве вывода?)"
                    )
                    self._log(
                        f"[WARN] WASAPI loopback: {reason} — "
                        f"alert: системный звук не обнаружен (проверь устройство вывода)"
                    )
                    self._fire_silence_alert(True)
                elif not self._silence_active and cb_quiet >= SILENCE_WARN_SEC:
                    self._log(f"[WARN] loopback callback молчит {cb_quiet:.1f}c")
                elif self._silence_active and (
                    cb_quiet < SILENCE_WARN_SEC and sig_quiet < SIGNAL_ALERT_SEC
                ):
                    self._silence_active = False
                    self._log("loopback возобновился — alert снят")
                    self._fire_silence_alert(False)
        finally:
            if held:
                _allow_sleep(self._log)
                self._log("anti-sleep OFF")

    def _fire_silence_alert(self, silent: bool) -> None:
        if self._on_silence_alert is None:
            return
        try:
            self._on_silence_alert(silent)
        except Exception:
            pass

    # --- T-176: потоковая запись сырья на диск ---
    def _setup_raw_log(self, loop_rate: int, loop_channels: int) -> None:
        """Открыть два raw-файла + написать meta-сайдкар ДО старта записи.

        При ошибке (диск недоступен/полон) — деградируем в RAM-only: live-запись
        продолжается без durability, но НЕ падает (acceptance §4 / edge переполнение).
        """
        try:
            calls_dir().mkdir(parents=True, exist_ok=True)
            mic_p, loop_p, meta_p = _raw_paths(self._stem)
            meta = {
                "stem": self._stem,
                "started_at": (self._started_at or datetime.now()).isoformat(),
                "mic_rate": MIC_SAMPLE_RATE,
                "mic_dtype": "<f4",
                "loop_rate": loop_rate,
                "loop_channels": loop_channels,
                "pre_roll_ms": PRE_ROLL_MS,
                "status": "recording",
                "version": 1,
            }
            meta_p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            self._mic_raw_f = open(mic_p, "wb")
            self._loop_raw_f = open(loop_p, "wb")
            self._raw_ok = True
            self._log(f"durability: raw-лог открыт ({self._stem}{RAW_MIC_SUFFIX}/{RAW_LOOP_SUFFIX})")
        except Exception as exc:
            self._raw_ok = False
            self._close_raw()
            self._log(f"[WARN] durability: raw-лог не открылся ({exc!r}) — пишу только в RAM")

    def _flush_raw(self) -> None:
        """Дописать новые чанки из RAM-буферов на диск по курсору.

        ``list.append`` атомарен (GIL): снимаем длину ``n`` и пишем срез
        ``[written:n]`` — колбэк-поток параллельно аппендит дальше, не мешая.
        Сбой записи (диск полон) → деградация в RAM-only, без падения записи.
        """
        if not self._raw_ok:
            return
        try:
            n = len(self._mic_chunks)
            if n > self._mic_written and self._mic_raw_f is not None:
                for i in range(self._mic_written, n):
                    self._mic_raw_f.write(self._mic_chunks[i].astype("<f4").tobytes())
                self._mic_written = n
                self._mic_raw_f.flush()
            m = len(self._loop_chunks)
            if m > self._loop_written and self._loop_raw_f is not None:
                for i in range(self._loop_written, m):
                    self._loop_raw_f.write(self._loop_chunks[i])
                self._loop_written = m
                self._loop_raw_f.flush()
        except Exception as exc:
            self._raw_ok = False
            self._log(f"[WARN] durability: сбой записи raw ({exc!r}) — дальше только RAM")
            self._close_raw()

    def _close_raw(self) -> None:
        for f in (self._mic_raw_f, self._loop_raw_f):
            try:
                if f is not None:
                    f.close()
            except Exception:
                pass
        self._mic_raw_f = None
        self._loop_raw_f = None

    def _writer_runner(self) -> None:
        """Живёт всю запись: раз в RAW_FLUSH_SEC сбрасывает накопленное на диск.

        Финальный flush в ``finally`` — чтобы хвост между последним тиком и стопом
        тоже лёг на диск (на чистом стопе он избыточен — WAV соберётся из RAM, — но
        для recovery после краша критичен).
        """
        self._start_event.wait()
        try:
            while not self._stop_event.wait(timeout=RAW_FLUSH_SEC):
                self._flush_raw()
        finally:
            self._flush_raw()
            self._close_raw()

    # --- live mic access (для конкурентной диктовки во время созвона, T-172 UI) ---
    def mic_sample_count(self) -> int:
        """Суммарное число записанных mic-сэмплов на данный момент.

        Безопасно вызывать из другого потока: читаем длины float32-чанков из
        ``_mic_chunks`` (list.append атомарен в CPython). Используется как
        курсор начала диктовки во время созвона (см. ``toggle_recording`` UI).
        """
        return sum(len(c) for c in self._mic_chunks)

    def mic_snapshot(self) -> np.ndarray:
        """Текущий mic-буфер целиком как int16 @16kHz (L-канал).

        Конвертация float32→int16 идентична ``stop()`` (clip ±32767). Пустой
        буфер → пустой int16-массив. Срез ``snap[start_idx:]`` даёт аудио
        диктовки, записанное mic-потоком CallRecorder'а пока шёл созвон.
        """
        chunks = list(self._mic_chunks)  # снимок ссылки на список (append атомарен)
        if not chunks:
            return np.zeros(0, dtype=np.int16)
        mic_f32 = np.concatenate(chunks).reshape(-1)
        return np.clip(mic_f32 * 32767.0, -32768, 32767).astype(np.int16)

    # --- public API ---
    def start(self) -> None:
        """Открыть оба потока синхронно и запустить запись. Не блокирует."""
        self._mic_info = (
            sd.query_devices(self._mic_device, kind="input")
            if self._mic_device is not None
            else sd.query_devices(kind="input")
        )
        self._log(f"mic device:      {self._mic_info['name']!r}")

        self._pa = pyaudio.PyAudio()
        self._loop_dev = find_loopback_device(self._pa)  # RuntimeError → наверх
        self._log(
            f"loopback device: #{self._loop_dev['index']} {self._loop_dev['name']!r} "
            f"(rate={int(self._loop_dev['defaultSampleRate'])} "
            f"ch={int(self._loop_dev['maxInputChannels'])})"
        )
        _ln = str(self._loop_dev["name"]).lower()
        if (
            "hands-free" in _ln or "головной телефон" in _ln
            or int(self._loop_dev["maxInputChannels"]) < 2
            or int(self._loop_dev["defaultSampleRate"]) <= 16000
        ):
            self._log(
                "[WARN] похоже на BT Hands-Free endpoint (звонковый, моно 16кГц). "
                "Медиа/созвон Windows гонит на СТЕРЕО-выход → канал R будет тихий. "
                "Переключи вывод Windows на 'Наушники (... Stereo)' или колонки ДО старта записи."
            )

        # T-176: started_at/stem фиксируем ЗДЕСЬ (не на set()) — нужны для имени
        # raw-лога; открываем raw-файлы + meta ДО старта потоков, чтобы краш в любой
        # момент записи терял максимум RAW_FLUSH_SEC секунд аудио.
        self._started_at = datetime.now()
        self._stem = default_stem_name(self._started_at)
        self._setup_raw_log(
            int(self._loop_dev["defaultSampleRate"]),
            int(self._loop_dev["maxInputChannels"]),
        )

        self._mic_stream = sd.InputStream(
            samplerate=MIC_SAMPLE_RATE,
            channels=MIC_CHANNELS,
            dtype="float32",
            device=self._mic_device,
            blocksize=MIC_BLOCKSIZE,
            callback=self._mic_cb,
        )

        self._threads = [
            threading.Thread(target=self._watchdog_runner, name="call-watchdog", daemon=True),
            threading.Thread(target=self._mic_runner, name="call-mic", daemon=True),
            threading.Thread(target=self._loop_runner, name="call-loop", daemon=True),
            threading.Thread(target=self._writer_runner, name="call-writer", daemon=True),
        ]
        for t in self._threads:
            t.start()
        time.sleep(0.2)  # дать потокам встать в wait

        self._record_start = time.perf_counter()
        self._start_event.set()
        self._log("call recording started")

    def stop(self) -> CallRecording:
        """Остановить запись, собрать стерео-буфер (L=mic, R=sys), вернуть CallRecording."""
        record_end = time.perf_counter()
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=3)

        try:
            if self._mic_stream is not None:
                if self._mic_stream.active:
                    self._mic_stream.stop()
                self._mic_stream.close()
            if self._loop_stream is not None:
                try:
                    self._loop_stream.stop_stream()
                finally:
                    self._loop_stream.close()
        finally:
            if self._pa is not None:
                self._pa.terminate()
                self._pa = None

        if not self._mic_chunks:
            if self._mic_start_error is not None:
                raise RuntimeError(
                    f"mic-поток не стартовал ({self._mic_start_error!r}). "
                    f"Частая причина: устройство занято (запущен daily-driver saytype "
                    f"и держит микрофон под pre-roll) или не поддерживает 16kHz/mono/float32. "
                    f"Закрой UI saytype через трей или укажи --mic-device явно."
                )
            raise RuntimeError("mic не записал ничего — устройство не подаёт сигнал?")

        loop_rate = int(self._loop_dev["defaultSampleRate"])
        loop_channels = int(self._loop_dev["maxInputChannels"])

        loop_silent = not self._loop_chunks
        if loop_silent:
            self._log("[WARN] loopback callback не сработал ни разу — на endpoint тишина?")

        # T-176: та же сборка, что в recovery (build_call_channels) — L/R не разъедутся
        mic_f32 = np.concatenate(self._mic_chunks).reshape(-1)
        mic_int16, sys_int16 = build_call_channels(
            mic_f32, b"".join(self._loop_chunks), loop_rate, loop_channels
        )

        # 2026-07-12: peak R-канала — цифровой ноль = loopback писал не тот endpoint
        # (кейс 03.07/05.07: звук созвона на BT-гарнитуре, default-выход — монитор).
        loop_signal_peak = int(np.abs(sys_int16).max()) if sys_int16.size else 0
        if not loop_silent and loop_signal_peak < LOOP_SIGNAL_PEAK_MIN:
            self._log(
                f"[WARN] loopback callbacks шли, но R-канал — цифровая тишина "
                f"(peak={loop_signal_peak}). Звук созвона шёл на другое устройство вывода?"
            )

        stats = {
            "actual_duration_s": record_end - self._record_start,
            "mic_buffer_s": len(mic_int16) / MIC_SAMPLE_RATE,
            "loop_buffer_s": len(sys_int16) / MIC_SAMPLE_RATE,
            "drift_ms": (len(mic_int16) - len(sys_int16)) / MIC_SAMPLE_RATE * 1000,
            "mic_errors": self._mic_errors,
            "loop_errors": self._loop_errors,
            "loop_silent": loop_silent,
            "loop_signal_peak": loop_signal_peak,
            "silence_alert_fired": self._silence_active,
            "loop_device": self._loop_dev["name"],
            "mic_device": self._mic_info["name"] if self._mic_info else None,
        }
        self._log(
            f"call recording stopped: {stats['mic_buffer_s']:.1f}s "
            f"(drift {stats['drift_ms']:+.0f} ms, "
            f"mic_err={len(self._mic_errors)} loop_err={len(self._loop_errors)})"
        )
        return CallRecording(
            mic_int16=mic_int16,
            sys_int16=sys_int16,
            sample_rate=MIC_SAMPLE_RATE,
            started_at=self._started_at or datetime.now(),
            stats=stats,
            raw_stem=self._stem,
        )


def write_stereo_wav(path: Path, left: np.ndarray, right: np.ndarray, rate: int) -> None:
    n = max(len(left), len(right))
    if len(left) < n:
        left = np.concatenate([left, np.zeros(n - len(left), dtype=np.int16)])
    if len(right) < n:
        right = np.concatenate([right, np.zeros(n - len(right), dtype=np.int16)])
    stereo = np.stack([left, right], axis=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(stereo.tobytes())


def find_ffmpeg() -> Optional[str]:
    """Путь к ffmpeg или None.

    ffmpeg в поставку не входит: готовые сборки идут под GPL, и бандлинг утянул бы
    в GPL весь проект. Поэтому MP3 — опциональная возможность: есть ffmpeg в PATH
    (или установлен через winget) — конвертируем, нет — работаем на WAV.
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    if sys.platform == "win32":
        winget = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
        try:
            for exe in winget.glob("Gyan.FFmpeg*/ffmpeg*/bin/ffmpeg.exe"):
                return str(exe)
        except OSError:
            pass
    return None


def ffmpeg_available() -> bool:
    """Можно ли сделать MP3. Отсутствие ffmpeg — не ошибка, а отключённая опция."""
    return find_ffmpeg() is not None


def convert_to_mp3(wav_path: Path, mp3_path: Path, logger: Callable[[str], None] = log) -> bool:
    """stereo WAV → MP3 128 kbps stereo (libmp3lame). Возвращает True при успехе.

    Без ffmpeg возвращает False и пишет **информационную** строку, не ошибку:
    запись созвона от этого не считается неудачной — WAV на месте.
    """
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        logger("ffmpeg не найден в PATH — MP3 не создаю, WAV сохранён. "
               "Поставьте ffmpeg, если нужен аудио-архив в MP3.")
        return False
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(wav_path),
        "-c:a", "libmp3lame", "-b:a", MP3_BITRATE, "-ac", "2",
        str(mp3_path),
    ]
    flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, creationflags=flags)
    except FileNotFoundError:
        logger(f"ffmpeg не запустился ({ffmpeg}) — MP3 не создан, WAV сохранён")
        return False
    if proc.returncode != 0:
        logger(f"[ERR] ffmpeg rc={proc.returncode}: {proc.stderr.strip()[:300]}")
        return False
    logger(f"MP3 готов: {mp3_path}")
    return True


def load_model_standalone(logger: Callable[[str], None] = log, spec: Optional[str] = None):
    """Загрузить модель для standalone-режима (UI передаёт свой ``model``).

    T-259: вся механика — CUDA DLL setup, fallback-цепочка compute_type, кэш и
    скачивание — в ``engine``. Здесь остаётся только выбор спеки: явная ``spec``,
    иначе ``settings.ini`` (та же модель, что в UI), иначе дефолт модуля.
    """
    from . import engine

    if spec is None:
        spec = _model_spec_from_settings() or WHISPER_MODEL
    return engine.load_model(spec, logger=logger)


def _read_settings_option(name: str, fallback: str = "") -> str:
    """Одно значение из общего ``settings.ini`` — без зависимости от PySide6.

    Модуль запускается и из UI (настройки уже в памяти), и из CLI, где QSettings
    поднимать не из-за чего.
    """
    ini = profile.settings_file()
    if not ini.exists():
        return fallback
    try:
        import configparser

        cp = configparser.ConfigParser()
        cp.read(ini, encoding="utf-8")
        for section in cp.sections() + ["DEFAULT"]:
            if cp.has_option(section, name):
                return (cp.get(section, name) or "").strip() or fallback
    except Exception:
        pass
    return fallback


def _model_spec_from_settings() -> Optional[str]:
    """Модель из общего ``settings.ini`` saytype."""
    key = _read_settings_option("model")
    if not key:
        return None
    if key == "custom":
        return _read_settings_option("custom_model") or None
    return key


def speakers_from_settings() -> tuple[str, str]:
    """Имена говорящих для транскрипта: («моё имя», «имя собеседника»)."""
    return (
        _read_settings_option("speaker_self", DEFAULT_SPEAKER_L) or DEFAULT_SPEAKER_L,
        _read_settings_option("speaker_other", DEFAULT_SPEAKER_R) or DEFAULT_SPEAKER_R,
    )


def active_model_label() -> str:
    """Имя модели для frontmatter транскрипта: реально загруженная либо из настроек."""
    try:
        from . import engine

        return engine.current_label() or engine.spec_display(
            _model_spec_from_settings() or WHISPER_MODEL
        )
    except Exception:
        return WHISPER_MODEL


def transcribe_channel(
    model,
    audio_int16: np.ndarray,
    language: str = WHISPER_LANG,
    progress_cb: Optional[Callable[[float, float], None]] = None,
    initial_prompt: Optional[str] = None,
    postproc: Optional[Callable[[str], str]] = None,
) -> list[tuple[float, float, str]]:
    """Один канал → список (start, end, text) с сегментными таймстампами.

    ``progress_cb(done_sec, total_sec)`` — опц. (T-173 блок C): вызывается по мере
    выдачи сегментов ленивым генератором faster-whisper, даёт грубый прогресс по
    аудио-таймлайну для UI-индикатора обработки. Исключения в cb проглатываются —
    прогресс не должен ронять транскрипцию.

    2026-07-12: ``initial_prompt`` — словарь пользователя (UI передаёт свой;
    standalone-CLI живёт без него). ``postproc`` — словарные замены на каждый
    сегмент (UI передаёт post_process с tail_rules=False — хвостовые $-правила
    диктовки в сегментах созвона стреляли бы по живой речи).
    """
    audio_f32 = audio_int16.astype(np.float32) / 32768.0
    segments, info = model.transcribe(
        audio_f32,
        language=language,
        beam_size=5,
        vad_filter=True,
        initial_prompt=initial_prompt,
        # 2026-07-12: дефолт condition_on_previous_text=True на 30+ мин созвона
        # тянет одну галлюцинацию контекстом в следующие окна (повторы/loop'ы).
        # В диктовке выключено ещё hotfix'ом 2026-05-19 — сюда не было портировано.
        condition_on_previous_text=False,
    )
    total = float(getattr(info, "duration", 0.0)) or (len(audio_int16) / 16000.0)
    out: list[tuple[float, float, str]] = []
    for seg in segments:
        text = (seg.text or "").strip()
        if text and postproc is not None:
            try:
                text = postproc(text).strip()
            except Exception:
                pass  # словарная замена не должна ронять транскрипцию
        if text:
            out.append((float(seg.start), float(seg.end), text))
        if progress_cb is not None:
            try:
                progress_cb(float(seg.end), total)
            except Exception:
                pass
    return out


def _fmt_ts(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


def merge_segments_to_markdown(
    left_segs: list[tuple[float, float, str]],
    right_segs: list[tuple[float, float, str]],
    speaker_l: Optional[str] = None,
    speaker_r: Optional[str] = None,
) -> str:
    """Merge L/R по start-времени в speaker-attributed транскрипт.

    Соседние сегменты одного спикера склеиваются в одну реплику. Каждая реплика —
    ``[MM:SS] **[Спикер]:** текст``. Имена — из настроек, если не переданы явно.
    """
    if speaker_l is None or speaker_r is None:
        from_settings = speakers_from_settings()
        speaker_l = speaker_l or from_settings[0]
        speaker_r = speaker_r or from_settings[1]
    tagged = [(s, e, speaker_l, t) for (s, e, t) in left_segs]
    tagged += [(s, e, speaker_r, t) for (s, e, t) in right_segs]
    tagged.sort(key=lambda x: x[0])
    if not tagged:
        return "_(речь не распознана ни на одном канале)_\n"

    lines: list[str] = []
    cur_speaker: Optional[str] = None
    cur_start = 0.0
    cur_texts: list[str] = []

    def flush() -> None:
        if cur_texts:
            lines.append(f"[{_fmt_ts(cur_start)}] **[{cur_speaker}]:** " + " ".join(cur_texts))

    for start, _end, speaker, text in tagged:
        if speaker != cur_speaker:
            flush()
            cur_speaker, cur_start, cur_texts = speaker, start, [text]
        else:
            cur_texts.append(text)
    flush()
    return "\n\n".join(lines) + "\n"


def build_markdown(
    recording: CallRecording,
    mp3_path: Optional[Path],
    body: str,
    speaker_l: Optional[str] = None,
    speaker_r: Optional[str] = None,
) -> str:
    """frontmatter (дата, длительность, каналы, модель) + тело транскрипта.

    ``mp3_path=None`` — аудио созвона не сохранялось (дефолт: храним только
    транскрипт) → ``source_recording: null`` + пометка в шапке.
    """
    if speaker_l is None or speaker_r is None:
        from_settings = speakers_from_settings()
        speaker_l = speaker_l or from_settings[0]
        speaker_r = speaker_r or from_settings[1]
    dur = int(round(recording.duration_sec))
    date = recording.started_at.strftime("%Y-%m-%d")
    # 2026-07-12: имя loopback-endpoint'а в frontmatter — чтобы «почему канал R
    # пустой» диагностировался по самому .md (не на какое устройство шёл звук).
    loop_dev = (getattr(recording, "stats", {}) or {}).get("loop_device")
    model_label = active_model_label()  # T-259: модель настраивается, не константа
    if mp3_path is not None:
        source_line = f"source_recording: {mp3_path.as_posix()}"
        audio_note = f"> Аудио-исходник: `{mp3_path}`"
    else:
        source_line = "source_recording: null  # аудио не сохранялось, только транскрипт"
        audio_note = "> Аудио не сохранялось — только транскрипт."
    fm = [
        "---",
        f"date: {date}",
        f"duration_sec: {dur}",
        f'duration_human: "{_fmt_ts(dur)}"',
        "with: <заполнить>",
        "type: <заполнить>",
        "project: <заполнить>",
        "topic: <заполнить>",
        source_line,
        f"whisper_model: {model_label}",
        f"whisper_lang: {WHISPER_LANG}",
        "channels: stereo_LR",
        "speaker_attribution: deterministic_L_R",
        f"loopback_device: {json.dumps(loop_dev, ensure_ascii=False) if loop_dev else 'null'}",
        "---",
        "",
        f"# Расшифровка созвона {date}",
        "",
        f"> Двухканальная расшифровка saytype (faster-whisper `{model_label}`, RU). "
        f"L=микрофон ({speaker_l}), R=системный звук ({speaker_r}). **Не редактировать.**",
        audio_note,
        "",
        "---",
        "",
        body,
    ]
    return "\n".join(fm)


def default_stem_name(started_at: datetime) -> str:
    """Имя файлов записи без расширения: ``YYYY-MM-DD HH-MM-SS``."""
    return started_at.strftime("%Y-%m-%d %H-%M-%S")


# === T-176: recovery прерванной записи из raw-лога ===
def _raw_paths(stem: str) -> tuple[Path, Path, Path]:
    """(mic.f32raw, loop.raw, callmeta.json) для данного stem в папке созвонов."""
    return (
        calls_dir() / f"{stem}{RAW_MIC_SUFFIX}",
        calls_dir() / f"{stem}{RAW_LOOP_SUFFIX}",
        calls_dir() / f"{stem}{RAW_META_SUFFIX}",
    )


def cleanup_raw_log(stem: Optional[str], logger: Callable[[str], None] = log) -> None:
    """Удалить raw-сайдкары записи (mic/loop/meta). Idempotent."""
    if not stem:
        return
    for p in _raw_paths(stem):
        try:
            if p.exists():
                p.unlink()
        except OSError as exc:
            logger(f"[WARN] не смог удалить raw {p.name}: {exc}")


def find_orphaned_recordings() -> list[str]:
    """Stem'ы прерванных записей: есть ``callmeta.json``, но НЕТ собранного ``.wav``.

    Наличие WAV = запись уже финализирована/восстановлена → такой raw устарел
    (его чистит ``cleanup_raw_log`` отдельно). Возвращаем только кандидатов на
    recovery (краш ДО создания WAV), отсортированных по времени (старые первыми).
    """
    out: list[str] = []
    if not calls_dir().exists():
        return out
    for meta_p in sorted(calls_dir().glob(f"*{RAW_META_SUFFIX}")):
        stem = meta_p.name[: -len(RAW_META_SUFFIX)]
        if (calls_dir() / f"{stem}.wav").exists():
            continue
        out.append(stem)
    return out


def recover_recording_to_wav(
    stem: str, logger: Callable[[str], None] = log
) -> Optional[CallRecording]:
    """Собрать прерванную запись из raw-лога → записать WAV (аудио сразу durable).

    Возвращает :class:`CallRecording` (с ``wav_path``) для последующей дотранскрибации,
    либо ``None`` если восстанавливать нечего (mic-сырьё пусто — краш до первого
    звука). После успешной записи WAV сырой raw-лог удаляется (роль выполнена).
    """
    mic_p, loop_p, meta_p = _raw_paths(stem)
    if not meta_p.exists():
        return None
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger(f"[WARN] recovery {stem}: meta нечитаема ({exc!r}) — пропуск")
        return None
    loop_rate = int(meta.get("loop_rate", 48000))
    loop_channels = int(meta.get("loop_channels", 2))
    try:
        started_at = (
            datetime.fromisoformat(meta["started_at"])
            if meta.get("started_at")
            else datetime.now()
        )
    except Exception:
        started_at = datetime.now()

    mic_bytes = mic_p.read_bytes() if mic_p.exists() else b""
    loop_bytes = loop_p.read_bytes() if loop_p.exists() else b""
    mic_bytes = mic_bytes[: len(mic_bytes) // 4 * 4]  # выравнивание float32 (raw мог оборваться)
    if not mic_bytes:
        logger(f"recovery {stem}: mic-сырьё пусто — нечего восстанавливать, чищу raw")
        cleanup_raw_log(stem, logger=logger)
        return None

    mic_f32 = np.frombuffer(mic_bytes, dtype="<f4")
    mic_int16, sys_int16 = build_call_channels(mic_f32, loop_bytes, loop_rate, loop_channels)
    recording = CallRecording(
        mic_int16=mic_int16,
        sys_int16=sys_int16,
        sample_rate=MIC_SAMPLE_RATE,
        started_at=started_at,
        stats={"recovered": True, "loop_silent": not loop_bytes},
        raw_stem=stem,
    )
    wav_path = calls_dir() / f"{stem}.wav"
    write_stereo_wav(wav_path, mic_int16, sys_int16, MIC_SAMPLE_RATE)
    recording.wav_path = wav_path
    logger(f"recovery {stem}: WAV восстановлен ({recording.duration_sec:.1f}s) → {wav_path}")
    cleanup_raw_log(stem, logger=logger)
    return recording


def rotate_call_audio(keep: int = CALL_AUDIO_KEEP, logger: Callable[[str], None] = log) -> int:
    """Ротация буфера WAV созвонов в папке ``Calls`` (T-175, durability-страховка).

    Держим последние ``keep`` WAV-записей созвонов как возможность «вернуться»
    (переслушать / дотранскрибировать, если транскрипт кривой). Старейшие (>keep)
    по mtime удаляются при каждой новой записи. Возвращает число удалённых файлов.

    Трогает **только ``*.wav``** в папке ``Calls``: транскрипты ``.md`` (T-174 — не
    ротируются, их разбирает пользователь) и ``.mp3`` (keep_audio-архив) НЕ
    затрагиваются. Надиктовки в корне папки истории — отдельный ``rotate_history()``
    в ``transcribe_ui.py``; эта функция их не видит (сканит только подпапку
    ``Calls\\``), так что буферы независимы.
    """
    if keep < 0:
        return 0
    wavs = sorted(calls_dir().glob("*.wav"), key=lambda p: p.stat().st_mtime)
    removed = 0
    while len(wavs) > keep:
        oldest = wavs.pop(0)
        try:
            oldest.unlink()
            removed += 1
            logger(f"буфер созвонов: удалён старый WAV {oldest.name} (держим последние {keep})")
        except OSError as exc:
            logger(f"[WARN] буфер созвонов: не смог удалить {oldest.name}: {exc}")
    return removed


def finalize_recording(
    recording: CallRecording,
    model=None,
    name: Optional[str] = None,
    keep_audio: bool = False,
    call_audio_keep: Optional[int] = None,
    do_transcribe: bool = True,
    logger: Callable[[str], None] = log,
    progress_cb: Optional[Callable[[float], None]] = None,
    initial_prompt: Optional[str] = None,
    postproc: Optional[Callable[[str], str]] = None,
    speaker_l: Optional[str] = None,
    speaker_r: Optional[str] = None,
) -> dict:
    """WAV → (MP3) → L/R транскрипт → merge в один ``.md``.

    Всё складывается в подпапку ``Calls`` папки истории — **вне ротации
    надиктовок**: ``rotate_history`` сканирует только корень history и в
    подпапку не заходит, поэтому транскрипты (.md) не удаляются автоматически.

    WAV созвона = **ротируемый буфер последних N записей** (T-175;
    ``call_audio_keep``, иначе дефолт ``CALL_AUDIO_KEEP``=2): остаётся после
    транскрипта как страховка «вернуться» (переслушать / дотранскрибировать).
    Старейшие (>N) удаляет ``rotate_call_audio()`` по mtime — **только ``.wav``**,
    не трогая .md/.mp3. При сбое транскрипта WAV тоже остаётся (и тем более
    нужен — .md ещё нет).

    MP3 (опц. долговременный архив, независим от WAV-буфера, не ротируется):
      * ``keep_audio=False`` (дефолт) — MP3 не создаётся, только WAV-буфер + .md.
      * ``keep_audio=True`` — MP3 128k рядом с .md, если в системе есть ffmpeg.

    ``model=None`` — загрузить свою (standalone). Возвращает dict путей и метаданных.
    """
    # 2026-07-28: логгер оборачиваем ПЕРВЫМ делом — падение лога не должно
    # стоить нам транскрипта 45-минутного созвона (см. `_safe_logger`).
    logger = _safe_logger(logger)
    stem = name or default_stem_name(recording.started_at)
    dest = calls_dir()
    dest.mkdir(parents=True, exist_ok=True)

    # Временный WAV — в подпапке Calls, НЕ в корне history: иначе попал бы под
    # rotate_history() надиктовок (glob '*.wav' по корню) и мог бы вытеснить
    # настоящую запись пользователя.
    wav_path = calls_dir() / f"{stem}.wav"
    write_stereo_wav(wav_path, recording.mic_int16, recording.sys_int16, recording.sample_rate)
    recording.wav_path = wav_path
    logger(f"stereo WAV: {wav_path}")
    # T-176: WAV записан и durable → сырой raw-лог (его роль — пережить краш ВО ВРЕМЯ
    # записи) больше не нужен; дальше страховка — сам WAV + ротируемый буфер T-175.
    cleanup_raw_log(recording.raw_stem or stem, logger=logger)

    result: dict = {"wav_path": wav_path, "stem": stem}

    mp3_path = dest / f"{stem}.mp3"
    mp3_ok = convert_to_mp3(wav_path, mp3_path, logger=logger) if keep_audio else False
    result["mp3_path"] = mp3_path if mp3_ok else None

    md_path: Optional[Path] = None
    if do_transcribe:
        # Транскрипция — НЕ фатальна: сбой модели/GPU не должен терять запись (WAV ещё
        # на диске). Шаг идёт ~15-30 сек ПОСЛЕ записи (load + 2 прохода).
        try:
            logger("⏳ транскрибирую: загрузка модели + 2 прохода (L/R), подожди ~15-30 сек...")
            if model is None:
                model = load_model_standalone(logger=logger)
            # Прогресс (T-173 C): два прохода по одному аудио-таймлайну → канал L
            # даёт 0..50%, канал R — 50..100%. progress_cb(fraction 0..1) → UI.
            def _l_progress(done: float, total: float) -> None:
                if progress_cb is not None and total > 0:
                    progress_cb(0.5 * min(done / total, 1.0))

            def _r_progress(done: float, total: float) -> None:
                if progress_cb is not None and total > 0:
                    progress_cb(0.5 + 0.5 * min(done / total, 1.0))

            logger("транскрипт канала L (mic)...")
            left = transcribe_channel(
                model, recording.mic_int16, progress_cb=_l_progress,
                initial_prompt=initial_prompt, postproc=postproc,
            )
            logger(f"  L: {len(left)} сегментов")
            logger("транскрипт канала R (sys)...")
            right = transcribe_channel(
                model, recording.sys_int16, progress_cb=_r_progress,
                initial_prompt=initial_prompt, postproc=postproc,
            )
            logger(f"  R: {len(right)} сегментов")
            if progress_cb is not None:
                try:
                    progress_cb(1.0)
                except Exception:
                    pass

            body = merge_segments_to_markdown(
                left, right, speaker_l=speaker_l, speaker_r=speaker_r
            )
            # 2026-07-12: мёртвый R-канал — loopback писал цифровую тишину, потому
            # что звук созвона шёл на гарнитуру, а устройством вывода «по умолчанию»
            # был монитор. Раньше это давало молчаливую «портянку» из одной реплики
            # без намёка на проблему — теперь явный баннер.
            if left and not right:
                loop_dev = (getattr(recording, "stats", {}) or {}).get("loop_device") or "?"
                body = (
                    "> ⚠️ **Канал собеседника (R) пуст — спикеры не разделены.** "
                    "WASAPI loopback записал тишину: звук созвона шёл не на то устройство "
                    f"вывода (loopback писал endpoint: {loop_dev}). Ниже — только микрофон.\n"
                    "> Фикс: перед созвоном сделать устройством вывода Windows «по умолчанию» "
                    "то, где реально слышно собеседника (гарнитуру), и перезапустить запись.\n\n"
                ) + body
                result["loop_dead"] = True
                logger("[WARN] канал R пуст — транскрипт без разделения спикеров (баннер в .md)")
            md_path = dest / f"{stem}.md"
            md_path.write_text(
                build_markdown(
                    recording, mp3_path if mp3_ok else None, body,
                    speaker_l=speaker_l, speaker_r=speaker_r,
                ),
                encoding="utf-8",
            )
            logger(f"speaker-merged .md: {md_path}")
            result["md_path"] = md_path
            result["segments_L"] = len(left)
            result["segments_R"] = len(right)
        except Exception as exc:
            import traceback
            logger(f"[ERR] транскрипт упал: {exc!r} — .md НЕ создан.")
            logger(traceback.format_exc())
            logger(f"WAV оставлен для дотранскрибации: {wav_path}")
            result["md_path"] = None
            result["transcribe_error"] = repr(exc)
    else:
        logger("[skip-transcribe] транскрипт пропущен")
        result["md_path"] = None

    # Аудио-политика T-175 (durability-буфер): WAV созвона НЕ удаляется после
    # успешного транскрипта — остаётся в Calls\ как страховка «вернуться»
    # (переслушать / дотранскрибировать кривой транскрипт). При сбое транскрипта
    # WAV тем более остаётся (раньше это был единственный кейс удержания).
    # Держим последние CALL_AUDIO_KEEP записей; старейшие удаляет ротация ниже.
    if md_path is not None:
        logger(f"WAV оставлен в буфере созвонов: {wav_path}")
    else:
        logger(f"WAV сохранён (нет готового транскрипта): {wav_path}")
    # Ротация буфера: только *.wav в Calls\; .md (T-174) и .mp3 (keep_audio) не трогаются.
    # Текущий WAV — новейший по mtime, при keep>=1 не попадёт под удаление.
    # keep из Settings UI (call_audio_keep) или дефолт CALL_AUDIO_KEEP для CLI/standalone.
    keep_n = CALL_AUDIO_KEEP if call_audio_keep is None else int(call_audio_keep)
    rotate_call_audio(keep=keep_n, logger=logger)

    return result


def _parse_mic_device(arg: Optional[str]):
    if not arg:
        return None
    return int(arg) if arg.lstrip("-").isdigit() else arg


def main() -> int:
    parser = argparse.ArgumentParser(description="saytype call recorder (standalone)")
    parser.add_argument("--duration", type=float, default=None, help="секунды записи")
    parser.add_argument("--list-devices", action="store_true",
                        help="показать аудио-устройства (default input + WASAPI loopback) и выйти")
    parser.add_argument("--mic-device", default=None, help="имя/индекс input для sounddevice")
    parser.add_argument("--name", default=None, help="имя файла без расширения (по умолч. YYYY-MM-DD HH-MM-SS)")
    parser.add_argument("--history-dir", default=None,
                        help="папка истории (по умолч. — из настроек / профиля пользователя)")
    parser.add_argument("--skip-transcribe", action="store_true", help="не грузить модель / не транскрибировать")
    parser.add_argument("--keep-audio", action="store_true",
                        help="оставить MP3 128k рядом с .md (нужен ffmpeg в PATH)")
    args = parser.parse_args()

    configured = _read_settings_option("history_dir")
    if args.history_dir:
        set_history_dir(args.history_dir)
    elif configured:
        set_history_dir(configured)

    if args.list_devices:
        log("=== sounddevice (input/output, '>' = default) ===")
        log(str(sd.query_devices()))
        try:
            with pyaudio.PyAudio() as pa:
                dev = find_loopback_device(pa)
                log(f"\nWASAPI loopback для текущего default playback: #{dev['index']} "
                    f"{dev['name']!r} (rate={int(dev['defaultSampleRate'])} ch={int(dev['maxInputChannels'])})")
        except Exception as exc:
            log(f"\nloopback resolve fail: {exc!r}")
        return 0

    if args.duration is None:
        parser.error("нужен --duration <секунды> (или --list-devices)")

    rec = CallRecorder(mic_device=_parse_mic_device(args.mic_device))
    try:
        rec.start()
    except RuntimeError as exc:
        log(f"[ERR] {exc}")
        return 3

    log(f"recording {args.duration:.0f}s... (Ctrl+C — досрочный стоп с сохранением)")
    interrupted = False
    try:
        deadline = time.perf_counter() + args.duration
        while time.perf_counter() < deadline:
            time.sleep(min(0.5, deadline - time.perf_counter()))
    except KeyboardInterrupt:
        interrupted = True
        log("\n[INTERRUPT] Ctrl+C — стоп, сохраняю что записалось")

    try:
        recording = rec.stop()
    except RuntimeError as exc:
        log(f"[ERR] {exc}")
        return 2
    recording.stats["interrupted"] = interrupted

    result = finalize_recording(
        recording,
        name=args.name,
        keep_audio=args.keep_audio,
        do_transcribe=not args.skip_transcribe,
    )

    log("")
    log("--- итог ---")
    log(f"WAV:  {result.get('wav_path')}")
    log(f"MP3:  {result.get('mp3_path')}")
    log(f"MD:   {result.get('md_path')}")
    log(f"(папка созвонов: {calls_dir()})")
    return 0


if __name__ == "__main__":
    # T-268: см. engine.hard_exit — разрушение модели CT2 после генерации с
    # temperature-fallback убивает процесс с 0xC0000409. Транскрипт к этому
    # моменту уже на диске, но код возврата выглядел бы как провал записи.
    from . import engine as _engine

    _engine.hard_exit(main())
