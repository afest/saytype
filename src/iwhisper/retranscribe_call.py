"""Собрать транскрипт созвона из уже записанного стерео-WAV (L=mic, R=система).

Зачем: WAV созвона пишется на диск ДО транскрипции и остаётся в буфере
`Calls\\` (T-175/T-176). Если транскрипт упал (модель, GPU, кодировка лога —
как 2026-07-28), запись цела и `.md` собирается отдельно, без перезаписи созвона.

Параметры транскрипции — те же, что в бою: словарь и замены читаются из профиля
пользователя (`profile.py`), а не из UI-модуля — импортировать `transcribe_ui`
здесь нельзя, он поднял бы single-instance mutex работающего приложения.

Запуск:
    python -m iwhisper.retranscribe_call                  # последний WAV в Calls
    python -m iwhisper.retranscribe_call "<путь к .wav>"  # конкретный файл
"""

from __future__ import annotations

import sys
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

from . import profile
from . import transcribe_call as tc


def log(msg: str) -> None:
    """Безопасный лог (инцидент 2026-07-28: cp1251-stderr + эмодзи = потерянный транскрипт)."""
    try:
        print(msg, file=sys.stderr, flush=True)
    except Exception:
        try:
            print(msg.encode("ascii", errors="replace").decode("ascii"), file=sys.stderr, flush=True)
        except Exception:
            pass


def read_stereo_wav(path: Path) -> tuple[np.ndarray, np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        ch = wf.getnchannels()
        frames = wf.readframes(wf.getnframes())
    data = np.frombuffer(frames, dtype=np.int16)
    if ch == 2:
        data = data.reshape(-1, 2)
        return data[:, 0].copy(), data[:, 1].copy(), rate
    return data.copy(), np.zeros(0, dtype=np.int16), rate


def main() -> int:
    if len(sys.argv) > 1:
        wav_path = Path(sys.argv[1])
    else:
        configured = tc._read_settings_option("history_dir")
        if configured:
            tc.set_history_dir(configured)
        wavs = sorted(tc.calls_dir().glob("*.wav"), key=lambda p: p.stat().st_mtime)
        if not wavs:
            log(f"[ERR] в {tc.calls_dir()} нет .wav")
            return 2
        wav_path = wavs[-1]
    if not wav_path.exists():
        log(f"[ERR] нет файла: {wav_path}")
        return 2

    stem = wav_path.stem
    md_path = wav_path.with_suffix(".md")
    if md_path.exists():
        log(f"[ERR] {md_path.name} уже есть — не перезаписываю. Удали или переименуй.")
        return 3

    mic, sys_ch, rate = read_stereo_wav(wav_path)
    dur_min = max(len(mic), len(sys_ch)) / rate / 60
    log(f"WAV: {wav_path.name} · {dur_min:.1f} мин · {rate} Гц · "
        f"L peak={int(np.abs(mic).max()) if len(mic) else 0} "
        f"R peak={int(np.abs(sys_ch).max()) if len(sys_ch) else 0}")

    try:
        started_at = datetime.strptime(stem[:19], "%Y-%m-%d %H-%M-%S")
    except ValueError:
        started_at = datetime.fromtimestamp(wav_path.stat().st_mtime)

    prompt = profile.load_dictionary() or None
    # tail_rules не применяем: хвостовые правила рассчитаны на одну надиктовку,
    # в сегментах живого созвона они стреляли бы по настоящим репликам.
    body_rules, _tail = profile.compile_rules()

    def postproc(text: str) -> str:
        for regex, repl in body_rules:
            try:
                text = regex.sub(repl, text)
            except Exception:
                continue
        return text

    from . import engine

    model = engine.load_model(logger=log)
    log(f"модель: {engine.current_label()} · {engine.current_device()}/{engine.current_compute_type()}")

    def _progress(tag: str):
        state = {"last": -1}

        def cb(done: float, total: float) -> None:
            if total <= 0:
                return
            pct = int(done * 100 / total)
            if pct >= state["last"] + 5:
                state["last"] = pct
                log(f"  {tag}: {pct}%")
        return cb

    log("транскрипт канала L (микрофон)...")
    left = tc.transcribe_channel(model, mic, progress_cb=_progress("L"),
                                 initial_prompt=prompt, postproc=postproc)
    log(f"  L: {len(left)} сегментов")
    log("транскрипт канала R (система)...")
    right = tc.transcribe_channel(model, sys_ch, progress_cb=_progress("R"),
                                  initial_prompt=prompt, postproc=postproc) if len(sys_ch) else []
    log(f"  R: {len(right)} сегментов")

    body = tc.merge_segments_to_markdown(left, right)
    if left and not right:
        body = (
            "> ⚠️ **Канал собеседника (R) пуст — спикеры не разделены.** "
            "Ниже только микрофон.\n\n"
        ) + body

    recording = tc.CallRecording(
        mic_int16=mic, sys_int16=sys_ch, sample_rate=rate, started_at=started_at,
        wav_path=wav_path, stats={},
    )
    md = tc.build_markdown(recording, None, body)
    # build_markdown рассчитан на обычный флоу («аудио не сохранялось»), а тут
    # мы восстанавливаемся ИМЕННО из сохранённого WAV — не врать в артефакте.
    md = md.replace(
        "source_recording: null  # аудио не сохранялось, только транскрипт",
        f"source_recording: {wav_path.as_posix()}  # восстановлено из WAV (retranscribe_call.py)",
    ).replace(
        "> Аудио не сохранялось — только транскрипт.",
        f"> Собрано из сохранённого WAV: `{wav_path}` (буфер созвонов, ротируется).",
    )
    md_path.write_text(md, encoding="utf-8")
    log(f"готово: {md_path}")
    print(md_path)
    return 0


if __name__ == "__main__":
    # T-268: не sys.exit — интерпретатор на финализации разрушил бы модель CT2,
    # а после генерации с temperature-fallback это убивает процесс с 0xC0000409.
    from . import engine as _engine

    _engine.hard_exit(main())
