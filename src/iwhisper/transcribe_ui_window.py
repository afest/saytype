"""UI слой над transcribe_ui.py — окно истории + плеер + settings dialog + tray.

Содержит:
- MainWindow: QMainWindow с таблицей последних записей, QMediaPlayer для wav,
  QPlainTextEdit с полным текстом, кнопка copy. Под крышкой — QSystemTrayIcon
  с меню (Открыть/Настройки/Выход).
- SettingsDialog: hotkey / папка истории / словарь / замены / автостарт и т.д.
- ReplacementsDialog: редактор правил замен в готовом тексте.
- QSettings слой (settings.ini в профиле пользователя, см. `profile.py`).
- Helpers для Windows shell:startup автостарта.

Подключается из transcribe_ui.py — там сидит запись/транскрипция/глобальный
hotkey, отсюда — только UI. Связь через колбэки (history_dir_getter,
rotation_count_getter) и сигналы (settings_changed, quit_requested).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import wave
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import cuda_layer  # T-261: докачиваемый CUDA-рантайм (строка «Ускорение GPU»)
from . import engine  # T-259: пресеты моделей, валидация «своей модели», учёт места
from . import profile  # где лежат настройки, словарь и замены
from . import updater  # T-262: проверка обновлений через Velopack

from PySide6.QtCore import Qt, QSettings, QUrl, QSize, QMargins, Signal, Slot
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPen, QPixmap, QPolygon
from PySide6.QtCore import QPoint, QRect
from PySide6.QtCharts import (
    QBarCategoryAxis,
    QBarSeries,
    QBarSet,
    QChart,
    QChartView,
    QLineSeries,
    QValueAxis,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QKeySequenceEdit,
    QButtonGroup,
    QLabel,
    QLineEdit,
    QMainWindow,
    QRadioButton,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


APPDATA = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
SETTINGS_FILE = profile.settings_file()
SETTINGS_DIR = SETTINGS_FILE.parent
STARTUP_DIR = APPDATA / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
STARTUP_SHORTCUT_NAME = "iWhisper.lnk"

# hotkey хранится в pynput-формате: <ctrl>+<shift>+q (модификаторы в <>, lowercase)
DEFAULT_HOTKEY = "<ctrl>+<shift>+q"
DEFAULT_HISTORY_DIR = profile.default_history_dir().as_posix()
DEFAULT_ROTATION_COUNT = 5
DEFAULT_AUTOSTART = False
DEFAULT_START_MINIMIZED = False
# T-165: hybrid streaming/batch — заменил DEFAULT_STREAMING_MODE (bool).
# Значения processing_mode: "auto" (batch ≤ N сек, streaming >), "always_batch",
# "always_streaming". Старый bool-ключ streaming_mode мигрируется в load_settings_dict.
DEFAULT_PROCESSING_MODE = "auto"
DEFAULT_AUTO_THRESHOLD_SEC = 10
DEFAULT_PRE_ROLL_ENABLED = False  # T-164: rolling 500мс ДО hotkey (always-on mic, дефолт OFF)
DEFAULT_KEEP_CALL_AUDIO = False   # T-174: хранить MP3 созвона (дефолт OFF — нужен только транскрипт)
DEFAULT_CHECK_UPDATES = True      # T-262: фоновая проверка обновлений (выключаемая в настройках)
# T-263: устройство записи. Пусто = системное по умолчанию — так было всегда и
# так остаётся, пока человек не выберет конкретный микрофон в мастере.
DEFAULT_MIC_DEVICE = ""
DEFAULT_CALL_AUDIO_KEEP = 2       # T-175: сколько последних WAV созвонов держать в Calls\ (буфер «вернуться»)
# T-259: модель транскрипции. `model` = ключ пресета из engine.PRESETS либо
# "custom"; при "custom" значение берётся из `custom_model` (HF repo id или путь
# к папке с CT2-моделью). Для СУЩЕСТВУЮЩИХ настроек (settings.ini уже есть)
# дефолт — прежний "small": молча менять модель работающему пользователю нельзя.
# Для первого запуска дефолт подбирается по железу (engine.recommended_preset()).
DEFAULT_MODEL_EXISTING = "small"
# Имена говорящих в транскрипте созвона. Нейтральные — своё имя вписывает пользователь.
DEFAULT_SPEAKER_SELF = "Я"
DEFAULT_SPEAKER_OTHER = "Собеседник"

_HOTKEY_MODIFIERS = {"ctrl", "shift", "alt", "meta", "cmd", "win"}


# === Векторные иконки через QPainter — гарантированно видны без MDL2 шрифта ===

def _mk_icon(draw_fn, size: int = 18, color: str = "#2a2a2a") -> QIcon:
    """Создать QIcon рисованием через callback draw_fn(painter, rect, color)."""
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    rect = QRect(0, 0, size, size)
    draw_fn(p, rect, QColor(color))
    p.end()
    return QIcon(pix)


def _mk_pen(c: QColor, size: int) -> QPen:
    """Единый pen — stroke 2px (как в дизайне), пропорционально масштабируется.
    Для размера 24 → 2px. Для 16 → 1.33px. Для 14 → 1.17px.
    Минимум 1px чтобы не пропадал на маленьких иконках."""
    pen = QPen(c)
    # 2 / 24 = 0.0833, то есть stroke = size * 0.0833 (24→2, 16→1.33, 12→1.0)
    pen.setWidthF(max(1.0, size * 2.0 / 24.0))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    return pen


def _draw_play(p: QPainter, r: QRect, c: QColor) -> None:
    """Filled triangle (Win11 media controls — solid)."""
    p.setBrush(c)
    p.setPen(Qt.NoPen)
    s = r.width()
    pad = int(s * 0.22)
    tri = QPolygon([
        QPoint(pad, pad),
        QPoint(pad, s - pad),
        QPoint(s - pad, s // 2),
    ])
    p.drawPolygon(tri)


def _draw_pause(p: QPainter, r: QRect, c: QColor) -> None:
    p.setBrush(c)
    p.setPen(Qt.NoPen)
    s = r.width()
    w = int(s * 0.18)
    h = int(s * 0.55)
    y = (s - h) // 2
    p.drawRoundedRect(int(s * 0.28) - w // 2, y, w, h, 1, 1)
    p.drawRoundedRect(int(s * 0.72) - w // 2, y, w, h, 1, 1)


def _draw_stop(p: QPainter, r: QRect, c: QColor) -> None:
    p.setBrush(c)
    p.setPen(Qt.NoPen)
    s = r.width()
    pad = int(s * 0.28)
    p.drawRoundedRect(pad, pad, s - pad * 2, s - pad * 2, 1, 1)


def _draw_copy(p: QPainter, r: QRect, c: QColor) -> None:
    """Copy — копия icons.jsx CopyIcon:
       <rect x=8 y=8 w=12 h=12 rx=2 />
       <path d="M16 8 V6 a2 2 0 0 0 -2 -2 H6 a2 2 0 0 0 -2 2 v8 a2 2 0 0 0 2 2 h2" />
    """
    from PySide6.QtGui import QPainterPath
    p.setPen(_mk_pen(c, r.width()))
    p.setBrush(Qt.NoBrush)
    s = r.width()
    k = s / 24.0
    # back rect (back paper) — рисуем как open path сначала
    path = QPainterPath()
    path.moveTo(16*k, 8*k)
    path.lineTo(16*k, 6*k)              # вверх до tab
    path.arcTo(14*k, 4*k, 4*k, 4*k, 0, 90)   # rounded corner
    path.lineTo(6*k, 4*k)
    path.arcTo(4*k, 4*k, 4*k, 4*k, 90, 90)
    path.lineTo(4*k, 14*k)
    path.arcTo(4*k, 12*k, 4*k, 4*k, 180, 90)
    path.lineTo(8*k, 16*k)
    p.drawPath(path)
    # front rect (foreground paper)
    p.drawRoundedRect(int(8*k), int(8*k), int(12*k), int(12*k), int(2*k), int(2*k))


def _draw_mic_icon(p: QPainter, r: QRect, c: QColor) -> None:
    """Mic — точная копия icons.jsx MicIcon (24x24 viewBox):
       <rect x=9 y=3 w=6 h=11 rx=3 />
       <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0" />
       <line x1=12 y1=18 x2=12 y2=21 />
       <line x1=9 y1=21 x2=15 y2=21 />
    """
    p.setPen(_mk_pen(c, r.width()))
    p.setBrush(Qt.NoBrush)
    s = r.width()
    k = s / 24.0
    # rect 9,3 → 15,14 (6x11, rx=3)
    p.drawRoundedRect(int(9*k), int(3*k), int(6*k), int(11*k), int(3*k), int(3*k))
    # arc (5.5, 11.5) → (18.5, 18) шириной 13, полукруг 0..180 (нижний полукруг)
    p.drawArc(int(5.5*k), int(5*k), int(13*k), int(13*k), 0, -180 * 16)
    # vertical line 12,18 → 12,21
    p.drawLine(int(12*k), int(18*k), int(12*k), int(21*k))
    # horizontal base 9,21 → 15,21
    p.drawLine(int(9*k), int(21*k), int(15*k), int(21*k))


def _draw_gear(p: QPainter, r: QRect, c: QColor) -> None:
    """Settings gear — упрощённая Win11-shape: outline зубцы + центральный круг.
    Дизайн icons.jsx SettingsIcon слишком сложный (Bezier-кривые), упрощаем
    до 8-зубчатого outline + центральный hole-circle.
    """
    from PySide6.QtGui import QPainterPath
    import math
    p.setPen(_mk_pen(c, r.width()))
    p.setBrush(Qt.NoBrush)
    s = r.width()
    cx, cy = s / 2.0, s / 2.0
    outer = s * 0.40
    tooth = s * 0.10
    hub = s * 0.12
    path = QPainterPath()
    n_teeth = 8
    pts = 32  # 4 точки на зубец для smooth shape
    for i in range(pts):
        angle = i * (2 * math.pi / pts) - math.pi / 2
        local = i % 4
        # 0=tip start, 1=tip end, 2=valley start, 3=valley end
        if local in (0, 1):
            radius = outer + tooth
        else:
            radius = outer
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        if i == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closeSubpath()
    p.drawPath(path)
    p.drawEllipse(int(cx - hub), int(cy - hub), int(hub * 2), int(hub * 2))


def _draw_folder(p: QPainter, r: QRect, c: QColor) -> None:
    """Folder — копия icons.jsx FolderIcon:
       M3 7 a2 2 0 0 1 2-2 h3.5 l2 2 H19 a2 2 0 0 1 2 2 v8 a2 2 0 0 1 -2 2 H5 a2 2 0 0 1 -2 -2 Z
    """
    from PySide6.QtGui import QPainterPath
    p.setPen(_mk_pen(c, r.width()))
    p.setBrush(Qt.NoBrush)
    s = r.width()
    k = s / 24.0
    path = QPainterPath()
    # начало tab — 5,5 (после rounded corner)
    path.moveTo(5*k, 5*k)
    path.lineTo(8.5*k, 5*k)      # вверх tab
    path.lineTo(10.5*k, 7*k)     # диагональ к body
    path.lineTo(19*k, 7*k)       # верх body
    # правый верхний угол с радиусом 2
    path.arcTo(17*k, 7*k, 4*k, 4*k, 90, -90)
    path.lineTo(21*k, 17*k)      # правая сторона
    path.arcTo(17*k, 15*k, 4*k, 4*k, 0, -90)
    path.lineTo(5*k, 19*k)       # низ
    path.arcTo(3*k, 15*k, 4*k, 4*k, 270, -90)
    path.lineTo(3*k, 7*k)        # левая
    path.arcTo(3*k, 5*k, 4*k, 4*k, 180, -90)
    path.closeSubpath()
    p.drawPath(path)


def _draw_chart(p: QPainter, r: QRect, c: QColor) -> None:
    """Bar chart — три вертикальные линии (низкая/высокая/средняя) на base-line.
    Стиль Lucide bar-chart-3 (stroke 2px, round caps как у gear/folder).
       <line x1=6  x2=6  y1=20 y2=14 />
       <line x1=12 x2=12 y1=20 y2=4  />
       <line x1=18 x2=18 y1=20 y2=10 />
    """
    p.setPen(_mk_pen(c, r.width()))
    p.setBrush(Qt.NoBrush)
    s = r.width()
    k = s / 24.0
    p.drawLine(int(6*k),  int(20*k), int(6*k),  int(14*k))
    p.drawLine(int(12*k), int(20*k), int(12*k), int(4*k))
    p.drawLine(int(18*k), int(20*k), int(18*k), int(10*k))


def _draw_chip(p: QPainter, r: QRect, c: QColor) -> None:
    """Chip/cpu — квадрат с ножками (T-259, кнопка «Модели»).
    Стиль Lucide cpu: рамка 6..18, внутренний квадрат 9..15, по 2 ножки с каждой стороны.
    """
    p.setPen(_mk_pen(c, r.width()))
    p.setBrush(Qt.NoBrush)
    s = r.width()
    k = s / 24.0
    p.drawRoundedRect(int(6 * k), int(6 * k), int(12 * k), int(12 * k), 2 * k, 2 * k)
    p.drawRect(int(9.5 * k), int(9.5 * k), int(5 * k), int(5 * k))
    for pos in (9.5, 14.5):
        p.drawLine(int(pos * k), int(2.5 * k), int(pos * k), int(6 * k))    # сверху
        p.drawLine(int(pos * k), int(18 * k), int(pos * k), int(21.5 * k))  # снизу
        p.drawLine(int(2.5 * k), int(pos * k), int(6 * k), int(pos * k))    # слева
        p.drawLine(int(18 * k), int(pos * k), int(21.5 * k), int(pos * k))  # справа


def _draw_phone(p: QPainter, r: QRect, c: QColor) -> None:
    """Phone handset — упрощённая Lucide phone (stroke 2px, round caps как gear/folder).
    Скруглённая «трубка»: диагональ из левого-верха в правый-низ с расширениями
    на концах (ушки наушника/микрофона). Достаточно узнаваемо в 18px.
    """
    from PySide6.QtGui import QPainterPath
    p.setPen(_mk_pen(c, r.width()))
    p.setBrush(Qt.NoBrush)
    s = r.width()
    k = s / 24.0
    path = QPainterPath()
    # верхнее «ушко» (earpiece) слева сверху
    path.moveTo(7.5*k, 4*k)
    path.arcTo(4*k, 4*k, 5*k, 5*k, 90, 90)      # скругление в левом-верхнем углу
    path.lineTo(4*k, 9*k)
    # диагональ тела трубки к нижнему-правому «ушку» (mouthpiece)
    path.lineTo(15*k, 20*k)
    path.arcTo(15*k, 15*k, 5*k, 5*k, 180, 90)   # скругление в правом-нижнем углу
    path.lineTo(20*k, 16.5*k)
    p.drawPath(path)


def hotkey_to_pynput(s: str) -> str:
    """Конвертация произвольной hotkey-строки в pynput-формат: '<ctrl>+<shift>+q'.

    Принимает 'Ctrl+Shift+Q' (Qt), 'ctrl+shift+q' (keyboard lib), '<ctrl>+<shift>+q' (pynput).
    Модификаторы в lowercase оборачиваются в <>. Обычные буквы — lowercase без <>.
    """
    if not s:
        return ""
    cleaned = s.replace("<", "").replace(">", "")
    parts = [p.strip().lower() for p in cleaned.split("+") if p.strip()]
    out = []
    for p in parts:
        mod = "cmd" if p in ("meta", "win") else p
        if mod in _HOTKEY_MODIFIERS:
            out.append(f"<{mod}>")
        else:
            out.append(p)
    return "+".join(out)


def hotkey_to_qt(s: str) -> str:
    """Конвертация pynput-формата в Qt KeySequence-style ('Ctrl+Shift+Q') для отображения."""
    if not s:
        return ""
    parts = s.split("+")
    out = []
    for p in parts:
        clean = p.replace("<", "").replace(">", "").strip()
        if not clean:
            continue
        if clean.lower() in _HOTKEY_MODIFIERS:
            out.append(clean.capitalize())
        else:
            out.append(clean.upper() if len(clean) == 1 else clean.capitalize())
    return "+".join(out)


def get_settings() -> QSettings:
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    # Настройки раньше жили в %APPDATA%\faster-whisper-ui — подхватываем их один
    # раз, чтобы обновление не сбросило пользователю hotkey и папку истории.
    note = profile.migrate_legacy_settings()
    if note:
        print(f"[settings] {note}", file=sys.stderr)
    return QSettings(str(SETTINGS_FILE), QSettings.IniFormat)


_VALID_PROCESSING_MODES = ("auto", "always_batch", "always_streaming")


def load_settings_dict() -> dict:
    s = get_settings()
    raw_hotkey = s.value("hotkey", DEFAULT_HOTKEY, type=str)

    # T-165: миграция streaming_mode (bool) → processing_mode (str).
    # Старый bool: True (streaming всегда) → "always_streaming"; False (full всегда) → "auto"
    # (дефолт нового флоу — автомат, без ручного управления). Старый ключ удаляем.
    if s.contains("streaming_mode") and not s.contains("processing_mode"):
        legacy_streaming = _as_bool(s.value("streaming_mode", False))
        migrated = "always_streaming" if legacy_streaming else "auto"
        s.setValue("processing_mode", migrated)
        s.remove("streaming_mode")
        s.sync()

    processing_mode = s.value("processing_mode", DEFAULT_PROCESSING_MODE, type=str)
    if processing_mode not in _VALID_PROCESSING_MODES:
        processing_mode = DEFAULT_PROCESSING_MODE  # повреждённое значение → дефолт

    threshold = int(s.value("auto_threshold_sec", DEFAULT_AUTO_THRESHOLD_SEC))
    threshold = max(1, min(60, threshold))  # clamp в acceptance-диапазон

    # T-259: модель. Первый запуск (ключей в ini ещё нет) → подбор по железу;
    # существующий пользователь без ключа `model` → прежний small, без сюрпризов.
    if s.contains("model"):
        model_key = s.value("model", DEFAULT_MODEL_EXISTING, type=str)
    elif SETTINGS_FILE.exists() and s.contains("hotkey"):
        model_key = DEFAULT_MODEL_EXISTING
        s.setValue("model", model_key)
        s.sync()
    else:
        model_key = engine.recommended_preset()
        s.setValue("model", model_key)
        s.sync()
    # T-263: мастер первого запуска — по тому же правилу, что и модель выше.
    # У того, кто пользовался приложением до появления мастера, настройки уже
    # выставлены, и показывать ему шаги «выберите микрофон, выберите хоткей»
    # после обновления — навязываться с тем, что он давно решил.
    if s.contains("wizard_done"):
        wizard_done = _as_bool(s.value("wizard_done", False))
    elif SETTINGS_FILE.exists() and s.contains("hotkey"):
        wizard_done = True
        s.setValue("wizard_done", True)
        s.sync()
    else:
        wizard_done = False

    custom_model = s.value("custom_model", "", type=str) or ""
    if model_key not in engine.PRESET_KEYS and model_key != engine.CUSTOM_KEY:
        model_key = DEFAULT_MODEL_EXISTING  # повреждённое значение → дефолт
    if model_key == engine.CUSTOM_KEY and not custom_model.strip():
        model_key = DEFAULT_MODEL_EXISTING  # "custom" без значения — бессмысленно

    return {
        "model": model_key,
        "custom_model": custom_model,
        "hotkey": hotkey_to_pynput(raw_hotkey),  # старые значения (без <>) автоматом нормализуются
        "history_dir": s.value("history_dir", DEFAULT_HISTORY_DIR, type=str),
        "rotation_count": int(s.value("rotation_count", DEFAULT_ROTATION_COUNT)),
        "autostart": _as_bool(s.value("autostart", DEFAULT_AUTOSTART)),
        "start_minimized": _as_bool(s.value("start_minimized", DEFAULT_START_MINIMIZED)),
        "processing_mode": processing_mode,
        "auto_threshold_sec": threshold,
        "pre_roll_enabled": _as_bool(s.value("pre_roll_enabled", DEFAULT_PRE_ROLL_ENABLED)),
        "keep_call_audio": _as_bool(s.value("keep_call_audio", DEFAULT_KEEP_CALL_AUDIO)),
        "call_audio_keep": max(1, int(s.value("call_audio_keep", DEFAULT_CALL_AUDIO_KEEP))),
        "speaker_self": s.value("speaker_self", DEFAULT_SPEAKER_SELF, type=str) or DEFAULT_SPEAKER_SELF,
        "speaker_other": s.value("speaker_other", DEFAULT_SPEAKER_OTHER, type=str) or DEFAULT_SPEAKER_OTHER,
        # T-261: пользователь отказался от докачки CUDA-слоя. Спрашиваем один
        # раз: повторять предложение на каждом старте — навязчиво, вернуться к
        # нему можно кнопкой в настройках.
        "cuda_layer_declined": _as_bool(s.value("cuda_layer_declined", False)),
        # T-262: фоновая проверка обновлений при старте
        "check_updates": _as_bool(s.value("check_updates", DEFAULT_CHECK_UPDATES)),
        # T-263: имя устройства записи (пусто — системное) и признак того, что
        # мастер первого запуска уже пройден
        "mic_device": s.value("mic_device", DEFAULT_MIC_DEVICE, type=str) or "",
        "wizard_done": wizard_done,
        # Язык интерфейса запоминается уже сейчас, хотя переключать пока нечего:
        # перевод вынесен в отдельную задачу, а определить язык по локали при
        # первом запуске надо в момент первого запуска, а не задним числом.
        "ui_language": s.value("ui_language", "", type=str) or "",
        # Словарь — отдельный файл в профиле, а не значение ini: пользователь
        # правит его руками и делится им, а QSettings экранирует не-ASCII.
        "dictionary": profile.load_dictionary(),
    }


def save_settings_dict(d: dict) -> None:
    s = get_settings()
    s.setValue("hotkey", hotkey_to_pynput(d["hotkey"]))
    s.setValue("history_dir", d["history_dir"])
    s.setValue("rotation_count", int(d["rotation_count"]))
    s.setValue("autostart", bool(d["autostart"]))
    s.setValue("start_minimized", bool(d["start_minimized"]))
    mode = d.get("processing_mode", DEFAULT_PROCESSING_MODE)
    if mode not in _VALID_PROCESSING_MODES:
        mode = DEFAULT_PROCESSING_MODE
    s.setValue("processing_mode", mode)
    threshold = int(d.get("auto_threshold_sec", DEFAULT_AUTO_THRESHOLD_SEC))
    s.setValue("auto_threshold_sec", max(1, min(60, threshold)))
    s.setValue("pre_roll_enabled", bool(d.get("pre_roll_enabled", DEFAULT_PRE_ROLL_ENABLED)))
    s.setValue("keep_call_audio", bool(d.get("keep_call_audio", DEFAULT_KEEP_CALL_AUDIO)))
    s.setValue("call_audio_keep", max(1, int(d.get("call_audio_keep", DEFAULT_CALL_AUDIO_KEEP))))
    # T-259: модель + «своя модель» (repo id / путь). Пустой custom при model=custom
    # не сохраняем как custom — откатываем на дефолт, чтобы не получить нерабочий ini.
    model_key = d.get("model", DEFAULT_MODEL_EXISTING)
    custom_model = (d.get("custom_model") or "").strip()
    if model_key not in engine.PRESET_KEYS and model_key != engine.CUSTOM_KEY:
        model_key = DEFAULT_MODEL_EXISTING
    if model_key == engine.CUSTOM_KEY and not custom_model:
        model_key = DEFAULT_MODEL_EXISTING
    s.setValue("model", model_key)
    s.setValue("custom_model", custom_model)
    s.setValue("speaker_self", (d.get("speaker_self") or DEFAULT_SPEAKER_SELF).strip()
               or DEFAULT_SPEAKER_SELF)
    s.setValue("speaker_other", (d.get("speaker_other") or DEFAULT_SPEAKER_OTHER).strip()
               or DEFAULT_SPEAKER_OTHER)
    if "cuda_layer_declined" in d:
        s.setValue("cuda_layer_declined", bool(d["cuda_layer_declined"]))
    s.setValue("check_updates", bool(d.get("check_updates", DEFAULT_CHECK_UPDATES)))
    if "mic_device" in d:
        s.setValue("mic_device", (d.get("mic_device") or "").strip())
    if "wizard_done" in d:
        s.setValue("wizard_done", bool(d["wizard_done"]))
    if d.get("ui_language"):
        s.setValue("ui_language", d["ui_language"])
    s.sync()
    if "dictionary" in d:
        profile.save_dictionary(d.get("dictionary") or "")


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("true", "1", "yes", "on")
    return bool(v)


def autostart_shortcut_exists() -> bool:
    return (STARTUP_DIR / STARTUP_SHORTCUT_NAME).exists()


def create_autostart_shortcut(target_script: Path | None = None) -> tuple[bool, str]:
    """Создать ярлык автозапуска в shell:startup.

    Из исходников запускаем пакет модулем, а не файлом: внутри пакета
    относительные импорты, и прямой запуск `transcribe_ui.py` файлом их не
    разрешит. `target_script` оставлен для совместимости вызовов и используется
    только как рабочая папка.

    В собранном виде (`sys.frozen`) целью становится сам `iwhisper.exe` без
    аргументов: `-m iwhisper` бутлоадер PyInstaller не понимает и передал бы
    строку приложению как argv — автозапуск молча ломался бы (T-261).

    Возвращает (success, message). message — пояснение для UI на случай fail.
    """
    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        target = Path(sys.executable)
        arguments = ""
        working_dir = target.parent
    else:
        target = Path(sys.executable).with_name("pythonw.exe")
        if not target.exists():
            target = Path(sys.executable)
        arguments = "-m iwhisper"
        working_dir = (target_script or Path(__file__).resolve()).parent
    STARTUP_DIR.mkdir(parents=True, exist_ok=True)
    shortcut_path = STARTUP_DIR / STARTUP_SHORTCUT_NAME
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$lnk = $ws.CreateShortcut('{shortcut_path}'); "
        f"$lnk.TargetPath = '{target}'; "
        f"$lnk.Arguments = '{arguments}'; "
        f"$lnk.WorkingDirectory = '{working_dir}'; "
        "$lnk.WindowStyle = 7; "
        "$lnk.Save()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            check=True,
            capture_output=True,
            text=True,
            creationflags=0x08000000,  # CREATE_NO_WINDOW — без вспышки консоли
        )
        return True, str(shortcut_path)
    except subprocess.CalledProcessError as exc:
        return False, exc.stderr or str(exc)


def remove_autostart_shortcut() -> tuple[bool, str]:
    shortcut = STARTUP_DIR / STARTUP_SHORTCUT_NAME
    if not shortcut.exists():
        return True, "ярлык не существовал"
    try:
        shortcut.unlink()
        return True, "удалён"
    except OSError as exc:
        return False, str(exc)


def read_history_entries(history_dir: Path, count: int) -> list[dict]:
    """Прочитать N последних wav+txt пар (+ опциональный .meta.json), отсортированных от новых к старым."""
    if not history_dir.exists():
        return []
    wavs = sorted(history_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)[:count]
    entries = []
    for wav in wavs:
        txt = wav.with_suffix(".txt")
        text_full = ""
        if txt.exists():
            try:
                text_full = txt.read_text(encoding="utf-8")
            except OSError:
                text_full = ""
        preview = text_full.replace("\n", " ").strip()
        if len(preview) > 60:
            preview = preview[:60].rstrip() + "…"
        dur_sec = 0.0
        try:
            with wave.open(str(wav), "rb") as wf:
                fr = wf.getframerate() or 16000
                dur_sec = wf.getnframes() / fr
        except Exception:
            pass
        # Sidecar meta: elapsed_sec (время транскрипции) + ratio_x (× realtime)
        meta = {}
        meta_path = wav.parent / f"{wav.stem}.meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        entries.append({
            "ts": wav.stem,
            "duration_sec": dur_sec,
            "text_preview": preview,
            "text_full": text_full,
            "wav_path": wav,
            "txt_path": txt,
            "meta": meta,
        })
    return entries


CALL_CARDS_MAX = 12  # сколько последних созвонов показывать карточками (Calls\ не ротируется)


def _parse_call_md(md_path: Path) -> "dict | None":
    """Распарсить .md созвона (frontmatter + тело реплик) в entry для карточки (T-173 A).

    Превью — первые ~4 реплики: транскрипт созвона большой, полный текст открывается
    по кнопке «Открыть». None — если файл нечитаем."""
    try:
        raw = md_path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm: dict[str, str] = {}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            for line in parts[1].splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    fm[k.strip()] = v.strip()
            body = parts[2]
    # Реплики — строки со speaker-тегом '**[' (см. merge_segments_to_markdown).
    reply_lines = [ln.strip() for ln in body.splitlines() if "**[" in ln]
    preview = "\n".join(reply_lines[:4])
    if len(preview) > 280:
        preview = preview[:280].rstrip() + "…"
    try:
        mtime = md_path.stat().st_mtime
    except OSError:
        mtime = 0.0
    try:
        dur_sec = int(float(fm.get("duration_sec", "0")))
    except ValueError:
        dur_sec = 0
    return {
        "kind": "call",
        "stem": md_path.stem,                         # "YYYY-MM-DD HH-MM-SS"
        "duration_human": fm.get("duration_human", "").strip('"'),
        "duration_sec": dur_sec,
        "channels": fm.get("channels", ""),
        "reply_count": len(reply_lines),
        "preview": preview if preview.strip() else "(реплики не распознаны)",
        "md_path": md_path,
        "mtime": mtime,
    }


def read_call_entries(calls_dir: Path, count: int = CALL_CARDS_MAX) -> list[dict]:
    """N последних транскриптов созвонов из ``Calls\\`` (.md), новые сверху (T-173 A).

    Папка ``Calls\\`` вне ротации надиктовок (T-174) — может копиться; показываем
    последние ``count``, остальные доступны через «Открыть папку»."""
    if not calls_dir.exists():
        return []
    mds = sorted(calls_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:count]
    out: list[dict] = []
    for md in mds:
        e = _parse_call_md(md)
        if e is not None:
            out.append(e)
    return out


def _percentile(data: list[float], p: float) -> float:
    """Linear-interpolation percentile (numpy-compatible). 0.0 для пустого."""
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (k - f) * (s[c] - s[f])


def read_stats_jsonl(stats_path: Path, last_n: int = 100) -> tuple[list[dict], dict | None]:
    """Прочитать `_stats.jsonl`: вернуть (последние N валидных записей, самая первая запись).

    Использует deque(maxlen=N) — O(1) хранение хвоста при стрим-чтении.
    Битые строки пропускаются. Самая первая запись — для показа «глубины» истории
    (когда начали вести данные), считается отдельно от хвоста.
    """
    from collections import deque
    if not stats_path.exists():
        return [], None
    oldest: dict | None = None
    recent: deque = deque(maxlen=last_n)
    try:
        with open(stats_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if oldest is None:
                    oldest = rec
                recent.append(rec)
    except OSError:
        return [], None
    return list(recent), oldest


def compute_stats_aggregates(records: list[dict], oldest: dict | None) -> dict:
    """Из последних N записей `_stats.jsonl` — агрегаты для StatsDialog."""
    ratios = [float(r.get("ratio_x", 0)) for r in records if "ratio_x" in r]
    durations = [float(r.get("duration_sec", 0)) for r in records if "duration_sec" in r]
    devices: dict[str, int] = {}
    for r in records:
        dev = str(r.get("device", "unknown"))
        devices[dev] = devices.get(dev, 0) + 1
    return {
        "count": len(records),
        "avg_ratio": (sum(ratios) / len(ratios)) if ratios else 0.0,
        "p50_ratio": _percentile(ratios, 50),
        "p95_ratio": _percentile(ratios, 95),
        "sum_duration_sec": sum(durations),
        "devices": devices,
        "oldest_ts": (oldest or {}).get("ts", ""),
    }


# === Helpers для графиков StatsDialog (tab «Тренд» и tab «По бакетам») ===

# T-133: фильтр по режиму транскрипции (full/streaming) для StatsDialog.
# Старые записи (до T-133, без поля mode) интерпретируются как `full` —
# тогда streaming не существовал, фильтр работает обратно совместимо со всем
# накопленным журналом.
MODE_FILTERS: list[tuple[str, str]] = [
    ("all", "Все режимы"),
    ("full", "Только full"),
    ("streaming", "Только streaming"),
]

# T-133: цвета для графиков StatsDialog. Привязка по режиму, не по фильтру —
# чтобы переключение «Только streaming» сразу окрашивало график в фирменный
# оранжевый, а «Только full» — в синий. На «Все режимы» рядом стоят оба цвета
# (две линии в тренде, два столбика в бакетах) для прямого визуального сравнения.
COLOR_FULL = "#2563EB"       # синий — base mode (или legacy записи до T-133)
COLOR_STREAMING = "#F97316"  # оранжевый — контраст к синему, не сливается
COLOR_RAW = "#B4B4B8"        # серый — для сырого ratio_x при одиночном фильтре


def _filter_by_mode(records: list[dict], mode_filter: str) -> list[dict]:
    if mode_filter == "all":
        return records
    if mode_filter == "streaming":
        return [r for r in records if r.get("mode") == "streaming"]
    return [r for r in records if r.get("mode", "full") == "full"]


def _split_by_mode(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Разделить записи на (full, streaming). Legacy (без поля mode) → full."""
    full = [r for r in records if r.get("mode", "full") == "full"]
    streaming = [r for r in records if r.get("mode") == "streaming"]
    return full, streaming


# Границы бакетов длительности. Под T-128: ожидаем что streaming проиграет на
# `<10 сек` (overhead) и выиграет на `60+ сек` (амортизация).
DURATION_BUCKETS: list[tuple[str, "callable"]] = [
    ("<10 сек", lambda d: d < 10),
    ("10-30 сек", lambda d: 10 <= d < 30),
    ("30-60 сек", lambda d: 30 <= d < 60),
    ("60+ сек", lambda d: d >= 60),
]


def _compute_rolling(values: list[float], window: int) -> list[float]:
    """Rolling mean длиной `window`. Возвращает len(values) значений — в начале
    окно partial (i+1 точек), что сглаживает старт без NaN-ов.
    """
    if not values:
        return []
    out = []
    for i in range(len(values)):
        lo = max(0, i - window + 1)
        chunk = values[lo:i + 1]
        out.append(sum(chunk) / len(chunk))
    return out


def _bucket_stats(records: list[dict]) -> list[tuple[str, float, int]]:
    """Группировка записей по `DURATION_BUCKETS`. Возвращает (name, avg_ratio, count)."""
    out = []
    for name, pred in DURATION_BUCKETS:
        rs = [
            float(r.get("ratio_x", 0))
            for r in records
            if pred(float(r.get("duration_sec", 0)))
        ]
        avg = sum(rs) / len(rs) if rs else 0.0
        out.append((name, avg, len(rs)))
    return out


def _build_trend_chart(records: list[dict], mode_filter: str = "all") -> QChart:
    """Line chart с цветовым разделением по режиму (T-133).

    - `mode_filter == "all"`: две rolling-линии (full синий + streaming оранжевый),
      X — индекс записи внутри своего режима (1..N_mode). Raw-линии нет, иначе
      4 линии забивают график.
    - `mode_filter == "full" | "streaming"`: одна сырая (серая) + одна rolling
      (цвет mode-а). Как было до T-133, но цвет соответствует выбранному режиму.

    При <2 записях в нужном ряду — title-placeholder вместо линий.
    """
    chart = QChart()
    chart.setBackgroundBrush(QColor("#FFFFFF"))
    chart.setBackgroundRoundness(0)
    chart.setMargins(QMargins(8, 8, 8, 8))
    chart.legend().setAlignment(Qt.AlignBottom)

    if mode_filter == "all":
        # === Split mode: две rolling-линии для прямого сравнения ===
        full, streaming = _split_by_mode(records)
        all_y: list[float] = []
        max_n = 0
        for grp, color, name in (
            (full, COLOR_FULL, "full"),
            (streaming, COLOR_STREAMING, "streaming"),
        ):
            if len(grp) < 2:
                continue
            ratios = [float(r.get("ratio_x", 0)) for r in grp]
            rolled = _compute_rolling(ratios, 5)
            line = QLineSeries()
            line.setName(f"rolling N=5 ({name})")
            pen = QPen(QColor(color))
            pen.setWidth(3)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            line.setPen(pen)
            for i, v in enumerate(rolled):
                line.append(i + 1, v)
            chart.addSeries(line)
            all_y.extend(rolled)
            max_n = max(max_n, len(rolled))

        if not all_y:
            chart.setTitle("Недостаточно данных для тренда (нужно ≥2 записей в режиме)")
            return chart

        ax = QValueAxis()
        ax.setRange(1, max(2, max_n))
        ax.setLabelFormat("%d")
        ax.setTitleText("номер записи внутри режима")
        ax.setGridLineColor(QColor("#E4E4E7"))

        ay = QValueAxis()
        ay.setRange(0, max(all_y) * 1.15)
        ay.setLabelFormat("%.1f×")
        ay.setTitleText("ratio_x  (× realtime)")
        ay.setGridLineColor(QColor("#E4E4E7"))

        chart.addAxis(ax, Qt.AlignBottom)
        chart.addAxis(ay, Qt.AlignLeft)
        for s in chart.series():
            s.attachAxis(ax)
            s.attachAxis(ay)
        return chart

    # === Single-mode: raw (серая) + rolling (цвет режима) ===
    color = COLOR_STREAMING if mode_filter == "streaming" else COLOR_FULL
    ratios = [float(r.get("ratio_x", 0)) for r in records]
    n = len(ratios)

    if n < 2:
        chart.setTitle(f"Недостаточно данных для тренда ({mode_filter})")
        return chart

    raw = QLineSeries()
    raw.setName(f"сырой ratio_x ({mode_filter})")
    raw.setColor(QColor(COLOR_RAW))
    for i, v in enumerate(ratios):
        raw.append(i + 1, v)

    rolled = _compute_rolling(ratios, 5)
    rolling = QLineSeries()
    rolling.setName(f"rolling N=5 ({mode_filter})")
    pen = QPen(QColor(color))
    pen.setWidth(3)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    rolling.setPen(pen)
    for i, v in enumerate(rolled):
        rolling.append(i + 1, v)

    chart.addSeries(raw)
    chart.addSeries(rolling)

    ax = QValueAxis()
    ax.setRange(1, n)
    ax.setLabelFormat("%d")
    ax.setTitleText("номер записи")
    ax.setGridLineColor(QColor("#E4E4E7"))

    y_max = max(ratios) * 1.15 if ratios else 1.0
    ay = QValueAxis()
    ay.setRange(0, y_max)
    ay.setLabelFormat("%.1f×")
    ay.setTitleText("ratio_x  (× realtime)")
    ay.setGridLineColor(QColor("#E4E4E7"))

    chart.addAxis(ax, Qt.AlignBottom)
    chart.addAxis(ay, Qt.AlignLeft)
    raw.attachAxis(ax)
    raw.attachAxis(ay)
    rolling.attachAxis(ax)
    rolling.attachAxis(ay)
    return chart


def _build_buckets_chart(records: list[dict], mode_filter: str = "all") -> QChart:
    """Bar chart с цветовым разделением по режиму (T-133).

    - `mode_filter == "all"`: два QBarSet рядом в каждом бакете — full (синий) и
      streaming (оранжевый). Легенда снизу. Подпись бакета — оба count'а.
    - `mode_filter == "full" | "streaming"`: один QBarSet в фирменном цвете режима.

    Пустые бакеты остаются с нулевой высотой — «N=0» читается как сигнал
    «надиктовываю только длинные» (или наоборот).
    """
    chart = QChart()
    chart.setBackgroundBrush(QColor("#FFFFFF"))
    chart.setBackgroundRoundness(0)
    chart.setMargins(QMargins(8, 8, 8, 8))

    if mode_filter == "all":
        chart.legend().setVisible(True)
        chart.legend().setAlignment(Qt.AlignBottom)

        full, streaming = _split_by_mode(records)
        full_buckets = _bucket_stats(full)
        streaming_buckets = _bucket_stats(streaming)

        bar_full = QBarSet("full")
        bar_full.setColor(QColor(COLOR_FULL))
        bar_full.setBorderColor(QColor(COLOR_FULL))
        bar_streaming = QBarSet("streaming")
        bar_streaming.setColor(QColor(COLOR_STREAMING))
        bar_streaming.setBorderColor(QColor(COLOR_STREAMING))

        categories: list[str] = []
        all_max: list[float] = []
        for (name_f, avg_f, cnt_f), (_, avg_s, cnt_s) in zip(full_buckets, streaming_buckets):
            bar_full.append(avg_f)
            bar_streaming.append(avg_s)
            categories.append(f"{name_f}\nfull n={cnt_f} · stream n={cnt_s}")
            all_max.extend([avg_f, avg_s])

        series = QBarSeries()
        series.append(bar_full)
        series.append(bar_streaming)
        series.setLabelsVisible(True)
        series.setLabelsFormat("@value")
        series.setLabelsPosition(QBarSeries.LabelsOutsideEnd)
        chart.addSeries(series)

        ax = QBarCategoryAxis()
        ax.append(categories)

        max_avg = max(all_max, default=1.0)
        ay = QValueAxis()
        ay.setRange(0, max_avg * 1.25 if max_avg > 0 else 1.0)
        ay.setLabelFormat("%.1f×")
        ay.setTitleText("средний ratio_x")
        ay.setGridLineColor(QColor("#E4E4E7"))

        chart.addAxis(ax, Qt.AlignBottom)
        chart.addAxis(ay, Qt.AlignLeft)
        series.attachAxis(ax)
        series.attachAxis(ay)
        return chart

    chart.legend().setVisible(False)
    color = COLOR_STREAMING if mode_filter == "streaming" else COLOR_FULL
    bucket_data = _bucket_stats(records)

    bar_set = QBarSet(mode_filter)
    bar_set.setColor(QColor(color))
    bar_set.setBorderColor(QColor(color))
    categories = []
    for name, avg, count in bucket_data:
        bar_set.append(avg)
        categories.append(f"{name}\n(n={count})")

    series = QBarSeries()
    series.append(bar_set)
    series.setLabelsVisible(True)
    series.setLabelsFormat("@value")
    series.setLabelsPosition(QBarSeries.LabelsOutsideEnd)
    chart.addSeries(series)

    ax = QBarCategoryAxis()
    ax.append(categories)

    max_avg = max((v[1] for v in bucket_data), default=1.0)
    ay = QValueAxis()
    ay.setRange(0, max_avg * 1.2 if max_avg > 0 else 1.0)
    ay.setLabelFormat("%.1f×")
    ay.setTitleText("средний ratio_x")
    ay.setGridLineColor(QColor("#E4E4E7"))

    chart.addAxis(ax, Qt.AlignBottom)
    chart.addAxis(ay, Qt.AlignLeft)
    series.attachAxis(ax)
    series.attachAxis(ay)
    return chart


def parse_hotkey_valid(hotkey: str) -> tuple[bool, str]:
    """Проверка hotkey через pynput.HotKey.parse. Возвращает (ok, msg)."""
    try:
        from pynput import keyboard as pnk
        pnk.HotKey.parse(hotkey)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def hotkey_conflicts_with_handy(hotkey: str) -> bool:
    """Проверка пересечения с Handy (`ctrl_left+\\``) или с базовыми ОС-шорткатами."""
    h = hotkey.replace(" ", "").lower()
    if "`" in h or "\\" in h:
        return True
    reserved_pnp = {"<alt>+<tab>", "<ctrl>+<alt>+delete", "<cmd>+l", "<cmd>+d", "<cmd>+e", "<alt>+<f4>"}
    return h in reserved_pnp


class _EqualizerBars(QWidget):
    """4 bar-эквалайзер по `.eq` из iwhisper.css: width 22, gap 2, bar 3px, radius 1."""

    def __init__(self, parent=None, color="#FFFFFF", height=16):
        super().__init__(parent)
        self._color = color
        self.setFixedSize(22, height)
        self._levels = [0.0] * 4

    def update_level(self, level: float) -> None:
        self._levels = self._levels[1:] + [level]
        self.update()

    def reset(self) -> None:
        self._levels = [0.0] * 4
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self._color))
        w, h = self.width(), self.height()
        bar_w = 3
        gap = 2
        total = 4 * bar_w + 3 * gap  # 4*3 + 3*2 = 18, в 22px остаётся 4 для центрирования
        start_x = (w - total) // 2
        for i, lvl in enumerate(self._levels):
            amplified = min(1.0, max(0.15, lvl * 5.0))
            bh = int(amplified * (h - 2))
            y = (h - bh) // 2
            x = start_x + i * (bar_w + gap)
            p.drawRoundedRect(x, y, bar_w, bh, 1, 1)


class StatusOverlay(QWidget):
    """Toast-pill индикатор внизу экрана (Claude Design `.toast` стиль).
    height 40, bg #1F1F22, border-radius 20 (pill), white text.
    recording: pulse dot + label + mono time + dashed sep + эквалайзер.
    processing: amber spinner + label.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._frame = QWidget(self)
        self._frame.setObjectName("toastFrame")
        outer.addWidget(self._frame)

        row = QHBoxLayout(self._frame)
        row.setContentsMargins(12, 0, 14, 0)
        row.setSpacing(10)

        self._dot = QLabel("●")
        self._dot.setStyleSheet(
            "color: #DC2626; font-size: 13px; font-weight: bold; background: transparent;"
        )
        self._dot.setFixedWidth(11)

        self._spinner = QLabel("")
        self._spinner.setFixedSize(16, 16)
        self._spinner.setStyleSheet("background: transparent;")
        self._spinner_angle = 0
        from PySide6.QtCore import QTimer
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(60)
        self._spinner_timer.timeout.connect(self._rotate_spinner)

        self._label = QLabel("Запись")
        self._label.setStyleSheet(
            "color: #FFFFFF; font-size: 13px; font-weight: 500; background: transparent;"
            " font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif;"
        )

        self._time = QLabel("00:00")
        self._time.setStyleSheet(
            "color: rgba(255,255,255,0.65); font-size: 12px; background: transparent;"
            " font-family: 'Cascadia Mono','Cascadia Code','Consolas',monospace;"
            " letter-spacing: 0.5px;"
        )

        # По CSS `.toast__sep { width: 22px; height: 1px; border-top: 1px dashed #5B5B62; }`
        # — это hairline-полоска. Реализую через QFrame высотой 1px.
        self._sep = QFrame()
        self._sep.setFixedSize(22, 1)
        self._sep.setStyleSheet(
            "background: transparent; border: none; border-top: 1px dashed #5B5B62;"
        )

        self._bars_widget = _EqualizerBars(self, color="#FFFFFF", height=18)

        # Зелёная галочка для toast «Скопировано в буфер» (state="saved")
        self._check = QLabel("")
        self._check.setFixedSize(16, 16)
        self._check.setStyleSheet("background: transparent;")
        _check_pix = QPixmap(16, 16)
        _check_pix.fill(Qt.transparent)
        _check_painter = QPainter(_check_pix)
        _check_painter.setRenderHint(QPainter.Antialiasing)
        _check_pen = QPen(QColor("#34D399"))
        _check_pen.setWidthF(2.0)
        _check_pen.setCapStyle(Qt.RoundCap)
        _check_pen.setJoinStyle(Qt.RoundJoin)
        _check_painter.setPen(_check_pen)
        _check_painter.drawPolyline(QPolygon([QPoint(3, 8), QPoint(7, 12), QPoint(13, 5)]))
        _check_painter.end()
        self._check.setPixmap(_check_pix)

        row.addWidget(self._dot)
        row.addWidget(self._spinner)
        row.addWidget(self._check)
        row.addWidget(self._label)
        row.addWidget(self._time)
        row.addWidget(self._sep)
        row.addWidget(self._bars_widget)

        # По CSS `.toast { background: #1F1F22; border: 1px solid #2F2F35; border-radius: 999px; }`
        # Высота 40 → radius 20 = pill. Цвета 100% opacity.
        self._frame.setStyleSheet(
            "#toastFrame {"
            " background-color: #1F1F22;"
            " border: 1px solid #2F2F35;"
            " border-radius: 20px;"
            "}"
        )
        self.setFixedHeight(40)
        self.setMinimumWidth(240)

        self._dot_anim_timer = QTimer(self)
        self._dot_anim_timer.setInterval(700)
        self._dot_visible = True
        self._dot_anim_timer.timeout.connect(self._toggle_dot)

    def _toggle_dot(self) -> None:
        self._dot_visible = not self._dot_visible
        opacity = 1.0 if self._dot_visible else 0.35
        self._dot.setStyleSheet(
            f"color: rgba(220, 38, 38, {opacity});"
            f" font-size: 13px; font-weight: bold; background: transparent;"
        )

    def _rotate_spinner(self) -> None:
        self._spinner_angle = (self._spinner_angle + 24) % 360
        pix = QPixmap(16, 16)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor("#F59E0B"))
        pen.setWidthF(1.8)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.translate(8, 8)
        p.rotate(self._spinner_angle)
        p.translate(-8, -8)
        p.drawArc(2, 2, 12, 12, 0, 270 * 16)
        p.end()
        self._spinner.setPixmap(pix)

    def show_state(self, state: str) -> None:
        if state == "recording":
            self._dot.show()
            self._spinner.hide()
            self._check.hide()
            self._spinner_timer.stop()
            self._dot_anim_timer.start()
            self._label.setText("Запись")
            self._time.show()
            self._sep.show()
            self._bars_widget.show()
            self._bars_widget.reset()
            self._position_bottom()
            self.show()
            self.raise_()
        elif state == "processing":
            self._dot.hide()
            self._check.hide()
            self._dot_anim_timer.stop()
            self._spinner.show()
            self._spinner_timer.start()
            self._rotate_spinner()
            self._label.setText("Транскрибирую…")
            self._time.hide()
            self._sep.hide()
            self._bars_widget.hide()
            self._position_bottom()
            self.show()
            self.raise_()
        elif state == "saved":
            self._dot.hide()
            self._dot_anim_timer.stop()
            self._spinner.hide()
            self._spinner_timer.stop()
            self._bars_widget.hide()
            self._time.hide()
            self._sep.hide()
            self._check.show()
            self._label.setText("Скопировано в буфер")
            self._position_bottom()
            self.show()
            self.raise_()
            # auto-hide через 2.5 сек (как `.toast` ToastSaved в макете)
            from PySide6.QtCore import QTimer as _QT
            _QT.singleShot(2500, self.hide)
        else:
            self._dot_anim_timer.stop()
            self._spinner_timer.stop()
            self._bars_widget.reset()
            self.hide()

    def update_audio_level(self, level: float) -> None:
        if self._bars_widget.isVisible():
            self._bars_widget.update_level(level)

    def update_time(self, seconds: int) -> None:
        mm, ss = divmod(max(0, int(seconds)), 60)
        self._time.setText(f"{mm:02d}:{ss:02d}")

    def _position_bottom(self) -> None:
        """Правый нижний угол над таскбаром (Win11 toast convention).
        availableGeometry() уже исключает таскбар, поэтому отступ 16px от правого края."""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.adjustSize()
        w = max(240, self.width())
        x = geo.x() + geo.width() - w - 16
        y = geo.y() + geo.height() - self.height() - 16
        self.move(x, y)


class _CallStatusOverlay(QWidget):
    """Индикатор записи/обработки СОЗВОНА (T-173 B+C). Синий акцент #2563EB —
    отличается от красного индикатора диктовки. Отдельный от StatusOverlay инстанс
    и позиция (верх-центр), чтобы во время созвона диктовка (ctrl+shift+0) показывала
    свой toast внизу-справа, не конфликтуя за один виджет.

    recording:  синяя пульс-точка + «Запись созвона» + таймер записи + эквалайзер.
    processing: синий spinner + «Обработка созвона…» + таймер обработки + грубый %.
    """

    ACCENT = "#2563EB"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        from PySide6.QtCore import QTimer
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._frame = QWidget(self)
        self._frame.setObjectName("callToastFrame")
        outer.addWidget(self._frame)

        row = QHBoxLayout(self._frame)
        row.setContentsMargins(14, 0, 16, 0)
        row.setSpacing(10)

        self._dot = QLabel("●")
        self._dot.setStyleSheet(
            f"color: {self.ACCENT}; font-size: 13px; font-weight: bold; background: transparent;"
        )
        self._dot.setFixedWidth(11)

        self._spinner = QLabel("")
        self._spinner.setFixedSize(16, 16)
        self._spinner.setStyleSheet("background: transparent;")
        self._spinner_angle = 0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(60)
        self._spinner_timer.timeout.connect(self._rotate_spinner)

        self._label = QLabel("Запись созвона")
        self._label.setStyleSheet(
            "color: #FFFFFF; font-size: 13px; font-weight: 500; background: transparent;"
            " font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif;"
        )

        self._time = QLabel("00:00")
        self._time.setStyleSheet(
            "color: rgba(255,255,255,0.65); font-size: 12px; background: transparent;"
            " font-family: 'Cascadia Mono','Cascadia Code','Consolas',monospace; letter-spacing: 0.5px;"
        )

        self._progress = QLabel("")
        self._progress.setStyleSheet(
            f"color: {self.ACCENT}; font-size: 12px; font-weight: 600; background: transparent;"
            " font-family: 'Cascadia Mono','Cascadia Code','Consolas',monospace;"
        )

        self._sep = QFrame()
        self._sep.setFixedSize(22, 1)
        self._sep.setStyleSheet(
            "background: transparent; border: none; border-top: 1px dashed #5B5B62;"
        )

        self._bars = _EqualizerBars(self, color=self.ACCENT, height=18)

        row.addWidget(self._dot)
        row.addWidget(self._spinner)
        row.addWidget(self._label)
        row.addWidget(self._time)
        row.addWidget(self._progress)
        row.addWidget(self._sep)
        row.addWidget(self._bars)

        self._frame.setStyleSheet(
            "#callToastFrame {"
            " background-color: #1F1F22;"
            f" border: 1px solid {self.ACCENT};"
            " border-radius: 20px;"
            "}"
        )
        self.setFixedHeight(40)
        self.setMinimumWidth(240)

        self._dot_anim = QTimer(self)
        self._dot_anim.setInterval(700)
        self._dot_on = True
        self._dot_anim.timeout.connect(self._toggle_dot)

    def _toggle_dot(self) -> None:
        self._dot_on = not self._dot_on
        op = 1.0 if self._dot_on else 0.35
        self._dot.setStyleSheet(
            f"color: rgba(37, 99, 235, {op}); font-size: 13px; font-weight: bold; background: transparent;"
        )

    def _rotate_spinner(self) -> None:
        self._spinner_angle = (self._spinner_angle + 24) % 360
        pix = QPixmap(16, 16)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(self.ACCENT))
        pen.setWidthF(1.8)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.translate(8, 8)
        p.rotate(self._spinner_angle)
        p.translate(-8, -8)
        p.drawArc(2, 2, 12, 12, 0, 270 * 16)
        p.end()
        self._spinner.setPixmap(pix)

    def show_recording(self) -> None:
        self._dot.show()
        self._spinner.hide()
        self._spinner_timer.stop()
        self._dot_anim.start()
        self._label.setText("Запись созвона")
        self._time.setText("00:00")
        self._time.show()
        self._progress.hide()
        # Эквалайзер не показываем: уровень аудио созвона в UI не прокидывается
        # (T-172 не реализовал call level meter — опц. в acceptance). Замерший эквалайзер
        # выглядел бы зависшим; живость даёт пульс-точка + тикающий таймер.
        self._sep.hide()
        self._bars.hide()
        self._position_top()
        self.show()
        self.raise_()

    def show_processing(self) -> None:
        self._dot.hide()
        self._dot_anim.stop()
        self._spinner.show()
        self._spinner_timer.start()
        self._rotate_spinner()
        self._label.setText("Обработка созвона…")
        self._time.setText("00:00")
        self._time.show()
        self._progress.setText("")
        self._progress.show()
        self._sep.hide()
        self._bars.hide()
        self._position_top()
        self.show()
        self.raise_()

    def hide_overlay(self) -> None:
        self._dot_anim.stop()
        self._spinner_timer.stop()
        self._bars.reset()
        self.hide()

    def update_time(self, seconds: int) -> None:
        mm, ss = divmod(max(0, int(seconds)), 60)
        self._time.setText(f"{mm:02d}:{ss:02d}")

    def update_progress(self, frac: float) -> None:
        pct = max(0, min(100, int(round(frac * 100))))
        self._progress.setText(f"· {pct}%")
        self._position_top()

    def update_audio_level(self, level: float) -> None:
        if self._bars.isVisible():
            self._bars.update_level(level)

    def _position_top(self) -> None:
        """Верх-центр экрана — отдельно от toast диктовки (низ-право)."""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.adjustSize()
        w = max(240, self.width())
        x = geo.x() + (geo.width() - w) // 2
        y = geo.y() + 16
        self.move(x, y)


class _SpinnerLabel(QLabel):
    """Маленький spinner для transcribing state в кнопке. 16x16, amber."""

    def __init__(self, parent=None, color="#92400E", size=16):
        super().__init__(parent)
        self._color = color
        self._size = size
        self._angle = 0
        self.setFixedSize(size, size)
        self.setStyleSheet("background: transparent;")
        from PySide6.QtCore import QTimer
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._tick)

    def start(self):
        self._timer.start()
        self._tick()

    def stop(self):
        self._timer.stop()
        self.clear()

    def _tick(self):
        self._angle = (self._angle + 24) % 360
        pix = QPixmap(self._size, self._size)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(self._color))
        pen.setWidthF(1.8)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.translate(self._size / 2, self._size / 2)
        p.rotate(self._angle)
        p.translate(-self._size / 2, -self._size / 2)
        p.drawArc(2, 2, self._size - 4, self._size - 4, 0, 270 * 16)
        p.end()
        self.setPixmap(pix)


class _PulseDot(QLabel):
    """Пульсирующая красная точка (0.7s on/off cycle)."""

    def __init__(self, parent=None, color="#DC2626", size=8):
        super().__init__(parent)
        self._color = color
        self.setFixedSize(size + 2, size + 2)
        self._dot_size = size
        self._opacity = 1.0
        self._on = True
        from PySide6.QtCore import QTimer
        self._timer = QTimer(self)
        self._timer.setInterval(700)
        self._timer.timeout.connect(self._toggle)
        self.setStyleSheet("background: transparent;")

    def start(self):
        self._on = True
        self._opacity = 1.0
        self._timer.start()
        self.update()

    def stop(self):
        self._timer.stop()
        self._opacity = 1.0
        self.update()

    def _toggle(self):
        self._on = not self._on
        self._opacity = 1.0 if self._on else 0.35
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(self._color)
        c.setAlphaF(self._opacity)
        p.setBrush(c)
        p.setPen(Qt.NoPen)
        margin = (self.width() - self._dot_size) // 2
        p.drawEllipse(margin, margin, self._dot_size, self._dot_size)


class _PrimaryActionButton(QFrame):
    """Composite кнопка с idle/recording/transcribing state внутри.
    Размер не меняется → layout не прыгает.

    idle:        [mic] Записать
    recording:   [•] Запись           00:14 [eq]
    transcribing:[spinner] Транскрибирую…       02.4s
    """

    def __init__(self, on_click, parent=None):
        super().__init__(parent)
        self._on_click = on_click
        self._state = "idle"
        self.setFixedHeight(44)
        self.setCursor(Qt.PointingHandCursor)
        self._build()
        self.set_state("idle")

    def _build(self):
        self.setObjectName("primaryBtn")
        # Внутри HBox: label-зона (icon+text) слева; meta-зона (time+eq) справа
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(10)

        # left side: icon + label
        self._icon_label = QLabel()
        self._icon_label.setFixedSize(18, 18)
        self._icon_label.setStyleSheet("background: transparent;")

        self._pulse_dot = _PulseDot(self, color="#DC2626", size=8)
        self._pulse_dot.hide()

        self._spinner = _SpinnerLabel(self, color="#92400E", size=16)
        self._spinner.hide()

        self._label = QLabel("Записать")
        self._label.setStyleSheet(
            "color: #18181B; font-size: 14px; font-weight: 500; background: transparent;"
            " font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif;"
        )

        lay.addWidget(self._icon_label)
        lay.addWidget(self._pulse_dot)
        lay.addWidget(self._spinner)
        lay.addWidget(self._label)
        lay.addStretch()

        # right side: meta (time + equalizer)
        self._time = QLabel("")
        self._time.setStyleSheet(
            "color: #DC2626; font-size: 12px; background: transparent;"
            " font-family: 'Cascadia Mono','Cascadia Code','Consolas',monospace;"
            " letter-spacing: 0.5px;"
        )
        self._eq = _EqualizerBars(self, color="#DC2626", height=14)
        self._eq.hide()
        lay.addWidget(self._time)
        lay.addWidget(self._eq)

    def mousePressEvent(self, ev):
        if self._state == "transcribing":
            return  # disabled во время транскрипции
        if self._on_click:
            self._on_click()

    def set_state(self, state: str):
        from PySide6.QtGui import QPixmap as _Pix
        # transcribe_ui.py использует 'processing' — нормализуем
        if state == "processing":
            state = "transcribing"
        self._state = state
        if state == "recording":
            # styling: white bg, red border, red label
            self.setStyleSheet(
                "#primaryBtn {"
                " background-color: #FFFFFF;"
                " border: 1px solid #DC2626;"
                " border-radius: 22px;"
                "}"
                "#primaryBtn:hover { background-color: #FEF2F2; }"
            )
            self._icon_label.hide()
            self._pulse_dot.show()
            self._pulse_dot.start()
            self._spinner.hide()
            self._spinner.stop()
            self._label.setText("Запись")
            self._label.setStyleSheet(
                "color: #DC2626; font-size: 14px; font-weight: 500; background: transparent;"
                " font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif;"
            )
            self._time.setText("00:00")
            self._time.show()
            self._eq.show()
            self._eq.reset()
            self.setCursor(Qt.PointingHandCursor)
        elif state == "transcribing":
            self.setStyleSheet(
                "#primaryBtn {"
                " background-color: #FFFBEB;"
                " border: 1px solid #D97706;"
                " border-radius: 22px;"
                "}"
            )
            self._icon_label.hide()
            self._pulse_dot.hide()
            self._pulse_dot.stop()
            self._spinner.show()
            self._spinner.start()
            self._label.setText("Транскрибирую…")
            self._label.setStyleSheet(
                "color: #92400E; font-size: 14px; font-weight: 500; background: transparent;"
                " font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif;"
            )
            self._time.setText("")
            self._time.hide()
            self._eq.hide()
            self.setCursor(Qt.ArrowCursor)
        else:  # idle
            self.setStyleSheet(
                "#primaryBtn {"
                " background-color: #FFFFFF;"
                " border: 1px solid #E7E7EA;"
                " border-radius: 22px;"
                "}"
                "#primaryBtn:hover { background-color: #F4F4F5; border-color: #D4D4D8; }"
            )
            # mic icon в idle (рисуем 18px темный)
            mic_pix = QPixmap(18, 18)
            mic_pix.fill(Qt.transparent)
            p = QPainter(mic_pix)
            p.setRenderHint(QPainter.Antialiasing)
            _draw_mic_icon(p, QRect(0, 0, 18, 18), QColor("#18181B"))
            p.end()
            self._icon_label.setPixmap(mic_pix)
            self._icon_label.show()
            self._pulse_dot.hide()
            self._pulse_dot.stop()
            self._spinner.hide()
            self._spinner.stop()
            self._label.setText("Записать")
            self._label.setStyleSheet(
                "color: #18181B; font-size: 14px; font-weight: 500; background: transparent;"
                " font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif;"
            )
            self._time.setText("")
            self._time.hide()
            self._eq.hide()
            self.setCursor(Qt.PointingHandCursor)

    def update_audio_level(self, level: float) -> None:
        if self._state == "recording":
            self._eq.update_level(level)

    def update_time(self, seconds: int) -> None:
        if self._state == "recording":
            mm, ss = divmod(max(0, int(seconds)), 60)
            self._time.setText(f"{mm:02d}:{ss:02d}")


def _build_empty_widget() -> QWidget:
    """Empty state по `.empty` из iwhisper.css: padding 32, gap 12, mic 56×56 в круге."""
    w = QWidget()
    w.setStyleSheet("background: transparent;")
    lay = QVBoxLayout(w)
    lay.setContentsMargins(32, 32, 32, 32)
    lay.setSpacing(12)
    lay.setAlignment(Qt.AlignCenter)

    # mic in circle
    icon_lbl = QLabel()
    icon_lbl.setFixedSize(56, 56)
    pix = QPixmap(56, 56)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    # круг
    p.setPen(QPen(QColor("#E7E7EA"), 1))
    p.setBrush(QColor("#FFFFFF"))
    p.drawEllipse(0, 0, 55, 55)
    # mic внутри
    _draw_mic_icon(p, QRect(16, 16, 24, 24), QColor("#A1A1AA"))
    p.end()
    icon_lbl.setPixmap(pix)
    icon_lbl.setAlignment(Qt.AlignCenter)
    lay.addWidget(icon_lbl, alignment=Qt.AlignCenter)

    title = QLabel("Здесь пока пусто")
    title.setStyleSheet(
        "color: #18181B; font-size: 14px; font-weight: 600; background: transparent;"
    )
    title.setAlignment(Qt.AlignCenter)
    lay.addWidget(title)

    sub = QLabel("Нажми «Записать», скажи фразу — и через секунду она появится здесь как текст.")
    sub.setWordWrap(True)
    sub.setMaximumWidth(320)
    sub.setStyleSheet(
        "color: #52525B; font-size: 13px; background: transparent; line-height: 1.5;"
    )
    sub.setAlignment(Qt.AlignCenter)
    lay.addWidget(sub, alignment=Qt.AlignCenter)

    return w


class HistoryCard(QFrame):
    """Карточка записи (Claude Design layout v2).

    Top row: дата (bold) + время (mono grey) + spacer + мета + кнопки copy/play.
    Body: полный текст (text-2 цвет).
    """

    play_requested = Signal(int)
    copy_requested = Signal(int)

    def __init__(self, row: int, entry: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._row = row
        self._is_active = False
        self.setObjectName("HistoryCard")
        # По CSS:
        # .item { bg #FFFFFF; border 1px #E7E7EA; radius 8px; }
        # .item:hover { border-color #D4D4D8; bg #FBFBFC; }
        # .item.is-active { border-color #18181B; }
        self.setStyleSheet(
            "#HistoryCard {"
            " background-color: #FFFFFF;"
            " border: 1px solid #E7E7EA;"
            " border-radius: 8px;"
            "}"
            "#HistoryCard:hover {"
            " background-color: #FBFBFC;"
            " border-color: #D4D4D8;"
            "}"
            "#HistoryCard[active=\"true\"] { border-color: #18181B; }"
            "#HistoryCard[active=\"true\"]:hover { border-color: #18181B; }"
        )
        self.setFrameShape(QFrame.NoFrame)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)

        # === Top row: date | time | ...spacer... | meta | actions ===
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(12)

        date_str, time_str = self._format_ts_parts(entry["ts"])
        date_lbl = QLabel(date_str)
        date_lbl.setStyleSheet(
            "color: #18181B; font-size: 13px; font-weight: 600; background: transparent;"
        )
        time_lbl = QLabel(time_str)
        time_lbl.setStyleSheet(
            "color: #A1A1AA; font-size: 12px; background: transparent;"
            " font-family: 'Cascadia Mono','Cascadia Code','Consolas',monospace;"
            " letter-spacing: 0.5px;"
        )
        date_row_w = QWidget()
        date_row_w.setStyleSheet("background: transparent;")
        date_row = QHBoxLayout(date_row_w)
        date_row.setContentsMargins(0, 0, 0, 0)
        date_row.setSpacing(8)
        date_row.addWidget(date_lbl)
        date_row.addWidget(time_lbl)
        top.addWidget(date_row_w)

        top.addStretch()

        meta = entry.get("meta") or {}
        meta_parts: list[str] = []
        dur = entry.get("duration_sec", 0)
        if dur:
            meta_parts.append(f"{dur:.1f}s запись")
        if meta.get("elapsed_sec") is not None and meta.get("ratio_x") is not None:
            meta_parts.append(f"{meta['elapsed_sec']:.1f}s обработка")
            meta_parts.append(f"{meta['ratio_x']:.1f}×")
        if meta_parts:
            meta_lbl = QLabel(" · ".join(meta_parts))
            meta_lbl.setStyleSheet(
                "color: #A1A1AA; font-size: 12px; font-weight: 400; background: transparent;"
            )
            top.addWidget(meta_lbl)

        copy_btn = QPushButton("")
        copy_btn.setObjectName("icon_btn")
        copy_btn.setToolTip("Скопировать текст")
        copy_btn.setIcon(_mk_icon(_draw_copy, 14, "#52525B"))
        copy_btn.setIconSize(QSize(14, 14))
        copy_btn.clicked.connect(lambda: self.copy_requested.emit(self._row))
        play_btn = QPushButton("")
        play_btn.setObjectName("icon_btn")
        play_btn.setToolTip("Воспроизвести")
        play_btn.setIcon(_mk_icon(_draw_play, 12, "#52525B"))
        play_btn.setIconSize(QSize(12, 12))
        play_btn.clicked.connect(lambda: self.play_requested.emit(self._row))
        top.addWidget(copy_btn)
        top.addWidget(play_btn)
        layout.addLayout(top)

        # === Body: полный текст транскрипции ===
        text = entry.get("text_full", "")
        text_lbl = QLabel(text if text.strip() else "(пусто)")
        text_lbl.setWordWrap(True)
        text_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        text_lbl.setStyleSheet(
            "color: #52525B; font-size: 13px; line-height: 1.55; background: transparent;"
            " font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif;"
        )
        layout.addWidget(text_lbl)

    @staticmethod
    def _format_ts_parts(ts: str) -> tuple[str, str]:
        try:
            dt = datetime.strptime(ts, "%Y-%m-%dT%H-%M-%S")
            return dt.strftime("%d.%m.%Y"), dt.strftime("%H:%M:%S")
        except Exception:
            return ts, ""

    def set_active(self, active: bool) -> None:
        """Подсветить карточку активной (играет) — `.item.is-active` в дизайне:
        border-color #18181B. Используется dynamic property + style().polish()
        чтобы QSS селектор [active="true"] перечитал значение."""
        self._is_active = active
        self.setProperty("active", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)


class CallHistoryCard(QFrame):
    """Карточка СОЗВОНА в истории (T-173 A). Визуально отличается от карточки
    надиктовки: синий бейдж «● Созвон» + синий левый акцент, мета (длительность /
    реплики / каналы L-R), превью первых реплик. Полный транскрипт (большой) — по
    кнопке «Открыть» или клику по карточке → немодальный _CallTranscriptDialog."""

    open_requested = Signal(int)

    def __init__(self, row: int, entry: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._row = row
        self.setObjectName("CallHistoryCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            "#CallHistoryCard {"
            " background-color: #FFFFFF;"
            " border: 1px solid #E7E7EA;"
            " border-left: 3px solid #2563EB;"
            " border-radius: 8px;"
            "}"
            "#CallHistoryCard:hover {"
            " background-color: #FBFBFC;"
            " border-color: #D4D4D8;"
            " border-left: 3px solid #2563EB;"
            "}"
        )
        self.setFrameShape(QFrame.NoFrame)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(10)

        badge = QLabel("● Созвон")
        badge.setStyleSheet(
            "color: #2563EB; font-size: 11px; font-weight: 700; background: #EFF6FF;"
            " border: 1px solid #BFDBFE; border-radius: 8px; padding: 1px 7px;"
        )
        top.addWidget(badge)

        date_str, time_str = self._format_stem(entry.get("stem", ""))
        date_lbl = QLabel(date_str)
        date_lbl.setStyleSheet(
            "color: #18181B; font-size: 13px; font-weight: 600; background: transparent;"
        )
        time_lbl = QLabel(time_str)
        time_lbl.setStyleSheet(
            "color: #A1A1AA; font-size: 12px; background: transparent;"
            " font-family: 'Cascadia Mono','Cascadia Code','Consolas',monospace; letter-spacing: 0.5px;"
        )
        top.addWidget(date_lbl)
        top.addWidget(time_lbl)
        top.addStretch()

        meta_parts: list[str] = []
        dh = entry.get("duration_human") or ""
        if dh:
            meta_parts.append(dh)
        elif entry.get("duration_sec"):
            mm, ss = divmod(int(entry["duration_sec"]), 60)
            meta_parts.append(f"{mm:02d}:{ss:02d}")
        if entry.get("reply_count"):
            meta_parts.append(f"{entry['reply_count']} реплик")
        if entry.get("channels"):
            meta_parts.append("L-R")
        if meta_parts:
            meta_lbl = QLabel(" · ".join(meta_parts))
            meta_lbl.setStyleSheet(
                "color: #A1A1AA; font-size: 12px; font-weight: 400; background: transparent;"
            )
            top.addWidget(meta_lbl)

        open_btn = QPushButton("Открыть")
        open_btn.setObjectName("call_open_btn")
        open_btn.setToolTip("Открыть полный транскрипт созвона")
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.setStyleSheet(
            "QPushButton#call_open_btn {"
            " background-color: #FFFFFF; color: #2563EB; border: 1px solid #BFDBFE;"
            " border-radius: 14px; padding: 0 14px; font-size: 12px; font-weight: 600;"
            " min-height: 28px; max-height: 28px;"
            "}"
            "QPushButton#call_open_btn:hover { background-color: #EFF6FF; border-color: #2563EB; }"
        )
        open_btn.clicked.connect(lambda: self.open_requested.emit(self._row))
        top.addWidget(open_btn)
        layout.addLayout(top)

        preview = entry.get("preview", "")
        prev_lbl = QLabel(preview)
        prev_lbl.setWordWrap(True)
        prev_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        prev_lbl.setStyleSheet(
            "color: #52525B; font-size: 12px; line-height: 1.5; background: transparent;"
            " font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif;"
        )
        layout.addWidget(prev_lbl)

    @staticmethod
    def _format_stem(stem: str) -> tuple[str, str]:
        """Имя файла созвона — «YYYY-MM-DD HH-MM-SS» (пробел, не T — формат OBS)."""
        try:
            dt = datetime.strptime(stem, "%Y-%m-%d %H-%M-%S")
            return dt.strftime("%d.%m.%Y"), dt.strftime("%H:%M:%S")
        except Exception:
            return stem, ""

    def mousePressEvent(self, ev) -> None:  # noqa: N802 — клик по карточке = «Открыть»
        self.open_requested.emit(self._row)
        super().mousePressEvent(ev)


class SettingsDialog(QDialog):
    """Настройки iWhisper по Claude Design v2.

    Структура:
    - settings-body с form rows (label-150 / input-1fr / action-auto), inputs h:34 r:8.
    - Чекбоксы 18×18 dark-fill чекмарк.
    - Footer (hairline сверху): note слева + Cancel (outline pill) + OK (черный pill).
    """

    # T-262: итог фоновой проверки обновлений приходит из рабочего потока
    update_checked = Signal(object, str)  # UpdateInfo | None, текст ошибки ("" — успех)

    def __init__(self, parent: QMainWindow | None, current: dict, model_locked: bool = False) -> None:
        super().__init__(parent)
        self.update_checked.connect(self._on_update_checked)
        self._model_locked = bool(model_locked)  # T-259: идёт запись/транскрипция
        # T-261: прежний отказ от CUDA-слоя и запрос скачивания из этого диалога
        self._cuda_declined = bool(current.get("cuda_layer_declined", False))
        self._cuda_requested = False
        self.setWindowTitle("Настройки iWhisper")
        self.setMinimumWidth(560)
        # Dialog-локальный стиль — переопределяет глобальный QPushButton (он pill для primary).
        self.setStyleSheet(
            "QDialog { background-color: #F6F6F7; }"
            " QLabel { background: transparent; color: #18181B;"
            "   font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif; }"
            " QLabel#frow_label { font-size: 13px; font-weight: 500; }"
            " QLabel#frow_hint { color: #A1A1AA; font-size: 12px; }"
            " QLabel#footer_note { color: #A1A1AA; font-size: 12px; }"
            " QLineEdit#settings_input, QKeySequenceEdit#settings_input, QSpinBox#numfield {"
            "   background: #FFFFFF; border: 1px solid #E7E7EA; border-radius: 8px;"
            "   padding: 0 12px; color: #18181B; font-size: 13px;"
            "   min-height: 32px; max-height: 34px;"
            "   font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif; }"
            " QLineEdit#settings_input_mono, QKeySequenceEdit#settings_input_mono {"
            "   background: #FFFFFF; border: 1px solid #E7E7EA; border-radius: 8px;"
            "   padding: 0 12px; color: #18181B; font-size: 12.5px;"
            "   min-height: 32px; max-height: 34px;"
            "   font-family: 'Cascadia Mono','Cascadia Code','Consolas',monospace; }"
            " QPlainTextEdit#settings_textarea {"
            "   background: #FFFFFF; border: 1px solid #E7E7EA; border-radius: 8px;"
            "   padding: 8px 10px; color: #18181B; font-size: 13px;"
            "   font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif; }"
            " QPlainTextEdit#settings_textarea:hover { border-color: #D4D4D8; }"
            " QPlainTextEdit#settings_textarea:focus { border-color: #18181B; }"
            " QLineEdit#settings_input:hover, QLineEdit#settings_input_mono:hover,"
            " QKeySequenceEdit#settings_input:hover, QKeySequenceEdit#settings_input_mono:hover,"
            " QSpinBox#numfield:hover { border-color: #D4D4D8; }"
            " QLineEdit#settings_input:focus, QLineEdit#settings_input_mono:focus,"
            " QKeySequenceEdit#settings_input:focus, QKeySequenceEdit#settings_input_mono:focus,"
            " QSpinBox#numfield:focus { border-color: #18181B; }"
            " QSpinBox#numfield { max-width: 96px; }"
            " QPushButton#btn_outline {"
            "   background-color: #FFFFFF; border: 1px solid #E7E7EA; border-radius: 17px;"
            "   padding: 0 16px; color: #18181B; font-size: 13px; font-weight: 500;"
            "   min-height: 32px; max-height: 34px;"
            "   font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif; }"
            " QPushButton#btn_outline:hover { background-color: #F4F4F5; border-color: #D4D4D8; }"
            " QPushButton#btn_outline:pressed { background-color: #EDEDF0; }"
            " QPushButton#btn_primary {"
            "   background-color: #18181B; border: 1px solid #18181B; border-radius: 18px;"
            "   padding: 0 22px; color: #FFFFFF; font-size: 13px; font-weight: 500;"
            "   min-height: 34px; max-height: 36px;"
            "   font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif; }"
            " QPushButton#btn_primary:hover { background-color: #27272A; border-color: #27272A; }"
            " QCheckBox { color: #18181B; font-size: 13px; spacing: 10px;"
            "   font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif; }"
            " QCheckBox::indicator { width: 18px; height: 18px;"
            "   border: 1px solid #D4D4D8; border-radius: 4px; background: #FFFFFF; }"
            " QCheckBox::indicator:hover { border-color: #18181B; }"
            " QCheckBox::indicator:checked {"
            "   background: #18181B; border-color: #18181B; }"
            # T-165 (followup 2026-05-25): QRadioButton indicator — круглый с
            # точкой по центру в checked. Первая версия `border: 5px solid` дала
            # квадрат — Qt не применяет border-radius когда
            # border-thickness близок к радиусу. Решение через qradialgradient:
            # тёмная точка радиуса 45% (stop 0..0.45), резкий переход на белый
            # (stop 0.5..1), border 1px по контуру.
            " QRadioButton { color: #18181B; font-size: 13px; spacing: 10px;"
            "   font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif; }"
            " QRadioButton::indicator { width: 18px; height: 18px; }"
            " QRadioButton::indicator:unchecked {"
            "   background: #FFFFFF; border: 1px solid #D4D4D8; border-radius: 9px; }"
            " QRadioButton::indicator:unchecked:hover { border-color: #18181B; }"
            " QRadioButton::indicator:checked {"
            "   border: 1px solid #18181B; border-radius: 9px;"
            "   background: qradialgradient(cx:0.5, cy:0.5, radius:0.5,"
            "     fx:0.5, fy:0.5,"
            "     stop:0 #18181B, stop:0.45 #18181B,"
            "     stop:0.5 #FFFFFF, stop:1 #FFFFFF); }"
            " QWidget#settings_footer { background: transparent;"
            "   border-top: 1px solid #E7E7EA; }"
        )

        # === Inputs ===
        # QKeySequenceEdit: layout-independent — Qt использует Windows virtual key codes
        self.hotkey_edit = QKeySequenceEdit()
        self.hotkey_edit.setObjectName("settings_input_mono")
        self.hotkey_edit.setMaximumSequenceLength(1)
        self.hotkey_edit.setKeySequence(QKeySequence(hotkey_to_qt(current["hotkey"])))

        self.path_edit = QLineEdit(current["history_dir"])
        self.path_edit.setObjectName("settings_input_mono")

        # Словарь: слова, имена и термины, которые модель путает. Из коробки
        # пусто — приложение не знает, чем занят конкретный пользователь.
        self.dict_edit = QPlainTextEdit(current.get("dictionary", "") or "")
        self.dict_edit.setObjectName("settings_textarea")
        self.dict_edit.setMinimumHeight(88)
        self.dict_edit.setMaximumHeight(120)
        self.dict_edit.setPlaceholderText(
            "Например: имена коллег, названия проектов, термины — через запятую "
            "или короткими фразами. Пусто — модель работает без подсказки."
        )
        self.dict_edit.setToolTip(
            "Текст уходит модели как initial_prompt: она начинает узнавать слова, "
            "которые до этого писала неправильно. Работает как подсказка, а не как "
            "жёсткая замена — для замен есть отдельный редактор ниже."
        )
        # Счётчик бюджета: у Whisper на initial_prompt жёсткий лимит, и лишнее
        # молча отрезается С НАЧАЛА строки — первые слова словаря перестают
        # работать без единой ошибки в логе. Поэтому число видно сразу.
        self.dict_counter = QLabel()
        self.dict_counter.setObjectName("frow_hint")
        self.dict_edit.textChanged.connect(self._update_dict_counter)

        # Имена говорящих в транскрипте созвона.
        self.speaker_self_edit = QLineEdit(current.get("speaker_self", DEFAULT_SPEAKER_SELF))
        self.speaker_self_edit.setObjectName("settings_input")
        self.speaker_self_edit.setPlaceholderText(DEFAULT_SPEAKER_SELF)
        self.speaker_other_edit = QLineEdit(current.get("speaker_other", DEFAULT_SPEAKER_OTHER))
        self.speaker_other_edit.setObjectName("settings_input")
        self.speaker_other_edit.setPlaceholderText(DEFAULT_SPEAKER_OTHER)

        # Замены живут в отдельном файле профиля и правятся отдельным диалогом:
        # таблица правил в этом окне сделала бы его вдвое выше.
        self._replacement_rules = profile.load_replacement_rules()
        self._replacements_edited = False

        self.count_spin = QSpinBox()
        self.count_spin.setObjectName("numfield")
        self.count_spin.setRange(3, 20)
        self.count_spin.setValue(int(current["rotation_count"]))
        self.count_spin.setButtonSymbols(QSpinBox.UpDownArrows)

        # Чекбоксы — без text, лейбл и meta живут в отдельных QLabel рядом
        # (по дизайну meta `.check__meta { color: text-3, font-weight 400 }` — другой цвет
        # и вес, native QCheckBox не даёт rich-text внутри. Композит через HBox).
        self.autostart_box = QCheckBox()
        self.autostart_box.setChecked(bool(current["autostart"]))

        self.startmin_box = QCheckBox()
        self.startmin_box.setChecked(bool(current["start_minimized"]))

        # T-165: hybrid streaming/batch — заменили чекбокс «Background streaming» на
        # radio-группу из 3 опций + spinbox порога. processing_mode из QSettings
        # подставляется в активный radio; threshold (1-60 сек) — в spinbox.
        # spinbox enabled только при «Авто».
        current_mode = current.get("processing_mode", "auto")
        if current_mode not in ("auto", "always_batch", "always_streaming"):
            current_mode = "auto"
        current_threshold = int(current.get("auto_threshold_sec", 10))
        current_threshold = max(1, min(60, current_threshold))

        self.mode_auto_radio = QRadioButton("Авто (batch ≤ N сек, streaming >)")
        self.mode_auto_radio.setToolTip(
            "Программа сама решает: до порога — batch (full-pass на стопе, точнее на "
            "коротких), после порога — streaming-worker (быстрее на длинных). Дефолт."
        )
        self.mode_batch_radio = QRadioButton("Всегда batch (full-pass на стопе)")
        self.mode_batch_radio.setToolTip(
            "Streaming-worker никогда не запускается. На стопе один полный проход "
            "Whisper. Самый предсказуемый режим, медленнее на длинных записях."
        )
        self.mode_streaming_radio = QRadioButton("Всегда streaming (LCP-2 + chunks)")
        self.mode_streaming_radio.setToolTip(
            "Фоновый LA-2 worker крутится с начала записи. Сокращает время "
            "транскрипции на длинных, но на коротких <10 сек может терять слова."
        )
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.mode_auto_radio)
        self.mode_group.addButton(self.mode_batch_radio)
        self.mode_group.addButton(self.mode_streaming_radio)
        if current_mode == "always_batch":
            self.mode_batch_radio.setChecked(True)
        elif current_mode == "always_streaming":
            self.mode_streaming_radio.setChecked(True)
        else:
            self.mode_auto_radio.setChecked(True)

        self.threshold_spin = QSpinBox()
        self.threshold_spin.setObjectName("numfield")
        self.threshold_spin.setRange(1, 60)
        self.threshold_spin.setValue(current_threshold)
        self.threshold_spin.setButtonSymbols(QSpinBox.UpDownArrows)
        self.threshold_spin.setSuffix(" сек")
        self.threshold_spin.setEnabled(self.mode_auto_radio.isChecked())
        self.mode_auto_radio.toggled.connect(self.threshold_spin.setEnabled)

        # T-164: pre-roll буфер — устраняет head-loss (потерю первых слов).
        # always-on микрофон → privacy concern → дефолт OFF.
        self.preroll_box = QCheckBox()
        self.preroll_box.setChecked(bool(current.get("pre_roll_enabled", False)))
        self.preroll_box.setToolTip(
            "Микрофон постоянно активен в фоне, держит последние 500мс аудио. "
            "При нажатии hotkey эти 500мс дописываются перед свежим аудио → "
            "Whisper не теряет первые слова. Windows показывает active mic "
            "индикатор в tray."
        )

        # T-174: хранить ли аудио (MP3) записанного созвона. Дефолт OFF —
        # обычно нужен только транскрипт, а аудио занимает десятки мегабайт.
        self.keep_call_audio_box = QCheckBox()
        self.keep_call_audio_box.setChecked(bool(current.get("keep_call_audio", False)))
        self.keep_call_audio_box.setToolTip(
            "По умолчанию запись созвона сохраняется только как текстовый транскрипт "
            "(L=ты, R=собеседник) в подпапке Calls папки истории, а само аудио "
            "удаляется. Включи, чтобы оставлять MP3 128k рядом с транскриптом. "
            "Нужен ffmpeg в PATH — без него MP3 просто не создаётся."
        )

        # T-262: автопроверка обновлений. Выключаемая сознательно — часть
        # аудитории выбирает оффлайн-инструменты именно за отсутствие сетевой
        # активности, и «тихий» выход в интернет по своей воле их не устроит.
        self.updates_box = QCheckBox()
        self.updates_box.setChecked(bool(current.get("check_updates", True)))
        self.updates_box.setToolTip(
            "При запуске приложение в фоне спрашивает у сервера обновлений, нет ли "
            "новой версии, и показывает ненавязчивое уведомление. Выключено — в сеть "
            "само не ходит; проверить вручную можно кнопкой ниже."
        )

        # T-175: размер ротируемого буфера WAV созвонов в Calls\ (durability-страховка
        # «вернуться» — переслушать / дотранскрибировать). Аналог «Количество записей»
        # надиктовок, но для созвонов; дефолт 2.
        self.call_keep_spin = QSpinBox()
        self.call_keep_spin.setObjectName("numfield")
        self.call_keep_spin.setRange(1, 20)
        self.call_keep_spin.setValue(int(current.get("call_audio_keep", DEFAULT_CALL_AUDIO_KEEP)))
        self.call_keep_spin.setButtonSymbols(QSpinBox.UpDownArrows)
        self.call_keep_spin.setToolTip(
            "Сколько последних WAV-записей созвонов держать в "
            "подпапке Calls как страховку «вернуться» "
            "(переслушать / дотранскрибировать кривой транскрипт). По умолчанию 2."
        )

        # === T-259: модель транскрипции ===
        # Сам выбор переехал в отдельный раздел «Модели» (кнопка-чип в шапке окна,
        # между статистикой и настройками): карточки с размером/скоростью,
        # скачиванием, удалением и «своей моделью». Здесь — только строка
        # «что сейчас», чтобы настройки не были вторым местом правды.
        self._model_spec = engine.spec_from_settings(current)
        self._current_model_key = current.get("model", DEFAULT_MODEL_EXISTING)
        self._current_custom_model = current.get("custom_model", "") or ""

        def _make_check_row(cb: QCheckBox, label_text: str, meta_text: str) -> QWidget:
            """`.check` composite: cb + label (text-1) + meta (text-3). Клик на любой
            QLabel → cb.toggle()."""
            w = QWidget()
            w.setStyleSheet("background: transparent;")
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(10)
            h.addWidget(cb)
            main_lbl = QLabel(label_text)
            main_lbl.setStyleSheet(
                "color: #18181B; font-size: 13px; font-weight: 500; background: transparent;"
                " font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif;"
            )
            main_lbl.setCursor(Qt.PointingHandCursor)
            main_lbl.mousePressEvent = lambda _ev, _cb=cb: _cb.toggle()
            h.addWidget(main_lbl)
            meta_lbl = QLabel(meta_text)
            meta_lbl.setStyleSheet(
                "color: #A1A1AA; font-size: 13px; font-weight: 400; background: transparent;"
                " font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif;"
            )
            meta_lbl.setCursor(Qt.PointingHandCursor)
            meta_lbl.mousePressEvent = lambda _ev, _cb=cb: _cb.toggle()
            h.addWidget(meta_lbl)
            h.addStretch()
            return w

        # === Body ===
        # По CSS `.settings-body { padding: 22px 24px 8px; gap: 16px }` → Qt margins L,T,R,B.
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        body_v = QVBoxLayout(body)
        body_v.setContentsMargins(24, 22, 24, 8)
        body_v.setSpacing(16)

        # frow_hotkey: label / input / clear-btn + hint span 2 cols
        from PySide6.QtWidgets import QGridLayout as _QGrid
        gh = _QGrid()
        gh.setContentsMargins(0, 0, 0, 0)
        gh.setHorizontalSpacing(8)
        gh.setVerticalSpacing(6)
        gh.setColumnMinimumWidth(0, 150)
        gh.setColumnStretch(1, 1)
        lbl_hk = QLabel("Hotkey:")
        lbl_hk.setObjectName("frow_label")
        gh.addWidget(lbl_hk, 0, 0)
        gh.addWidget(self.hotkey_edit, 0, 1)
        self.hotkey_clear_btn = QPushButton("Очистить")
        self.hotkey_clear_btn.setObjectName("btn_outline")
        self.hotkey_clear_btn.setCursor(Qt.PointingHandCursor)
        self.hotkey_clear_btn.clicked.connect(self.hotkey_edit.clear)
        gh.addWidget(self.hotkey_clear_btn, 0, 2)
        hint = QLabel(
            "Кликни в поле и нажми сочетание — Qt зафиксирует его автоматически. "
            "Работает независимо от раскладки клавиатуры (виртуальные key codes Windows)."
        )
        hint.setObjectName("frow_hint")
        hint.setWordWrap(True)
        gh.addWidget(hint, 1, 1)
        body_v.addLayout(gh)

        # frow_folder: label / input / browse-btn
        gf = _QGrid()
        gf.setContentsMargins(0, 0, 0, 0)
        gf.setHorizontalSpacing(8)
        gf.setVerticalSpacing(6)
        gf.setColumnMinimumWidth(0, 150)
        gf.setColumnStretch(1, 1)
        lbl_path = QLabel("Папка истории:")
        lbl_path.setObjectName("frow_label")
        gf.addWidget(lbl_path, 0, 0)
        gf.addWidget(self.path_edit, 0, 1)
        browse_btn = QPushButton("Обзор…")
        browse_btn.setObjectName("btn_outline")
        browse_btn.setCursor(Qt.PointingHandCursor)
        browse_btn.clicked.connect(self._browse_path)
        gf.addWidget(browse_btn, 0, 2)
        body_v.addLayout(gf)

        # frow_count: label / numfield (96px, выровнен слева)
        gc = _QGrid()
        gc.setContentsMargins(0, 0, 0, 0)
        gc.setHorizontalSpacing(8)
        gc.setVerticalSpacing(6)
        gc.setColumnMinimumWidth(0, 150)
        gc.setColumnStretch(2, 1)  # пустая колонка занимает остаток
        lbl_count = QLabel("Количество записей:")
        lbl_count.setObjectName("frow_label")
        gc.addWidget(lbl_count, 0, 0)
        gc.addWidget(self.count_spin, 0, 1)
        body_v.addLayout(gc)

        # frow_model: только показ текущей модели — сам выбор в разделе «Модели» (T-259)
        gmd = _QGrid()
        gmd.setContentsMargins(0, 0, 0, 0)
        gmd.setHorizontalSpacing(8)
        gmd.setVerticalSpacing(4)
        gmd.setColumnMinimumWidth(0, 150)
        gmd.setColumnStretch(1, 1)
        lbl_model = QLabel("Модель:")
        lbl_model.setObjectName("frow_label")
        gmd.addWidget(lbl_model, 0, 0)
        self.model_value_label = QLabel(engine.spec_display(self._model_spec))
        self.model_value_label.setStyleSheet(
            "font-size: 13px; font-weight: 500; color: #18181B; background: transparent;"
        )
        gmd.addWidget(self.model_value_label, 0, 1)
        model_hint = QLabel("Меняется в разделе «Модели» — кнопка-чип в шапке окна.")
        model_hint.setObjectName("frow_hint")
        gmd.addWidget(model_hint, 1, 1)
        body_v.addLayout(gmd)

        # frow_cuda: ускорение GPU — докачиваемый CUDA-слой (T-261).
        # Строка есть всегда, но на машине без NVIDIA она честно говорит, что
        # качать нечего: иначе «почему у меня медленно» остаётся без ответа.
        gcu = _QGrid()
        gcu.setContentsMargins(0, 0, 0, 0)
        gcu.setHorizontalSpacing(8)
        gcu.setVerticalSpacing(4)
        gcu.setColumnMinimumWidth(0, 150)
        gcu.setColumnStretch(1, 1)
        lbl_cuda = QLabel("Ускорение GPU:")
        lbl_cuda.setObjectName("frow_label")
        gcu.addWidget(lbl_cuda, 0, 0)
        self.cuda_value_label = QLabel()
        self.cuda_value_label.setStyleSheet(
            "font-size: 13px; font-weight: 500; color: #18181B; background: transparent;"
        )
        gcu.addWidget(self.cuda_value_label, 0, 1)
        self.cuda_btn = QPushButton()
        self.cuda_btn.setObjectName("btn_outline")
        self.cuda_btn.setCursor(Qt.PointingHandCursor)
        self.cuda_btn.clicked.connect(self._on_cuda_button)
        gcu.addWidget(self.cuda_btn, 0, 2)
        self.cuda_hint = QLabel()
        self.cuda_hint.setObjectName("frow_hint")
        self.cuda_hint.setWordWrap(True)
        gcu.addWidget(self.cuda_hint, 1, 1)
        body_v.addLayout(gcu)
        self._refresh_cuda_row()

        # frow_dict: словарь пользователя + счётчик бюджета промпта
        gdc = _QGrid()
        gdc.setContentsMargins(0, 0, 0, 0)
        gdc.setHorizontalSpacing(8)
        gdc.setVerticalSpacing(6)
        gdc.setColumnMinimumWidth(0, 150)
        gdc.setColumnStretch(1, 1)
        lbl_dict = QLabel("Словарь:")
        lbl_dict.setObjectName("frow_label")
        lbl_dict.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        gdc.addWidget(lbl_dict, 0, 0)
        gdc.addWidget(self.dict_edit, 0, 1)
        dict_hint = QLabel("Слова, имена и термины, которые модель путает.")
        dict_hint.setObjectName("frow_hint")
        dict_hint.setWordWrap(True)
        gdc.addWidget(dict_hint, 1, 1)
        gdc.addWidget(self.dict_counter, 2, 1)
        body_v.addLayout(gdc)
        self._update_dict_counter()

        # frow_replacements: сколько правил + кнопка редактора
        grp = _QGrid()
        grp.setContentsMargins(0, 0, 0, 0)
        grp.setHorizontalSpacing(8)
        grp.setVerticalSpacing(6)
        grp.setColumnMinimumWidth(0, 150)
        grp.setColumnStretch(1, 1)
        lbl_repl = QLabel("Замены в тексте:")
        lbl_repl.setObjectName("frow_label")
        grp.addWidget(lbl_repl, 0, 0)
        self.repl_value_label = QLabel()
        self.repl_value_label.setStyleSheet(
            "font-size: 13px; font-weight: 500; color: #18181B; background: transparent;"
        )
        grp.addWidget(self.repl_value_label, 0, 1)
        repl_btn = QPushButton("Редактировать…")
        repl_btn.setObjectName("btn_outline")
        repl_btn.setCursor(Qt.PointingHandCursor)
        repl_btn.clicked.connect(self._edit_replacements)
        grp.addWidget(repl_btn, 0, 2)
        repl_hint = QLabel(
            "Правило «шаблон → замена» применяется к готовому тексту. "
            "Помогает, когда подсказки словаря недостаточно."
        )
        repl_hint.setObjectName("frow_hint")
        repl_hint.setWordWrap(True)
        grp.addWidget(repl_hint, 1, 1)
        body_v.addLayout(grp)
        self._update_repl_label()

        # frow_speakers: имена в транскрипте созвона
        gsp = _QGrid()
        gsp.setContentsMargins(0, 0, 0, 0)
        gsp.setHorizontalSpacing(8)
        gsp.setVerticalSpacing(6)
        gsp.setColumnMinimumWidth(0, 150)
        gsp.setColumnStretch(1, 1)
        lbl_sp_self = QLabel("Моё имя:")
        lbl_sp_self.setObjectName("frow_label")
        gsp.addWidget(lbl_sp_self, 0, 0)
        gsp.addWidget(self.speaker_self_edit, 0, 1)
        lbl_sp_other = QLabel("Имя собеседника:")
        lbl_sp_other.setObjectName("frow_label")
        gsp.addWidget(lbl_sp_other, 1, 0)
        gsp.addWidget(self.speaker_other_edit, 1, 1)
        sp_hint = QLabel("Так подписаны реплики в транскрипте записанного созвона.")
        sp_hint.setObjectName("frow_hint")
        gsp.addWidget(sp_hint, 2, 1)
        body_v.addLayout(gsp)

        # frow_call_keep: label / numfield — буфер WAV созвонов (T-175)
        gck = _QGrid()
        gck.setContentsMargins(0, 0, 0, 0)
        gck.setHorizontalSpacing(8)
        gck.setVerticalSpacing(6)
        gck.setColumnMinimumWidth(0, 150)
        gck.setColumnStretch(2, 1)
        lbl_call_keep = QLabel("Буфер аудио созвонов:")
        lbl_call_keep.setObjectName("frow_label")
        gck.addWidget(lbl_call_keep, 0, 0)
        gck.addWidget(self.call_keep_spin, 0, 1)
        body_v.addLayout(gck)

        # checks (composite cb + main label + meta label)
        checks_lay = QVBoxLayout()
        checks_lay.setContentsMargins(0, 4, 0, 0)
        checks_lay.setSpacing(12)
        checks_lay.addWidget(_make_check_row(self.autostart_box, "Автостарт с Windows", "(shell:startup)"))
        checks_lay.addWidget(_make_check_row(self.startmin_box, "Запускать свёрнутым в tray", "(без открытого окна)"))
        checks_lay.addWidget(_make_check_row(self.preroll_box, "Pre-roll буфер (500мс ДО hotkey)", "(всегда-on микрофон, устраняет потерю первых слов)"))
        checks_lay.addWidget(_make_check_row(self.keep_call_audio_box, "Хранить аудио созвона (MP3)", "(по умолч. только транскрипт в Calls\\)"))
        checks_lay.addWidget(_make_check_row(self.updates_box, "Проверять обновления автоматически", "(выключено — приложение не выходит в сеть само)"))
        body_v.addLayout(checks_lay)

        # frow_version: версия + ручная проверка обновлений (T-262).
        # Ручная проверка живёт рядом с галкой автопроверки, но работает и при
        # выключенной: человек сам решил сходить в сеть — это не фоновая активность.
        gv = _QGrid()
        gv.setContentsMargins(0, 8, 0, 0)
        gv.setHorizontalSpacing(8)
        gv.setVerticalSpacing(4)
        gv.setColumnMinimumWidth(0, 150)
        gv.setColumnStretch(1, 1)
        lbl_ver = QLabel("Версия:")
        lbl_ver.setObjectName("frow_label")
        gv.addWidget(lbl_ver, 0, 0)
        self.version_label = QLabel(updater.current_version())
        self.version_label.setStyleSheet(
            "font-size: 13px; font-weight: 500; color: #18181B; background: transparent;"
        )
        gv.addWidget(self.version_label, 0, 1)
        self.update_btn = QPushButton("Проверить обновления")
        self.update_btn.setObjectName("btn_outline")
        self.update_btn.setCursor(Qt.PointingHandCursor)
        self.update_btn.setEnabled(updater.is_available())
        self.update_btn.clicked.connect(self._on_check_updates)
        gv.addWidget(self.update_btn, 0, 2)
        self.update_hint = QLabel(
            "" if updater.is_available()
            else "Обновления доступны только в установленной версии."
        )
        self.update_hint.setObjectName("frow_hint")
        self.update_hint.setWordWrap(True)
        gv.addWidget(self.update_hint, 1, 1)
        body_v.addLayout(gv)

        # frow_wizard: повторный проход мастера первого запуска (T-263)
        gw = _QGrid()
        gw.setContentsMargins(0, 8, 0, 0)
        gw.setHorizontalSpacing(8)
        gw.setVerticalSpacing(4)
        gw.setColumnMinimumWidth(0, 150)
        gw.setColumnStretch(1, 1)
        lbl_wiz = QLabel("Первый запуск:")
        lbl_wiz.setObjectName("frow_label")
        gw.addWidget(lbl_wiz, 0, 0)
        wiz_hint = QLabel(
            "Микрофон, горячая клавиша, модель и ускорение — по шагам, "
            "как при первом запуске."
        )
        wiz_hint.setObjectName("frow_hint")
        wiz_hint.setWordWrap(True)
        gw.addWidget(wiz_hint, 0, 1)
        self.wizard_btn = QPushButton("Пройти заново")
        self.wizard_btn.setObjectName("btn_outline")
        self.wizard_btn.setCursor(Qt.PointingHandCursor)
        self.wizard_btn.clicked.connect(self._on_run_wizard)
        gw.addWidget(self.wizard_btn, 0, 2)
        body_v.addLayout(gw)

        # T-165: frow_mode — Режим обработки (radio + spinbox порога).
        # Слева label-150 (как остальные frow_*), справа VBox с 3 radio + строка-spinbox.
        gm = _QGrid()
        gm.setContentsMargins(0, 8, 0, 0)
        gm.setHorizontalSpacing(8)
        gm.setVerticalSpacing(8)
        gm.setColumnMinimumWidth(0, 150)
        gm.setColumnStretch(1, 1)
        lbl_mode = QLabel("Режим обработки:")
        lbl_mode.setObjectName("frow_label")
        lbl_mode.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        gm.addWidget(lbl_mode, 0, 0)

        mode_box = QWidget()
        mode_box.setStyleSheet("background: transparent;")
        mode_v = QVBoxLayout(mode_box)
        mode_v.setContentsMargins(0, 0, 0, 0)
        mode_v.setSpacing(8)
        mode_v.addWidget(self.mode_auto_radio)
        mode_v.addWidget(self.mode_batch_radio)
        mode_v.addWidget(self.mode_streaming_radio)
        threshold_row = QHBoxLayout()
        threshold_row.setContentsMargins(0, 4, 0, 0)
        threshold_row.setSpacing(8)
        lbl_threshold = QLabel("Порог переключения:")
        lbl_threshold.setStyleSheet(
            "color: #18181B; font-size: 13px; background: transparent;"
            " font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif;"
        )
        threshold_row.addWidget(lbl_threshold)
        threshold_row.addWidget(self.threshold_spin)
        threshold_hint = QLabel("(активно при «Авто»)")
        threshold_hint.setObjectName("frow_hint")
        threshold_row.addWidget(threshold_hint)
        threshold_row.addStretch()
        mode_v.addLayout(threshold_row)
        gm.addWidget(mode_box, 0, 1)
        body_v.addLayout(gm)

        body_v.addStretch()

        # === Footer (hairline сверху, note слева, Cancel/OK справа) ===
        footer = QWidget()
        footer.setObjectName("settings_footer")
        footer_row = QHBoxLayout(footer)
        footer_row.setContentsMargins(20, 14, 20, 16)
        footer_row.setSpacing(12)
        note = QLabel("Изменения применяются сразу, без перезапуска.")
        note.setObjectName("footer_note")
        footer_row.addWidget(note, 1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("btn_outline")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.clicked.connect(self.reject)
        footer_row.addWidget(cancel_btn)
        ok_btn = QPushButton("OK")
        ok_btn.setObjectName("btn_primary")
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._validate_and_accept)
        footer_row.addWidget(ok_btn)

        # === Root layout ===
        # Настроек стало больше, чем помещается на невысоком экране (словарь,
        # замены, имена говорящих) — body в скролл, footer с кнопками закреплён.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        scroll.setWidget(body)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(scroll, 1)
        root.addWidget(footer)
        self.setMinimumHeight(560)

    def _update_dict_counter(self) -> None:
        """«N / 223 токенов» под словарём.

        Точное число берём у токенизатора загруженной модели; пока модель не в
        памяти — оценка, и это честно помечено, чтобы «142» не читалось как факт.
        """
        text = self.dict_edit.toPlainText().strip()
        exact = engine.count_prompt_tokens(text)
        if exact is None:
            n, suffix = profile.estimate_prompt_tokens(text), " (оценка, модель не загружена)"
        else:
            n, suffix = exact, ""
        budget = profile.PROMPT_TOKEN_BUDGET
        if n > budget:
            self.dict_counter.setStyleSheet("color: #DC2626; font-size: 12px; background: transparent;")
            tail = " — лишнее отрежется с НАЧАЛА словаря, сократите"
        else:
            self.dict_counter.setStyleSheet("color: #A1A1AA; font-size: 12px; background: transparent;")
            tail = ""
        self.dict_counter.setText(f"{n} / {budget} токенов{suffix}{tail}")

    def _update_repl_label(self) -> None:
        n = len(self._replacement_rules)
        if not n:
            self.repl_value_label.setText("правил нет")
            return
        tail = sum(1 for r in self._replacement_rules if r.get("tail_only"))
        text = f"{n} правил" if n != 1 else "1 правило"
        if tail:
            text += f" (из них {tail} только в конце текста)"
        self.repl_value_label.setText(text)

    def _edit_replacements(self) -> None:
        dlg = ReplacementsDialog(self, self._replacement_rules)
        if dlg.exec() == QDialog.Accepted:
            self._replacement_rules = dlg.rules()
            self._replacements_edited = True
            self._update_repl_label()

    def _browse_path(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Папка для истории записей", self.path_edit.text()
        )
        if chosen:
            self.path_edit.setText(chosen)

    # === T-261: строка «Ускорение GPU» ===
    def _refresh_cuda_row(self) -> None:
        """Три состояния: слой стоит / есть карта, но слоя нет / качать нечего."""
        gpu = cuda_layer.gpu()
        if cuda_layer.is_installed():
            size = cuda_layer.installed_bytes()
            self.cuda_value_label.setText(f"включено · {engine.fmt_bytes(size)}")
            self.cuda_hint.setText(
                "Транскрипция идёт на видеокарте. Удаление освободит место, "
                "приложение продолжит работать на процессоре."
            )
            self.cuda_btn.setText("Удалить")
            self.cuda_btn.setEnabled(True)
        elif cuda_layer.runtime_in_environment():
            self.cuda_value_label.setText("включено (системная CUDA)")
            self.cuda_hint.setText(
                "CUDA уже есть в системе — отдельная докачка не нужна."
            )
            self.cuda_btn.setText("Скачать")
            self.cuda_btn.setEnabled(False)
        elif gpu:
            self.cuda_value_label.setText("выключено")
            self.cuda_hint.setText(
                f"Найдена {gpu.get('name', 'NVIDIA')}. Докачка ~{cuda_layer.size_hint_mb()} МБ "
                "заметно ускорит транскрипцию."
            )
            self.cuda_btn.setText("Скачать")
            self.cuda_btn.setEnabled(True)
        else:
            self.cuda_value_label.setText("недоступно")
            self.cuda_hint.setText(
                "Видеокарта NVIDIA не найдена — работаем на процессоре."
            )
            self.cuda_btn.setText("Скачать")
            self.cuda_btn.setEnabled(False)

    # === T-262: ручная проверка обновлений ===
    @Slot(object, str)
    def _on_update_checked(self, info, error: str) -> None:
        """Итог проверки в GUI-потоке: обновить, «всё свежее» или причина отказа."""
        self.update_btn.setEnabled(updater.is_available())
        if error:
            self.update_hint.setText(f"Проверка не удалась: {error}")
            return
        if info is None:
            self.update_hint.setText(
                f"Установлена последняя версия ({updater.current_version()})."
            )
            return
        version = updater.version_of(info)
        self.update_hint.setText(f"Доступна версия {version}.")
        answer = QMessageBox.question(
            self, "Доступно обновление",
            f"Версия {version} готова к установке.\n\n"
            "Скачать и перезапустить приложение? Записи, настройки и скачанные "
            "модели останутся на месте.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return
        self.update_hint.setText("Скачиваю обновление…")
        self.update_btn.setEnabled(False)
        # Управление из download_and_apply не возвращается: velopack
        # перезапускает процесс сам, поэтому это последнее, что делает диалог.
        threading.Thread(
            target=lambda: updater.download_and_apply(info),
            daemon=True, name="update-apply",
        ).start()

    def _on_run_wizard(self) -> None:
        """Пройти мастер заново. Настройки закрываем: мастер пишет те же ключи,
        и оставленный позади диалог перетёр бы его выбор своим старым состоянием."""
        from .transcribe_ui import run_first_run_wizard

        self.accept()
        run_first_run_wizard(force=True)

    def _on_check_updates(self) -> None:
        self.update_btn.setEnabled(False)
        self.update_hint.setText("Проверяю…")

        def _worker() -> None:
            try:
                info = updater.check(quiet=False)
                self.update_checked.emit(info, "")
            except Exception as exc:
                self.update_checked.emit(None, f"{exc.__class__.__name__}: {exc}")

        threading.Thread(target=_worker, daemon=True, name="update-check").start()

    def _on_cuda_button(self) -> None:
        # Импорт здесь, а не наверху: transcribe_ui уже импортирует этот модуль,
        # и встречный импорт на уровне файла замкнул бы цикл.
        from .transcribe_ui import download_cuda_layer

        if cuda_layer.is_installed():
            ok, msg = cuda_layer.remove()
            if ok:
                QMessageBox.information(self, "Ускорение GPU", msg)
            else:
                QMessageBox.warning(self, "Ускорение GPU", msg)
            self._refresh_cuda_row()
            return
        # Настройки уходят на второй план: скачивание модальное и живёт дольше
        # диалога, поэтому родителем берём главное окно.
        self._cuda_requested = True
        self.accept()
        download_cuda_layer(parent=self.parent())

    def _hotkey_pynput(self) -> str:
        seq = self.hotkey_edit.keySequence()
        if seq.isEmpty():
            return ""
        return hotkey_to_pynput(seq.toString())

    def _validate_and_accept(self) -> None:
        hotkey = self._hotkey_pynput()
        if not hotkey:
            QMessageBox.warning(self, "Hotkey", "Hotkey не может быть пустым. Кликни в поле и нажми сочетание.")
            return
        ok, msg = parse_hotkey_valid(hotkey)
        if not ok:
            QMessageBox.warning(self, "Hotkey", f"Невалидный hotkey: {msg}")
            return
        if hotkey_conflicts_with_handy(hotkey):
            ans = QMessageBox.question(
                self,
                "Hotkey",
                "Этот hotkey может пересекаться с Handy (ctrl_left+`) или "
                "системным сочетанием. Сохранить всё равно?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ans != QMessageBox.Yes:
                return
        self.accept()

    def values(self) -> dict:
        # T-165: processing_mode — из активной radio-кнопки, auto_threshold_sec — из spinbox.
        if self.mode_batch_radio.isChecked():
            mode = "always_batch"
        elif self.mode_streaming_radio.isChecked():
            mode = "always_streaming"
        else:
            mode = "auto"
        # T-259: модель настройками не трогаем — она из раздела «Модели»,
        # отдаём как пришла, иначе OK в настройках перетёр бы свежий выбор.
        return {
            "model": self._current_model_key,
            "custom_model": self._current_custom_model,
            "hotkey": self._hotkey_pynput(),
            "history_dir": self.path_edit.text().strip(),
            "rotation_count": int(self.count_spin.value()),
            "autostart": bool(self.autostart_box.isChecked()),
            "start_minimized": bool(self.startmin_box.isChecked()),
            "processing_mode": mode,
            "auto_threshold_sec": int(self.threshold_spin.value()),
            "pre_roll_enabled": bool(self.preroll_box.isChecked()),
            "keep_call_audio": bool(self.keep_call_audio_box.isChecked()),
            "call_audio_keep": int(self.call_keep_spin.value()),
            "dictionary": self.dict_edit.toPlainText().strip(),
            "speaker_self": self.speaker_self_edit.text().strip() or DEFAULT_SPEAKER_SELF,
            "speaker_other": self.speaker_other_edit.text().strip() or DEFAULT_SPEAKER_OTHER,
            # T-261: нажали «Скачать» — прежний отказ снимаем, иначе следующий
            # старт снова считал бы, что от ускорения отказались навсегда.
            "cuda_layer_declined": False if self._cuda_requested else self._cuda_declined,
            "check_updates": bool(self.updates_box.isChecked()),
        }

    def replacement_rules(self) -> "list | None":
        """Правила замен, если редактор открывали, иначе None.

        None — сигнал «не трогать файл»: иначе OK в настройках переписывал бы
        правила, которые пользователь только что поправил в файле руками.
        """
        return self._replacement_rules if self._replacements_edited else None


class ReplacementsDialog(QDialog):
    """Редактор замен в готовом тексте: шаблон → замена + «только в конце текста».

    Таблица, а не свободный JSON: правило — это регулярка, и опечатка в ней
    молча выключает правило. Здесь она проверяется до сохранения.
    """

    def __init__(self, parent: QWidget | None, rules: list) -> None:
        super().__init__(parent)
        self.setWindowTitle("Замены в тексте")
        self.setMinimumSize(720, 420)
        self.setStyleSheet(
            BASE_DIALOG_QSS
            + " QTableWidget { background: #FFFFFF; border: 1px solid #E7E7EA;"
              "   border-radius: 8px; gridline-color: #F1F1F3; font-size: 13px;"
              "   font-family: 'Cascadia Mono','Consolas',monospace; }"
              " QHeaderView::section { background: #F6F6F7; border: none;"
              "   border-bottom: 1px solid #E7E7EA; padding: 6px 8px; font-size: 12px;"
              "   font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif; }"
        )

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Шаблон (регулярка)", "Замена", "Только в конце"])
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setColumnWidth(0, 320)
        self.table.setColumnWidth(1, 220)
        self.table.setColumnWidth(2, 120)
        self.table.verticalHeader().setVisible(False)
        for rule in rules:
            self._append_row(rule.get("pattern", ""), rule.get("replacement", ""),
                             bool(rule.get("tail_only")))

        hint = QLabel(
            "Шаблон — регулярное выражение Python; в замене работают ссылки на группы "
            "(\\1, \\2). «Только в конце» анкерит правило на конец текста: полезно для "
            "хвостовых галлюцинаций модели на тишине, но опасно для записи созвона — "
            "там короткая реплика в конце может быть настоящей."
        )
        hint.setObjectName("frow_hint")
        hint.setWordWrap(True)

        add_btn = QPushButton("Добавить")
        add_btn.setObjectName("btn_outline")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.clicked.connect(lambda: self._append_row("", "", False))
        del_btn = QPushButton("Удалить строку")
        del_btn.setObjectName("btn_outline")
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.clicked.connect(self._remove_current)

        footer = QWidget()
        footer.setObjectName("settings_footer")
        frow = QHBoxLayout(footer)
        frow.setContentsMargins(20, 14, 20, 16)
        frow.setSpacing(12)
        frow.addWidget(add_btn)
        frow.addWidget(del_btn)
        frow.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("btn_outline")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.clicked.connect(self.reject)
        frow.addWidget(cancel_btn)
        ok_btn = QPushButton("OK")
        ok_btn.setObjectName("btn_primary")
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._validate_and_accept)
        frow.addWidget(ok_btn)

        body = QWidget()
        body.setStyleSheet("background: transparent;")
        bv = QVBoxLayout(body)
        bv.setContentsMargins(24, 20, 24, 8)
        bv.setSpacing(10)
        bv.addWidget(self.table, 1)
        bv.addWidget(hint)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(body, 1)
        root.addWidget(footer)

    def _append_row(self, pattern: str, replacement: str, tail_only: bool) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(pattern))
        self.table.setItem(row, 1, QTableWidgetItem(replacement))
        flag = QTableWidgetItem()
        flag.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        flag.setCheckState(Qt.Checked if tail_only else Qt.Unchecked)
        self.table.setItem(row, 2, flag)

    def _remove_current(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _collect(self) -> list:
        out = []
        for row in range(self.table.rowCount()):
            pattern = (self.table.item(row, 0).text() if self.table.item(row, 0) else "").strip()
            if not pattern:
                continue  # пустая строка = «пользователь передумал», не ошибка
            replacement = self.table.item(row, 1).text() if self.table.item(row, 1) else ""
            flag = self.table.item(row, 2)
            out.append({
                "pattern": pattern,
                "replacement": replacement,
                "tail_only": bool(flag is not None and flag.checkState() == Qt.Checked),
            })
        return out

    def _validate_and_accept(self) -> None:
        for row in range(self.table.rowCount()):
            pattern = (self.table.item(row, 0).text() if self.table.item(row, 0) else "").strip()
            if not pattern:
                continue
            ok, msg = profile.validate_pattern(pattern)
            if not ok:
                self.table.setCurrentCell(row, 0)
                QMessageBox.warning(
                    self, "Замены",
                    f"Строка {row + 1}: шаблон не компилируется как регулярное "
                    f"выражение.\n\n{msg}",
                )
                return
        self._rules = self._collect()
        self.accept()

    def rules(self) -> list:
        return getattr(self, "_rules", [])


# === T-259: раздел «Модели» (карточки, как в Handy) ===
DEFAULT_CUSTOM_MODELS = "[]"  # JSON-список спек своих моделей в settings.ini

# Базовые токены диалогов (те же, что в SettingsDialog: Claude Design v2).
BASE_DIALOG_QSS = (
    "QDialog { background-color: #F6F6F7; }"
    " QLabel { background: transparent; color: #18181B;"
    "   font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif; }"
    " QLabel#frow_label { font-size: 13px; font-weight: 500; }"
    " QLabel#frow_hint { color: #A1A1AA; font-size: 12px; }"
    " QLabel#footer_note { color: #A1A1AA; font-size: 12px; }"
    " QLineEdit#settings_input_mono {"
    "   background: #FFFFFF; border: 1px solid #E7E7EA; border-radius: 8px;"
    "   padding: 0 12px; color: #18181B; font-size: 12.5px;"
    "   min-height: 32px; max-height: 34px;"
    "   font-family: 'Cascadia Mono','Cascadia Code','Consolas',monospace; }"
    " QLineEdit#settings_input_mono:hover { border-color: #D4D4D8; }"
    " QLineEdit#settings_input_mono:focus { border-color: #18181B; }"
    " QPushButton#btn_outline {"
    "   background-color: #FFFFFF; border: 1px solid #E7E7EA; border-radius: 17px;"
    "   padding: 0 16px; color: #18181B; font-size: 13px; font-weight: 500;"
    "   min-height: 32px; max-height: 34px;"
    "   font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif; }"
    " QPushButton#btn_outline:hover { background-color: #F4F4F5; border-color: #D4D4D8; }"
    " QPushButton#btn_outline:pressed { background-color: #EDEDF0; }"
    " QPushButton#btn_outline:disabled { color: #A1A1AA; background-color: #F4F4F5; }"
    " QPushButton#btn_primary {"
    "   background-color: #18181B; border: 1px solid #18181B; border-radius: 18px;"
    "   padding: 0 22px; color: #FFFFFF; font-size: 13px; font-weight: 500;"
    "   min-height: 34px; max-height: 36px;"
    "   font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif; }"
    " QPushButton#btn_primary:hover { background-color: #27272A; border-color: #27272A; }"
    " QWidget#settings_footer { background: transparent; border-top: 1px solid #E7E7EA; }"
)


def load_custom_models() -> list[str]:
    """Список «своих моделей» (repo id / папки) из settings.ini."""
    s = get_settings()
    raw = s.value("custom_models", DEFAULT_CUSTOM_MODELS, type=str) or "[]"
    try:
        items = json.loads(raw)
        return [str(x).strip() for x in items if str(x).strip()]
    except Exception:
        return []


def save_custom_models(items: list[str]) -> None:
    s = get_settings()
    seen, uniq = set(), []
    for it in items:
        key = it.strip()
        if key and key.lower() not in seen:
            seen.add(key.lower())
            uniq.append(key)
    s.setValue("custom_models", json.dumps(uniq, ensure_ascii=False))
    s.sync()


class _MeterBar(QWidget):
    """Пятисегментная шкала «точность/скорость» (как в Handy)."""

    def __init__(self, value: int, parent=None) -> None:
        super().__init__(parent)
        self._value = max(0, min(5, int(value)))
        self.setFixedSize(62, 6)

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        seg_w, gap = 10, 3
        for i in range(5):
            p.setBrush(QColor("#18181B" if i < self._value else "#E7E7EA"))
            p.drawRoundedRect(i * (seg_w + gap), 1, seg_w, 4, 2, 2)
        p.end()


class ModelCard(QFrame):
    """Карточка модели: имя, описание, шкалы, размер и действия.

    `spec` — то, что уйдёт в engine (пресет / repo id / папка). Состояния:
    активная (рамка тёмная + бейдж), скачанная (можно выбрать/удалить),
    не скачанная (кнопка «Скачать» + прогресс в самой карточке).
    """

    activate_requested = Signal(str)
    download_requested = Signal(str)
    delete_requested = Signal(str)
    forget_requested = Signal(str)  # убрать свою модель из списка

    def __init__(self, info: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec: str = info["spec"]
        self._downloaded = bool(info.get("downloaded"))
        self._active = bool(info.get("active"))
        self.setObjectName("model_card_active" if self._active else "model_card")
        self.setCursor(Qt.PointingHandCursor if self._downloaded and not self._active else Qt.ArrowCursor)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 13, 16, 13)
        root.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        title = QLabel(info.get("title") or self.spec)
        title.setStyleSheet(
            "font-size: 14px; font-weight: 600; color: #18181B; background: transparent;"
        )
        top.addWidget(title)
        if self._active:
            badge = QLabel("✓ Активная")
            badge.setObjectName("model_badge_active")
            top.addWidget(badge)
        top.addStretch()
        for label, val in (("точность", info.get("accuracy", 0)), ("скорость", info.get("speed", 0))):
            if not val:
                continue
            cap = QLabel(label.upper())
            cap.setStyleSheet(
                "font-size: 10px; letter-spacing: 0.5px; color: #A1A1AA; background: transparent;"
            )
            top.addWidget(cap)
            top.addWidget(_MeterBar(int(val)))
        root.addLayout(top)

        desc = QLabel(info.get("desc") or "")
        desc.setWordWrap(True)
        desc.setStyleSheet("font-size: 12.5px; color: #71717A; background: transparent;")
        root.addWidget(desc)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: #EFEFF1; background: #EFEFF1; max-height: 1px;")
        root.addWidget(line)

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        meta = QLabel(info.get("meta") or "")
        meta.setStyleSheet("font-size: 12px; color: #A1A1AA; background: transparent;")
        bottom.addWidget(meta)
        bottom.addStretch()

        self.progress = QProgressBar()
        self.progress.setObjectName("model_progress")
        self.progress.setTextVisible(True)
        self.progress.setFixedWidth(190)
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        bottom.addWidget(self.progress)

        if not self._downloaded:
            size_mb = int(info.get("size_mb") or 0)
            label = (
                f"⭳ Скачать · {engine.fmt_bytes(size_mb * 1_000_000)}" if size_mb else "⭳ Скачать"
            )
            self.dl_btn = QPushButton(label)
            self.dl_btn.setObjectName("btn_outline")
            self.dl_btn.setCursor(Qt.PointingHandCursor)
            self.dl_btn.clicked.connect(lambda: self.download_requested.emit(self.spec))
            bottom.addWidget(self.dl_btn)
        else:
            if not self._active:
                use_btn = QPushButton("Выбрать")
                use_btn.setObjectName("btn_primary_sm")
                use_btn.setCursor(Qt.PointingHandCursor)
                use_btn.clicked.connect(lambda: self.activate_requested.emit(self.spec))
                bottom.addWidget(use_btn)
            del_btn = QPushButton("Удалить")
            del_btn.setObjectName("btn_link_danger")
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setEnabled(not self._active)
            del_btn.setToolTip(
                "Активную модель удалить нельзя — сначала выбери другую"
                if self._active else "Удалить веса с диска"
            )
            del_btn.clicked.connect(lambda: self.delete_requested.emit(self.spec))
            bottom.addWidget(del_btn)
        if info.get("custom"):
            forget_btn = QPushButton("Убрать из списка")
            forget_btn.setObjectName("btn_link")
            forget_btn.setCursor(Qt.PointingHandCursor)
            forget_btn.setEnabled(not self._active)
            forget_btn.clicked.connect(lambda: self.forget_requested.emit(self.spec))
            bottom.addWidget(forget_btn)
        root.addLayout(bottom)

    def mousePressEvent(self, ev) -> None:  # noqa: N802 — клик по карточке = выбрать
        # T-269: базовую обработку делаем ДО эмита. Сигнал доставляется синхронно,
        # `ModelsDialog._activate` пересобирает список — карточка сносит саму себя
        # изнутри собственного обработчика, и всё, что стоит после эмита, работает
        # с уже мёртвым C++-объектом («libshiboken: ModelCard already deleted»).
        # Пара к этому — отложенный снос старых карточек в `_rebuild`.
        super().mousePressEvent(ev)
        if self._downloaded and not self._active:
            self.activate_requested.emit(self.spec)
            return  # после эмита `self` может быть уже помечен к удалению

    def show_progress(self, done: int, total: int) -> None:
        self.progress.setVisible(True)
        if hasattr(self, "dl_btn"):
            self.dl_btn.setEnabled(False)
            self.dl_btn.setText("Качаю…")
        if total > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(done * 100 / total))
            self.progress.setFormat(f"{done / 1e6:.0f} / {total / 1e6:.0f} МБ")
        else:
            self.progress.setRange(0, 0)
            self.progress.setFormat(f"{done / 1e6:.0f} МБ")


class _AddCustomModelDialog(QDialog):
    """Ввод своей модели: HF repo id или папка с CT2-моделью."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Добавить свою модель")
        self.setMinimumWidth(560)
        self.setStyleSheet(BASE_DIALOG_QSS)
        self.edit = QLineEdit()
        self.edit.setObjectName("settings_input_mono")
        self.edit.setPlaceholderText(
            "deepdml/faster-whisper-large-v3-turbo-ct2  или  D:\\models\\my-ct2-model"
        )
        browse = QPushButton("Выбрать папку")
        browse.setObjectName("btn_outline")
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(self._browse)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(10)
        head = QLabel("HuggingFace repo id или папка с моделью в формате CTranslate2")
        head.setObjectName("frow_label")
        root.addWidget(head)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.edit, 1)
        row.addWidget(browse)
        root.addLayout(row)
        hint = QLabel(
            "Модель в формате transformers (.safetensors) сначала конвертируется командой "
            "ct2-transformers-converter — покажу её, если формат не подойдёт."
        )
        hint.setObjectName("frow_hint")
        hint.setWordWrap(True)
        root.addWidget(hint)
        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton("Отмена")
        cancel.setObjectName("btn_outline")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        ok = QPushButton("Добавить")
        ok.setObjectName("btn_primary")
        ok.setCursor(Qt.PointingHandCursor)
        ok.setDefault(True)
        ok.clicked.connect(self._accept)
        btns.addWidget(ok)
        root.addLayout(btns)

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Папка с CT2-моделью", str(engine.models_root()))
        if chosen:
            self.edit.setText(chosen)

    def _accept(self) -> None:
        spec = self.edit.text().strip().strip('"')
        if not spec:
            QMessageBox.warning(self, "Своя модель", "Укажи repo id или выбери папку.")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ok, msg = engine.validate_spec(spec)
        finally:
            QApplication.restoreOverrideCursor()
        if not ok:
            QMessageBox.warning(self, "Своя модель", msg)
            return
        self._spec = spec
        self.accept()

    def spec(self) -> str:
        return getattr(self, "_spec", "")


class ModelsDialog(QDialog):
    """Раздел «Модели транскрипции» — карточки вместо выпадающего списка.

    Меняет только `model` / `custom_model` / `custom_models` в settings.ini;
    саму подмену модели в движке делает `transcribe_ui` по сигналу
    `settings_changed` (там же блокировка во время записи и прогресс загрузки).
    """

    _dl_progress = Signal(str, int, int)  # spec, done, total
    _dl_finished = Signal(str, str)       # spec, error ("" — успех)

    def __init__(self, parent: QWidget | None, current: dict, model_locked: bool = False) -> None:
        super().__init__(parent)
        self._locked = bool(model_locked)
        self._current = dict(current)
        self._active_spec = engine.spec_from_settings(current)
        self._customs = load_custom_models()
        # своя модель из старых настроек — подхватить в список, чтобы не потерялась
        legacy = (current.get("custom_model") or "").strip()
        if legacy and legacy.lower() not in {c.lower() for c in self._customs}:
            self._customs.append(legacy)
        self._cards: dict[str, ModelCard] = {}
        self._downloading: set[str] = set()

        self.setWindowTitle("Модели транскрипции")
        self.setMinimumSize(720, 600)
        self.setStyleSheet(
            BASE_DIALOG_QSS
            + " QFrame#model_card { background: #FFFFFF; border: 1px solid #E7E7EA;"
              "   border-radius: 12px; }"
              " QFrame#model_card:hover { border-color: #D4D4D8; }"
              " QFrame#model_card_active { background: #FFFFFF; border: 1.5px solid #18181B;"
              "   border-radius: 12px; }"
              " QLabel#model_badge_active { background: #18181B; color: #FFFFFF;"
              "   border-radius: 9px; padding: 2px 9px; font-size: 11px; font-weight: 600; }"
              " QLabel#section_title { font-size: 13px; font-weight: 600; color: #52525B; }"
              " QPushButton#btn_primary_sm { background-color: #18181B; border: 1px solid #18181B;"
              "   border-radius: 15px; padding: 0 16px; color: #FFFFFF; font-size: 12.5px;"
              "   min-height: 28px; max-height: 30px; }"
              " QPushButton#btn_primary_sm:hover { background-color: #27272A; }"
              " QPushButton#btn_link, QPushButton#btn_link_danger { background: transparent;"
              "   border: none; padding: 0 6px; font-size: 12.5px; min-height: 28px; }"
              " QPushButton#btn_link { color: #71717A; }"
              " QPushButton#btn_link:hover { color: #18181B; }"
              " QPushButton#btn_link_danger { color: #DC2626; }"
              " QPushButton#btn_link_danger:disabled, QPushButton#btn_link:disabled { color: #D4D4D8; }"
              " QProgressBar#model_progress { border: 1px solid #E7E7EA; border-radius: 8px;"
              "   background: #F4F4F5; height: 18px; font-size: 11px; color: #52525B;"
              "   text-align: center; }"
              " QProgressBar#model_progress::chunk { background: #18181B; border-radius: 7px; }"
              " QScrollArea { background: transparent; border: none; }"
              " QWidget#models_scroll_body { background: transparent; }"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QWidget()
        head_v = QVBoxLayout(head)
        head_v.setContentsMargins(24, 20, 24, 8)
        head_v.setSpacing(4)
        h1 = QLabel("Модели транскрипции")
        h1.setStyleSheet("font-size: 17px; font-weight: 600; color: #18181B; background: transparent;")
        head_v.addWidget(h1)
        sub = QLabel(
            "Выбери модель или скачай дополнительные. Чем крупнее модель — тем точнее "
            "и медленнее; веса качаются один раз."
        )
        sub.setObjectName("frow_hint")
        sub.setWordWrap(True)
        head_v.addWidget(sub)
        if self._locked:
            lock = QLabel("Идёт запись или транскрипция — смена модели заблокирована.")
            lock.setObjectName("frow_hint")
            head_v.addWidget(lock)
        root.addWidget(head)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        root.addWidget(self._scroll, 1)

        footer = QWidget()
        footer.setObjectName("settings_footer")
        f_row = QHBoxLayout(footer)
        f_row.setContentsMargins(20, 12, 20, 14)
        f_row.setSpacing(10)
        self.disk_label = QLabel("")
        self.disk_label.setObjectName("footer_note")
        f_row.addWidget(self.disk_label, 1)
        add_btn = QPushButton("Добавить свою…")
        add_btn.setObjectName("btn_outline")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.clicked.connect(self._add_custom)
        f_row.addWidget(add_btn)
        close_btn = QPushButton("Готово")
        close_btn.setObjectName("btn_primary")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        f_row.addWidget(close_btn)
        root.addWidget(footer)

        self._dl_progress.connect(self._on_dl_progress)
        self._dl_finished.connect(self._on_dl_finished)
        self._rebuild()

    # --- сборка списка ---
    def _entries(self) -> list[dict]:
        # реальные размеры на диске — чтобы «на диске» было не абстракцией
        try:
            on_disk = {e["repo"].lower(): e["bytes"] for e in engine.list_cached_models()}
        except Exception:
            on_disk = {}

        def _disk_note(spec: str) -> str:
            size = on_disk.get((engine.repo_for_spec(spec) or "").lower(), 0)
            return engine.fmt_bytes(size) + " на диске" if size else "на диске"

        out: list[dict] = []
        for key in engine.PRESET_KEYS:
            meta = engine.preset_meta(key)
            downloaded = engine.is_cached(key)
            out.append({
                "spec": key, "title": meta["title"], "desc": meta["desc"],
                "accuracy": meta["accuracy"], "speed": meta["speed"],
                "size_mb": meta["size_mb"], "downloaded": downloaded,
                "active": key == self._active_spec, "custom": False,
                "meta": "Многоязычная · " + (
                    _disk_note(key) if downloaded
                    else engine.fmt_bytes(meta["size_mb"] * 1_000_000)
                ),
            })
        for spec in self._customs:
            downloaded = engine.is_cached(spec)
            local = engine.is_local_path(spec)
            out.append({
                "spec": spec, "title": engine.spec_display(spec),
                "desc": "Своя модель · " + (spec if len(spec) < 70 else spec[:67] + "…"),
                "accuracy": 0, "speed": 0, "size_mb": 0, "downloaded": downloaded,
                "active": spec == self._active_spec, "custom": True,
                "meta": ("Папка на диске" if local else "HuggingFace") + " · " + (
                    _disk_note(spec) if downloaded and not local
                    else "готова" if downloaded else "ещё не скачана"
                ),
            })
        return out

    def _rebuild(self) -> None:
        entries = self._entries()
        body = QWidget()
        body.setObjectName("models_scroll_body")
        v = QVBoxLayout(body)
        v.setContentsMargins(24, 8, 24, 16)
        v.setSpacing(10)
        self._cards.clear()

        for section, want in (("Загруженные модели", True), ("Доступны для загрузки", False)):
            rows = [e for e in entries if bool(e["downloaded"]) is want]
            if not rows:
                continue
            title = QLabel(section)
            title.setObjectName("section_title")
            v.addSpacing(4)
            v.addWidget(title)
            for e in rows:
                card = ModelCard(e)
                card.activate_requested.connect(self._activate)
                card.download_requested.connect(self._download)
                card.delete_requested.connect(self._delete)
                card.forget_requested.connect(self._forget_custom)
                self._cards[e["spec"]] = card
                v.addWidget(card)
                if e["spec"] in self._downloading:
                    card.show_progress(0, 0)
        v.addStretch()
        # T-269: `QScrollArea.setWidget` удаляет прежний виджет НЕМЕДЛЕННО, вместе
        # со всеми карточками — а вызвать нас могли изнутри клика по одной из них.
        # Снимаем старый контейнер сами, глушим его сигналы (отложенный клик по
        # мёртвой карточке не должен второй раз дёрнуть `_activate`) и отдаём Qt на
        # `deleteLater` — объекты доживут до конца текущего события.
        old = self._scroll.takeWidget()
        if old is not None:
            for card in old.findChildren(ModelCard):
                card.blockSignals(True)
            old.setParent(None)
            old.deleteLater()
        self._scroll.setWidget(body)
        total = 0
        try:
            total = engine.cached_total_bytes()
        except Exception:
            pass
        self.disk_label.setText(f"Модели занимают {engine.fmt_bytes(total)} · {engine.models_root()}")

    # --- действия ---
    def _activate(self, spec: str) -> None:
        if self._locked:
            QMessageBox.information(
                self, "Модель",
                "Идёт запись или транскрипция — сменить модель нельзя. Останови и попробуй снова.",
            )
            return
        if spec in self._downloading:
            return
        if not engine.is_cached(spec):
            self._download(spec)
            return
        self._active_spec = spec
        if spec in engine.PRESET_KEYS:
            self._current["model"] = spec
            self._current["custom_model"] = ""
        else:
            self._current["model"] = engine.CUSTOM_KEY
            self._current["custom_model"] = spec
        save_settings_dict(self._current)
        save_custom_models(self._customs)
        self._rebuild()

    def _download(self, spec: str) -> None:
        if spec in self._downloading:
            return
        self._downloading.add(spec)
        card = self._cards.get(spec)
        if card is not None:
            card.show_progress(0, 0)

        def _worker() -> None:
            try:
                engine.ensure_downloaded(
                    spec,
                    progress_cb=lambda d, t: self._dl_progress.emit(spec, int(d), int(t)),
                )
                self._dl_finished.emit(spec, "")
            except Exception as exc:
                self._dl_finished.emit(spec, f"{exc.__class__.__name__}: {exc}")

        threading.Thread(target=_worker, daemon=True, name=f"model-dl-{spec}").start()

    @Slot(str, int, int)
    def _on_dl_progress(self, spec: str, done: int, total: int) -> None:
        card = self._cards.get(spec)
        if card is not None:
            card.show_progress(done, total)

    @Slot(str, str)
    def _on_dl_finished(self, spec: str, error: str) -> None:
        self._downloading.discard(spec)
        if error:
            self._rebuild()
            QMessageBox.warning(
                self, "Скачивание модели",
                f"{engine.spec_display(spec)} не скачалась:\n\n{error}",
            )
            return
        self._activate(spec) if not self._locked else self._rebuild()

    def _delete(self, spec: str) -> None:
        repo = (engine.repo_for_spec(spec) or "").lower()
        entries = [e for e in engine.list_cached_models() if e["repo"].lower() == repo]
        if not entries:
            QMessageBox.information(self, "Модель", "Веса не найдены на диске — удалять нечего.")
            self._rebuild()
            return
        total = sum(e["bytes"] for e in entries)
        ans = QMessageBox.question(
            self, "Удалить модель",
            f"Удалить веса {engine.spec_display(spec)} и освободить {engine.fmt_bytes(total)}?\n"
            "При следующем выборе модель скачается заново.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        errors = [msg for ok, msg in (engine.delete_cached_model(e["path"]) for e in entries) if not ok]
        self._rebuild()
        if errors:
            QMessageBox.warning(self, "Модель", "Не всё удалилось:\n" + "\n".join(errors))

    def _forget_custom(self, spec: str) -> None:
        self._customs = [c for c in self._customs if c.lower() != spec.lower()]
        save_custom_models(self._customs)
        if (self._current.get("custom_model") or "").lower() == spec.lower():
            self._current["custom_model"] = ""
        save_settings_dict(self._current)
        self._rebuild()

    def _add_custom(self) -> None:
        dlg = _AddCustomModelDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        spec = dlg.spec()
        if spec.lower() not in {c.lower() for c in self._customs}:
            self._customs.append(spec)
            save_custom_models(self._customs)
        self._rebuild()

    def result_settings(self) -> dict:
        """Итоговые настройки (их MainWindow отдаёт в settings_changed)."""
        return dict(self._current)


class StatsDialog(QDialog):
    """Агрегаты транскрипций по `_stats.jsonl` (последние 100 записей).

    Дизайн по Claude Design v2:
    - Mode chip-set (3 круглых chip с цветной точкой): Все режимы / Только full / Только streaming
    - Stats rows (mono values): count, avg, p50, p95, sum_duration, oldest, device
    - Underline tabs: Тренд (line + rolling) / По бакетам (bar)
    - Footer hairline + footnote с T-133 tag

    На смену фильтра — `_rebuild(mode_filter)` пересоздаёт content_widget.
    """

    _CHIP_DOTS = {"all": "#A1A1AA", "full": "#2563EB", "streaming": "#F59E0B"}

    def __init__(self, parent: QMainWindow | None, stats_path: Path) -> None:
        super().__init__(parent)
        self.setWindowTitle("Статистика iWhisper")
        self.setMinimumSize(800, 740)
        # Dialog-локальный стиль (chip, stats-row, tab, footer hairline)
        self.setStyleSheet(
            "QDialog { background-color: #F6F6F7; }"
            " QLabel { background: transparent; color: #18181B;"
            "   font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif; }"
            " QLabel#mode_title { font-size: 11px; font-weight: 600; color: #A1A1AA;"
            "   letter-spacing: 0.1em; }"
            " QLabel#mode_hint { font-size: 11px; color: #A1A1AA; }"
            " QLabel#stats_row_k { font-size: 12px; color: #A1A1AA;"
            "   font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif; }"
            " QLabel#stats_row_v { font-size: 13px; font-weight: 500; color: #18181B;"
            "   font-family: 'Cascadia Mono','Cascadia Code','Consolas',monospace;"
            "   letter-spacing: 0.5px; }"
            " QLabel#stats_footnote { font-size: 11px; color: #A1A1AA; }"
            # Chips — active через ТЁМНУЮ ОБВОДКУ (QSS `:checked`
            # с background не применялся стабильно из-за native-style override).
            # Цвет текста всегда #18181B; active = border #18181B + font-weight 600.
            " QPushButton#chip {"
            "   background-color: #FFFFFF;"
            "   border: 1px solid #E7E7EA;"
            "   border-radius: 13px;"
            "   color: #18181B; font-size: 12px; font-weight: 500;"
            "   min-height: 26px; max-height: 26px; padding: 0 12px 0 10px;"
            "   font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif;"
            "   text-align: left;"
            "   outline: none; }"
            " QPushButton#chip:hover {"
            "   background-color: #F4F4F5;"
            "   border: 1px solid #D4D4D8; }"
            " QPushButton#chip:checked {"
            "   background-color: #FFFFFF;"
            "   border: 1px solid #18181B;"
            "   font-weight: 600; }"
            " QPushButton#chip:checked:hover {"
            "   background-color: #F4F4F5;"
            "   border: 1px solid #18181B;"
            "   font-weight: 600; }"
            " QPushButton#tab {"
            "   background: transparent; border: none; border-bottom: 2px solid transparent;"
            "   padding: 0 14px; color: #A1A1AA; font-size: 13px; font-weight: 500;"
            "   min-height: 32px; max-height: 32px;"
            "   font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif; }"
            " QPushButton#tab:hover { color: #18181B; }"
            " QPushButton#tab:checked {"
            "   color: #18181B; border-bottom: 2px solid #18181B; }"
            " QWidget#stats_card {"
            "   background: #FFFFFF; border: 1px solid #E7E7EA; border-radius: 8px; }"
            # По CSS `.stats-footer` — НЕТ border-top (в отличие от `.settings-footer`).
            " QWidget#stats_footer { background: transparent; border: none; }"
        )

        self._stats_path = stats_path
        self._recent_all, self._oldest = read_stats_jsonl(stats_path, last_n=100)
        # Текущая активная вкладка (trend / buckets). Сохраняется через смены фильтра.
        self._active_tab: str = "trend"

        # Корневой layout: body (scroll-friendly) + footer
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._body = QWidget()
        self._body.setStyleSheet("background: transparent;")
        self._body_v = QVBoxLayout(self._body)
        # По CSS `.stats-body { padding: 18px 20px 0; gap: 14px }` — снизу 0 (footer прижат).
        self._body_v.setContentsMargins(20, 18, 20, 0)
        self._body_v.setSpacing(14)
        outer.addWidget(self._body, 1)

        if not self._recent_all:
            empty = QLabel(
                "Пока нет записей в _stats.jsonl.\n"
                "Сделай 2-3 транскрипции через hotkey — здесь появятся агрегаты."
            )
            empty.setWordWrap(True)
            empty.setStyleSheet("color: #71717A; padding: 12px 0;")
            self._body_v.addWidget(empty)
            self._body_v.addStretch()
            outer.addWidget(self._build_footer())
            return

        # === Mode chip-set (T-133) — заменил QRadioButton ===
        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.setSpacing(14)
        mode_title = QLabel("РЕЖИМ")
        mode_title.setObjectName("mode_title")
        mode_row.addWidget(mode_title)

        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        # Цветной dot — pixmap 7×7 для setIcon.
        def _dot_pix(color: str) -> QPixmap:
            pix = QPixmap(7, 7)
            pix.fill(Qt.transparent)
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setBrush(QColor(color))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(0, 0, 7, 7)
            painter.end()
            return pix

        # Hook на toggled — force unpolish/polish для applies QSS `:checked` background.
        # В Qt без этого `:checked` background иногда не перерисуется (особенно когда
        # QSS заявлен на parent dialog с большим количеством объект-имён).
        def _refresh_chip_style(_checked: bool, c: QPushButton) -> None:
            c.style().unpolish(c)
            c.style().polish(c)
            c.update()

        for i, (key, label) in enumerate(MODE_FILTERS):
            chip = QPushButton(label)
            chip.setObjectName("chip")
            chip.setCursor(Qt.PointingHandCursor)
            chip.setCheckable(True)
            chip.setProperty("mode_key", key)
            chip.setIcon(QIcon(_dot_pix(self._CHIP_DOTS.get(key, "#A1A1AA"))))
            chip.setIconSize(QSize(7, 7))
            # Fixed sizePolicy → Qt не сжимает chip ниже sizeHint (текст не обрезается с «…»)
            chip.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            chip.toggled.connect(lambda checked, c=chip: _refresh_chip_style(checked, c))
            if i == 0:
                chip.setChecked(True)
            # Force initial polish (чтобы первый чип с :checked сразу отрисовался чёрным)
            _refresh_chip_style(chip.isChecked(), chip)
            mode_row.addWidget(chip)
            self._filter_group.addButton(chip, i)
        mode_hint = QLabel("фильтр агрегатов и графиков")
        mode_hint.setObjectName("mode_hint")
        mode_row.addWidget(mode_hint)
        mode_row.addStretch(1)
        self._filter_group.buttonClicked.connect(self._on_filter_changed)
        self._body_v.addLayout(mode_row)

        # Контейнер content_widget вставляется через insertWidget — индекс
        # ПОСЛЕ mode_row (которая занимает 1 layout-item, т.е. index 1).
        self._content_widget: QWidget | None = None
        self._content_index = self._body_v.count()  # 1

        # === Footer (chrome) ===
        outer.addWidget(self._build_footer())

        # Первая отрисовка с дефолтным фильтром "all"
        self._rebuild("all")

    def _build_footer(self) -> QWidget:
        """Footer: только footnote (без tag и Close — крестик в
        title bar закрывает окно). По CSS `.stats-footer { padding: 18px 20px 20px; }`
        — БЕЗ border-top."""
        footer = QWidget()
        footer.setObjectName("stats_footer")
        row = QHBoxLayout(footer)
        row.setContentsMargins(20, 18, 20, 20)
        row.setSpacing(0)
        note = QLabel(
            "Агрегаты по последним 100 записям из _stats.jsonl. "
            "Файл не ротируется — растёт по мере работы (≈150 байт на транскрипцию)."
        )
        note.setObjectName("stats_footnote")
        note.setWordWrap(True)
        row.addWidget(note, 1)
        return footer

    @Slot()
    def _on_filter_changed(self) -> None:
        btn = self._filter_group.checkedButton()
        if btn is None:
            return
        self._rebuild(str(btn.property("mode_key")))

    def _rebuild(self, mode_filter: str) -> None:
        if self._content_widget is not None:
            self._content_widget.setParent(None)
            self._content_widget.deleteLater()
            self._content_widget = None

        records = _filter_by_mode(self._recent_all, mode_filter)
        self._content_widget = self._build_content(records, mode_filter)
        self._body_v.insertWidget(self._content_index, self._content_widget, 1)

    def _build_content(self, records: list[dict], mode_filter: str) -> QWidget:
        wrapper = QWidget()
        wrapper.setStyleSheet("background: transparent;")
        cl = QVBoxLayout(wrapper)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(12)

        agg = compute_stats_aggregates(records, self._oldest)
        dur_total = int(agg["sum_duration_sec"])
        h, rem = divmod(dur_total, 3600)
        m, s = divmod(rem, 60)
        dur_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

        # === Stats rows (mono values) — одинарные разделители через QFrame ===
        rows_box = QWidget()
        rows_box.setStyleSheet("background: transparent;")
        rows_lay = QVBoxLayout(rows_box)
        rows_lay.setContentsMargins(0, 0, 0, 0)
        rows_lay.setSpacing(0)

        def _make_row(k: str, v: str) -> QWidget:
            r = QWidget()
            r.setStyleSheet("background: transparent;")
            r_lay = QHBoxLayout(r)
            r_lay.setContentsMargins(0, 6, 0, 6)
            r_lay.setSpacing(16)
            k_lbl = QLabel(k)
            k_lbl.setObjectName("stats_row_k")
            k_lbl.setFixedWidth(180)
            r_lay.addWidget(k_lbl)
            v_lbl = QLabel(v)
            v_lbl.setObjectName("stats_row_v")
            v_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            r_lay.addWidget(v_lbl, 1)
            return r

        def _make_sep() -> QFrame:
            sep = QFrame()
            sep.setFixedHeight(1)
            sep.setStyleSheet("background-color: #F1F1F3; border: none;")
            return sep

        mode_label = (
            "оба"
            if mode_filter == "all"
            else ("только full" if mode_filter == "full" else "только streaming")
        )
        # Собираем как список (k, v), потом добавляем строки + separators (одинарные).
        data_rows: list[tuple[str, str]] = [
            ("Записей в окне", f"{agg['count']}  ·  {mode_label}"),
            ("Средний ratio", f"{agg['avg_ratio']:.2f}×"),
            ("Медиана ratio (p50)", f"{agg['p50_ratio']:.2f}×"),
            ("p95 ratio", f"{agg['p95_ratio']:.2f}×"),
            ("Суммарная длительность", f"{dur_str}  ·  {dur_total} сек"),
            ("Самая старая запись", agg["oldest_ts"] or "—"),
        ]
        devices = agg["devices"]
        if devices:
            total = sum(devices.values())
            top_dev, top_n = max(devices.items(), key=lambda x: x[1])
            pct = top_n * 100 // total if total else 0
            data_rows.append(("Устройство", f"{top_dev}  ·  {top_n} ({pct}%)"))
        for i, (k, v) in enumerate(data_rows):
            rows_lay.addWidget(_make_row(k, v))
            if i < len(data_rows) - 1:
                rows_lay.addWidget(_make_sep())
        cl.addWidget(rows_box)

        # === Underline tabs (Тренд / По бакетам) ===
        tabs_row = QHBoxLayout()
        tabs_row.setContentsMargins(0, 6, 0, 0)
        tabs_row.setSpacing(4)
        tab_group = QButtonGroup(wrapper)
        tab_group.setExclusive(True)
        self._tab_buttons: dict[str, QPushButton] = {}
        for key, label in (("trend", "Тренд"), ("buckets", "По бакетам")):
            tb = QPushButton(label)
            tb.setObjectName("tab")
            tb.setCheckable(True)
            tb.setCursor(Qt.PointingHandCursor)
            tb.setProperty("tab_key", key)
            tabs_row.addWidget(tb)
            tab_group.addButton(tb)
            self._tab_buttons[key] = tb
        tabs_row.addStretch(1)
        # Hairline под tabs (border-bottom: 1px solid #E7E7EA) — даёт underline-acc visual
        # Реализуем через QFrame высотой 1px ПОСЛЕ tabs_row
        cl.addLayout(tabs_row)
        tabs_underline = QFrame()
        tabs_underline.setFixedHeight(1)
        tabs_underline.setStyleSheet("background-color: #E7E7EA; margin-top: -1px;")
        cl.addWidget(tabs_underline)

        # === Chart card ===
        # По CSS `.chartwrap { padding: 16px 16px 12px; }` (Qt margins L,T,R,B)
        chart_card = QWidget()
        chart_card.setObjectName("stats_card")
        chart_lay = QVBoxLayout(chart_card)
        chart_lay.setContentsMargins(16, 16, 16, 12)
        chart_lay.setSpacing(8)

        trend_view = QChartView(_build_trend_chart(records, mode_filter))
        trend_view.setRenderHint(QPainter.Antialiasing)
        buckets_view = QChartView(_build_buckets_chart(records, mode_filter))
        buckets_view.setRenderHint(QPainter.Antialiasing)
        chart_lay.addWidget(trend_view, 1)
        chart_lay.addWidget(buckets_view, 1)
        cl.addWidget(chart_card, 1)

        def _select_tab(key: str) -> None:
            self._active_tab = key
            trend_view.setVisible(key == "trend")
            buckets_view.setVisible(key == "buckets")
            for k, btn in self._tab_buttons.items():
                btn.setChecked(k == key)

        for key, btn in self._tab_buttons.items():
            btn.clicked.connect(lambda _checked, k=key: _select_tab(k))
        _select_tab(self._active_tab)
        return wrapper


class _CallTranscriptDialog(QDialog):
    """T-172: нерезидентный диалог с транскриптом созвона (read-only + копировать).

    Формат транскрипта созвона (speaker-attributed .md из transcribe_call.py)
    не совпадает с форматом истории диктовки, поэтому показываем отдельным окном,
    а не в text_view главного окна. Немодальный — не блокирует диктовку/окно.
    """

    def __init__(self, parent: QWidget | None, text: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("Транскрипт созвона")
        self.resize(680, 560)
        # Окно живёт независимо от родителя и убирается при закрытии (не копится в памяти)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self._text = text or ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setPlainText(self._text)
        self._view.setStyleSheet(
            "QPlainTextEdit {"
            " background-color: #FFFFFF; color: #18181B;"
            " border: 1px solid #E7E7EA; border-radius: 8px; padding: 10px;"
            " font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif; font-size: 13px;"
            "}"
        )
        lay.addWidget(self._view, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch()
        self._copy_btn = QPushButton("Скопировать")
        self._copy_btn.setCursor(Qt.PointingHandCursor)
        self._copy_btn.clicked.connect(self._copy)
        btn_row.addWidget(self._copy_btn)
        self._close_btn = QPushButton("Закрыть")
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.clicked.connect(self.close)
        btn_row.addWidget(self._close_btn)
        lay.addLayout(btn_row)

    def _copy(self) -> None:
        from PySide6.QtCore import QTimer
        QApplication.clipboard().setText(self._text)
        self._copy_btn.setText("Скопировано ✓")
        QTimer.singleShot(1800, lambda: self._copy_btn.setText("Скопировать"))


class MainWindow(QMainWindow):
    settings_changed = Signal(dict)
    quit_requested = Signal()
    transcription_done = Signal()  # эмитится из transcribe_ui.py thread'а для auto-refresh
    _state_change_requested = Signal(str)  # thread-safe мост для set_state из worker thread'ов
    _audio_level_requested = Signal(float)  # thread-safe мост для эквалайзера из sd callback
    # T-172: thread-safe мосты для режима записи созвона (вызовы из worker/watchdog потоков)
    _call_state_requested = Signal(bool)      # active: True/False — вид кнопки созвона
    _call_transcript_requested = Signal(str)  # готовый транскрипт созвона (тело .md) или текст ошибки
    _call_alert_requested = Signal(bool)      # silent: True/False — статус «системный звук не обнаружен»
    # T-173: индикатор обработки созвона после стопа (блок C)
    _call_processing_requested = Signal(bool, float)  # (active, total_audio_sec)
    _call_progress_requested = Signal(float)          # грубый прогресс транскрипта 0..1

    def __init__(
        self,
        *,
        idle_icon_path: Path,
        recording_icon_path: Path,
        processing_icon_path: Path | None = None,
        app_icon_path: Path | None = None,
        history_dir_getter: Callable[[], Path],
        rotation_count_getter: Callable[[], int],
        entry_script: Path,
        toggle_recording_via_ui: Callable[[], None] | None = None,
        toggle_call_via_ui: Callable[[], None] | None = None,
        model_busy_getter: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__()
        self._model_busy_getter = model_busy_getter  # T-259: занят ли движок прямо сейчас
        self.setWindowTitle("iWhisper")
        self.resize(760, 620)

        self._idle_icon = QIcon(str(idle_icon_path))
        self._recording_icon = QIcon(str(recording_icon_path))
        self._processing_icon = (
            QIcon(str(processing_icon_path)) if processing_icon_path else QIcon(str(idle_icon_path))
        )
        self._app_icon = QIcon(str(app_icon_path)) if app_icon_path else self._idle_icon
        self.setWindowIcon(self._app_icon)  # для заголовка окна / taskbar

        # === Глобальный стиль приложения (Claude Design tokens, iwhisper.css v2) ===
        # Тёплая нейтральная палитра. Хардкод hex, QSS без переменных.
        # Шрифт — Segoe UI Variable Display (Win11 native), fallback Segoe UI.
        self.setStyleSheet("""
            QMainWindow, QWidget#central_widget {
                background-color: #F6F6F7;
                color: #18181B;
                font-family: 'Segoe UI Variable Display', 'Segoe UI', sans-serif;
            }
            QLabel { background: transparent; color: #18181B; }

            /* Primary action button (idle) — pill */
            QPushButton {
                background-color: #FFFFFF;
                color: #18181B;
                border: 1px solid #E7E7EA;
                border-radius: 22px;
                padding: 0 16px;
                font-size: 14px;
                font-weight: 500;
                font-family: 'Segoe UI Variable Display', 'Segoe UI', sans-serif;
            }
            QPushButton:hover    { background-color: #F4F4F5; border-color: #D4D4D8; }
            QPushButton:pressed  { background-color: #EDEDF0; border-color: #D4D4D8; }
            QPushButton:disabled { background-color: #FAFAFA; color: #C4C4C8; }

            /* Icon-button 30x30 (history copy/play) — круг (radius: 50%) */
            QPushButton#icon_btn {
                background-color: #FFFFFF;
                border: 1px solid #E7E7EA;
                border-radius: 15px;
                color: #52525B;
                padding: 0;
                min-width: 30px; max-width: 30px;
                min-height: 30px; max-height: 30px;
            }
            QPushButton#icon_btn:hover   { background-color: #F4F4F5; color: #18181B; border-color: #D4D4D8; }
            QPushButton#icon_btn:pressed { background-color: #EDEDF0; }

            /* Settings / folder / analytics — icon-only 44×44 круглые */
            QPushButton#btn_settings {
                background-color: #FFFFFF;
                border: 1px solid #E7E7EA;
                border-radius: 22px;
                min-width: 44px; max-width: 44px;
                min-height: 44px; max-height: 44px;
                padding: 0;
            }
            QPushButton#btn_settings:hover   { background-color: #F4F4F5; border-color: #D4D4D8; }
            QPushButton#btn_settings:pressed { background-color: #EDEDF0; }

            /* Player icon button — круг 36x36 (play/stop в верхней player-зоне) */
            QPushButton#icon_btn_player {
                background-color: #FFFFFF;
                border: 1px solid #E7E7EA;
                border-radius: 18px;
                padding: 0;
                min-width: 36px; max-width: 36px;
                min-height: 36px; max-height: 36px;
            }
            QPushButton#icon_btn_player:hover   { background-color: #F4F4F5; border-color: #D4D4D8; }
            QPushButton#icon_btn_player:pressed { background-color: #EDEDF0; }

            /* Slider — Win11-ish скруб */
            QSlider::groove:horizontal {
                height: 4px; background: #E4E4E7; border-radius: 2px;
            }
            QSlider::sub-page:horizontal { background: #18181B; border-radius: 2px; }
            QSlider::handle:horizontal {
                width: 12px; height: 12px; margin: -4px 0;
                background: #18181B; border-radius: 6px;
            }

            /* Scrollbar — тонкий, аккуратный */
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical {
                background: transparent; width: 10px; margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #D4D4D8; border-radius: 5px; min-height: 30px;
                border: 2px solid #F6F6F7;
            }
            QScrollBar::handle:vertical:hover { background: #A1A1AA; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; background: transparent; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        """)

        self._history_dir_getter = history_dir_getter
        self._rotation_count_getter = rotation_count_getter
        self._entry_script = entry_script
        self._toggle_recording_via_ui = toggle_recording_via_ui
        self._toggle_call_via_ui = toggle_call_via_ui
        self._call_active = False  # текущий режим записи созвона (для вида кнопки)
        self._call_processing = False  # T-173 C: идёт обработка созвона после стопа
        self._call_rec_started: float = 0.0
        self._call_proc_started: float = 0.0
        self._call_dialog: "_CallTranscriptDialog | None" = None
        self._entries: list[dict] = []
        self._user_quit = False
        self._state = "idle"
        self._current_row = -1
        # Секундомер записи: QTimer каждую секунду обновляет таймер в кнопке + overlay
        from PySide6.QtCore import QTimer
        from datetime import datetime as _dt
        self._rec_timer = QTimer(self)
        self._rec_timer.setInterval(1000)
        self._rec_started_at: float = 0.0
        self._rec_timer.timeout.connect(self._tick_rec_time)  # idle | recording | processing

        self._build_central()
        self._build_tray()
        self._overlay = StatusOverlay()
        self._call_overlay = _CallStatusOverlay()  # T-173 B+C: синий индикатор созвона
        self._call_timer = QTimer(self)
        self._call_timer.setInterval(1000)
        self._call_timer.timeout.connect(self._tick_call_time)
        self.transcription_done.connect(self.refresh_history)
        self._state_change_requested.connect(self.set_state)  # main-thread доставка
        # эквалайзер обновляется в overlay И в primary action button
        self._audio_level_requested.connect(self._on_audio_level)
        # T-172: мосты режима созвона — доставка в main thread
        self._call_state_requested.connect(self._on_call_state)
        self._call_transcript_requested.connect(self._on_call_transcript)
        self._call_alert_requested.connect(self._on_call_alert)
        self._call_processing_requested.connect(self._on_call_processing)
        self._call_progress_requested.connect(self._on_call_progress)
        self.refresh_history()

    @Slot(float)
    def _on_audio_level(self, level: float) -> None:
        self._overlay.update_audio_level(level)
        if hasattr(self, "action_btn"):
            self.action_btn.update_audio_level(level)

    @Slot()
    def notify_transcription_done(self) -> None:
        """Thread-safe способ дёрнуть refresh из transcribe_ui.py worker thread'а."""
        self.transcription_done.emit()

    def notify_set_state(self, state: str) -> None:
        """Thread-safe смена state. Qt widgets — main-thread-only; emit() с
        QueuedConnection доставит slot в main thread'е."""
        self._state_change_requested.emit(state)

    def notify_audio_level(self, level: float) -> None:
        """Thread-safe обновление эквалайзера. Вызывается из sounddevice
        callback'а (отдельный thread). Throttle на стороне вызывающего."""
        self._audio_level_requested.emit(level)

    # === T-172: thread-safe API режима записи созвона (emit из worker/watchdog) ===
    def notify_call_state(self, active: bool) -> None:
        """Thread-safe смена вида кнопки созвона (idle/активна)."""
        self._call_state_requested.emit(bool(active))

    def notify_call_transcript(self, text: str) -> None:
        """Thread-safe показ транскрипта созвона (тело .md) или сообщения об ошибке."""
        self._call_transcript_requested.emit(text)

    def notify_call_alert(self, silent: bool) -> None:
        """Thread-safe короткий статус «системный звук не обнаружен» (watchdog)."""
        self._call_alert_requested.emit(bool(silent))

    def notify_call_processing(self, active: bool, total_sec: float = 0.0) -> None:
        """Thread-safe (T-173 C): началась/закончилась обработка созвона после стопа."""
        self._call_processing_requested.emit(bool(active), float(total_sec))

    def notify_call_progress(self, fraction: float) -> None:
        """Thread-safe (T-173 C): грубый прогресс транскрипции созвона (0..1)."""
        self._call_progress_requested.emit(float(fraction))

    @Slot(bool)
    def _on_call_state(self, active: bool) -> None:
        """Main thread: индикатор записи созвона (T-173 B) + вид кнопки. active →
        синий overlay «Запись созвона» + таймер + tray + синяя кнопка; idle →
        полная очистка (этот сигнал приходит последним: стоп → обработка → конец)."""
        import time as _time
        self._call_active = active
        if active:
            self._call_processing = False
            self._call_rec_started = _time.monotonic()
            self._call_overlay.show_recording()
            self._call_overlay.update_time(0)
            if not self._call_timer.isActive():
                self._call_timer.start()
            self._tray.setIcon(self._recording_icon)
            self._tray.setToolTip("iWhisper · запись созвона")
        else:
            self._call_processing = False
            self._call_timer.stop()
            self._call_overlay.hide_overlay()
            # tray в idle только если прямо сейчас не идёт диктовка (у неё свой set_state)
            if self._state not in ("recording", "processing"):
                self._tray.setIcon(self._idle_icon)
                self._tray.setToolTip("iWhisper · готов")
        if hasattr(self, "call_btn"):
            if active:
                self.call_btn.setIcon(self._ico_phone_blue)
                self.call_btn.setToolTip("● Идёт запись созвона — Ctrl+Shift+E чтобы остановить")
                self.call_btn.setStyleSheet(
                    "QPushButton#btn_settings {"
                    " background-color: #EFF6FF; border: 1px solid #2563EB; border-radius: 22px;"
                    " min-width: 44px; max-width: 44px; min-height: 44px; max-height: 44px; padding: 0;"
                    "}"
                    "QPushButton#btn_settings:hover { background-color: #DBEAFE; }"
                )
            else:
                self.call_btn.setIcon(self._ico_phone)
                self.call_btn.setToolTip("Запись созвона (Ctrl+Shift+E)")
                self.call_btn.setStyleSheet("")  # вернуться к глобальному btn_settings стилю

    @Slot(bool, float)
    def _on_call_processing(self, active: bool, total_sec: float) -> None:
        """Main thread (T-173 C): индикатор обработки созвона после стопа.
        active=True → синий spinner + таймер обработки; False → скрыть (полную
        очистку кнопки/трея делает завершающий _on_call_state(False))."""
        import time as _time
        self._call_processing = active
        if active:
            self._call_proc_started = _time.monotonic()
            self._call_overlay.show_processing()
            self._call_overlay.update_time(0)
            if not self._call_timer.isActive():
                self._call_timer.start()
            self._tray.setIcon(self._processing_icon)
            self._tray.setToolTip("iWhisper · обработка созвона…")
        else:
            self._call_overlay.hide_overlay()

    @Slot(float)
    def _on_call_progress(self, fraction: float) -> None:
        """Main thread (T-173 C): грубый прогресс транскрипции созвона (0..1)."""
        if self._call_processing:
            self._call_overlay.update_progress(fraction)

    @Slot()
    def _tick_call_time(self) -> None:
        """Секундомер созвона: обработки (C) или записи (B)."""
        import time as _time
        if self._call_processing:
            self._call_overlay.update_time(int(_time.monotonic() - self._call_proc_started))
        elif self._call_active:
            self._call_overlay.update_time(int(_time.monotonic() - self._call_rec_started))
        else:
            self._call_timer.stop()

    @Slot(str)
    def _on_call_transcript(self, text: str) -> None:
        """Main thread: показать транскрипт созвона в отдельном нерезидентном диалоге
        и обновить историю — в ней появляется карточка-созвон (T-173 A)."""
        try:
            self._call_dialog = _CallTranscriptDialog(self, text)
            self._call_dialog.show()  # немодальный — не блокирует диктовку/окно
            self._call_dialog.raise_()
            self._call_dialog.activateWindow()
        except Exception:
            # fallback на простой messagebox, если диалог не построился
            QMessageBox.information(self, "Транскрипт созвона", text[:4000])
        try:
            self.refresh_history()  # T-173 A: подтянуть новую карточку-созвон
        except Exception:
            pass

    @Slot(bool)
    def _on_call_alert(self, silent: bool) -> None:
        """Main thread: короткий статус тишины loopback в tooltip кнопки.
        Меняем только подсказку (не трогаем иконку), чтобы не мигало при
        кратких паузах. Полный вид (красный/idle) управляется _on_call_state."""
        if not hasattr(self, "call_btn") or not self._call_active:
            return
        if silent:
            self.call_btn.setToolTip("● Запись созвона · системный звук не обнаружен (проверь источник)")
        else:
            self.call_btn.setToolTip("● Идёт запись созвона — Ctrl+Shift+E чтобы остановить")

    def _build_central(self) -> None:
        central = QWidget()
        central.setObjectName("central_widget")
        # === Outer layout БЕЗ паддингов: каждая секция управляет своим отступом ===
        # Это даёт полноширинный плеер-футер и separator'ы от края до края.
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Векторные иконки в едином стиле Claude Design (stroke 2px)
        self._ico_mic = _mk_icon(_draw_mic_icon, 18, "#18181B")
        self._ico_mic_red = _mk_icon(_draw_mic_icon, 18, "#DC2626")
        self._ico_stop_red = _mk_icon(_draw_stop, 18, "#DC2626")
        self._ico_gear = _mk_icon(_draw_gear, 22, "#52525B")
        self._ico_chart = _mk_icon(_draw_chart, 22, "#52525B")
        self._ico_chip = _mk_icon(_draw_chip, 22, "#52525B")  # T-259: раздел «Модели»
        self._ico_folder = _mk_icon(_draw_folder, 14, "#52525B")
        self._ico_play = _mk_icon(_draw_play, 14, "#52525B")
        self._ico_pause = _mk_icon(_draw_pause, 14, "#52525B")
        self._ico_stop_player = _mk_icon(_draw_stop, 14, "#52525B")

        # === Top section: action + folder + analytics + settings — 44px icon-кнопки рядом ===
        # По CSS `.actions { padding: 16px 20px 8px 20px; gap: 12px }` — Qt margins (L,T,R,B).
        top_bar = QWidget()
        top_bar.setStyleSheet("background: transparent;")
        top_row = QHBoxLayout(top_bar)
        top_row.setContentsMargins(20, 16, 20, 8)
        top_row.setSpacing(12)

        self.action_btn = _PrimaryActionButton(
            on_click=self._on_record_clicked,
        )
        top_row.addWidget(self.action_btn, 1)

        # T-172: кнопка записи созвона — 44×44 icon-кнопка (тот же стиль btn_settings).
        # Серая телефон-иконка в idle, красная при активной записи (см. _on_call_state).
        self._ico_phone = _mk_icon(_draw_phone, 18, "#52525B")
        self._ico_phone_red = _mk_icon(_draw_phone, 18, "#DC2626")
        self._ico_phone_blue = _mk_icon(_draw_phone, 18, "#2563EB")  # T-173: активная запись созвона (синий)
        self.call_btn = QPushButton()
        self.call_btn.setObjectName("btn_settings")  # переиспользуем круглый 44×44 стиль
        self.call_btn.setIcon(self._ico_phone)
        self.call_btn.setIconSize(QSize(18, 18))
        self.call_btn.setToolTip("Запись созвона (Ctrl+Shift+E)")
        self.call_btn.clicked.connect(
            lambda: self._toggle_call_via_ui and self._toggle_call_via_ui()
        )
        top_row.addWidget(self.call_btn)

        # Иконка папки 18px для большой кнопки (раньше 14px для ghost)
        self._ico_folder_lg = _mk_icon(_draw_folder, 18, "#52525B")

        self.open_folder_btn = QPushButton()
        self.open_folder_btn.setObjectName("btn_settings")  # переиспользуем стиль 44x44
        self.open_folder_btn.setIcon(self._ico_folder_lg)
        self.open_folder_btn.setIconSize(QSize(18, 18))
        self.open_folder_btn.setToolTip("Открыть папку с историей")
        self.open_folder_btn.clicked.connect(self._open_history_folder)
        top_row.addWidget(self.open_folder_btn)

        self.stats_btn = QPushButton()
        self.stats_btn.setObjectName("btn_settings")  # тот же 44×44 стиль
        self.stats_btn.setIcon(self._ico_chart)
        self.stats_btn.setIconSize(QSize(18, 18))
        self.stats_btn.setToolTip("Статистика транскрипций")
        self.stats_btn.clicked.connect(self._open_stats)
        top_row.addWidget(self.stats_btn)

        # T-259: раздел «Модели» — между статистикой и настройками (как в Handy)
        self.models_btn = QPushButton()
        self.models_btn.setObjectName("btn_settings")
        self.models_btn.setIcon(self._ico_chip)
        self.models_btn.setIconSize(QSize(18, 18))
        self.models_btn.setToolTip("Модели транскрипции")
        self.models_btn.clicked.connect(self.open_models)
        top_row.addWidget(self.models_btn)

        self.settings_btn = QPushButton()
        self.settings_btn.setObjectName("btn_settings")
        self.settings_btn.setIcon(self._ico_gear)
        self.settings_btn.setIconSize(QSize(18, 18))
        self.settings_btn.setToolTip("Настройки")
        self.settings_btn.clicked.connect(self.open_settings)
        top_row.addWidget(self.settings_btn)
        layout.addWidget(top_bar)

        # === Тонкая разделительная линия full-width под top bar ===
        # Карточки скролла уходят под эту линию при скролле сверху.
        sep_top = QFrame()
        sep_top.setFrameShape(QFrame.NoFrame)
        sep_top.setFixedHeight(1)
        sep_top.setStyleSheet("background-color: #E7E7EA;")
        layout.addWidget(sep_top)

        # === Скролл-список карточек (full-width, padding внутри 20/12/10/12) ===
        # right=10 чтобы card.right выровнялся с правым краем top buttons.
        # Scrollbar AlwaysOn — место под него зарезервировано всегда.
        self._history_scroll = QScrollArea()
        self._history_scroll.setWidgetResizable(True)
        self._history_scroll.setFrameShape(QFrame.NoFrame)
        self._history_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self._history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._history_scroll.setStyleSheet(
            "QScrollArea { background-color: transparent; border: none; }"
        )
        self._history_container = QWidget()
        self._history_container.setStyleSheet("background-color: transparent;")
        self._history_layout = QVBoxLayout(self._history_container)
        # По CSS `.history { padding: 14px 20px 16px 20px; gap: 10px }`. right=10 учитывает
        # 10px scrollbar (ScrollBarAlwaysOn) — visual card.right выровнен с player/top-bar.
        self._history_layout.setContentsMargins(20, 14, 10, 16)
        self._history_layout.setSpacing(10)

        self._empty_widget = _build_empty_widget()
        self._history_layout.addWidget(self._empty_widget)
        self._empty_widget.hide()

        self._history_layout.addStretch()
        self._history_scroll.setWidget(self._history_container)
        layout.addWidget(self._history_scroll, 1)
        self._cards: list[HistoryCard] = []
        self._call_cards: list = []          # T-173 A: карточки созвонов (CallHistoryCard)
        self._call_entries: list[dict] = []  # T-173 A: распарсенные .md из Calls\

        # === Player — компактная control-зона ВВЕРХУ под action row (Claude Design v2).
        # Высота 40px, прозрачный — выглядит как часть control-зоны, не как footer.
        # Разделитель между control-зоной и history живёт на самой history (border-top
        # хайрлайн добавлен ниже на _history_container через объект-имя).
        player_bar = QWidget()
        player_bar.setObjectName("playerBar")
        player_bar.setStyleSheet(
            "#playerBar { background-color: transparent; border: none; }"
        )
        # Высота bar = 64, top 8 / bottom 16 → content area 40 (запас
        # 2px сверху/снизу под кнопку 36 — без среза от exact match). Bottom 16 = такое
        # же расстояние до hairline, как от верха окна до кнопки «Записать».
        player_bar.setFixedHeight(64)
        player_row = QHBoxLayout(player_bar)
        player_row.setContentsMargins(20, 8, 20, 16)
        player_row.setSpacing(20)
        # Иконки 16px — плотнее чем были 14px, чтобы кнопки визуально не выглядели «пустыми»
        # (как у top buttons 20×20 в 44×44)
        self._ico_play_18 = _mk_icon(_draw_play, 16, "#18181B")
        self._ico_pause_18 = _mk_icon(_draw_pause, 16, "#18181B")
        self._ico_stop_player_18 = _mk_icon(_draw_stop, 16, "#18181B")
        self.play_btn = QPushButton("")
        self.play_btn.setObjectName("icon_btn_player")
        self.play_btn.setIcon(self._ico_play_18)
        self.play_btn.setIconSize(QSize(16, 16))
        self.play_btn.clicked.connect(self._toggle_play)
        self.stop_btn = QPushButton("")
        self.stop_btn.setObjectName("icon_btn_player")
        self.stop_btn.setIcon(self._ico_stop_player_18)
        self.stop_btn.setIconSize(QSize(16, 16))
        self.stop_btn.clicked.connect(self._stop_play)
        self.timeline = QSlider(Qt.Horizontal)
        self.timeline.setRange(0, 0)
        self.timeline.sliderMoved.connect(self._seek)
        self.timeline.sliderReleased.connect(self._seek_release)
        self.position_label = QLabel("00:00 / 00:00")
        self.position_label.setStyleSheet(
            "color: #52525B; font-size: 12px; background: transparent;"
            " font-family: 'Cascadia Mono','Cascadia Code','Consolas',monospace;"
            " letter-spacing: 0.5px;"
        )
        self.position_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.position_label.setMinimumWidth(95)
        # `.player__ctrls { gap: 6 }` — child layout. Между ctrls/timeline/time общий 12px.
        ctrls_w = QWidget()
        ctrls_w.setStyleSheet("background: transparent;")
        ctrls_row = QHBoxLayout(ctrls_w)
        ctrls_row.setContentsMargins(0, 0, 0, 0)
        ctrls_row.setSpacing(6)
        ctrls_row.addWidget(self.play_btn)
        ctrls_row.addWidget(self.stop_btn)
        player_row.addWidget(ctrls_w)
        player_row.addWidget(self.timeline, 1)
        player_row.addWidget(self.position_label)
        # Player вставляется в layout ПОСЛЕ top_bar (index 0) и ДО sep_top.
        # Sep_top сдвинется на index 2, history_scroll на index 3 — порядок становится:
        # 0 top_bar | 1 player_bar | 2 sep_top (hairline) | 3 history_scroll
        layout.insertWidget(1, player_bar)

        self._player = QMediaPlayer(self)
        self._audio_out = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_out)
        self._player.positionChanged.connect(self._on_player_position)
        self._player.durationChanged.connect(self._on_player_duration)
        self._player.playbackStateChanged.connect(self._update_play_btn)

        # текст транскрипции теперь внутри каждой карточки в истории,
        # отдельной области не нужно. Плеер обслуживает «текущую выбранную» запись.

        self.setCentralWidget(central)

    def _build_tray(self) -> None:
        self._tray = QSystemTrayIcon(self._idle_icon, self)
        self._tray.setToolTip("faster-whisper готов · ctrl+shift+Q")
        menu = QMenu()
        open_action = QAction("Открыть окно", self)
        open_action.triggered.connect(self.show_window)
        settings_action = QAction("Настройки…", self)
        settings_action.triggered.connect(self.open_settings)
        quit_action = QAction("Выход", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(open_action)
        menu.addAction(settings_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._user_quit:
            event.accept()
            return
        event.ignore()
        self.hide()

    def show_window(self) -> None:
        self.refresh_history()
        # showNormal() гарантирует развёрнутое состояние (не minimized/maximized).
        # Без него двойной клик ярлыка иногда оставлял окно в taskbar без выноса наверх.
        self.setWindowState((self.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_window()
        elif reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_window()

    def set_state(self, state: str) -> None:
        """state: 'idle' | 'recording' | 'processing'. Статус живёт ВНУТРИ action_btn
        (layout не прыгает, как в дизайне). Toast-overlay внизу — параллельно."""
        import time as _time
        self._state = state
        self._overlay.show_state(state)
        self.action_btn.set_state(state)
        # Секундомер записи
        if state == "recording":
            self._rec_started_at = _time.monotonic()
            self.action_btn.update_time(0)
            self._overlay.update_time(0)
            self._rec_timer.start()
        else:
            self._rec_timer.stop()
        # Tray
        if state == "recording":
            self._tray.setIcon(self._recording_icon)
            self._tray.setToolTip("iWhisper · запись")
        elif state == "processing":
            self._tray.setIcon(self._processing_icon)
            self._tray.setToolTip("iWhisper · транскрибирую…")
        elif self._call_processing:
            # T-173: диктовка во время созвона вернулась в idle, но созвон ещё обрабатывается
            self._tray.setIcon(self._processing_icon)
            self._tray.setToolTip("iWhisper · обработка созвона…")
        elif self._call_active:
            self._tray.setIcon(self._recording_icon)
            self._tray.setToolTip("iWhisper · запись созвона")
        else:
            self._tray.setIcon(self._idle_icon)
            self._tray.setToolTip("iWhisper · готов")

    @Slot()
    def _tick_rec_time(self) -> None:
        import time as _time
        if self._state != "recording":
            return
        elapsed = int(_time.monotonic() - self._rec_started_at)
        self.action_btn.update_time(elapsed)
        self._overlay.update_time(elapsed)

    def _on_record_clicked(self) -> None:
        if self._state == "processing":
            return  # на всякий случай — кнопка disabled, но дублируем
        if self._toggle_recording_via_ui is not None:
            self._toggle_recording_via_ui()
        else:
            QMessageBox.warning(self, "Запись", "Колбэк записи не подключён.")

    def refresh_history(self) -> None:
        history_dir = self._history_dir_getter()
        count = self._rotation_count_getter()
        self._entries = read_history_entries(history_dir, count)
        # T-173 A: транскрипты созвонов из Calls\ (вне ротации надиктовок, T-174)
        self._call_entries = read_call_entries(history_dir / "Calls", CALL_CARDS_MAX)

        # очищаем старые карточки (надиктовки + созвоны), кроме финального addStretch()
        for card in self._cards:
            self._history_layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()
        for card in self._call_cards:
            self._history_layout.removeWidget(card)
            card.deleteLater()
        self._call_cards.clear()

        # Единый список, новые сверху: надиктовки и созвоны вперемешку по времени.
        display: list[tuple] = []
        for i, e in enumerate(self._entries):
            mt = 0.0
            try:
                mt = e["wav_path"].stat().st_mtime
            except Exception:
                pass
            display.append((mt, "dictation", i, e))
        for i, e in enumerate(self._call_entries):
            display.append((e.get("mtime", 0.0), "call", i, e))
        display.sort(key=lambda x: x[0], reverse=True)

        for _mt, kind, idx, e in display:
            if kind == "call":
                card = CallHistoryCard(idx, e, parent=self._history_container)
                card.open_requested.connect(self._on_call_card_open)
                self._call_cards.append(card)
            else:
                card = HistoryCard(idx, e, parent=self._history_container)
                card.play_requested.connect(self._on_card_play)
                card.copy_requested.connect(self._on_card_copy)
                self._cards.append(card)
            # вставляем перед stretch (он в самом конце)
            self._history_layout.insertWidget(self._history_layout.count() - 1, card)

        # Empty state toggle — пусто только если нет ни надиктовок, ни созвонов
        has_items = bool(self._entries) or bool(self._call_entries)
        self._empty_widget.setVisible(not has_items)

        # Плеер/выделение — только по надиктовкам (у созвонов нет wav)
        if not self._entries:
            self._player.setSource(QUrl())
            self.timeline.setRange(0, 0)
            self._update_position_label()
            self._current_row = -1
        elif self._current_row < 0:
            self._select_row(0)

    @Slot(int)
    def _on_card_play(self, row: int) -> None:
        self._select_row(row)
        self._refresh_audio_device()
        self._player.play()

    @Slot(int)
    def _on_card_copy(self, row: int) -> None:
        if row < 0 or row >= len(self._entries):
            return
        text = self._entries[row].get("text_full", "")
        QApplication.clipboard().setText(text)
        # Свой ToastSaved (Claude Design): pill в правом нижнем углу с галочкой,
        # авто-скрытие через 2.5 сек. Заменяет tray.showMessage (раньше показывал
        # стандартный Windows toast — другой визуальный язык).
        self._overlay.show_state("saved")

    @Slot(int)
    def _on_call_card_open(self, call_row: int) -> None:
        """Открыть полный транскрипт созвона (T-173 A) в немодальном диалоге.
        Читаем тело .md заново — превью на карточке только начало диалога."""
        if call_row < 0 or call_row >= len(self._call_entries):
            return
        md_path = self._call_entries[call_row].get("md_path")
        try:
            body = md_path.read_text(encoding="utf-8") if md_path else "(нет файла)"
        except Exception as exc:
            body = f"(не смог прочитать {md_path}: {exc})"
        try:
            self._call_dialog = _CallTranscriptDialog(self, body)
            self._call_dialog.show()
            self._call_dialog.raise_()
            self._call_dialog.activateWindow()
        except Exception:
            QMessageBox.information(self, "Транскрипт созвона", body[:4000])

    def _select_row(self, row: int) -> None:
        """Установить текущую запись для плеера (загрузить wav).
        Подсвечивает активную карточку через `.item.is-active` (border #18181B)."""
        if row < 0 or row >= len(self._entries):
            self._player.setSource(QUrl())
            self.timeline.setRange(0, 0)
            self._current_row = -1
            # Снять active со всех карточек
            for c in self._cards:
                c.set_active(False)
            return
        e = self._entries[row]
        self._refresh_audio_device()
        self._player.setSource(QUrl.fromLocalFile(str(e["wav_path"])))
        self._current_row = row
        self._update_position_label()
        # Подсветить ровно одну (текущую) карточку. Сравниваем по _row, а не по
        # позиции в self._cards: порядок карточек теперь визуальный (созвоны
        # вперемешку с надиктовками), не равен индексу entry (T-173 A).
        for c in self._cards:
            c.set_active(getattr(c, "_row", -1) == row)

    def _refresh_audio_device(self) -> None:
        """Перевыбираем текущее default audio device. Берёт актуальное системное."""
        try:
            default_dev = QMediaDevices.defaultAudioOutput()
            if not default_dev.isNull():
                self._audio_out.setDevice(default_dev)
        except Exception:
            pass

    def _toggle_play(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlayingState:
            self._player.pause()
        else:
            # На каждый старт play — сверяем audio device с system default
            # (пользователь мог переключить наушники/колонки после открытия окна)
            self._refresh_audio_device()
            self._player.play()

    def _stop_play(self) -> None:
        self._player.stop()

    def _update_play_btn(self, state) -> None:
        self.play_btn.setIcon(self._ico_pause_18 if state == QMediaPlayer.PlayingState else self._ico_play_18)

    def _on_player_duration(self, ms: int) -> None:
        self.timeline.setRange(0, max(0, int(ms)))
        self._update_position_label()

    def _on_player_position(self, ms: int) -> None:
        if not self.timeline.isSliderDown():
            self.timeline.setValue(int(ms))
        self._update_position_label()

    def _seek(self, ms: int) -> None:
        # обновляем label во время перетаскивания, фактический seek — при release
        self._update_position_label(override_pos=ms)

    def _seek_release(self) -> None:
        self._player.setPosition(int(self.timeline.value()))

    def _update_position_label(self, override_pos: int | None = None) -> None:
        pos = max(0, (override_pos if override_pos is not None else self._player.position()) // 1000)
        dur = max(0, self._player.duration() // 1000)
        self.position_label.setText(
            f"{pos // 60:02d}:{pos % 60:02d} / {dur // 60:02d}:{dur % 60:02d}"
        )

    def _open_history_folder(self) -> None:
        """Открыть папку с записями в Explorer."""
        try:
            hdir = self._history_dir_getter()
            if hdir.exists():
                os.startfile(str(hdir))
            else:
                QMessageBox.information(self, "Папка истории", f"Папка ещё не создана:\n{hdir}")
        except Exception as exc:
            QMessageBox.warning(self, "Папка истории", str(exc))

    def open_settings(self) -> None:
        current = load_settings_dict()
        # T-259: во время записи/транскрипции ряд «Модель» в диалоге заблокирован
        # (менять модель под работающим m.transcribe нельзя).
        locked = False
        if self._model_busy_getter is not None:
            try:
                locked = bool(self._model_busy_getter())
            except Exception:
                locked = False
        dlg = SettingsDialog(self, current, model_locked=locked)
        if dlg.exec() != QDialog.Accepted:
            return
        new = dlg.values()
        save_settings_dict(new)
        # Замены живут в своём файле; пишем их только если редактор открывали —
        # иначе OK в настройках перетирал бы правки, сделанные в файле руками.
        if dlg.replacement_rules() is not None:
            profile.save_replacement_rules(dlg.replacement_rules())
        self._apply_autostart(new["autostart"])
        self.settings_changed.emit(new)
        self.refresh_history()

    def open_models(self) -> None:
        """T-259: раздел «Модели». Диалог пишет настройки сам, а подмену модели
        в движке делает transcribe_ui по `settings_changed` (там прогресс и
        блокировка во время записи)."""
        before = load_settings_dict()
        locked = False
        if self._model_busy_getter is not None:
            try:
                locked = bool(self._model_busy_getter())
            except Exception:
                locked = False
        dlg = ModelsDialog(self, before, model_locked=locked)
        dlg.exec()
        after = load_settings_dict()
        if (after.get("model"), after.get("custom_model")) != (
            before.get("model"), before.get("custom_model")
        ):
            self.settings_changed.emit(after)

    def _open_stats(self) -> None:
        """Открыть модальный диалог агрегатов из `<history_dir>/_stats.jsonl`."""
        stats_path = self._history_dir_getter() / "_stats.jsonl"
        dlg = StatsDialog(self, stats_path)
        dlg.exec()

    def _apply_autostart(self, enable: bool) -> None:
        if enable:
            ok, msg = create_autostart_shortcut(self._entry_script)
            if not ok:
                QMessageBox.warning(self, "Автостарт", f"Не получилось создать ярлык:\n{msg}")
        else:
            ok, msg = remove_autostart_shortcut()
            if not ok:
                QMessageBox.warning(self, "Автостарт", f"Не получилось удалить ярлык:\n{msg}")

    def _quit(self) -> None:
        self._user_quit = True
        self.quit_requested.emit()
        self._tray.hide()
        try:
            self._overlay.hide()
            self._overlay.deleteLater()
        except Exception:
            pass
        QApplication.instance().quit()
