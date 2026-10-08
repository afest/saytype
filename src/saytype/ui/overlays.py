"""Плавающие индикаторы записи — отдельные top-level окна, живут при скрытом
главном окне: не берут фокус, не мешают автопасте и hotkey.

* диктовка — правый нижний угол над taskbar экрана, где главное окно (или основного);
* созвон — верх-центр того же экрана.

Состав: точка состояния (при записи дышит в такт голосу) · подпись · счётчик
мини-часами (разряды двигаются независимо, цвет плавный) · «Отменить» при
обработке. Время обработки не продолжает длительность записи: при обработке
счётчик замирает серым, рядом спиннер и отдельная подпись «обработка m:ss».
"""
from __future__ import annotations

import time

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QHBoxLayout, QWidget

from .digits import MiniClock, fmt_mmss
from .tokens import Color, Font, qc
from .widgets import Button, Label, LevelDot, Spinner


class _Dot(LevelDot):
    """Точка состояния; при записи ореол дышит в такт голосу (`set_level`)."""

    def __init__(self, parent=None):
        super().__init__(18, Color.SIGNAL, parent)

    def set(self, color: str, ring: bool) -> None:
        self.set_color(color)
        self.set_live(ring)


class FloatingStatus(QWidget):
    cancel_clicked = Signal()

    def __init__(self, kind: str = "voice", anchor_getter=None):
        super().__init__(None)
        self.kind = kind
        self._anchor_getter = anchor_getter   # → QWidget главного окна (для выбора монитора)
        self._mode = "hidden"
        self._proc_started = 0.0
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 10, 16, 10)
        row.setSpacing(10)
        self.dot = _Dot()
        self.spinner = Spinner(15, "#C9CFDC", Color.ACCENT)
        self.spinner.hide()
        self.label = Label("Диктовка" if kind == "voice" else "Созвон", size=Font.SMALL, color=Color.INK)
        self.clock = MiniClock(height=22, color=Color.ACCENT)
        self.clock.setFixedWidth(96)
        self.proc_label = Label("", size=Font.TINY, color=Color.MUTED)
        self.proc_label.hide()
        self.cancel_btn = Button("Отменить", small=True)
        self.cancel_btn.clicked.connect(self.cancel_clicked.emit)
        self.cancel_btn.hide()
        row.addWidget(self.dot)
        row.addWidget(self.spinner)
        row.addWidget(self.label)
        row.addWidget(self.clock)
        row.addWidget(self.proc_label)
        row.addWidget(self.cancel_btn)
        self.setFixedHeight(46)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._hide_if_idle)
        self._proc_timer = QTimer(self)
        self._proc_timer.setInterval(1000)
        self._proc_timer.timeout.connect(self._tick_proc)

    # --- геометрия -----------------------------------------------------------
    def _screen(self):
        anchor = self._anchor_getter() if self._anchor_getter else None
        screen = None
        if anchor is not None:
            try:
                screen = anchor.screen() if anchor.isVisible() else None
            except Exception:  # noqa: BLE001
                screen = None
        return screen or QApplication.primaryScreen()

    def _place(self) -> None:
        screen = self._screen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.adjustSize()
        w, h = max(240, self.width()), self.height()
        if self.kind == "call":
            x = geo.x() + (geo.width() - w) // 2
            y = geo.y() + 16
        else:
            x = geo.x() + geo.width() - w - 16
            y = geo.y() + geo.height() - h - 16
        self.move(x, y)

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0x26, 0x30, 0x4B, 0x18))
        p.drawRoundedRect(QRectF(2, 6, self.width() - 4, self.height() - 4), 6, 6)
        p.setBrush(qc(Color.BG))
        p.setPen(QPen(qc(Color.LINE), 1))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 5, 5)
        p.end()

    def _show(self) -> None:
        self._place()
        self.show()
        self.raise_()

    # --- состояния --------------------------------------------------------------
    def show_recording(self) -> None:
        self._mode = "recording"
        self._hide_timer.stop()
        self._proc_timer.stop()
        self.dot.set(Color.SIGNAL, True)
        self.dot.show()
        self.spinner.hide()
        self.label.setText("Диктовка" if self.kind == "voice" else "Созвон")
        self.clock.show()
        self.clock.set_color(Color.ACCENT, animate=False)
        self.clock.set_seconds(0, animate=False)
        self.proc_label.hide()
        self.cancel_btn.hide()
        self._show()

    def set_seconds(self, seconds: int) -> None:
        if self._mode == "recording":
            self.clock.set_seconds(seconds)

    def show_processing(self, can_cancel: bool = True) -> None:
        self._mode = "processing"
        self._hide_timer.stop()
        self.dot.hide()
        self.spinner.show()
        self.label.setText("Распознаю…" if self.kind == "voice" else "Обработка созвона…")
        self.clock.set_color(Color.DIGIT_MINI, animate=True)
        self.proc_label.setText("")
        self.proc_label.setVisible(self.kind == "call")
        self._proc_started = time.monotonic()
        if self.kind == "call":
            self._proc_timer.start()
        self.cancel_btn.setText("Отменить")
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setVisible(can_cancel)
        self._show()

    def set_progress(self, fraction: float) -> None:
        if self._mode == "processing" and self.kind == "call":
            pct = max(0, min(100, int(round(fraction * 100))))
            self.proc_label.setText(f"обработка {fmt_mmss(time.monotonic() - self._proc_started)} · {pct}%")
            self._place()

    def _tick_proc(self) -> None:
        if self._mode != "processing":
            self._proc_timer.stop()
            return
        text = self.proc_label.text()
        tail = text.split("·")[-1].strip() if "·" in text else ""
        self.proc_label.setText(f"обработка {fmt_mmss(time.monotonic() - self._proc_started)}" + (f" · {tail}" if tail else ""))

    def show_cancelling(self) -> None:
        self._mode = "cancelling"
        self.label.setText("Останавливаю…")
        self.cancel_btn.setText("Останавливаю…")
        self.cancel_btn.setEnabled(False)
        self._place()

    def show_info(self, text: str, seconds: int = 4) -> None:
        self._mode = "info"
        self._proc_timer.stop()
        self.dot.set(Color.MUTED, False)
        self.dot.show()
        self.spinner.hide()
        self.clock.hide()
        self.proc_label.hide()
        self.cancel_btn.hide()
        self.label.setText(text if len(text) <= 90 else text[:88].rstrip() + "…")
        self._show()
        self._hide_timer.start(max(1, seconds) * 1000)

    def show_saved(self, text: str = "Скопировано в буфер") -> None:
        self._mode = "info"
        self._proc_timer.stop()
        self.dot.set(Color.SUCCESS, False)
        self.dot.show()
        self.spinner.hide()
        self.clock.hide()
        self.proc_label.hide()
        self.cancel_btn.hide()
        self.label.setText(text)
        self._show()
        self._hide_timer.start(2500)

    def show_error(self, text: str, seconds: int = 12) -> None:
        self._mode = "error"
        self._proc_timer.stop()
        self.dot.set(Color.ERROR, True)
        self.dot.show()
        self.spinner.hide()
        self.clock.hide()
        self.proc_label.hide()
        self.cancel_btn.hide()
        self.label.setText(text if len(text) <= 90 else text[:88].rstrip() + "…")
        self._show()
        self._hide_timer.start(max(1, seconds) * 1000)

    def hide_overlay(self) -> None:
        self._mode = "hidden"
        self._proc_timer.stop()
        self._hide_timer.stop()
        self.hide()

    def _hide_if_idle(self) -> None:
        if self._mode in ("info", "error"):
            self.hide_overlay()

    def mode(self) -> str:
        return self._mode

    def update_audio_level(self, level: float) -> None:
        """Уровень голоса 0..1 (огибающая из окна): точка дышит, пока идёт запись —
        видно, что микрофон слышит, а не пишет тишину."""
        if self._mode == "recording":
            self.dot.set_level(level)
