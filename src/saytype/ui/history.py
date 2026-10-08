"""Строка истории V5 (`.record-row`): дата/время · текст · длительность, обработка,
ускорение и три действия. Воспроизведение — штриховка прослушанной части по всей
строке, счётчик прошедшего времени с независимыми разрядами, «/ всего»,
перемотка по нижней границе (мышь и клавиатура).

`AudioPlayer` — обёртка над QMediaPlayer/QAudioOutput: одна на страницу, строка
только запрашивает действия сигналами, а состояние получает обратно.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import re as _re
from pathlib import Path

from PySide6.QtCore import QFile, QObject, QPointF, QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from .digits import DigitStrip, MiniClock, fmt_mmss
from .icons import IconButton
from .tokens import Color, Font, Grid, Hatch, body_font, qc
from .widgets import TOOLTIP_QSS, Cell, Label, TextViewer

PREVIEW_CHARS = 350
COLLAPSED_LINES = 5
LINE_HEIGHT = 1.5        # межстрочный текста записи (в прототипе 1.8 — просили компактнее)


def _fmt_ru_num(value: float, digits: int = 1) -> str:
    s = f"{value:.{digits}f}".replace(".", ",")
    return s


def _fmt_chars(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " знаков"


def clean_call_markdown(raw: str) -> str:
    """Тело .md созвона → реплики без заголовка, цитат и разделителей."""
    out = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(">") or s == "---":
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(s)
    return "\n".join(out).strip()


_BOLD = _re.compile(r"\*\*(.+?)\*\*")


def text_to_html(text: str, px: int) -> str:
    """Абзацы с межстрочным `LINE_HEIGHT`; `**…**` → жирный (реплики созвона)."""
    paras = [p for p in _re.split(r"\n\s*\n", text) if p.strip()]
    chunks = []
    lh = int(round(LINE_HEIGHT * 100))
    for i, para in enumerate(paras):
        esc = _html.escape(para).replace("\n", "<br>")
        esc = _BOLD.sub(r"<b>\1</b>", esc)
        margin = 0 if i == len(paras) - 1 else 12
        chunks.append(f'<p style="margin:0 0 {margin}px 0; line-height:{lh}%;">{esc}</p>')
    return f'<div style="font-size:{px}px;">' + "".join(chunks) + "</div>"


def stamp_parts(stem: str) -> tuple[str, str]:
    """`YYYY-MM-DDTHH-MM-SS` или `YYYY-MM-DD HH-MM-SS` → («СЕГОДНЯ» | дд.мм.гггг, «чч:мм»)."""
    try:
        date_part, time_part = stem[:10], stem[11:19]
        d = _dt.date.fromisoformat(date_part)
        hh, mm = time_part[0:2], time_part[3:5]
    except Exception:  # noqa: BLE001
        return (stem, "--:--")
    today = _dt.date.today()
    if d == today:
        day = "Сегодня"
    elif d == today - _dt.timedelta(days=1):
        day = "Вчера"
    else:
        day = d.strftime("%d.%m.%Y")
    return day, f"{hh}:{mm}"


def history_entry_files(entry: dict) -> list[Path]:
    """Файлы одной записи истории, которые уносит кнопка «Удалить».

    Диктовка — тройка `<ts>.wav/.txt/.meta.json`, ровно как в `rotate_history()`.
    Созвон — `.md` плюс `.wav` из буфера (T-175) и `.mp3`-архив, если они есть.
    Hi-fi копия (T-389) и сырьё `.mic.f32raw`/`.loop.raw` сюда не входят: первая —
    датасет голоса вне истории, второе живёт только во время записи и нужно recovery.
    """
    if entry.get("md_path"):
        md = Path(entry["md_path"])
        candidates = [md, md.with_suffix(".wav"), md.with_suffix(".mp3")]
    else:
        wav = Path(entry["wav_path"])
        txt = entry.get("txt_path")
        candidates = [wav, Path(txt) if txt else wav.with_suffix(".txt"), wav.parent / f"{wav.stem}.meta.json"]
    return [p for p in candidates if p.exists()]


def move_files_to_trash(paths: list[Path]) -> list[str]:
    """Убрать файлы в Корзину Windows; вернуть «файл: причина» по тем, что остались.

    Корзина, а не unlink: история лежит на диске без бэкапа, и промах по кнопке не
    должен стоить созвона. Если файл держит другая программа, он остаётся на месте и
    попадает в список — стирать его мимо Корзины молча не стоит.
    """
    failed: list[str] = []
    for p in paths:
        if not QFile.moveToTrash(str(p)):
            failed.append(f"{p.name}: файл занят другой программой или Корзина недоступна")
    return failed


class AudioPlayer(QObject):
    """QMediaPlayer с перевыбором системного вывода перед каждым play."""

    position_changed = Signal(float)      # секунды
    duration_changed = Signal(float)
    state_changed = Signal(bool)          # playing
    finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._out = QAudioOutput(self)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._out)
        self._source: Path | None = None
        self._player.positionChanged.connect(lambda ms: self.position_changed.emit(ms / 1000.0))
        self._player.durationChanged.connect(lambda ms: self.duration_changed.emit(ms / 1000.0))
        self._player.playbackStateChanged.connect(
            lambda st: self.state_changed.emit(st == QMediaPlayer.PlayingState))
        self._player.mediaStatusChanged.connect(self._on_status)

    def _on_status(self, st) -> None:
        if st == QMediaPlayer.EndOfMedia:
            self.finished.emit()

    def _refresh_device(self) -> None:
        try:
            dev = QMediaDevices.defaultAudioOutput()
            if not dev.isNull():
                self._out.setDevice(dev)
        except Exception:  # noqa: BLE001
            pass

    def source(self) -> Path | None:
        return self._source

    def play(self, path: Path) -> None:
        if self._source != path:
            self._source = path
            self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._refresh_device()
        self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def toggle(self, path: Path) -> None:
        if self._source == path and self._player.playbackState() == QMediaPlayer.PlayingState:
            self.pause()
        else:
            self.play(path)

    def stop(self) -> None:
        self._player.stop()

    def release(self) -> None:
        """Остановить и закрыть файл: Windows не отдаст открытый wav в Корзину."""
        self._player.stop()
        self._player.setSource(QUrl())
        self._source = None

    def seek(self, seconds: float) -> None:
        self._player.setPosition(int(max(0.0, seconds) * 1000))

    def is_playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlayingState

    def position(self) -> float:
        return self._player.position() / 1000.0

    def duration(self) -> float:
        return self._player.duration() / 1000.0


class _MetricsRow(QWidget):
    def __init__(self, name: str, value: str, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 5, 0, 5)
        row.setSpacing(5)
        self.name = Label(name, size=11, color=Color.MUTED)
        self.value = Label(value, size=11, color=Color.INK)
        self.value.setFont(body_font(11))
        row.addWidget(self.name)
        row.addStretch(1)
        row.addWidget(self.value)


class _SeekBar(QWidget):
    """Перемотка по нижней границе строки — отдельный слой поверх строк списка.

    Колонки строки лежат поверх её низа и забирали движение мыши: строка не
    знала, что курсор над полосой, и ползунок не появлялся. Слой ловит наведение
    сам: полоса утолщается, на позиции — квадратный ползунок; тянется мышью,
    клик — переход. Подсказки со временем нет: она дёргалась за курсором, а
    позицию и так показывают мини-часы строки справа.

    Слой живёт в контейнере списка, а не в строке: внутри строки ниже края места
    нет, и утолщённая полоса с ползунком уезжали вверх. Здесь он лежит на границе
    строк, и полоса растёт в обе стороны от своей линии — центр не двигается.
    """

    ZONE = 14        # высота зоны наведения: 8 px над краем строки, 6 px под ним
    CENTER = 7.0     # линия полосы в координатах слоя = два нижних пикселя строки
    THUMB = 10       # квадратный ползунок
    BAR = 2          # полоса в покое
    BAR_HOVER = 4    # полоса при наведении

    def __init__(self, row: "RecordRow"):
        super().__init__(row)
        self._row = row
        self._hover = False
        self._drag = False
        self.setMouseTracking(True)
        self.setCursor(Qt.SizeHorCursor)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.hide()
        row.destroyed.connect(self.deleteLater)

    def active(self) -> bool:
        return self._hover or self._drag

    def place(self) -> None:
        host = self._row.parentWidget()
        if host is None:
            return
        if self.parentWidget() is not host:
            visible = self.isVisible() or self._row._audio_active
            self.setParent(host)
            self.setVisible(visible)
        g = self._row.geometry()
        self.setGeometry(g.x(), g.y() + g.height() - int(self.CENTER) - 1, g.width(), self.ZONE)
        self.raise_()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        W = self.width()
        prog = self._row.progress()
        big = self.active() or self._row.hasFocus()
        bar = self.BAR_HOVER if big else self.BAR
        y = self.CENTER - bar / 2.0
        p.fillRect(QRectF(0, y, W, bar), qc(Color.LINE))
        p.fillRect(QRectF(0, y, W * prog, bar), qc(Color.ACCENT))
        if big:
            x = max(0.0, min(W - self.THUMB, W * prog - self.THUMB / 2.0))
            p.fillRect(QRectF(x, self.CENTER - self.THUMB / 2.0, self.THUMB, self.THUMB), qc(Color.ACCENT))
        p.end()

    def _frac(self, x: float) -> float:
        return max(0.0, min(1.0, x / max(1.0, self.width())))

    def enterEvent(self, ev) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(ev)

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.LeftButton:
            self._drag = True
            self._row.seek_fraction(self._frac(ev.position().x()))
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        if not self._hover:  # вход курсора бывает пропущен (окно под курсором открылось) — движение надёжнее
            self._hover = True
            self.update()
        if self._drag:
            self._row.seek_fraction(self._frac(ev.position().x()))
        ev.accept()

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if self._drag:
            self._drag = False
            self._row.seek_fraction(self._frac(ev.position().x()))
            self.update()
            ev.accept()
            return
        super().mouseReleaseEvent(ev)


class RecordRow(Cell):
    """Одна запись — диктовка (`kind="voice"`) или созвон (`kind="call"`)."""

    play_requested = Signal(object)
    pause_requested = Signal(object)
    seek_requested = Signal(object, float)
    copy_requested = Signal(object)
    note_requested = Signal(object)
    delete_requested = Signal(object)

    def __init__(self, entry: dict, kind: str = "voice", parent=None):
        super().__init__(parent=parent)
        self.entry = entry
        self.kind = kind
        self._audio_active = False      # выбрана (пауза/играет)
        self._playing = False
        self._progress = 0.0            # 0..1
        self._position = 0.0
        self.setMinimumHeight(Grid.RECORD_ROW_MIN)
        self.setFocusPolicy(Qt.TabFocus)
        self._kb_focus = False

        self.audio_path: Path | None = self._resolve_audio()
        self.text_full: str = self._resolve_text()
        self.duration_sec: float = float(entry.get("duration_sec") or 0.0)

        day, hhmm = stamp_parts(str(entry.get("ts") or entry.get("stem") or ""))

        # --- колонка 1: дата / время ---
        self.col_stamp = QWidget()
        c1 = QVBoxLayout(self.col_stamp)
        c1.setContentsMargins(20, 27, 20, 20)
        c1.setSpacing(13)
        self.day_label = Label(day, size=Font.TINY, color=Color.MUTED, letter_spacing=0.35, upper=True)
        # время серым: главное в строке — текст, а не штамп (08.10)
        self.time_strip = DigitStrip(hhmm, height=23, color=Color.DIGIT_MINI, colon_width=6.3, digit_width=18.9,
                                     gap=1)
        c1.addWidget(self.day_label)
        c1.addWidget(self.time_strip)
        c1.addStretch(1)

        # --- колонка 2: текст ---
        self.col_text = QWidget()
        c2 = QVBoxLayout(self.col_text)
        c2.setContentsMargins(28, 25, 28, 27)
        c2.setSpacing(0)
        self.text_label = QLabel()
        self.text_label.setWordWrap(True)
        self.text_label.setFont(body_font(Font.BODY))
        self.text_label.setStyleSheet(f"color:{Color.INK}; background:transparent;" + TOOLTIP_QSS)
        self.text_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.text_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.text_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        c2.addWidget(self.text_label)
        self.disclosure = QWidget()
        dl = QHBoxLayout(self.disclosure)
        dl.setContentsMargins(0, 18, 0, 0)
        dl.setSpacing(18)
        self.more_btn = QLabel("Весь текст")
        self.more_btn.setFont(body_font(12))
        self.more_btn.setStyleSheet(f"color:{Color.ACCENT}; background:transparent;" + TOOLTIP_QSS)
        self.more_btn.setCursor(Qt.PointingHandCursor)
        # Полный текст — листом поверх окна и у созвона, и у диктовки: раскрытие в строке
        # растягивало список на экраны (08.10).
        self.more_btn.mousePressEvent = lambda ev: self.open_viewer()  # type: ignore[assignment]
        self.chars_label = Label(_fmt_chars(len(self.text_full)), size=11, color=Color.MUTED)
        dl.addWidget(self.more_btn)
        dl.addWidget(self.chars_label)
        dl.addStretch(1)
        c2.addWidget(self.disclosure)
        c2.addStretch(1)
        self.disclosure.setVisible(len(self.text_full) > PREVIEW_CHARS)

        # --- колонка 3: длительность, метрики, действия ---
        self.col_end = QWidget()
        c3 = QVBoxLayout(self.col_end)
        c3.setContentsMargins(20, 28, 20, 20)
        c3.setSpacing(0)
        time_block = QWidget()
        tb = QHBoxLayout(time_block)
        tb.setContentsMargins(0, 0, 0, 19)
        tb.setSpacing(7)
        self.mini = MiniClock(height=28, color=Color.DIGIT_MINI)
        self.mini.set_seconds(int(self.duration_sec), animate=False)
        self.total_label = Label("", size=9, color=Color.MUTED)
        self.total_label.hide()
        tb.addWidget(self.mini)
        tb.addWidget(self.total_label, 0, Qt.AlignBottom)
        tb.addStretch(1)  # часы прижаты влево и в покое, и при воспроизведении
        c3.addWidget(time_block)
        meta = entry.get("meta") or {}
        elapsed = meta.get("elapsed_sec")
        ratio = meta.get("ratio_x")
        if ratio is None and elapsed and self.duration_sec:
            ratio = self.duration_sec / float(elapsed) if float(elapsed) > 0 else None
        self.m_proc = _MetricsRow("Обработка", f"{_fmt_ru_num(float(elapsed))} с" if elapsed else "—")
        self.m_ratio = _MetricsRow("Ускорение", f"{_fmt_ru_num(float(ratio))}×" if ratio else "—")
        c3.addWidget(self.m_proc)
        c3.addWidget(self.m_ratio)
        actions = QWidget()
        al = QHBoxLayout(actions)
        al.setContentsMargins(0, 18, 0, 0)
        al.setSpacing(6)
        self.play_btn = IconButton("play", size=Grid.ROW_ACTION, icon_size=Grid.ICON_ROW,
                                   tooltip="Воспроизвести запись")
        self.note_btn = IconButton("note", size=Grid.ROW_ACTION, icon_size=Grid.ICON_ROW, tooltip="В заметки")
        self.copy_btn = IconButton("copy", size=Grid.ROW_ACTION, icon_size=Grid.ICON_ROW, tooltip="Копировать")
        if self.audio_path is None:
            self.play_btn.setEnabled(False)
            self.play_btn.setToolTip("Аудио этой записи уже нет: буфер хранит только последние файлы")
        # корзина — отдельно у правого края: необратимое действие не рядом с «копировать»
        self.delete_btn = IconButton("trash", size=Grid.ROW_ACTION, icon_size=Grid.ICON_ROW,
                                     hover_color=Color.DANGER,
                                     tooltip="Удалить созвон" if kind == "call" else "Удалить запись")
        al.addWidget(self.play_btn)
        al.addWidget(self.note_btn)
        al.addWidget(self.copy_btn)
        al.addStretch(1)
        al.addWidget(self.delete_btn)
        c3.addWidget(actions)
        c3.addStretch(1)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self.col_stamp, 1)
        row.addWidget(self.col_text, 3)
        row.addWidget(self.col_end, 1)

        self.play_btn.clicked.connect(self._on_play_clicked)
        self.copy_btn.clicked.connect(lambda: self.copy_requested.emit(self))
        self.note_btn.clicked.connect(lambda: self.note_requested.emit(self))
        self.delete_btn.clicked.connect(lambda: self.delete_requested.emit(self))
        self.seek = _SeekBar(self)
        self._apply_text()

    # --- данные ---------------------------------------------------------------
    def _resolve_audio(self) -> Path | None:
        e = self.entry
        if self.kind == "voice":
            p = e.get("wav_path")
            return Path(p) if p and Path(p).exists() else None
        md = e.get("md_path")
        if md:
            for suf in (".wav", ".mp3"):
                cand = Path(md).with_suffix(suf)
                if cand.exists():
                    return cand
        return None

    def _resolve_text(self) -> str:
        e = self.entry
        if self.kind == "voice":
            return (e.get("text_full") or "").strip()
        md = e.get("md_path")
        if md:
            try:
                raw = Path(md).read_text(encoding="utf-8")
                if raw.startswith("---"):
                    parts = raw.split("---", 2)
                    if len(parts) == 3:
                        raw = parts[2]
                return clean_call_markdown(raw)
            except OSError:
                pass
        return (e.get("preview") or "").strip()

    def _apply_text(self) -> None:
        text = self.text_full or "(пусто)"
        if len(text) > PREVIEW_CHARS:
            text = text[:PREVIEW_CHARS].rstrip() + "…"
            fm = QFontMetrics(self.text_label.font())
            self.text_label.setMaximumHeight(int(fm.lineSpacing() * LINE_HEIGHT * COLLAPSED_LINES) + 4)
        else:
            self.text_label.setMaximumHeight(16777215)
        self.text_label.setTextFormat(Qt.RichText)
        self.text_label.setText(text_to_html(text, self.text_label.font().pixelSize()))
        self.more_btn.setText("Открыть текст" if self.kind == "call" else "Весь текст")

    def files(self) -> list[Path]:
        """Файлы записи на диске — то, что уносит корзина строки (см. `history_entry_files`)."""
        return history_entry_files(self.entry)

    def open_viewer(self) -> None:
        """Полный текст записи листом поверх приложения."""
        title = f"{'Созвон' if self.kind == 'call' else 'Диктовка'} · {self.day_label.text()} {self.time_strip.text()}"
        TextViewer(title, text_to_html(self.text_full or "(пусто)", Font.BODY), self.text_full,
                   parent=self.window()).show()

    # --- воспроизведение ------------------------------------------------------
    def _on_play_clicked(self) -> None:
        if self.audio_path is None:
            return
        if self._playing:
            self.pause_requested.emit(self)
        else:
            self.play_requested.emit(self)

    def set_audio_state(self, selected: bool, playing: bool) -> None:
        self._audio_active = selected
        self._playing = playing
        self.play_btn.set_icon_name("pause" if playing else "play")
        self.play_btn.set_pressed_state(playing)
        if selected:
            color = Color.ACCENT if playing else Color.DIGIT_MINI_SELECTED
            total = fmt_mmss(self.duration_sec)
            self.total_label.setText(f"/ {total}")
            self.total_label.show()
            self.play_btn.setToolTip(
                f"Пауза · {fmt_mmss(self._position)} / {total}" if playing
                else f"Продолжить · {fmt_mmss(self._position)} / {total}")
        else:
            color = Color.DIGIT_MINI
            self.total_label.hide()
            self._progress = 0.0
            self._position = 0.0
            self.mini.set_seconds(int(self.duration_sec), animate=False)
            self.play_btn.setToolTip("Воспроизвести запись")
        self.mini.set_color(color)
        # перемотка — только у выбранной записи с аудио (как `.row-seek` в прототипе)
        if selected and self.audio_path is not None:
            self.seek.place()
            self.seek.show()
        else:
            self.seek.hide()
        self.update()

    def set_position(self, seconds: float, duration: float | None = None) -> None:
        if duration and duration > 0:
            self.duration_sec = max(self.duration_sec, duration) if self.duration_sec < 1 else self.duration_sec
        dur = self.duration_sec or (duration or 0.0)
        self._position = max(0.0, min(seconds, dur if dur else seconds))
        self._progress = (self._position / dur) if dur else 0.0
        if self._audio_active:
            self.mini.set_seconds(int(self._position), animate=True)
        self.update()
        self.seek.update()

    def progress(self) -> float:
        return self._progress

    def seek_fraction(self, frac: float) -> None:
        """Перемотка с полосы: позиция сразу в строке, плееру — запрос."""
        if not self.duration_sec:
            return
        self._progress = max(0.0, min(1.0, frac))
        self._position = self._progress * self.duration_sec
        self.mini.set_seconds(int(self._position), animate=False)
        self.update()
        self.seek.update()
        self.seek_requested.emit(self, self._position)

    def set_finished(self) -> None:
        self._position = self.duration_sec
        self._progress = 1.0
        self._playing = False
        self.play_btn.set_icon_name("play")
        self.play_btn.set_pressed_state(False)
        self.mini.set_seconds(int(self.duration_sec), animate=True)
        self.mini.set_color(Color.DIGIT_MINI_SELECTED)
        self.update()
        self.seek.update()

    # --- отрисовка ------------------------------------------------------------
    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), qc(Color.BG))
        W, H = self.width(), self.height()
        if self._audio_active and self._progress > 0:
            p.fillRect(QRectF(0, 0, W * self._progress, H), Hatch.brush(self.devicePixelRatioF()))
        pen = QPen(qc(Color.LINE), 1)
        p.setPen(pen)
        p.drawLine(QPointF(0, H - 0.5), QPointF(W, H - 0.5))
        # вертикальные границы колонок
        x1 = self.col_text.x()
        x2 = self.col_end.x()
        p.drawLine(QPointF(x1 - 0.5, 0), QPointF(x1 - 0.5, H))
        p.drawLine(QPointF(x2 + 0.5, 0), QPointF(x2 + 0.5, H))
        # полоса перемотки и ползунок — слой `_SeekBar` поверх колонок
        if self._kb_focus:
            p.setPen(QPen(qc(Color.ACCENT), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(1, 1, W - 2, H - 3))
        p.end()

    def focusInEvent(self, ev) -> None:  # noqa: N802
        self._kb_focus = ev.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason)
        super().focusInEvent(ev)
        self.update()
        self.seek.update()

    def focusOutEvent(self, ev) -> None:  # noqa: N802
        self._kb_focus = False
        super().focusOutEvent(ev)
        self.update()
        self.seek.update()

    # слой перемотки живёт в контейнере списка — следует за строкой сам
    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        self.seek.place()

    def moveEvent(self, ev) -> None:  # noqa: N802
        super().moveEvent(ev)
        self.seek.place()

    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        if self._audio_active and self.audio_path is not None:
            self.seek.place()
            self.seek.show()

    def hideEvent(self, ev) -> None:  # noqa: N802
        super().hideEvent(ev)
        if self.seek.parentWidget() is not self:
            self.seek.hide()

    def keyPressEvent(self, ev) -> None:  # noqa: N802
        if self.audio_path is not None and ev.key() in (Qt.Key_Left, Qt.Key_Right):
            delta = -5.0 if ev.key() == Qt.Key_Left else 5.0
            pos = max(0.0, min(self.duration_sec, self._position + delta))
            if self.duration_sec:
                self.seek_fraction(pos / self.duration_sec)
            return
        if ev.key() in (Qt.Key_Space, Qt.Key_Return) and self.audio_path is not None:
            self._on_play_clicked()
            return
        super().keyPressEvent(ev)

    def set_window_width(self, w: int) -> None:
        pad = Grid.pad(w)
        self.col_stamp.layout().setContentsMargins(pad - 10, 27, pad - 10, 20)
        self.col_text.layout().setContentsMargins(pad - 2, 25, pad - 2, 27)
        self.col_end.layout().setContentsMargins(pad - 10, 28, pad - 10, 20)
        self.text_label.setFont(body_font(13 if w <= 1100 else (16 if w >= 1500 else Font.BODY)))
        self._apply_text()
