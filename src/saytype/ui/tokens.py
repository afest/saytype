"""Токены дизайна SayType V5 — единственное место, где живут цвета, шрифты,
размеры и параметры движения интерфейса.

Источник значений — `_design/saytype-v5-qt-spec.md` (снято с финального каскада
прототипа `saytype-prototype-v5.html`, редакция 03.10.2026). Менять внешний вид
нужно здесь, а не в страницах: компоненты читают токены при отрисовке.

Где что менять (кратко, подробнее — `ui/README.md`):
  * цвет акцента / линий / текста     → класс `Color`
  * заголовочный шрифт и кегли         → `Font` (файл шрифта — `assets/fonts/`)
  * ритм сетки (шапка, паддинг, часы)  → `Grid`
  * штриховка                          → `Hatch`
  * длительности и кривые              → `Motion`
  * знак                               → `assets/mark.svg`
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QPointF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontDatabase, QPainter, QPen, QPixmap

ASSETS = Path(__file__).resolve().parent / "assets"


# ---------------------------------------------------------------------------
# Цвета
# ---------------------------------------------------------------------------
class Color:
    """Hex-строки из прототипа. Для QColor — `qc(Color.ACCENT)`."""

    BG = "#FFFFFF"
    LINE = "#E8EBF2"             # направляющие и границы ячеек
    INK = "#171B2B"              # основной текст
    MUTED = "#687083"            # вторичный текст
    ACCENT = "#3448EE"
    ACCENT_HOVER = "#273ACB"
    TINT = "#EEF1FF"             # подложка активного / hover

    DIGIT_IDLE = "#B7BDC9"       # цифры таймера в покое
    DIGIT_ACTIVE = "#3448EE"
    DIGIT_MINI = "#969EAE"       # мини-часы в строке истории
    DIGIT_MINI_SELECTED = "#858FA3"
    DIGIT_STAT = "#989FAD"       # метрики статистики
    NAV_INDEX = "#B7BECC"        # декоративный номер меню (opacity .33)
    NAV_INDEX_ACTIVE = "#A1AABD"  # (opacity .26)

    NAV_TEXT = "#687083"
    NAV_TEXT_HOVER = "#343C50"
    NAV_BG_HOVER = "#FAFBFE"
    NAV_BG_ACTIVE = "#F6F8FC"
    NAV_INSET = "#8D98AC"        # 2 px слева у активного пункта

    HATCH_RGBA = (89, 121, 228, 36)   # rgba(89,121,228,.14)
    CLOCK_LABEL_ALPHA = 0.85          # подпись над часами: белый 85 % (qc(Color.BG, Color.CLOCK_LABEL_ALPHA))

    ICON_ACTION = "#7E889B"      # иконки действий в строке истории
    KBD = "#7B8395"
    COUNT = "#8790A2"            # счётчик у заголовка секции

    BTN_BORDER = "#DDE1EC"
    BTN_BORDER_HOVER = "#C4CBDE"
    BTN_BG_HOVER = "#F5F7FC"
    FIELD_BORDER = "#DDE1EC"
    FIELD_FOCUS = "#7989ED"
    SWITCH_OFF = "#CED4E1"
    PROGRESS_TRACK = "#E9ECF5"
    OVER_BUDGET = "#DE2B3C"
    # «Удалить» и ошибки — алый под синий акцент (08.10: прежний #A73419 из V4 читался
    # коричневым). Контраст на белом 4,65 — проходит AA и для мелкого текста.
    DANGER = "#DE2B3C"
    STREAMING = "#EC7849"
    FULL = "#3448EE"
    CHART_AXIS = "#788193"
    CHART_RAW = "#BDC3D1"
    TAB_UNDERLINE = "#A5AFC0"
    STATS_HOVER = "#F7F8FC"

    STRIP_BG = "#3448EE"         # плавающий статус записи
    TOAST_BG = "#202A4C"
    MODAL_BORDER = "#E1E6F0"
    SCRIM = "#171B2B"            # затемнение под листом и диалогом — INK с альфой SCRIM_ALPHA
    SCRIM_ALPHA = 0.32
    DROP_BORDER = "#D7DEF0"
    DROP_BG = "#F5F7FD"
    DROP_OVER = "#EDF0FF"
    MODEL_SELECTED_SUB = "#E0E5FF"
    MODEL_DOT_OFF = "#DCE1F0"    # пустая точка шкалы «точность / скорость»
    NOTE_ACTIVE_META = "#7480AE"
    MARK = "#343B4B"
    SIGNAL = "#3448EE"

    # Иконка приложения и трея — белый знак на плитке (icons.app_tile_pixmap).
    # Знак временный; цвет плитки в трее — состояние.
    APP_TILE = "#3448EE"             # покой — акцент V5
    APP_TILE_RECORDING = "#E5484D"   # запись диктовки или созвона
    APP_TILE_PROCESSING = "#E8920C"  # распознавание
    APP_TILE_MARK = "#FFFFFF"

    # Семантика состояний (сохраняем читаемость warning/error)
    WARNING = "#B45309"
    ERROR = "#DE2B3C"
    SUCCESS = "#2E7D5B"


def qc(hex_or_rgba, alpha: float | None = None) -> QColor:
    """QColor из hex-строки или кортежа RGBA; `alpha` 0..1 перекрывает альфу."""
    if isinstance(hex_or_rgba, tuple):
        c = QColor(*hex_or_rgba)
    else:
        c = QColor(hex_or_rgba)
    if alpha is not None:
        c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


def mix(a: str, b: str, t: float) -> QColor:
    """Линейное смешение двух цветов в sRGB, t=0 → a, t=1 → b."""
    ca, cb = QColor(a), QColor(b)
    t = max(0.0, min(1.0, t))
    return QColor(
        round(ca.red() + (cb.red() - ca.red()) * t),
        round(ca.green() + (cb.green() - ca.green()) * t),
        round(ca.blue() + (cb.blue() - ca.blue()) * t),
    )


# ---------------------------------------------------------------------------
# Шрифты
# ---------------------------------------------------------------------------
class Font:
    BODY_FAMILIES = ["Segoe UI Variable Text", "Segoe UI", "sans-serif"]
    HEAD_FILE = ASSETS / "fonts" / "Unbounded.ttf"
    HEAD_FAMILY = "Unbounded"      # подменяется реальным именем после загрузки
    HEAD_WEIGHT = 450              # variable-шрифт; Qt 6.11 принимает произвольный вес

    BODY = 14
    SMALL = 12
    TINY = 10
    NAV = 12
    H1_MAX = 29                    # clamp(18px, 2.2vw, 29px)
    H1_MIN = 18
    H2 = 16
    MODEL_TITLE = 23
    NOTE_TITLE = 26
    BTN = 13
    BTN_SM = 12
    KBD = 11
    LABEL = 14
    HINT = 12
    COLON_BIG = 42                 # двоеточие большого таймера, 600
    COLON_MINI = 23                # двоеточие мини-часов, 500
    STAT_SYMBOL_MAX = 40           # «,» и «×» в метриках, clamp(20px, 3vw, 40px)
    STAT_SYMBOL_MIN = 20


_FONT_STATE = {"loaded": False, "head_family": None}


def load_fonts() -> str:
    """Загрузить Unbounded из комплекта приложения (без сети). Возвращает имя семейства."""
    if _FONT_STATE["loaded"]:
        return _FONT_STATE["head_family"] or Font.HEAD_FAMILY
    _FONT_STATE["loaded"] = True
    fid = QFontDatabase.addApplicationFont(str(Font.HEAD_FILE)) if Font.HEAD_FILE.exists() else -1
    fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
    _FONT_STATE["head_family"] = fams[0] if fams else None
    return _FONT_STATE["head_family"] or Font.HEAD_FAMILY


def body_font(size: int = Font.BODY, weight: int = 400, letter_spacing: float = 0.0) -> QFont:
    f = QFont()
    f.setFamilies(Font.BODY_FAMILIES)
    f.setPixelSize(int(size))
    f.setWeight(QFont.Weight(weight))
    if letter_spacing:
        f.setLetterSpacing(QFont.AbsoluteSpacing, letter_spacing)
    return f


def head_font(size: int, weight: int = Font.HEAD_WEIGHT, letter_spacing: float = -0.9) -> QFont:
    fam = load_fonts()
    f = QFont(fam)
    f.setFamilies([fam, *Font.BODY_FAMILIES])
    f.setPixelSize(int(size))
    f.setWeight(QFont.Weight(weight))
    f.setCapitalization(QFont.Capitalization.AllUppercase)
    if letter_spacing:
        f.setLetterSpacing(QFont.AbsoluteSpacing, letter_spacing)
    return f


def head_font_plain(size: int, weight: int = Font.HEAD_WEIGHT, letter_spacing: float = -0.4) -> QFont:
    """Unbounded без капитализации (wordmark, символы в метриках)."""
    f = head_font(size, weight, letter_spacing)
    f.setCapitalization(QFont.Capitalization.MixedCase)
    return f


def tabular(font: QFont) -> QFont:
    """Табличные цифры (font-variant-numeric: tabular-nums)."""
    f = QFont(font)
    try:
        from PySide6.QtGui import QFont as _QF
        f.setFeature(_QF.Tag("tnum"), 1)
    except Exception:  # noqa: BLE001 — старый Qt без setFeature
        pass
    return f


def clamp_vw(window_width: int, min_px: float, vw: float, max_px: float) -> float:
    """CSS clamp(min, vw%, max) по ширине окна."""
    return max(min_px, min(max_px, window_width * vw / 100.0))


# ---------------------------------------------------------------------------
# Сетка и размеры
# ---------------------------------------------------------------------------
class Grid:
    SIDEBAR = 210
    SIDEBAR_NARROW = 184           # окно ≤ 1100
    SIDEBAR_COMPACT = 64           # окно ≤ 850 (только иконки)
    HEAD = 94                      # высота шапки (бренд, page-head, instrument-head)
    PAD = 30                       # паддинг ячеек (≥1500: 42; ≤1100: 24)
    PAD_WIDE = 42
    PAD_NARROW = 24
    LINE = 1
    GUIDES = (0.2, 0.4, 0.6, 0.8)  # вертикальные направляющие по ширине main
    CLOCK_MIN, CLOCK_VW, CLOCK_MAX = 190, 22.0, 300   # clamp(190px, 22vw, 300px)
    CLOCK_NARROW = 220             # ≤1100
    CLOCK_LABEL = 39
    CONTROLS = 88
    SECTION_HEAD = 78
    RECORD_ROW_MIN = 175
    NAV_ITEM = 61
    NAV_PAD_X = 20
    NAV_GAP = 12
    NAV_INDEX_W, NAV_INDEX_H = 60, 64
    NAV_INDEX_RIGHT, NAV_INDEX_BOTTOM = 9, -24
    BRAND_MARK = 44
    APP_TILE_RADIUS = 0.22         # скругление плитки иконки, доля стороны
    APP_TILE_MARK = 0.62           # знак внутри плитки, доля стороны
    STAT_READOUT_MIN, STAT_READOUT_VW, STAT_READOUT_MAX = 42, 5.8, 78
    STAT_DIGIT_MAX_W = 58
    BTN_H = 40
    BTN_PRIMARY_MIN_W = 170
    BTN_PRIMARY_H = 44
    BTN_SM_H = 31
    BTN_SQUARE = 36
    # Лист с длинным текстом (созвон, словарь): ширина под удобное чтение, а не на всё
    # окно — на развёрнутом приложении строка в 1800 px не читается (08.10).
    SHEET_MIN_W, SHEET_MAX_W = 480, 720
    SHEET_MIN_H, SHEET_MAX_H = 320, 880
    SHEET_MARGIN = 32            # от краёв окна, пока окно меньше максимума листа
    MODAL_MARGIN = 16
    ROW_ACTION = 32
    ICON = 20
    ICON_NAV = 18
    ICON_ROW = 17
    ICON_STROKE = 1.7
    ICON_NAV_STROKE = 1.65
    SWITCH_W, SWITCH_H, SWITCH_KNOB = 38, 20, 14   # прямоугольный: дорожка и квадратная кнопка
    FIELD_MIN_H = 107
    SETTINGS_TAB_H = 66
    MODEL_CARD_MIN_H = 235
    NOTE_ITEM_MIN_H = 112
    WINDOW_MIN_W, WINDOW_MIN_H = 760, 560
    # Открывается в минимальной ширине — компактнее (живой просмотр 05.10.2026; было 1180).
    # Шире — растянуть руками, размер между запусками не запоминается.
    WINDOW_W, WINDOW_H = WINDOW_MIN_W, 780
    RADIUS = 0

    SIDEBAR_ALWAYS_COMPACT = True  # меню всегда иконками, подпись — подсказкой при наведении

    @staticmethod
    def sidebar_width(window_width: int) -> int:
        if Grid.SIDEBAR_ALWAYS_COMPACT or window_width <= 850:
            return Grid.SIDEBAR_COMPACT
        if window_width <= 1100:
            return Grid.SIDEBAR_NARROW
        return Grid.SIDEBAR

    @staticmethod
    def pad(window_width: int) -> int:
        if window_width >= 1500:
            return Grid.PAD_WIDE
        if window_width <= 1100:
            return Grid.PAD_NARROW
        return Grid.PAD

    @staticmethod
    def clock_height(window_width: int) -> int:
        if window_width <= 1100:
            return Grid.CLOCK_NARROW
        return int(round(clamp_vw(window_width, Grid.CLOCK_MIN, Grid.CLOCK_VW, Grid.CLOCK_MAX)))

    @staticmethod
    def h1_size(window_width: int) -> int:
        if window_width <= 850:
            return 21
        return int(round(clamp_vw(window_width, Font.H1_MIN, 2.2, Font.H1_MAX)))


# ---------------------------------------------------------------------------
# Штриховка
# ---------------------------------------------------------------------------
class Hatch:
    """repeating-linear-gradient(130deg, rgba(89,121,228,.14) 0 1px, transparent 1px 3px)."""

    ANGLE_DEG = 130
    PERIOD = 3
    LINE = 1
    RGBA = Color.HATCH_RGBA

    @staticmethod
    @lru_cache(maxsize=16)
    def tile(dpr: float = 1.0, alpha: float = 1.0) -> QPixmap:
        """Повторяемый тайл штриховки (кэшируется по DPR и прозрачности)."""
        # Период 3 px по нормали к линиям под 130° даёт повторение по
        # горизонтали и вертикали с шагом 3/sin(50°)·… — берём тайл 6×6 с тремя
        # линиями: совпадает по шагу с CSS в пределах субпикселя.
        size = 6
        px = QPixmap(int(size * dpr), int(size * dpr))
        px.setDevicePixelRatio(dpr)
        px.fill(Qt.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.Antialiasing, True)
        c = QColor(*Hatch.RGBA)
        c.setAlphaF(c.alphaF() * alpha)
        pen = QPen(c, Hatch.LINE)
        pen.setCapStyle(Qt.FlatCap)
        p.setPen(pen)
        # линии «снизу-слева вверх-направо» под 50° к горизонтали
        step = Hatch.PERIOD / 0.766  # 3 px по нормали → ≈3.92 px по горизонтали
        x = -size
        while x < size * 2:
            p.drawLine(QPointF(x, size), QPointF(x + size / 0.8391, 0))  # tan(50°)=1.19
            x += step
        p.end()
        return px

    @staticmethod
    def brush(dpr: float = 1.0, alpha: float = 1.0) -> QBrush:
        return QBrush(Hatch.tile(dpr, alpha))


# ---------------------------------------------------------------------------
# Движение
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Curve:
    x1: float
    y1: float
    x2: float
    y2: float

    def easing(self) -> QEasingCurve:
        c = QEasingCurve(QEasingCurve.BezierSpline)
        c.addCubicBezierSegment(QPointF(self.x1, self.y1), QPointF(self.x2, self.y2), QPointF(1.0, 1.0))
        return c


class Motion:
    DIGIT_MS = 780                      # прокрутка изменившейся цифры таймера
    DIGIT_CURVE = Curve(.65, 0, .2, 1)
    MINI_DIGIT_MS = 600                 # мини-часы плеера
    RESET_MS = 740                      # обратный сброс при старте записи
    RESET_DELAY_PER_DIGIT_MS = 20
    RESET_CURVE = Curve(.2, .7, .2, 1)
    UNIT_COLOR_MS = 450                 # серый ↔ синий у юнита таймера
    MINI_COLOR_MS = 400
    CHART_MS = 900                      # линия тренда / рост столбика на странице «Статистика»
    CHART_CURVE = Curve(.65, 0, .35, 1) # ease-in-out
    CHART_BAR_DELAY_MS = 40             # лесенка между столбиками
    PAGE_MS = 650
    PAGE_CURVE = Curve(.22, .8, .22, 1)
    PAGE_DIRECTION = 1                  # все переходы в одну сторону: 1 — слева направо, -1 — справа налево
    PAGE_LEAVE_SHIFT = 0.05             # уходящая страница сдвигается на 5 %
    PAGE_LEAVE_OPACITY = 0.65
    NAV_HATCH_MS = 420
    NAV_HATCH_CURVE = Curve(.22, .8, .22, 1)
    NAV_COLOR_MS = 250
    NAV_ICON_MS = 300
    BTN_MS = 150
    OVERLAY_MS = 180                    # затемнение и подъём карточки листа / диалога
    OVERLAY_RISE = 12                   # на сколько px карточка поднимается при появлении
    TOAST_MS = 200
    TOAST_VISIBLE_MS = 3400
    SPINNER_MS = 700
    PROGRESS_MS = 200

    _user_off = False                   # настройка «Анимации интерфейса» выключена

    @staticmethod
    def set_user_enabled(on: bool) -> None:
        """Настройка приложения: выключено — цифры перещёлкиваются, страницы меняются без перехода."""
        Motion._user_off = not on

    @staticmethod
    def reduced() -> bool:
        """Без анимации: выключено в настройках приложения или системное «уменьшение
        анимации» Windows (SPI_GETCLIENTAREAANIMATION)."""
        if Motion._user_off:
            return True
        try:
            import ctypes
            val = ctypes.c_int(1)
            ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(val), 0)
            return val.value == 0
        except Exception:  # noqa: BLE001
            return False


def line_pen(width: int = Grid.LINE) -> QPen:
    pen = QPen(qc(Color.LINE), width)
    pen.setCapStyle(Qt.FlatCap)
    return pen


# ---------------------------------------------------------------------------
# Подмена токенов без правки файла (проверка «меняю в одном месте»)
# ---------------------------------------------------------------------------
def _apply_env_overrides() -> None:
    """`SAYTYPE_TOKENS` — JSON вида {"Color": {"ACCENT": "#E0452A"}, "head_font": "…ttf"}.

    Читается при импорте модуля, до создания любого компонента: эффект тот же,
    что от правки значений в этом файле и перезапуска. Для галереи и проверок;
    в обычном запуске переменной нет.
    """
    import json
    import os

    raw = os.environ.get("SAYTYPE_TOKENS", "").strip()
    if not raw:
        return
    try:
        data = json.loads(raw)
    except ValueError:
        return
    for name, value in (data.get("Color") or {}).items():
        if hasattr(Color, name):
            setattr(Color, name, value)
    if data.get("head_font"):
        Font.HEAD_FILE = Path(data["head_font"])


_apply_env_overrides()
