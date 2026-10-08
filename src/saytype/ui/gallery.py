"""Галерея компонентов V5 для ручной проверки: `python -m saytype.ui.gallery`.

Показывает каждый компонент во всех состояниях на одной странице с прокруткой:
цвета и штриховку, типографику, иконки и знак, цифры (строка, разряд, большой
таймер с кнопками «старт/стоп/сброс» и тестом границ 59→60 / 3599→3600, мини-часы
с плеером), кнопки, переключатель, поля, пункты меню, строку истории в четырёх
состояниях, тост, плавающий статус и модальное окно. Смена знака, шрифта или
акцента в `tokens.py` / `assets/` видна здесь без правки страниц.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QApplication, QGridLayout, QHBoxLayout, QLabel, QMainWindow, QScrollArea, QVBoxLayout,
                               QWidget)

from .digits import ClockGrid, DigitReel, DigitStrip, MiniClock
from .history import RecordRow
from .icons import ICONS, NAV_ICONS, IconButton, icon_pixmap, mark_pixmap
from .overlays import FloatingStatus
from .shell import GuideCanvas, NavButton
from .tokens import Color, Font, Grid, Hatch, Motion, body_font, head_font, load_fonts, qc
from .widgets import (FIELD_QSS, ActionField, Button, ComboBox, Field, Label, LineEdit, Modal, SectionHead, SpinBox,
                      Spinner, Switch, TextEdit, Toast)


class _Swatch(QWidget):
    def __init__(self, name: str, value: str, parent=None):
        super().__init__(parent)
        self._name, self._value = name, value
        self.setFixedSize(150, 64)

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(0, 0, 150, 36, qc(self._value))
        p.setPen(qc(Color.LINE))
        p.drawRect(0, 0, 149, 35)
        p.setFont(body_font(10))
        p.setPen(qc(Color.INK))
        p.drawText(0, 40, 150, 12, Qt.AlignLeft, self._name)
        p.setPen(qc(Color.MUTED))
        p.drawText(0, 52, 150, 12, Qt.AlignLeft, self._value)
        p.end()


class _HatchBox(QWidget):
    def __init__(self, alpha: float, parent=None):
        super().__init__(parent)
        self._alpha = alpha
        self.setFixedSize(150, 40)

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), qc(Color.BG))
        p.setOpacity(self._alpha)
        p.fillRect(self.rect(), Hatch.brush(self.devicePixelRatioF()))
        p.setOpacity(1)
        p.setPen(qc(Color.LINE))
        p.drawRect(0, 0, 149, 39)
        p.end()


class _IconSheet(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(140)

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        dpr = self.devicePixelRatioF()
        x, y = 0, 0
        p.setFont(body_font(9))
        for name in NAV_ICONS:
            p.drawPixmap(x, y, icon_pixmap(name, 20, Color.INK, None, True, dpr))
            p.setPen(qc(Color.MUTED))
            p.drawText(x - 6, y + 34, 44, 12, Qt.AlignCenter, name)
            x += 52
        x, y = 0, 60
        for name in ICONS:
            p.drawPixmap(x, y, icon_pixmap(name, 20, Color.ICON_ACTION, None, False, dpr))
            p.drawText(x - 10, y + 34, 48, 12, Qt.AlignCenter, name)
            x += 52
            if x > self.width() - 52:
                x, y = 0, y + 50
        p.drawPixmap(self.width() - 110, 0, mark_pixmap(44, Color.MARK, dpr))
        p.drawPixmap(self.width() - 56, 0, mark_pixmap(48, Color.MARK, dpr))
        p.end()


def _section(title: str) -> QWidget:
    return SectionHead(title)


def _row(*widgets, spacing=12) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(Grid.PAD, 16, Grid.PAD, 16)
    h.setSpacing(spacing)
    for x in widgets:
        h.addWidget(x, 0, Qt.AlignVCenter)
    h.addStretch(1)
    return w


class Gallery(QMainWindow):
    def __init__(self):
        super().__init__()
        load_fonts()
        self.setWindowTitle("SayType V5 — галерея компонентов")
        self.resize(1180, 900)
        self.setStyleSheet(f"QMainWindow{{background:{Color.BG};}}" + FIELD_QSS)
        content = GuideCanvas()
        v = QVBoxLayout(content)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.v = v

        # --- токены ---
        v.addWidget(_section("Токены: цвета и штриховка"))
        grid_w = QWidget()
        g = QGridLayout(grid_w)
        g.setContentsMargins(Grid.PAD, 16, Grid.PAD, 16)
        names = ["BG", "LINE", "INK", "MUTED", "ACCENT", "ACCENT_HOVER", "TINT", "DIGIT_IDLE", "DIGIT_ACTIVE",
                 "DIGIT_MINI", "DIGIT_STAT", "NAV_INDEX", "ICON_ACTION", "DANGER", "STREAMING", "WARNING", "ERROR",
                 "SUCCESS", "TOAST_BG", "STRIP_BG"]
        for i, n in enumerate(names):
            g.addWidget(_Swatch(n, getattr(Color, n)), i // 6, i % 6)
        v.addWidget(grid_w)
        v.addWidget(_row(Label("Штриховка 1.0 / 0.8 (меню) / 0.55 (hover)", size=Font.SMALL, color=Color.MUTED),
                         _HatchBox(1.0), _HatchBox(0.8), _HatchBox(0.55)))

        # --- типографика ---
        v.addWidget(_section("Типографика"))
        t = QWidget()
        tl = QVBoxLayout(t)
        tl.setContentsMargins(Grid.PAD, 16, Grid.PAD, 16)
        h1 = QLabel("Заголовок страницы")
        h1.setFont(head_font(29))
        tl.addWidget(h1)
        tl.addWidget(Label("h2 секции 16 px · letter-spacing −0.3", size=Font.H2, letter_spacing=-0.3))
        tl.addWidget(Label("Body 14 px — Segoe UI Variable Text, line-height 1.5 в транскрипте", size=Font.BODY))
        tl.addWidget(Label("Подпись 12 px muted", size=Font.SMALL, color=Color.MUTED))
        tl.addWidget(Label("ЛЕЙБЛ 10 PX LS .8", size=Font.TINY, color=Color.MUTED, letter_spacing=0.8, upper=True))
        v.addWidget(t)

        # --- иконки ---
        v.addWidget(_section("Иконки и знак"))
        sheet = _IconSheet()
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(Grid.PAD, 16, Grid.PAD, 16)
        wl.addWidget(sheet)
        v.addWidget(wrap)
        v.addWidget(_row(IconButton("play", tooltip="play"), IconButton("pause", tooltip="pause"),
                         IconButton("note", tooltip="note"), IconButton("copy", tooltip="copy"),
                         IconButton("trash", color=Color.DANGER, hover_color=Color.DANGER, tooltip="danger")))

        # --- цифры ---
        v.addWidget(_section("Цифры: строка, статистика, номер меню"))
        v.addWidget(_row(DigitStrip("10:05", height=23, color=Color.INK, colon_width=6.3, digit_width=18.9),
                         DigitStrip("01:41:28", height=28, color=Color.DIGIT_MINI),
                         DigitStrip("9,7×", height=74, color=Color.DIGIT_STAT, max_digit_width=58, symbol_px=38),
                         DigitStrip("100", height=74, color=Color.DIGIT_STAT, max_digit_width=58),
                         DigitStrip("07", height=42, color=Color.NAV_INDEX)))
        v.addWidget(_section("Большой таймер"))
        self.clock = ClockGrid()
        self.clock.set_window_height(220)
        v.addWidget(self.clock)
        self._t0 = None
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick)
        b_start = Button("Старт записи (сброс + синий)", variant="primary")
        b_stop = Button("Стоп → серый, запомнить")
        b_59 = Button("Граница 59→60")
        b_3599 = Button("Граница 3599→3600")
        b_set = Button("Показать 02:37")
        b_start.clicked.connect(self._start)
        b_stop.clicked.connect(self._stop)
        b_59.clicked.connect(lambda: self._jump(59))
        b_3599.clicked.connect(lambda: self._jump(3599))
        b_set.clicked.connect(lambda: (self.clock.set_recording(False), self.clock.set_seconds(157, animate=False)))
        self.reduced_label = Label(f"reduced motion: {Motion.reduced()}", size=Font.SMALL, color=Color.MUTED)
        v.addWidget(_row(b_start, b_stop, b_59, b_3599, b_set, self.reduced_label))

        v.addWidget(_section("Мини-часы и разряд"))
        self.mini = MiniClock()
        self.mini.setFixedWidth(128)
        self.mini.set_seconds(157, animate=False)
        reel = DigitReel(mod=10)
        reel.set_window_height(80)
        reel.setFixedWidth(60)
        reel.set_active(True, animate=False)
        b_m = Button("Мини +1", small=True)
        b_m.clicked.connect(lambda: self.mini.set_seconds(self.mini.seconds_value() + 1))
        b_r = Button("Разряд +1", small=True)
        b_r.clicked.connect(lambda: reel.set_value(reel.value() + 1))
        b_z = Button("Разряд → 0", small=True)
        b_z.clicked.connect(reel.roll_to_zero)
        v.addWidget(_row(self.mini, b_m, reel, b_r, b_z))

        # --- кнопки/поля ---
        v.addWidget(_section("Кнопки и контролы"))
        dis = Button("Disabled")
        dis.setEnabled(False)
        v.addWidget(_row(Button("Начать запись", icon="mic", variant="primary"), Button("Использовать"),
                         Button("Из файла", icon="upload", variant="ghost", small=True),
                         Button("Удалить", variant="danger"), dis, Button("", icon="close", square=True),
                         Switch(False), Switch(True), Spinner(15, "#888", Color.ACCENT)))
        f1 = Field("Горячая клавиша диктовки", LineEdit("Ctrl + Shift + Q"),
                   "Нажмите один раз для записи, ещё раз — чтобы завершить.")
        cb = ComboBox()
        cb.addItems(["Автоматически", "После записи", "Во время записи"])
        f2 = Field("Режим обработки", cb, "После записи — один полный проход.")
        f3 = Field("Сохранять начало фразы", Switch(False), "Буфер 500 мс до нажатия клавиши.", inline=True)
        f4 = ActionField("Проверка микрофона", "Тест записи · 3 секунды", "Проверить")
        f5 = Field("Словарь", TextEdit("", rows=4), "≈ 12 / 223 токена · оценка")
        f6 = Field("Порог автоматического режима, с", SpinBox(1, 60, 10), "До порога — после записи.")
        for f in (f1, f2, f3, f4, f5, f6):
            v.addWidget(f)

        # --- меню ---
        v.addWidget(_section("Пункты меню"))
        nav_w = QWidget()
        nl = QHBoxLayout(nav_w)
        nl.setContentsMargins(Grid.PAD, 16, Grid.PAD, 16)
        n1 = NavButton("home", "Диктовки", "mic", 1)
        n2 = NavButton("calls", "Созвоны", "phone", 2)
        n2.set_active(True)
        n3 = NavButton("notes", "Заметки", "note", 3)
        n3.set_count(3)
        n4 = NavButton("stats", "Статистика", "chart", 5)
        n4.set_compact(True)
        for n in (n1, n2, n3):
            n.setFixedWidth(210)
            nl.addWidget(n)
        n4.setFixedWidth(64)
        nl.addWidget(n4)
        nl.addStretch(1)
        v.addWidget(nav_w)

        # --- строка истории ---
        v.addWidget(_section("Строка истории: покой · выбрана · играет · без аудио"))
        sample = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
        wavs = sorted(sample.glob("*.wav")) if sample.exists() else []
        wav = wavs[0] if wavs else None
        long_text = ("Смотри, я хочу ещё раз спокойно пройтись по тому, как человек пользуется диктовкой в течение дня. "
                     "Обычно я не открываю приложение ради самого приложения. У меня уже есть мысль, я работаю над текстом, "
                     "отвечаю на сообщение или пытаюсь сформулировать задачу. В этот момент хочется нажать сочетание клавиш, "
                     "сказать всё своими словами и сразу продолжить работу. Поэтому главное действие должно быть понятным "
                     "без дополнительных объяснений.")
        entry = {"ts": time.strftime("%Y-%m-%dT%H-%M-%S"), "duration_sec": 157.0, "text_full": long_text,
                 "wav_path": wav, "meta": {"elapsed_sec": 12.6, "ratio_x": 12.5}}
        r1 = RecordRow(dict(entry), "voice")
        r2 = RecordRow(dict(entry), "voice")
        r2.set_audio_state(True, False)
        r2.set_position(40, 157)
        r3 = RecordRow(dict(entry), "voice")
        r3.set_audio_state(True, True)
        r3.set_position(95, 157)
        r4 = RecordRow(dict(entry, wav_path=None, text_full="Короткая запись без аудио — буфер уже вытеснил файл."), "voice")
        for r in (r1, r2, r3, r4):
            v.addWidget(r)

        # --- тост, статус, модал ---
        v.addWidget(_section("Тост, модальное окно, индикаторы"))
        b_toast = Button("Показать тост")
        b_modal = Button("Открыть модал")
        b_ov1 = Button("Оверлей: запись")
        b_ov2 = Button("Оверлей: обработка")
        b_ov3 = Button("Оверлей: ошибка")
        b_ov4 = Button("Оверлей созвона: обработка 40 %")
        b_ov5 = Button("Скрыть оверлеи")
        v.addWidget(_row(b_toast, b_modal))
        v.addWidget(_row(b_ov1, b_ov2, b_ov3, b_ov4, b_ov5))
        v.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setCentralWidget(scroll)
        self.toast = Toast(scroll)
        self.ov = FloatingStatus("voice", anchor_getter=lambda: self)
        self.ov_call = FloatingStatus("call", anchor_getter=lambda: self)
        b_toast.clicked.connect(lambda: self.toast.show_text("Скопировано в буфер"))
        b_modal.clicked.connect(self._modal)
        b_ov1.clicked.connect(lambda: (self.ov.show_recording(), self.ov.set_seconds(12),
                                       self.ov.update_audio_level(0.7)))
        b_ov2.clicked.connect(lambda: self.ov.show_processing(True))
        b_ov3.clicked.connect(lambda: self.ov.show_error("Не удалось распознать запись: модель не загрузилась"))
        b_ov4.clicked.connect(lambda: (self.ov_call.show_processing(False), self.ov_call.set_progress(0.4)))
        b_ov5.clicked.connect(lambda: (self.ov.hide_overlay(), self.ov_call.hide_overlay()))

    def _start(self) -> None:
        self.clock.roll_to_zero()
        self.clock.set_recording(True)
        self._t0 = time.monotonic()
        self._timer.start()

    def _stop(self) -> None:
        self._timer.stop()
        self.clock.set_recording(False)

    def _jump(self, sec: int) -> None:
        self._t0 = time.monotonic() - sec
        self.clock.set_recording(True)
        self.clock.set_seconds(sec, animate=False)
        self._timer.start()

    def _tick(self) -> None:
        if self._t0 is None:
            return
        self.clock.set_seconds(int(time.monotonic() - self._t0))

    def _modal(self) -> None:
        dlg = Modal("Удалить заметку?", self, width=520)
        dlg.body_layout.addWidget(Label("Заметка «Идеи» будет удалена без возможности восстановить.",
                                        size=Font.BODY, color=Color.MUTED, wrap=True))
        c = dlg.add_button(Button("Отмена"))
        d = dlg.add_button(Button("Удалить заметку", variant="danger"))
        c.clicked.connect(dlg.reject)
        d.clicked.connect(dlg.accept)
        dlg.exec()


def _flags_to_env(argv: list[str]) -> dict | None:
    """Флаги галереи → JSON для `SAYTYPE_TOKENS`.

        python -m saytype.ui.gallery --accent "#E0452A" --mark "#1B1A16" --head-font C:/path/Font.ttf

    Подмена должна случиться до импорта компонентов (часть из них берёт цвета
    как значения по умолчанию), поэтому галерея перезапускает себя с переменной
    окружения — ровно как после правки `tokens.py`.
    """
    def take(flag: str) -> str | None:
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                return argv[i + 1]
        return None

    data: dict = {}
    accent = take("--accent")
    if accent:
        data.setdefault("Color", {}).update({n: accent for n in ("ACCENT", "DIGIT_ACTIVE", "SIGNAL", "STRIP_BG", "FULL")})
        data["Color"]["ACCENT_HOVER"] = accent
    mark = take("--mark")
    if mark:
        data.setdefault("Color", {})["MARK"] = mark
    font = take("--head-font")
    if font:
        data["head_font"] = font
    return data or None


def main() -> None:
    overrides = _flags_to_env(sys.argv)
    if overrides and not os.environ.get("SAYTYPE_TOKENS"):
        import json
        import subprocess
        env = dict(os.environ, SAYTYPE_TOKENS=json.dumps(overrides))
        sys.exit(subprocess.call([sys.executable, "-m", "saytype.ui.gallery"], env=env))
    app = QApplication(sys.argv)
    app.setApplicationName("SayType V5 gallery")
    g = Gallery()
    g.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
