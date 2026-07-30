"""Local Agreement-2 worker для онлайн-обработки записи во время надиктовки.

T-132 — наполнение каркаса T-131 настоящей LA-2 логикой по Macháček 2023
(https://github.com/ufal/whisper_streaming). Worker раз в interval_sec делает
snapshot аудио, прогоняет окно ~window_sec через faster-whisper с word
timestamps, сравнивает с предыдущей гипотезой через LCP и накапливает
стабилизированный confirmed_text. POST_REPLACEMENTS не применяются здесь —
только к финальному тексту после tail-merge в transcribe_ui.py.
"""

from __future__ import annotations

import threading
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import numpy as np

if TYPE_CHECKING:
    from faster_whisper import WhisperModel


INTERVAL_SEC = 2.5
BEAM_SIZE = 1
INITIAL_PROMPT_TAIL_CHARS = 150
TIMESTAMP_TOLERANCE_SEC = 0.3   # слова считаются «одинаковыми» если |p.start - c.start| < tolerance
NEW_CONFIRMED_MARGIN_SEC = 0.05 # фильтр LCP-слов уже выписанных в confirmed_text
MAX_WINDOW_SEC = 30.0           # верх growing-window'а: если worker долго не confirm'ит, обрезаем слева
# WINDOW_SEC удалён 2026-05-22 (T-132 follow-up): переход с sliding на growing window
# по Macháček 2023 — окно теперь от last_confirmed_end до now, чтобы не терять слова
# которые worker ещё не успел confirm'ить через LCP (sliding выкидывал их из окна).


@dataclass
class WordHyp:
    text: str
    start: float
    end: float


class StreamingProcessor:
    """Worker-thread для онлайн-обработки записи через LA-2.

    Lifecycle: __init__ -> start() -> stop() возвращает
    (confirmed_text, last_confirmed_end_sec, chunks_processed). После stop()
    объект не переиспользуется (одна запись = один StreamingProcessor).

    Сам model не вызывается параллельно с потребителем: stop() блокирует до
    join worker'а (с большим таймаутом, перекрывающим processing 10-сек окна).
    """

    def __init__(
        self,
        model: "WhisperModel | None",
        get_audio_snapshot: Callable[[], np.ndarray],
        sample_rate: int = 16000,
        max_window_sec: float = MAX_WINDOW_SEC,
        interval_sec: float = INTERVAL_SEC,
        beam_size: int = BEAM_SIZE,
        initial_prompt: str = "",
        initial_prompt_tail_chars: int = INITIAL_PROMPT_TAIL_CHARS,
        language: str = "ru",
        log: Callable[[str], None] = print,
        debug_log_path: "Path | None" = None,
    ) -> None:
        self._model = model
        self._get_audio_snapshot = get_audio_snapshot
        self._sample_rate = sample_rate
        self._max_window_sec = max_window_sec
        self._interval_sec = interval_sec
        self._beam_size = beam_size
        self._initial_prompt = initial_prompt
        self._initial_prompt_tail_chars = initial_prompt_tail_chars
        self._language = language
        self._log_orig = log
        self._log = log
        self._debug_log_path = debug_log_path
        self._debug_file = None

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._worker_busy = False

        self._confirmed_text: str = ""
        self._last_confirmed_end_sec: float = 0.0
        self._first_confirmed_start_sec: float | None = None
        # ↑ T-159-followup (2026-05-23): timestamp первого слова, попавшего в
        # confirmed_text. None если LCP ни разу не стабилизировал префикс.
        # Используется в transcribe_ui.py для head-merge: если значение > порога,
        # значит LCP начал confirm только с середины записи и голова потеряна —
        # нужен дополнительный transcribe audio[0..first_start + overlap].
        self._chunks_processed: int = 0
        self._prev_hypothesis: list[WordHyp] = []

    def start(self) -> None:
        if self._thread is not None:
            self._log("streaming start ignored: already started")
            return
        if self._debug_log_path is not None:
            try:
                self._debug_file = open(self._debug_log_path, "a", encoding="utf-8", buffering=1)
                self._debug_file.write(f"\n=== streaming start {datetime.now().isoformat()} ===\n")
                original_log = self._log_orig
                debug_file = self._debug_file

                def _wrapped(msg: str) -> None:
                    original_log(msg)
                    try:
                        debug_file.write(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}\n")
                    except Exception:
                        pass

                self._log = _wrapped
            except Exception as exc:
                self._log_orig(f"streaming debug log open fail: {exc}")
                self._debug_file = None
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="StreamingProcessor",
            daemon=True,
        )
        self._thread.start()
        self._log("streaming worker started")

    def stop(self) -> tuple[str, float, int, float | None]:
        """Сигнал worker'у завершиться, ждать join, вернуть финальное состояние.

        Worker может быть внутри m.transcribe — timeout перекрывает window_sec
        с запасом. Если всё ещё работает после — лог-warning, но возвращаем
        накопленное состояние (вызывающая сторона разрулит fallback).

        Returns:
            (confirmed_text, last_confirmed_end_sec, chunks_processed, first_confirmed_start_sec)
            first_confirmed_start_sec=None если ни одного успешного LCP-confirm
            не было (короткая запись / тишина / нестабильный whisper).
        """
        self._stop_event.set()
        if self._thread is not None:
            join_timeout = self._max_window_sec + 2.0
            self._thread.join(timeout=join_timeout)
            if self._thread.is_alive():
                self._log(
                    f"streaming worker did not exit within {join_timeout:.0f}s "
                    f"(may still be inside m.transcribe)"
                )
            self._thread = None
        confirmed = self._confirmed_text.strip()
        first_start_str = (
            f"{self._first_confirmed_start_sec:.2f}s"
            if self._first_confirmed_start_sec is not None
            else "None"
        )
        self._log(
            f"streaming worker stopped "
            f"(chunks={self._chunks_processed}, "
            f"confirmed_chars={len(confirmed)}, "
            f"first_start={first_start_str}, "
            f"last_end={self._last_confirmed_end_sec:.2f}s)"
        )
        if self._debug_file is not None:
            try:
                self._debug_file.write(
                    f"=== streaming stop confirmed_chars={len(confirmed)} "
                    f"chunks={self._chunks_processed} "
                    f"first_start={first_start_str} "
                    f"last_end={self._last_confirmed_end_sec:.2f}s ===\n"
                    f"FULL CONFIRMED RAW: '{confirmed}'\n"
                )
                self._debug_file.close()
            except Exception:
                pass
            self._debug_file = None
        return (
            confirmed,
            self._last_confirmed_end_sec,
            self._chunks_processed,
            self._first_confirmed_start_sec,
        )

    def _worker_loop(self) -> None:
        """LA-2 loop по Macháček 2023: growing window от last_confirmed_end до now.

        Tick раз в interval_sec. Окно — audio[consumed:now], где consumed = last_end
        в samples. После confirm consumed двигается вперёд → следующий tick видит
        меньшее окно. Если worker не confirm'ит долго — окно ограничено
        max_window_sec слева (защита от unbounded processing).

        Stop-event прерывает wait сразу. Dropping policy через _worker_busy.
        Pre-filter через silero VAD; на failure (например 2D audio) — fallback
        transcribe без VAD-skip.
        """
        try:
            from faster_whisper.vad import get_speech_timestamps
            vad_available = True
        except Exception as exc:
            self._log(f"streaming VAD unavailable: {exc} — все окна считаем речью")
            get_speech_timestamps = None
            vad_available = False

        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=self._interval_sec):
                break
            if self._stop_event.is_set():
                break

            if self._worker_busy:
                self._log("streaming tick dropped (worker still busy)")
                continue
            if self._model is None:
                self._log("streaming tick skipped (model is None)")
                continue

            self._worker_busy = True
            try:
                audio_full = self._get_audio_snapshot()
                if audio_full is None or audio_full.size == 0:
                    continue

                consumed_sample = int(self._last_confirmed_end_sec * self._sample_rate)
                if consumed_sample < 0:
                    consumed_sample = 0
                if consumed_sample >= audio_full.size:
                    continue  # всё уже подтверждено — нет нового аудио

                # Growing window: от consumed до конца. Ограничиваем max_window_sec
                # слева — защита если worker долго не confirm'ит (whisper нестабилен).
                max_samples = int(self._max_window_sec * self._sample_rate)
                if audio_full.size - consumed_sample > max_samples:
                    audio_window = audio_full[-max_samples:]
                    window_start_sec = (audio_full.size - max_samples) / self._sample_rate
                else:
                    audio_window = audio_full[consumed_sample:]
                    window_start_sec = consumed_sample / self._sample_rate

                if vad_available:
                    try:
                        speech_ts = get_speech_timestamps(
                            audio_window, sampling_rate=self._sample_rate
                        )
                    except Exception as exc:
                        self._log(f"streaming VAD fail: {exc} — fallback transcribe")
                        speech_ts = [{"start": 0, "end": len(audio_window)}]
                    if not speech_ts:
                        self._log(
                            f"streaming tick: silence "
                            f"(window {window_start_sec:.1f}s..{window_start_sec + len(audio_window)/self._sample_rate:.1f}s), skip"
                        )
                        continue

                curr_hyps = self._process_window(audio_window, window_start_sec)
                self._chunks_processed += 1

                confirmed = self._lcp(self._prev_hypothesis, curr_hyps)
                if confirmed:
                    new_confirmed = [
                        w for w in confirmed
                        if w.start > self._last_confirmed_end_sec - NEW_CONFIRMED_MARGIN_SEC
                    ]
                    if new_confirmed:
                        raw_added = " ".join(w.text for w in new_confirmed)
                        if self._confirmed_text:
                            self._confirmed_text += " " + raw_added
                        else:
                            self._confirmed_text = raw_added
                            # Первое слово первого confirm'а — фиксируем для head-merge.
                            # Только при первом успешном confirm; дальше не трогаем.
                            self._first_confirmed_start_sec = new_confirmed[0].start
                        self._last_confirmed_end_sec = new_confirmed[-1].end

                self._prev_hypothesis = curr_hyps
            except Exception as exc:
                self._log(f"streaming worker error: {exc}")
            finally:
                self._worker_busy = False

    def _process_window(
        self, audio_window: np.ndarray, window_start_sec: float
    ) -> list[WordHyp]:
        """Transcribe одного окна -> list[WordHyp] с global timestamps."""
        if self._model is None:
            return []
        prompt_tail = self._confirmed_text[-self._initial_prompt_tail_chars:]
        prompt = (self._initial_prompt + " " + prompt_tail).strip() if prompt_tail else self._initial_prompt
        segments, _info = self._model.transcribe(
            audio_window,
            beam_size=self._beam_size,
            word_timestamps=True,
            vad_filter=False,
            initial_prompt=prompt,
            temperature=0.0,
            condition_on_previous_text=False,
            language=self._language,
        )
        result: list[WordHyp] = []
        for seg in segments:
            words = getattr(seg, "words", None) or []
            for w in words:
                text = (getattr(w, "word", "") or "").strip()
                if not text:
                    continue
                start = float(getattr(w, "start", 0.0) or 0.0) + window_start_sec
                end = float(getattr(w, "end", 0.0) or 0.0) + window_start_sec
                result.append(WordHyp(text=text, start=start, end=end))
        return result

    def _lcp(self, prev: list[WordHyp], curr: list[WordHyp]) -> list[WordHyp]:
        """Longest Common Prefix двух гипотез с alignment по start timestamp.

        Когда окно сдвинулось, prev может начинаться раньше curr — отбрасываем
        prev-слова до curr[0].start (с tolerance). После alignment — линейный
        zip с проверкой текста и близости timestamp'ов. Возвращаем WordHyp
        из curr (свежие timestamps).
        """
        if not prev or not curr:
            return []
        curr_first_start = curr[0].start
        prev_aligned = [
            p for p in prev
            if p.start >= curr_first_start - TIMESTAMP_TOLERANCE_SEC
        ]
        if not prev_aligned:
            return []
        prev_first_start = prev_aligned[0].start
        curr_aligned = [
            c for c in curr
            if c.start >= prev_first_start - TIMESTAMP_TOLERANCE_SEC
        ]
        if not curr_aligned:
            return []
        result: list[WordHyp] = []
        for p, c in zip(prev_aligned, curr_aligned):
            if (
                p.text.strip().lower() == c.text.strip().lower()
                and abs(p.start - c.start) < TIMESTAMP_TOLERANCE_SEC
            ):
                result.append(c)
            else:
                break
        return result


def _run_lcp_tests() -> None:
    """Unit-тесты _lcp на 6 кейсах. Запуск: python transcribe_streaming.py"""

    def W(text: str, start: float, end: float | None = None) -> WordHyp:
        return WordHyp(text=text, start=start, end=end if end is not None else start + 0.4)

    sp = StreamingProcessor(model=None, get_audio_snapshot=lambda: np.zeros(0, np.float32), log=lambda m: None)

    case = "1. полное совпадение"
    prev = [W("привет", 1.0), W("мир", 2.0), W("это", 3.0)]
    curr = [W("привет", 1.0), W("мир", 2.0), W("это", 3.0)]
    got = sp._lcp(prev, curr)
    assert [w.text for w in got] == ["привет", "мир", "это"], f"{case} got={got}"
    print(f"OK {case}")

    case = "2. частичное (curr длиннее, prefix совпадает)"
    prev = [W("привет", 1.0), W("мир", 2.0)]
    curr = [W("привет", 1.0), W("мир", 2.0), W("это", 3.0), W("новое", 4.0)]
    got = sp._lcp(prev, curr)
    assert [w.text for w in got] == ["привет", "мир"], f"{case} got={got}"
    print(f"OK {case}")

    case = "3. пустой prev"
    got = sp._lcp([], [W("привет", 1.0)])
    assert got == [], f"{case} got={got}"
    print(f"OK {case}")

    case = "4. пустой curr"
    got = sp._lcp([W("привет", 1.0)], [])
    assert got == [], f"{case} got={got}"
    print(f"OK {case}")

    case = "5. одинаковое слово, разный timestamp (повтор) — НЕ matched"
    got = sp._lcp([W("привет", 1.0)], [W("привет", 5.0)])
    assert got == [], f"{case} got={got} (повтор слова за 4 сек не должен LCP'нуться)"
    print(f"OK {case}")

    case = "6. alignment при сдвиге окна (prev был на окне 0..10, curr на 2..12)"
    prev = [W("a", 1.0), W("b", 3.0), W("c", 5.0), W("d", 7.0), W("e", 9.0)]
    curr = [W("b", 3.1), W("c", 5.0), W("d", 7.1), W("e", 9.0), W("f", 11.0)]
    got = sp._lcp(prev, curr)
    assert [w.text for w in got] == ["b", "c", "d", "e"], f"{case} got=[{','.join(w.text for w in got)}]"
    print(f"OK {case}")

    case = "7. case-insensitive + strip"
    got = sp._lcp([W("Привет,", 1.0)], [W("привет,", 1.05)])
    assert [w.text for w in got] == ["привет,"], f"{case} got={got}"
    print(f"OK {case}")

    print("\nall _lcp tests passed")


if __name__ == "__main__":
    _run_lcp_tests()
