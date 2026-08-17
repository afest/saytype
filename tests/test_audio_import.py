"""Импорт аудиофайла (T-351): декодер QAudioDecoder и выбор имени для txt.

Модель не грузится — проверяется только путь «файл → numpy 16 кГц mono float32».
Нужен Qt (QCoreApplication) и FFmpeg-плагин Qt Multimedia, то есть тот же PySide6,
что у приложения.

Запуск:  python -m tests.test_audio_import   (из корня репозитория)
"""

import math
import os
import struct
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # тесты не открывают окон

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from saytype import audio_import  # noqa: E402

FAILED = []
# Именно QApplication, а не QCoreApplication: в одном прогоне pytest живёт ещё и
# оконный тест, а QWidget поверх «просто ядра» роняет процесс без трейсбека.
_APP = QApplication.instance() or QApplication(sys.argv)
_TMP = Path(tempfile.mkdtemp(prefix="saytype-import-test-"))


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def make_wav(path: Path, seconds: float, rate: int, channels: int, freq: float = 440.0) -> Path:
    """Синус нужной длины — фикстура, которая не тянет за собой внешних файлов."""
    frames = int(seconds * rate)
    samples = []
    for i in range(frames):
        value = int(0.4 * 32767 * math.sin(2 * math.pi * freq * i / rate))
        samples.extend([value] * channels)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack("<%dh" % len(samples), *samples))
    return path


def test_decode_native_format() -> None:
    print("wav 16 кГц mono — формат приложения:")
    src = make_wav(_TMP / "native.wav", seconds=2.0, rate=16000, channels=1)
    audio = audio_import.decode_audio_file(src)
    check("тип float32", audio.dtype == np.float32, str(audio.dtype))
    check("одномерный массив", audio.ndim == 1, str(audio.shape))
    dur = audio.size / audio_import.SAMPLE_RATE
    check("длительность ~2 с", 1.9 < dur < 2.1, f"{dur:.3f}")
    check("не тишина", float(np.abs(audio).max()) > 0.1, str(float(np.abs(audio).max())))


def test_decode_resamples_and_downmixes() -> None:
    print("wav 44.1 кГц stereo — сводим в 16 кГц mono сами:")
    src = make_wav(_TMP / "stereo44.wav", seconds=1.5, rate=44100, channels=2)
    audio = audio_import.decode_audio_file(src)
    dur = audio.size / audio_import.SAMPLE_RATE
    check("длительность ~1.5 с", 1.4 < dur < 1.6, f"{dur:.3f}")
    check("тип float32", audio.dtype == np.float32, str(audio.dtype))


def test_decode_mp3_44100() -> None:
    """Регрессия T-351: mp3 44.1 кГц вешал декодер, когда частоту просили у Qt.

    Живые файлы (`Blog/hub/ai-клон/голос/*.mp3`) не читались вообще: ни буферов,
    ни `finished`, ни ошибки — только watchdog. Фикстура повторяет их профиль
    (mp3, 44100 Гц, mono); ресэмплинг теперь наш, а не Qt-шный.
    """
    print("mp3 44.1 кГц mono — фикстура из tests/fixtures:")
    src = Path(__file__).resolve().parent / "fixtures" / "sine-44100-mono.mp3"
    check("фикстура на месте", src.exists(), str(src))
    if not src.exists():
        return
    audio = audio_import.decode_audio_file(src)
    dur = audio.size / audio_import.SAMPLE_RATE
    check("длительность ~2 с", 1.9 < dur < 2.2, f"{dur:.3f}")
    check("тип float32", audio.dtype == np.float32, str(audio.dtype))
    check("не тишина", float(np.abs(audio).max()) > 0.1, str(float(np.abs(audio).max())))


def test_decode_mp4_container() -> None:
    """Видеоконтейнер: берём звуковую дорожку (T-382 — «кто-то закинет урок»).

    Фикстура — mp4/AAC 48 кГц stereo, то есть ровно профиль записи с телефона
    или скринкаста: и контейнер другой, и частота, и число каналов.
    """
    print("mp4 (AAC 48 кГц stereo) — звуковая дорожка из видеоконтейнера:")
    src = Path(__file__).resolve().parent / "fixtures" / "sine-48000-stereo.mp4"
    check("фикстура на месте", src.exists(), str(src))
    if not src.exists():
        return
    audio = audio_import.decode_audio_file(src)
    dur = audio.size / audio_import.SAMPLE_RATE
    check("длительность ~2 с", 1.85 < dur < 2.25, f"{dur:.3f}")
    check("моно", audio.ndim == 1, str(audio.shape))
    check("не тишина", float(np.abs(audio).max()) > 0.1, str(float(np.abs(audio).max())))
    check("расширение в списке", audio_import.is_supported(src))


def test_resample_helper() -> None:
    print("пересчёт частоты:")
    src = np.sin(2 * np.pi * 440 * np.arange(44100) / 44100).astype(np.float32)
    out = audio_import.resample_to_16k(src, 44100)
    check("длина по коэффициенту", abs(out.size - 16000) <= 2, str(out.size))
    check("тип float32", out.dtype == np.float32, str(out.dtype))
    check("амплитуда сохранилась", 0.8 < float(np.abs(out).max()) < 1.2,
          str(float(np.abs(out).max())))
    same = audio_import.resample_to_16k(src, 16000)
    check("16 кГц не трогается", same is src or np.array_equal(same, src))


def test_progress_and_cancel() -> None:
    print("прогресс и отмена:")
    src = make_wav(_TMP / "progress.wav", seconds=3.0, rate=16000, channels=1)
    seen = []
    audio_import.decode_audio_file(src, progress_cb=lambda d, t: seen.append((d, t)))
    check("прогресс вызывался", len(seen) >= 2, f"{len(seen)} вызовов")
    check("прогресс не убывает", all(
        seen[i][0] <= seen[i + 1][0] for i in range(len(seen) - 1)
    ))
    cancelled = False
    try:
        audio_import.decode_audio_file(src, should_cancel=lambda: True)
    except audio_import.AudioImportCancelled:
        cancelled = True
    check("отмена поднимает AudioImportCancelled", cancelled)


def test_errors_are_readable() -> None:
    print("понятные ошибки вместо трейсбека:")
    missing = False
    try:
        audio_import.decode_audio_file(_TMP / "нет-такого.wav")
    except audio_import.AudioImportError as exc:
        missing = "не найден" in str(exc).lower()
    check("нет файла", missing)

    broken_path = _TMP / "broken.wav"
    broken_path.write_bytes(b"RIFF____WAVEfmt not really audio at all")
    broken = False
    try:
        audio_import.decode_audio_file(broken_path)
    except audio_import.AudioImportError:
        broken = True
    check("битый файл", broken)


def test_supported_suffixes() -> None:
    print("список форматов:")
    check("wav поддержан", audio_import.is_supported("a.WAV"))
    check("голосовое телеграма поддержано", audio_import.is_supported("voice.oga"))
    check("видео поддержано (звуковая дорожка)", audio_import.is_supported("clip.mp4"))
    check("текст не поддержан", not audio_import.is_supported("readme.txt"))


def test_output_txt_path() -> None:
    print("куда ложится txt:")
    src = make_wav(_TMP / "интервью.wav", seconds=0.2, rate=16000, channels=1)
    first = audio_import.output_txt_path(src)
    check("рядом с исходником", first == src.with_suffix(".txt"), str(first))
    first.write_text("уже есть", encoding="utf-8")
    second = audio_import.output_txt_path(src)
    check("чужой txt не перезаписывается", second != first and not second.exists(), str(second))
    check("имя с номером", second.name == "интервью (2).txt", second.name)


def main() -> int:
    for fn in (
        test_decode_native_format,
        test_decode_resamples_and_downmixes,
        test_decode_mp3_44100,
        test_decode_mp4_container,
        test_resample_helper,
        test_progress_and_cancel,
        test_errors_are_readable,
        test_supported_suffixes,
        test_output_txt_path,
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
