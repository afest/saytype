"""T-487: скриншоты и видео нового интерфейса из живого приложения.

Исполняется внутри приложения через `SAYTYPE_BENCH=tools/shots_v5.py`
(запуск: `tools\\dev_run.ps1 -UI v5 -Bench tools/shots_v5.py`); модуль приложения
приходит как `app_module`. Режим — через `SAYTYPE_SHOTS_CFG` (JSON):
  {"out": "_dev/shots", "sizes": [[1280,800],[900,700],[800,640]], "video": true, "fps": 30}

Снимки: все 7 страниц на каждой ширине + состояния «запись», «обработка»,
«после», вкладки настроек, строка истории «играет», узкое окно. Видео (если
`video`): кадры окна во время переходов вниз/вверх, быстрых повторных кликов,
сброса прошлого времени, разрядной анимации, плеера и reduced-motion →
`ffmpeg` склеивает в `demo.mp4` (системный ffmpeg из PATH).

Масштаб DPI задаётся снаружи: `QT_SCALE_FACTOR=1.25` и т.д. — отдельный запуск
на каждый масштаб, файлы получают суффикс.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

m = app_module  # noqa: F821
CFG = json.loads(os.environ.get("SAYTYPE_SHOTS_CFG") or "{}")
ROOT = Path(os.environ.get("SAYTYPE_BENCH", "tools/shots_v5.py")).resolve().parent.parent
OUT = Path(CFG.get("out") or "_dev/shots")
if not OUT.is_absolute():
    OUT = ROOT / OUT
OUT.mkdir(parents=True, exist_ok=True)
SIZES = CFG.get("sizes") or [[1280, 800], [900, 700], [800, 640]]
VIDEO = bool(CFG.get("video", False))
FPS = int(CFG.get("fps", 30))
SCALE = os.environ.get("QT_SCALE_FACTOR", "")
SUFFIX = f"-x{SCALE}" if SCALE else ""
PAGES = ["home", "calls", "notes", "models", "stats", "settings", "about"]
W = m.window


def LOG(s):
    m.log(f"[shots] {s}")


def run_steps(gen):
    def step():
        try:
            nxt = next(gen)
        except StopIteration:
            return
        except Exception as exc:  # noqa: BLE001
            import traceback
            LOG(f"FAIL {exc!r}\n{traceback.format_exc()}")
            finish()
            return
        QTimer.singleShot(int(nxt), step)
    step()


def shot(name: str) -> None:
    QApplication.processEvents()
    path = OUT / f"{name}{SUFFIX}.png"
    W.grab().save(str(path))
    LOG(f"shot {path.name}")


class Recorder:
    """Кадры окна на таймере → папка → ffmpeg."""

    def __init__(self, name: str):
        self.dir = OUT / f"_frames-{name}"
        if self.dir.exists():
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True)
        self.name = name
        self.i = 0
        self.timer = QTimer()
        self.timer.setInterval(int(1000 / FPS))
        self.timer.timeout.connect(self._frame)

    def _frame(self):
        W.grab().save(str(self.dir / f"f{self.i:05d}.png"))
        self.i += 1

    def start(self):
        self.timer.start()

    def stop(self):
        self.timer.stop()

    def encode(self) -> Path | None:
        ffmpeg = shutil.which("ffmpeg")
        out = OUT / f"{self.name}{SUFFIX}.mp4"
        if not ffmpeg:
            LOG("ffmpeg не найден — кадры оставлены в папке")
            return None
        subprocess.run([ffmpeg, "-y", "-framerate", str(FPS), "-i", str(self.dir / "f%05d.png"),
                        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-crf", "20", str(out)], capture_output=True, creationflags=0x08000000)
        shutil.rmtree(self.dir, ignore_errors=True)
        LOG(f"video {out.name} ({self.i} кадров)")
        return out


def _mute_players():
    """Offscreen-платформа глушит только картинку: звук плеера ушёл бы в колонки человека."""
    for page in (W.page_home, W.page_calls):
        try:
            page.player._out.setVolume(0.0)
        except Exception:  # noqa: BLE001
            pass


def main_gen():
    _mute_players()
    yield 1500
    W.show_window()
    yield 500
    for w, h in SIZES:
        W.resize(w, h)
        yield 400
        for pid in PAGES:
            W.navigate(pid)
            yield 900
            shot(f"{w}-{pid}")
            if pid == "settings" and hasattr(W.shell.host.page("settings"), "show_tab"):
                page = W.shell.host.page("settings")
                for tab in getattr(page, "TAB_IDS", ["record", "recognition", "calls", "storage", "hifi", "app"]):
                    page.show_tab(tab)
                    yield 250
                    shot(f"{w}-settings-{tab}")
            if pid == "stats" and hasattr(W.shell.host.page("stats"), "_set_tab"):
                page = W.shell.host.page("stats")
                page._set_tab("buckets")
                yield 250
                shot(f"{w}-stats-buckets")
                page._set_tab("trend")
        # состояния записи на странице диктовок (без микрофона: только UI)
        W.navigate("home")
        yield 900
        W.set_state("recording")
        W._rec_started_at -= 70
        yield 1400
        shot(f"{w}-home-recording")
        W.set_state("processing")
        yield 400
        shot(f"{w}-home-processing")
        W.set_state("idle")
        yield 900
        shot(f"{w}-home-after")
        rows = W.page_home._rows
        if rows and rows[0].audio_path is not None:
            W.page_home._on_row_play(rows[0])
            yield 2500
            shot(f"{w}-home-playing")
            W.page_home.pause_player()
            rows[0].open_viewer()  # полный текст — листом поверх окна (08.10)
            yield 300
            shot(f"{w}-home-viewer")
            for tw in QApplication.topLevelWidgets():
                if tw.metaObject().className() == "TextViewer" and tw.isVisible():
                    tw.close()
        # оверлеи
        W._overlay.show_recording()
        W._overlay.set_seconds(12)
        yield 300
        W._overlay.grab().save(str(OUT / f"overlay-voice-recording{SUFFIX}.png"))
        W._overlay.show_processing(True)
        yield 300
        W._overlay.grab().save(str(OUT / f"overlay-voice-processing{SUFFIX}.png"))
        W._overlay.hide_overlay()
        W._call_overlay.show_recording()
        W._call_overlay.set_seconds(95)
        yield 300
        W._call_overlay.grab().save(str(OUT / f"overlay-call-recording{SUFFIX}.png"))
        W._call_overlay.hide_overlay()
    if VIDEO:
        W.resize(1280, 800)
        W.navigate("home")
        yield 900
        rec = Recorder("demo")
        rec.start()
        yield 600
        # переходы вниз и вверх по меню
        for pid in ("notes", "stats", "calls", "home"):
            W.navigate(pid)
            yield 1000
        # быстрые повторные клики
        W.navigate("models")
        yield 150
        W.navigate("settings")
        yield 120
        W.navigate("about")
        yield 1200
        W.navigate("home")
        yield 1000
        # прошлое время 02:37 → старт: сброс разрядов, синий, тики, порог 59→60
        W.page_home.remember_duration(157)
        yield 800
        W.set_state("recording")
        W._rec_started_at -= 57
        yield 4500
        W.set_state("processing")
        yield 1200
        W.set_state("idle")
        yield 1500
        # плеер и перемотка
        rows = W.page_home._rows
        if rows and rows[0].audio_path is not None:
            W.page_home._on_row_play(rows[0])
            yield 3000
            W.page_home.player.seek(max(0.0, rows[0].duration_sec * 0.7))
            yield 2500
            W.page_home.pause_player()
            yield 600
        # reduced motion: мгновенные переходы
        from saytype.ui import tokens
        orig = tokens.Motion.reduced
        tokens.Motion.reduced = staticmethod(lambda: True)
        W.navigate("stats")
        yield 700
        W.navigate("home")
        yield 700
        tokens.Motion.reduced = orig
        rec.stop()
        rec.encode()
    finish()


def finish():
    LOG(f"done → {OUT}")
    q = getattr(W, "_quit", None)
    if callable(q):
        q()
    else:
        QApplication.instance().quit()


run_steps(main_gen())
