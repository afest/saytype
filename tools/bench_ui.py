"""T-487: парный бенчмарк интерфейса. Исполняется ВНУТРИ приложения (GUI-поток)
через `SAYTYPE_BENCH=tools/bench_ui.py`; модуль приложения приходит как `app_module`.

Идея: один и тот же код, модель, устройство, режим и аудио — меняется только
окно (`SAYTYPE_UI=legacy|v5`). Запись эмулируется подстановкой готового буфера
(батч) или подачей чанков в реальном времени (стриминг), стоп идёт штатным
`toggle_recording(via_ui=True)` → `_stop_thread` → `stop_recording_and_transcribe`,
то есть ровно тем путём, что и живая диктовка (без автопаста: via_ui).

Что пишем на каждый прогон (JSONL в cfg["out"]):
  wall_sec      — от вызова стопа до выхода worker'а (busy=False): финализация целиком;
  elapsed_sec   — время транскрипции, которое приложение само записало в _stats.jsonl;
  proc_cpu_sec  — CPU-время процесса за прогон; sys_busy_pct — загрузка всей системы;
  rss_mb_*      — память процесса до/после; gpu_* — util/память/чужие процессы (nvidia-smi);
  lag_p95_ms / lag_max_ms — задержка таймера 50 мс в GUI-потоке (отзывчивость окна).
Отдельно — idle: CPU процесса за N секунд покоя при видимом и свёрнутом окне.

Конфиг — JSON в SAYTYPE_BENCH_CFG: samples{имя:путь}, repeats, warmup, modes[batch|streaming],
visible[true,false], idle_sec, rec_visual_sec, out. Чужая нагрузка не отфильтровывается —
она записывается в строку (gpu others / sys_busy), чтобы выброс можно было объяснить.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
import wave
from pathlib import Path

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

m = app_module  # noqa: F821 — подставляет хук SAYTYPE_BENCH


class _NoClipboard:
    """Штатный стоп копирует текст в буфер обмена — в бенчмарке это портит буфер человека."""

    @staticmethod
    def copy(_text):
        return None


m.pyperclip = _NoClipboard()

CFG = json.loads(os.environ.get("SAYTYPE_BENCH_CFG") or "{}")
ROOT = Path(os.environ.get("SAYTYPE_BENCH", "tools/bench_ui.py")).resolve().parent.parent  # корень worktree
_samples_cfg = CFG.get("samples") or ["short", "long"]
if isinstance(_samples_cfg, dict):
    SAMPLES = dict(_samples_cfg)
else:  # список имён → стандартные пути
    SAMPLES = {name: f"_dev/bench/{name}.wav" for name in _samples_cfg}
REPEATS = int(CFG.get("repeats", 10))
WARMUP = int(CFG.get("warmup", 1))
MODES = CFG.get("modes") or ["batch"]
VISIBLE = CFG["visible"] if CFG.get("visible") is not None else [True, False]
IDLE_SEC = float(CFG.get("idle_sec", 10))
REC_VISUAL_SEC = float(CFG.get("rec_visual_sec", 3))
OUT = Path(CFG.get("out") or "_dev/bench/results")
OUT.mkdir(parents=True, exist_ok=True)
UI = os.environ.get("SAYTYPE_UI", "v5").strip().lower() or "v5"
DEV = "cpu" if os.environ.get("CUDA_VISIBLE_DEVICES", "x").strip() in ("", "-1") else "cuda"  # фактическое устройство пишется ещё и в stats_device
SURFACE = "offscreen" if os.environ.get("QT_QPA_PLATFORM", "") == "offscreen" else "screen"
TAG = f"{UI}-{DEV}-{SURFACE}-{time.strftime('%Y%m%d-%H%M%S')}"
ROWS = OUT / f"{TAG}.jsonl"
PROGRESS = OUT / "progress.txt"
SR = 16000


def LOG(s: str) -> None:
    m.log(f"[bench] {s}")


# --- системные замеры -------------------------------------------------------
class _FILETIME(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]


def _ft(ft: _FILETIME) -> float:
    return ((ft.hi << 32) | ft.lo) / 1e7


def sys_times() -> tuple[float, float]:
    """(idle, total) секунд CPU по всей системе — для доли занятости."""
    i, k, u = _FILETIME(), _FILETIME(), _FILETIME()
    ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u))
    return _ft(i), _ft(k) + _ft(u)


class _PMC(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def rss_mb() -> float:
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(pmc)
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    h = k32.GetCurrentProcess()
    fn = getattr(k32, "K32GetProcessMemoryInfo", None) or ctypes.windll.psapi.GetProcessMemoryInfo
    fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
    fn.restype = ctypes.c_int
    if not fn(h, ctypes.byref(pmc), pmc.cb):
        return -1.0
    return round(pmc.WorkingSetSize / (1024 * 1024), 1)


def gpu_snapshot() -> dict:
    try:
        flags = 0x08000000
        q = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, creationflags=flags,
        ).stdout.strip()
        util, mem = [x.strip() for x in q.split(",")]
        def _num(v):
            try:
                return int(v)
            except ValueError:
                return None  # «[N/A]» — драйвер не отдаёт поле
        apps = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, creationflags=flags,
        ).stdout.strip().splitlines()
        own, others = 0, []
        for line in apps:
            if not line.strip():
                continue
            pid, used = [x.strip() for x in line.split(",")]
            if _num(pid) == os.getpid():
                own = _num(used)
            else:
                others.append({"pid": _num(pid), "mb": _num(used)})
        return {"util": _num(util), "mem_mb": _num(mem), "own_mb": own, "others": others}
    except Exception as exc:  # noqa: BLE001
        return {"error": repr(exc)}


class LagProbe:
    """Таймер 50 мс в GUI-потоке: насколько он опаздывает — столько окно «висит»."""

    def __init__(self) -> None:
        self.timer = QTimer()
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._tick)
        self.samples: list[float] = []
        self._last = 0.0

    def start(self) -> None:
        self.samples = []
        self._last = time.perf_counter()
        self.timer.start()

    def _tick(self) -> None:
        now = time.perf_counter()
        self.samples.append(max(0.0, (now - self._last) - 0.05) * 1000.0)
        self._last = now

    def stop(self) -> dict:
        self.timer.stop()
        if not self.samples:
            return {"lag_p95_ms": None, "lag_max_ms": None, "lag_n": 0}
        arr = np.array(self.samples)
        return {
            "lag_p95_ms": round(float(np.percentile(arr, 95)), 2),
            "lag_max_ms": round(float(arr.max()), 2),
            "lag_n": int(arr.size),
        }


LAG = LagProbe()


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == SR and w.getnchannels() == 1 and w.getsampwidth() == 2, path
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0


AUDIO = {name: load_wav(ROOT / p if not Path(p).is_absolute() else Path(p)) for name, p in SAMPLES.items()}


class _DummyStream:
    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


def level_at(audio: np.ndarray, t: float) -> float:
    i = int(t * SR) % max(1, audio.size)
    seg = audio[i:i + 800]
    return float(np.sqrt(np.mean(seg ** 2))) if seg.size else 0.0


def set_visible(vis: bool) -> None:
    if vis:
        m.window.show_window()
    else:
        m.window.hide()


def stats_row_since(t_iso: str) -> dict:
    path = m.history_dir() / "_stats.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in reversed(lines[-20:]):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("ts", "") >= t_iso:
            return rec
    return {}


def write_row(row: dict) -> None:
    with ROWS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


TOTAL = len(VISIBLE) * len(MODES) * len(SAMPLES) * (REPEATS + WARMUP)
DONE = 0
T_START = time.perf_counter()


def progress(note: str = "") -> None:
    elapsed = time.perf_counter() - T_START
    eta = (elapsed / DONE * (TOTAL - DONE)) if DONE else 0.0
    text = (f"{TAG} · {DONE}/{TOTAL} · {DONE * 100 // max(1, TOTAL)}% · "
            f"прошло {elapsed / 60:.1f} мин · ETA {eta / 60:.1f} мин · {note}")
    PROGRESS.write_text(text + "\n", encoding="utf-8")
    LOG(text)


# --- планировщик шагов на QTimer (GUI-поток не блокируем) -------------------
def run_steps(gen) -> None:
    def step(_=None):
        try:
            nxt = next(gen)
        except StopIteration:
            return
        except Exception as exc:  # noqa: BLE001
            LOG(f"FAIL {exc!r}")
            finish()
            return
        if callable(nxt):
            def poll():
                try:
                    ok = nxt()
                except Exception as exc:  # noqa: BLE001
                    LOG(f"predicate fail {exc!r}")
                    ok = True
                if ok:
                    step()
                else:
                    QTimer.singleShot(50, poll)
            poll()
        else:
            QTimer.singleShot(int(nxt), step)
    step()


def deadline(seconds: float, what: str):
    t0 = time.perf_counter()

    def pred_factory(cond):
        def pred():
            if cond():
                return True
            if time.perf_counter() - t0 > seconds:
                raise TimeoutError(f"{what}: {seconds} s")
            return False
        return pred
    return pred_factory


def one_run(vis: bool, mode: str, name: str, index: int):
    global DONE
    audio = AUDIO[name]
    set_visible(vis)
    yield 800
    t_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    gpu0 = gpu_snapshot()
    cpu0 = time.process_time()
    sys0 = sys_times()
    rss0 = rss_mb()
    LAG.start()

    # — запись (эмуляция) —
    m.recording = True
    m.audio_buffer = []
    m.audio_stream = _DummyStream()
    m._active_rate = SR
    m._active_hifi = False
    m._active_processing_mode = "always_streaming" if mode == "streaming" else "always_batch"
    m._streaming_worker_started = False
    m._pre_roll_used = False
    m.streaming_processor = None
    m.via_ui_request = True   # без автопаста и без захвата чужого окна
    m.note_dictation = False
    m.captured_hwnd = 0
    m.set_state("recording")
    t_rec0 = time.perf_counter()
    if mode == "batch":
        m.audio_buffer = [audio.copy()]
        while time.perf_counter() - t_rec0 < REC_VISUAL_SEC:
            m.window.notify_audio_level(level_at(audio, time.perf_counter() - t_rec0))
            yield 50
    else:
        started = m._start_streaming_worker_now()
        if started:
            m._streaming_worker_started = True
        chunk = SR // 10
        pos = 0
        while pos < audio.size:
            m.audio_buffer.append(audio[pos:pos + chunk].copy())
            pos += chunk
            m.window.notify_audio_level(level_at(audio, pos / SR))
            # реальное время: догоняем расписание, а не копим дрейф
            target = t_rec0 + pos / SR
            wait_ms = max(1, int((target - time.perf_counter()) * 1000))
            yield wait_ms
    rec_sec = time.perf_counter() - t_rec0

    # — стоп штатным путём —
    t_stop = time.perf_counter()
    m.toggle_recording(via_ui=True)
    wait = deadline(900, f"run {name}/{mode}")
    yield wait(lambda: (not m.busy) and (not m.recording))
    wall = time.perf_counter() - t_stop

    lag = LAG.stop()
    cpu1 = time.process_time()
    sys1 = sys_times()
    rss1 = rss_mb()
    gpu1 = gpu_snapshot()
    stats = stats_row_since(t_iso)
    idle_d = sys1[0] - sys0[0]
    total_d = sys1[1] - sys0[1]
    row = {
        "tag": TAG, "ui": UI, "device": DEV, "surface": SURFACE, "mode": mode, "sample": name,
        "visible": vis, "i": index, "warmup": index < 0,
        "audio_sec": round(audio.size / SR, 2), "rec_sec": round(rec_sec, 2),
        "wall_sec": round(wall, 3),
        "elapsed_sec": stats.get("elapsed_sec"), "chars": stats.get("chars"),
        "stats_mode": stats.get("mode"), "stats_processing_mode": stats.get("processing_mode"),
        "stats_device": stats.get("device"), "model": stats.get("model_size") or m.engine.current_label(),
        "proc_cpu_sec": round(cpu1 - cpu0, 3),
        "sys_busy_pct": round((1 - idle_d / total_d) * 100, 1) if total_d > 0 else None,
        "rss_mb_before": rss0, "rss_mb_after": rss1,
        "gpu_before": gpu0, "gpu_after": gpu1,
        **lag,
        "ts": t_iso,
    }
    write_row(row)
    DONE += 1
    progress(f"{name}/{mode}/{'окно' if vis else 'скрыто'} #{index}: wall {wall:.2f} с, transcribe {stats.get('elapsed_sec')}")
    yield 500


def idle_measure(vis: bool):
    set_visible(vis)
    yield 1500
    LAG.start()
    cpu0, sys0, rss0, t0 = time.process_time(), sys_times(), rss_mb(), time.perf_counter()
    yield int(IDLE_SEC * 1000)
    cpu1, sys1, rss1, t1 = time.process_time(), sys_times(), rss_mb(), time.perf_counter()
    lag = LAG.stop()
    idle_d = sys1[0] - sys0[0]
    total_d = sys1[1] - sys0[1]
    row = {
        "tag": TAG, "ui": UI, "device": DEV, "surface": SURFACE, "kind": "idle", "visible": vis,
        "idle_sec": round(t1 - t0, 2),
        "proc_cpu_pct_one_core": round((cpu1 - cpu0) / (t1 - t0) * 100, 2),
        "sys_busy_pct": round((1 - idle_d / total_d) * 100, 1) if total_d > 0 else None,
        "rss_mb": rss1, **lag, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    write_row(row)
    LOG(f"idle {'окно' if vis else 'скрыто'}: CPU процесса {row['proc_cpu_pct_one_core']}% одного ядра, lag p95 {row['lag_p95_ms']} мс")


def finish() -> None:
    LOG(f"done → {ROWS}")
    PROGRESS.write_text(f"{TAG} · ГОТОВО · {ROWS}\n", encoding="utf-8")
    q = getattr(m.window, "_quit", None)
    if callable(q):
        q()
    else:
        QApplication.instance().quit()


def main_gen():
    LOG(f"старт: ui={UI} device={DEV} samples={list(SAMPLES)} repeats={REPEATS} warmup={WARMUP} modes={MODES} visible={VISIBLE}")
    wait = deadline(900, "model load")
    yield wait(lambda: m.engine.is_loaded() and not m.model_busy())
    yield 2000
    LOG(f"модель: {m.engine.current_label()} · {m.MODEL_DEVICE}/{m.MODEL_COMPUTE_TYPE}")
    for vis in VISIBLE:
        yield from idle_measure(vis)
    for vis in VISIBLE:
        for mode in MODES:
            for name in SAMPLES:
                for i in range(-WARMUP, REPEATS):
                    yield from one_run(vis, mode, name, i)
    finish()


run_steps(main_gen())
