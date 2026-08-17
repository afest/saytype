"""Импорт готового аудио- или видеофайла в формат, в котором работает диктовка.

T-351. Пользователь приносит файл (голосовое из мессенджера, диктофон, запись
урока или созвона видеофайлом) — мы декодируем его в float32 16 кГц mono и
отдаём модели ровно тем же путём, что и живую запись с микрофона. Из видео
берётся звуковая дорожка: контейнер декодеру безразличен, видеопоток он просто
не выдаёт — проверено на mp4/AAC 48 кГц и mkv.

**Почему QAudioDecoder, а не отдельная библиотека.** FFmpeg уже лежит в
поставке — его кладёт Qt Multimedia (LGPL-2.1, собран без ``--enable-gpl``):
``avcodec``/``avformat``/``avutil``/``swresample`` + ``ffmpegmediaplugin``. Значит
декодирование доступно без единой новой зависимости и без нового лицензионного
обязательства. Путь к файлу самой модели отдавать нельзя (правило 11 проекта,
T-318): faster-whisper декодировал бы его через PyAV, а тот везёт FFmpeg с
libx264/libx265 под GPLv2+. Альтернативы (``soundfile``, ``miniaudio``, внешний
``ffmpeg.exe``) либо добавляют лицензионное обязательство, либо не покрывают
форматы, которые Qt покрывает бесплатно — разбор в
``Code/projects/iwhisper/docs/оценка-новых-фич-2026-08.md`` (T-350).

**Поток.** ``QAudioDecoder`` — Qt-объект, поэтому декодирование обязано идти в
GUI-потоке (T-284: из worker'ов к Qt только сигналами). Функция синхронная, но
внутри крутит локальный ``QEventLoop`` — окно остаётся живым, кнопка «Отмена»
кликается. В worker уходит уже готовый numpy-массив.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

SAMPLE_RATE = 16000
CHANNELS = 1

# Потолок длительности. 4 часа float32-моно — это ~920 МБ в RAM; дальше импорт
# упирается не в модель, а в память, и честнее отказать с внятным текстом.
MAX_IMPORT_SEC = 4 * 3600

# Декодер молчит дольше этого — считаем, что он завис (битый контейнер, файл на
# отвалившемся сетевом диске, формат, который плагин Qt не осилил молча). Без
# этого QEventLoop крутился бы вечно. Ошибка выдаётся на втором пустом тике, то
# есть реально ждём вдвое дольше.
STALL_TIMEOUT_MS = 15_000

# Что предлагаем в диалоге и что принимаем перетаскиванием. Это **подсказка, а не
# запрет**: незнакомое расширение окно предлагает попробовать, а не отвергает.
# Причина — `QMediaFormat.supportedAudioCodecs` занижает список (opus в нём нет,
# а файлы декодируются, T-350), то есть заранее знать полный набор нельзя.
SUPPORTED_SUFFIXES = {
    # аудио
    ".wav", ".mp3", ".m4a", ".m4b", ".aac", ".flac", ".ogg", ".oga", ".opus",
    ".wma", ".aif", ".aiff", ".amr", ".caf", ".mpga", ".weba", ".wv",
    # видео — берём звуковую дорожку
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".3gp", ".mpg",
    ".mpeg", ".ts", ".flv",
}

# Заполняется при первом декодировании (нужен импортированный QAudioFormat).
_SAMPLE_DTYPES: dict = {}

FILE_DIALOG_FILTER = (
    "Аудио и видео (*.wav *.mp3 *.m4a *.m4b *.aac *.flac *.ogg *.oga *.opus "
    "*.wma *.aif *.aiff *.amr *.caf *.mpga *.weba *.wv *.mp4 *.m4v *.mov *.mkv "
    "*.avi *.wmv *.webm *.3gp *.mpg *.mpeg *.ts *.flv);;Все файлы (*)"
)


class AudioImportError(RuntimeError):
    """Файл не удалось декодировать — текст исключения показывается пользователю."""


class AudioImportCancelled(RuntimeError):
    """Пользователь нажал «Отмена» во время декодирования."""


def is_supported(path: "Path | str") -> bool:
    return Path(path).suffix.lower() in SUPPORTED_SUFFIXES


def decode_audio_file(
    path: "Path | str",
    *,
    progress_cb: Optional[Callable[[float, float], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> np.ndarray:
    """Файл → float32 16 кГц mono (тот же формат, что даёт микрофонный поток).

    ``progress_cb(done_sec, total_sec)`` — сколько аудио уже декодировано;
    ``total_sec`` = 0.0, пока длительность неизвестна (у части контейнеров она
    появляется не сразу). ``should_cancel()`` опрашивается по ходу: True →
    ``AudioImportCancelled``.

    Вызывать **только из GUI-потока** — внутри живёт Qt-объект и event loop.
    """
    from PySide6.QtCore import QCoreApplication, QEventLoop, QThread, QTimer, QUrl
    from PySide6.QtMultimedia import QAudioDecoder, QAudioFormat

    global _SAMPLE_DTYPES
    if not _SAMPLE_DTYPES:
        # Что декодер может отдать в своём родном формате: mp3/aac приходят
        # Float, wav — Int16. Ключи резолвятся лениво: QAudioFormat живёт в
        # PySide6, а модуль должен импортироваться и без Qt (тесты, CLI).
        _SAMPLE_DTYPES = {
            QAudioFormat.Float: (np.float32, 0.0),
            QAudioFormat.Int16: (np.int16, 32768.0),
            QAudioFormat.Int32: (np.int32, 2147483648.0),
            QAudioFormat.UInt8: (np.uint8, 0.0),  # смещение, а не деление — см. to_float_mono
        }

    src = Path(path)
    if not src.exists():
        raise AudioImportError(f"Файл не найден: {src}")
    if src.is_dir():
        raise AudioImportError("Это папка, а не файл записи")

    app = QCoreApplication.instance()
    if app is None:
        raise AudioImportError("Декодер недоступен: нет QApplication")
    if QThread.currentThread() is not app.thread():
        # T-284: единственная причина этой проверки — не дать будущей правке
        # утащить декодирование в worker-поток. Падение было бы не здесь, а
        # где-то внутри Qt через секунды, без трейсбека.
        raise AudioImportError("Декодирование должно идти в главном потоке")

    if should_cancel is not None and should_cancel():
        raise AudioImportCancelled("Импорт отменён")

    # Целевой формат декодеру НЕ задаём — берём его собственный и приводим сами.
    # Причина найдена на живых файлах (mp3 44.1 кГц mono): просьба выдать
    # 16 кГц вешала ffmpeg-плагин Qt намертво — ни буферов, ни `finished`, ни
    # ошибки, только наш watchdog. Тот же файл без запрошенной частоты
    # декодируется мгновенно, и wav 44.1 кГц с ресэмплом тоже проходит: ломается
    # ресэмплер Qt именно на mp3. Свой ресэмпл (`resample_poly`, тот же, что
    # сводит loopback-канал созвона) убирает эту зависимость целиком.
    decoder = QAudioDecoder()
    decoder.setSource(QUrl.fromLocalFile(str(src.resolve())))

    loop = QEventLoop()
    chunks: list[np.ndarray] = []
    state = {"frames": 0, "error": "", "cancelled": False, "seen_frames": -1, "rate": 0}

    def total_sec() -> float:
        dur_ms = decoder.duration()
        return float(dur_ms) / 1000.0 if dur_ms and dur_ms > 0 else 0.0

    def to_float_mono(buf) -> "np.ndarray | None":
        """Буфер Qt в его собственном формате → float32 mono [-1, 1]."""
        buf_fmt = buf.format()
        sample_format = buf_fmt.sampleFormat()
        channels = max(1, buf_fmt.channelCount())
        dtype, scale = _SAMPLE_DTYPES.get(sample_format, (None, 0.0))
        if dtype is None:
            state["error"] = f"Декодер отдал неизвестный формат сэмплов ({sample_format})"
            return None
        # memoryview на буфер Qt — копируем сразу: Qt переиспользует память.
        raw = np.frombuffer(bytes(buf.data()), dtype=dtype)
        if raw.size == 0:
            return raw.astype(np.float32)
        if dtype == np.uint8:
            data = (raw.astype(np.float32) - 128.0) / 128.0
        elif scale:
            data = raw.astype(np.float32) / scale
        else:
            data = raw.astype(np.float32, copy=False)
        if channels > 1:
            usable = (data.size // channels) * channels
            data = data[:usable].reshape(-1, channels).mean(axis=1)
        return data

    def drain() -> None:
        while decoder.bufferAvailable():
            # Отмену проверяем на каждом буфере, а не только по таймеру: короткий
            # файл успевает раскодироваться целиком между двумя тиками, и нажатая
            # «Отмена» тогда просто не замечалась бы.
            if should_cancel is not None and should_cancel():
                state["cancelled"] = True
                loop.quit()
                return
            buf = decoder.read()
            if not buf.isValid():
                break
            rate = buf.format().sampleRate()
            if state["rate"] and rate != state["rate"]:
                # Смена частоты посреди файла — склеивать такое вслепую нельзя:
                # часть речи поехала бы по скорости.
                state["error"] = (
                    f"Частота меняется по ходу файла ({state['rate']} → {rate} Гц)"
                )
                loop.quit()
                return
            state["rate"] = rate
            chunk = to_float_mono(buf)
            if chunk is None:
                loop.quit()
                return
            if chunk.size:
                chunks.append(chunk)
                state["frames"] += chunk.size
            if rate and state["frames"] > MAX_IMPORT_SEC * rate:
                state["error"] = (
                    f"Файл длиннее {MAX_IMPORT_SEC // 3600} ч — такой объём "
                    "не влезет в память. Разрежьте файл на части."
                )
                loop.quit()
                return
            if progress_cb is not None and rate:
                try:
                    progress_cb(state["frames"] / rate, total_sec())
                except Exception:
                    pass  # индикатор не должен ронять импорт

    def on_error(_err) -> None:
        state["error"] = decoder.errorString() or "неизвестная ошибка декодера"
        loop.quit()

    def on_finished() -> None:
        drain()
        loop.quit()

    def on_tick() -> None:
        """Опрос отмены и watchdog зависшего декодера (событий может не быть вовсе)."""
        if should_cancel is not None and should_cancel():
            state["cancelled"] = True
            loop.quit()
            return
        if state["frames"] == state["seen_frames"]:
            state["error"] = (
                "Декодер не ответил за 30 секунд — формат не поддерживается, файл "
                "повреждён или недоступен. Проверенные форматы: "
                + ", ".join(sorted(s.lstrip('.') for s in SUPPORTED_SUFFIXES))
            )
            loop.quit()
            return
        state["seen_frames"] = state["frames"]

    decoder.bufferReady.connect(drain)
    decoder.finished.connect(on_finished)
    decoder.error.connect(on_error)

    cancel_timer = QTimer()
    cancel_timer.setInterval(100)
    cancel_timer.timeout.connect(
        lambda: (
            (state.__setitem__("cancelled", True), loop.quit())
            if should_cancel is not None and should_cancel()
            else None
        )
    )
    stall_timer = QTimer()
    stall_timer.setInterval(STALL_TIMEOUT_MS)
    stall_timer.timeout.connect(on_tick)

    try:
        cancel_timer.start()
        stall_timer.start()
        decoder.start()
        loop.exec()
    finally:
        cancel_timer.stop()
        stall_timer.stop()
        try:
            decoder.stop()
        except Exception:
            pass
        # Отключаем слоты явно: декодер живёт до сборки мусора, а loop уже мёртв.
        for sig, slot in (
            (decoder.bufferReady, drain),
            (decoder.finished, on_finished),
            (decoder.error, on_error),
        ):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    if state["cancelled"]:
        raise AudioImportCancelled("Импорт отменён")
    if state["error"]:
        raise AudioImportError(state["error"])
    if not chunks:
        raise AudioImportError(
            "В файле не нашлось звуковой дорожки (или формат не поддерживается)"
        )

    audio = np.concatenate(chunks, axis=0).astype(np.float32, copy=False)
    audio = resample_to_16k(audio, state["rate"])
    if progress_cb is not None:
        try:
            progress_cb(audio.size / SAMPLE_RATE, audio.size / SAMPLE_RATE)
        except Exception:
            pass
    return audio


def resample_to_16k(audio: np.ndarray, source_rate: int) -> np.ndarray:
    """Привести float32 mono к 16 кГц. `resample_poly` — тот же путь, которым
    сводится loopback-канал созвона (`transcribe_call`), а не своя интерполяция."""
    if not source_rate or source_rate == SAMPLE_RATE or audio.size == 0:
        return audio.astype(np.float32, copy=False)
    from scipy.signal import resample_poly

    return resample_poly(audio, SAMPLE_RATE, source_rate).astype(np.float32, copy=False)


def output_txt_path(src: "Path | str") -> Path:
    """Куда лечь транскрипту: ``<файл>.txt`` рядом с исходником (как батч-CLI).

    Чужой ``.txt`` не перезаписываем — рядом лёг бы результат поверх готового
    текста, а импортируем мы как раз чужие файлы.
    """
    src = Path(src)
    candidate = src.with_suffix(".txt")
    if not candidate.exists():
        return candidate
    for n in range(2, 100):
        alt = src.with_name(f"{src.stem} ({n}).txt")
        if not alt.exists():
            return alt
    return src.with_name(f"{src.stem} ({src.stat().st_mtime_ns}).txt")


@dataclass(frozen=True)
class FileImportApi:
    """Мост «окно → движок» для импорта файла (T-351).

    Живёт здесь, а не в ``transcribe_ui``, потому что импортируют его оба модуля:
    окно вызывает, движок предоставляет, а обратный импорт ``transcribe_ui``
    из окна был бы циклическим (и поднял бы single-instance mutex).

    ``begin()`` — занять движок (False, если он уже занят записью / созвоном /
    другим импортом); ``run(path, audio, progress_cb, should_cancel)`` — worker-поток,
    возвращает словарь с результатом; ``end()`` — освободить движок.
    """

    begin: Callable[[], bool]
    run: Callable[..., dict]
    end: Callable[[], None]
