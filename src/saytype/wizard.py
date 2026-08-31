"""wizard.py — мастер первого запуска (T-263).

Показывается один раз, до основного окна, и доводит человека до первой удачной
диктовки. Всё, что можно решить за него, решено: микрофон системный, хоткей
`Ctrl+Shift+Q`, модель подобрана по железу, автостарт включён. Каждый шаг —
готовый ответ и кнопка «Дальше»; менять что-то нужно только тем, кого дефолт
не устроил.

Почему шагов пять, а не шесть. В описании задачи первым идёт выбор языка
интерфейса, но перевода интерфейса пока нет (вынесен в отдельную задачу), и
переключатель, который ничего не меняет, читается как поломка. Язык определяется
по системной локали и запоминается — переключатель появится вместе с переводом.

Тяжёлое (скачивание модели и CUDA-слоя) мастер не делает сам: он записывает
выбор в настройки, а качает уже приложение своим обычным путём, с теми же
прогрессом и обработкой ошибок. Дублировать эту логику ради мастера — заводить
второе место, где она может разойтись.
"""

from __future__ import annotations

import locale
import sys
from typing import Optional

import numpy as np
import sounddevice as sd
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import cuda_layer, engine
from .audio_quality import input_devices
from .transcribe_ui_window import (
    DEFAULT_HOTKEY,
    hotkey_to_canonical,
    hotkey_to_qt,
    hotkey_conflicts_with_handy,
    parse_hotkey_valid,
)

SAMPLE_RATE = 16000

_STYLE = """
QDialog { background-color: #F6F6F7; }
QLabel { background: transparent; color: #18181B;
  font-family: 'Segoe UI Variable Display','Segoe UI',sans-serif; }
QLabel#step_title { font-size: 20px; font-weight: 600; }
QLabel#step_text { font-size: 13px; color: #52525B; }
QLabel#step_hint { font-size: 12px; color: #A1A1AA; }
QLabel#step_counter { font-size: 12px; color: #A1A1AA; }
QComboBox, QKeySequenceEdit { background: #FFFFFF; border: 1px solid #E7E7EA;
  border-radius: 8px; padding: 0 12px; color: #18181B; font-size: 13px;
  min-height: 32px; max-height: 34px; }
QComboBox:hover, QKeySequenceEdit:hover { border-color: #D4D4D8; }
QPushButton#btn_outline { background-color: #FFFFFF; border: 1px solid #E7E7EA;
  border-radius: 17px; padding: 0 16px; color: #18181B; font-size: 13px;
  font-weight: 500; min-height: 32px; max-height: 34px; }
QPushButton#btn_outline:hover { background-color: #F4F4F5; border-color: #D4D4D8; }
QPushButton#btn_primary { background-color: #18181B; border: 1px solid #18181B;
  border-radius: 18px; padding: 0 22px; color: #FFFFFF; font-size: 13px;
  font-weight: 500; min-height: 34px; max-height: 36px; }
QPushButton#btn_primary:hover { background-color: #27272A; }
QPushButton#btn_primary:disabled { background-color: #A1A1AA; border-color: #A1A1AA; }
QCheckBox { color: #18181B; font-size: 13px; spacing: 10px; }
QCheckBox::indicator { width: 18px; height: 18px; border: 1px solid #D4D4D8;
  border-radius: 4px; background: #FFFFFF; }
QCheckBox::indicator:checked { background: #18181B; border-color: #18181B; }
QProgressBar { border: 1px solid #E7E7EA; border-radius: 6px; background: #FFFFFF;
  height: 10px; text-align: center; }
QProgressBar::chunk { background-color: #18181B; border-radius: 5px; }
QWidget#footer { background: transparent; border-top: 1px solid #E7E7EA; }
"""


def system_language() -> str:
    """`ru` или `en` — язык, на котором стоит здороваться.

    На Windows спрашиваем язык **интерфейса**, а не локаль форматов: русские
    даты и разделители при английской Windows — обычное дело, и здороваться
    по ним было бы неверно. `locale.getdefaultlocale()` вдобавок объявлен
    устаревшим и исчезнет в Python 3.15.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "ru" if (lang_id & 0x3FF) == 0x19 else "en"  # 0x19 = LANG_RUSSIAN
        except Exception:
            pass
    try:
        code = locale.getlocale()[0] or ""
    except Exception:
        code = ""
    return "ru" if code.lower().startswith(("ru", "russian")) else "en"


class _LevelMeter(QWidget):
    """Полоска уровня микрофона.

    Нужна не для красоты: без неё человек узнает, что выбрал не тот вход, только
    после первой пустой расшифровки. Поток открывается на время шага и
    закрывается при уходе с него — иначе мастер держал бы микрофон занятым.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stream: Optional[sd.InputStream] = None
        self._level = 0.0
        self._device: Optional[int] = None

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.status = QLabel("Скажите что-нибудь — полоска должна двигаться")
        self.status.setObjectName("step_hint")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.bar)
        lay.addWidget(self.status)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def _callback(self, indata, _frames, _time, _status) -> None:
        try:
            self._level = float(np.sqrt(np.mean(indata**2)))
        except Exception:
            self._level = 0.0

    def _tick(self) -> None:
        # Корень сжимает динамику: тихая речь иначе даёт пару процентов и
        # выглядит как «микрофон не работает».
        self.bar.setValue(int(min(1.0, self._level**0.5 * 2.2) * 100))

    def start(self, device: Optional[int]) -> None:
        self.stop()
        self._device = device
        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                device=device, callback=self._callback,
            )
            self._stream.start()
            self._timer.start(60)
            self.status.setText("Скажите что-нибудь — полоска должна двигаться")
        except Exception as exc:
            self._stream = None
            self.status.setText(f"Не удалось открыть микрофон: {exc}")

    def stop(self) -> None:
        self._timer.stop()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self.bar.setValue(0)


def _step(title: str, text: str) -> tuple[QWidget, QVBoxLayout]:
    """Каркас шага: заголовок, пояснение и место под содержимое."""
    page = QWidget()
    lay = QVBoxLayout(page)
    lay.setContentsMargins(32, 28, 32, 12)
    lay.setSpacing(14)
    head = QLabel(title)
    head.setObjectName("step_title")
    lay.addWidget(head)
    if text:
        sub = QLabel(text)
        sub.setObjectName("step_text")
        sub.setWordWrap(True)
        lay.addWidget(sub)
    return page, lay


class FirstRunWizard(QDialog):
    """Мастер первого запуска. `values()` отдаёт изменения для настроек."""

    finished_ok = Signal()

    def __init__(self, current: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("SayType — первый запуск")
        self.setMinimumSize(560, 460)
        self.setStyleSheet(_STYLE)
        self._current = dict(current)
        self._language = system_language()

        self.stack = QStackedWidget()
        self._build_mic_step()
        self._build_hotkey_step()
        self._build_model_step()
        self._build_gpu_step()
        self._build_finish_step()

        self.counter = QLabel()
        self.counter.setObjectName("step_counter")
        self.back_btn = QPushButton("Назад")
        self.back_btn.setObjectName("btn_outline")
        self.back_btn.setCursor(Qt.PointingHandCursor)
        self.back_btn.clicked.connect(self._go_back)
        self.next_btn = QPushButton("Дальше")
        self.next_btn.setObjectName("btn_primary")
        self.next_btn.setCursor(Qt.PointingHandCursor)
        self.next_btn.clicked.connect(self._go_next)

        footer = QWidget()
        footer.setObjectName("footer")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(32, 12, 32, 16)
        fl.setSpacing(10)
        fl.addWidget(self.counter)
        fl.addStretch(1)
        fl.addWidget(self.back_btn)
        fl.addWidget(self.next_btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.stack, 1)
        root.addWidget(footer)

        self.stack.currentChanged.connect(self._on_page_changed)
        self._on_page_changed(0)

    # === Шаги ===
    def _build_mic_step(self) -> None:
        page, lay = _step(
            "Микрофон",
            "Выбран системный микрофон. Если голос пишется не с того устройства — "
            "выберите нужное и проверьте по полоске.",
        )
        self.mic_combo = QComboBox()
        self._devices = input_devices()
        chosen = (self._current.get("mic_device") or "").strip()
        self.mic_combo.addItem("Системный микрофон", "")
        for dev in self._devices:
            label = dev["name"] + (" — системный" if dev["default"] else "")
            self.mic_combo.addItem(label, dev["name"])
        if chosen:
            idx = self.mic_combo.findData(chosen)
            self.mic_combo.setCurrentIndex(idx if idx >= 0 else 0)
        lay.addWidget(self.mic_combo)

        self.meter = _LevelMeter()
        lay.addWidget(self.meter)
        if not self._devices:
            warn = QLabel(
                "Микрофоны не найдены. Подключите устройство — приложение без "
                "него запустится, но записывать будет нечего."
            )
            warn.setObjectName("step_hint")
            warn.setWordWrap(True)
            lay.addWidget(warn)
        lay.addStretch(1)
        self.mic_combo.currentIndexChanged.connect(self._restart_meter)
        self.stack.addWidget(page)

    def _build_hotkey_step(self) -> None:
        page, lay = _step(
            "Горячая клавиша",
            "Нажатие начинает запись, повторное — заканчивает и вставляет текст "
            "в активное окно.",
        )
        self.hotkey_edit = QKeySequenceEdit()
        self.hotkey_edit.setMaximumSequenceLength(1)
        self.hotkey_edit.setKeySequence(
            QKeySequence(hotkey_to_qt(self._current.get("hotkey") or DEFAULT_HOTKEY))
        )
        lay.addWidget(self.hotkey_edit)
        self.hotkey_hint = QLabel()
        self.hotkey_hint.setObjectName("step_hint")
        self.hotkey_hint.setWordWrap(True)
        lay.addWidget(self.hotkey_hint)
        lay.addStretch(1)
        self.hotkey_edit.keySequenceChanged.connect(self._check_hotkey)
        # Первую проверку делает переход на шаг: кнопок футера на этот момент
        # ещё нет, а `_check_hotkey` управляет их состоянием.
        self.stack.addWidget(page)

    def _build_model_step(self) -> None:
        gpu = engine.detect_gpu()
        preset = engine.recommended_preset()
        meta = engine.preset_meta(preset)
        page, lay = _step(
            "Модель распознавания",
            f"Подобрана по вашему железу: {meta['title']}. "
            + (f"Найдена {gpu['name']}." if gpu else "Видеокарта NVIDIA не найдена, работаем на процессоре."),
        )
        self.model_combo = QComboBox()
        for p in engine.PRESETS:
            size = f"{p['size_mb']} МБ" if p["size_mb"] < 1000 else f"{p['size_mb'] / 1000:.1f} ГБ"
            self.model_combo.addItem(f"{p['title']} · {size} · {p['note']}", p["key"])
        idx = self.model_combo.findData(self._current.get("model") or preset)
        self.model_combo.setCurrentIndex(idx if idx >= 0 else self.model_combo.findData(preset))
        lay.addWidget(self.model_combo)
        self.model_hint = QLabel()
        self.model_hint.setObjectName("step_hint")
        self.model_hint.setWordWrap(True)
        lay.addWidget(self.model_hint)
        lay.addStretch(1)
        self.model_combo.currentIndexChanged.connect(self._update_model_hint)
        self._update_model_hint()
        self.stack.addWidget(page)

    def _build_gpu_step(self) -> None:
        gpu = cuda_layer.gpu()
        page, lay = _step(
            "Ускорение на видеокарте",
            f"Найдена {gpu['name']}. Дополнительные библиотеки NVIDIA "
            f"(~{cuda_layer.size_hint_mb()} МБ) заметно ускоряют распознавание."
            if gpu else "",
        )
        self.gpu_check = QCheckBox("Скачать ускорение после мастера")
        self.gpu_check.setChecked(True)
        lay.addWidget(self.gpu_check)
        hint = QLabel(
            "Без него всё работает на процессоре — просто медленнее. "
            "Передумать можно в настройках."
        )
        hint.setObjectName("step_hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addStretch(1)
        self._gpu_page_index = self.stack.count()
        self._gpu_needed = bool(gpu) and not cuda_layer.is_installed() and not cuda_layer.runtime_in_environment()
        self.stack.addWidget(page)

    def _build_finish_step(self) -> None:
        page, lay = _step(
            "Готово",
            "Приложение живёт в трее — значок в правом нижнем углу. Окно можно "
            "закрыть, запись это не остановит.",
        )
        self.autostart_check = QCheckBox("Запускать вместе с Windows")
        # На первом проходе автостарт предлагается включённым: инструмент,
        # который надо каждый раз запускать руками, забывают на второй день.
        # При повторном проходе — показываем то, что человек уже выбрал.
        first_time = not self._current.get("wizard_done", False)
        self.autostart_check.setChecked(
            True if first_time else bool(self._current.get("autostart", False))
        )
        lay.addWidget(self.autostart_check)

        card = QFrame()
        card.setStyleSheet(
            "QFrame { background: #FFFFFF; border: 1px solid #E7E7EA; border-radius: 10px; }"
        )
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(6)
        self.try_label = QLabel()
        self.try_label.setStyleSheet("font-size: 14px; font-weight: 600; border: none;")
        cl.addWidget(self.try_label)
        tip = QLabel(
            "Откройте любое окно с текстовым полем, нажмите сочетание, скажите "
            "фразу и нажмите ещё раз — текст вставится сам."
        )
        tip.setObjectName("step_text")
        tip.setWordWrap(True)
        tip.setStyleSheet("border: none;")
        cl.addWidget(tip)
        lay.addWidget(card)
        lay.addStretch(1)
        self.stack.addWidget(page)

    # === Логика ===
    def _restart_meter(self) -> None:
        name = self.mic_combo.currentData() or ""
        index = None
        for dev in self._devices:
            if dev["name"] == name:
                index = dev["index"]
                break
        self.meter.start(index)

    def _check_hotkey(self) -> None:
        hotkey = self._hotkey_value()
        if not hotkey:
            self.hotkey_hint.setText("Нажмите сочетание клавиш.")
            self.next_btn.setEnabled(False)
            return
        ok, msg = parse_hotkey_valid(hotkey)
        if not ok:
            self.hotkey_hint.setText(f"Не подходит: {msg}")
            self.next_btn.setEnabled(False)
            return
        if hotkey_conflicts_with_handy(hotkey):
            self.hotkey_hint.setText(
                "Это сочетание занято приложением Handy — выберите другое, "
                "иначе сработает только одно из них."
            )
        else:
            self.hotkey_hint.setText("Сочетание свободно.")
        self.next_btn.setEnabled(True)

    def _hotkey_value(self) -> str:
        seq = self.hotkey_edit.keySequence()
        if seq.isEmpty():
            return ""
        return hotkey_to_canonical(seq.toString())

    def _update_model_hint(self) -> None:
        key = self.model_combo.currentData()
        if engine.is_cached(key):
            self.model_hint.setText("Уже скачана — ждать не придётся.")
        else:
            self.model_hint.setText(
                f"Скачается при первом запуске: {engine.preset_size_mb(key)} МБ. "
                "Прогресс будет виден."
            )

    def _visible_pages(self) -> list[int]:
        """Индексы шагов, которые человек действительно увидит."""
        return [
            i for i in range(self.stack.count())
            if i != self._gpu_page_index or self._gpu_needed
        ]

    def _on_page_changed(self, index: int) -> None:
        total = self.stack.count()
        # Считаем по видимым шагам, а не по всем: с пропущенным шагом про
        # видеокарту счётчик иначе прыгает с «3 из 5» на «5 из 5».
        pages = self._visible_pages()
        position = pages.index(index) + 1 if index in pages else len(pages)
        self.counter.setText(f"Шаг {position} из {len(pages)}")
        self.back_btn.setEnabled(index > 0)
        self.next_btn.setText("Начать пользоваться" if index == total - 1 else "Дальше")
        if index == 0:
            self._restart_meter()
        else:
            self.meter.stop()
        if index == 1:
            self._check_hotkey()
        else:
            self.next_btn.setEnabled(True)
        if index == total - 1:
            self.try_label.setText(f"Попробуйте: {hotkey_to_qt(self._hotkey_value())}")

    def _go_back(self) -> None:
        index = self.stack.currentIndex() - 1
        # Шаг про видеокарту пропускается на машинах, которым он не нужен
        if index == self._gpu_page_index and not self._gpu_needed:
            index -= 1
        self.stack.setCurrentIndex(max(0, index))

    def _go_next(self) -> None:
        index = self.stack.currentIndex() + 1
        if index == self._gpu_page_index and not self._gpu_needed:
            index += 1
        if index >= self.stack.count():
            self.meter.stop()
            self.accept()
            self.finished_ok.emit()
            return
        self.stack.setCurrentIndex(index)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.meter.stop()
        super().closeEvent(event)

    # === Результат ===
    def values(self) -> dict:
        """Что мастер меняет в настройках."""
        return {
            "mic_device": self.mic_combo.currentData() or "",
            "hotkey": self._hotkey_value() or DEFAULT_HOTKEY,
            "model": self.model_combo.currentData(),
            "autostart": bool(self.autostart_check.isChecked()),
            "ui_language": self._language,
            "wizard_done": True,
        }

    def wants_cuda_layer(self) -> bool:
        """Согласился ли человек на докачку CUDA-слоя (и нужна ли она вообще)."""
        return bool(self._gpu_needed and self.gpu_check.isChecked())
