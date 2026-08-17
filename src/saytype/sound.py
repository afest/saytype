"""T-355: короткие звуковые сигналы старт/стоп диктовки.

История двух неудачных попыток:
1) `winsound.PlaySound(SND_MEMORY | SND_ASYNC)` — комбинация не поддерживается
   Python (`RuntimeError: Cannot play asynchronously from memory`), тихо
   глоталась широким `except`.
2) `winsound.PlaySound(SND_MEMORY)` без `ASYNC` в отдельном потоке — исключений
   не бросало, но звук был слышен нестабильно (то есть, то нет), пока в микшере
   громкости не подвигать ползунок — после этого звук появлялся.
   Классический симптом Windows Audio Session: WinMM (`winsound`) открывает и
   закрывает нативное аудио-устройство на КАЖДЫЙ вызов; если процесс не трогал
   звук какое-то время, сессия успевает "остыть", и короткий (90-130мс) сигнал
   укладывается в задержку установления сессии — играет в момент, когда сессия
   ещё не до конца согласована с аудио-движком.

Фикс — `QSoundEffect` (PySide6.QtMultimedia, уже зависимость проекта). В
отличие от `winsound`, держит аудио-поток открытым всё время жизни объекта
(создаётся один раз при старте приложения) — сессия остаётся "тёплой", той же
проблемы с прогревом при коротких редких звуках нет.

`QSoundEffect` не умеет играть из байтов в памяти — только `QUrl` на файл,
поэтому синтезированные тона один раз пишутся в профиль пользователя
(`profile.sounds_dir()`, как иконки трея — код генерирует сам, не asset).

Thread-safety: `QSoundEffect` — обычный QObject, живёт в Qt main thread и
безопасно дёргается только оттуда. Три из четырёх точек вызова (старт
надиктовки, диктовка/запись во время созвона) уже выполняются в main thread.
Четвёртая (`stop_recording_and_transcribe`, worker-поток `_stop_thread`) должна
звать `play_stop()` через `Qt.QueuedConnection`-сигнал, а не напрямую — см.
`window.notify_stop_sound` в `transcribe_ui.py`/`transcribe_ui_window.py`.
"""

from __future__ import annotations

import io
import math
import struct
import wave
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QSoundEffect

from . import profile

_SAMPLE_RATE = 22050
_FADE_SEC = 0.008  # ~8мс fade in/out — иначе на границах тона слышен щелчок


def _tone_wav_bytes(freq_hz: float, duration_ms: int, volume: float = 0.6) -> bytes:
    n_samples = int(_SAMPLE_RATE * duration_ms / 1000)
    fade_samples = max(1, int(_SAMPLE_RATE * _FADE_SEC))
    frames = bytearray()
    for i in range(n_samples):
        amp = volume
        if i < fade_samples:
            amp *= i / fade_samples
        elif i > n_samples - fade_samples:
            amp *= (n_samples - i) / fade_samples
        sample = amp * math.sin(2 * math.pi * freq_hz * i / _SAMPLE_RATE)
        frames += struct.pack("<h", int(sample * 32767))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


def _write_if_changed(path: Path, data: bytes) -> None:
    if not path.exists() or path.read_bytes() != data:
        path.write_bytes(data)


# Выше тон на старте, ниже на стопе — как в диктофонах/Zoom. 130мс/0.6 громкости
# (были 90мс/0.3 — на грани слышимости, см. история выше).
_START_SOUND = _tone_wav_bytes(880.0, 130)
_STOP_SOUND = _tone_wav_bytes(587.0, 130)

_player: "Optional[_SoundPlayer]" = None


class _SoundPlayer:
    """Держит оба QSoundEffect живыми на весь run приложения — audio session
    процесса остаётся активной, короткие редкие сигналы не обрываются."""

    def __init__(self) -> None:
        sdir = profile.sounds_dir()
        start_path = sdir / "start.wav"
        stop_path = sdir / "stop.wav"
        _write_if_changed(start_path, _START_SOUND)
        _write_if_changed(stop_path, _STOP_SOUND)

        self.start_fx = QSoundEffect()
        self.start_fx.setSource(QUrl.fromLocalFile(str(start_path)))
        self.start_fx.setVolume(1.0)

        self.stop_fx = QSoundEffect()
        self.stop_fx.setSource(QUrl.fromLocalFile(str(stop_path)))
        self.stop_fx.setVolume(1.0)


def init_player() -> None:
    """Вызывать один раз из main thread при старте приложения (после
    QApplication) — прогревает оба QSoundEffect заранее, до первого хоткея."""
    global _player
    if _player is None:
        _player = _SoundPlayer()


def play_start() -> None:
    """Только из main/Qt thread."""
    if _player is not None:
        _player.start_fx.play()


def play_stop() -> None:
    """Только из main/Qt thread. Из worker-потока — через сигнал
    `window.notify_stop_sound`, не напрямую."""
    if _player is not None:
        _player.stop_fx.play()
