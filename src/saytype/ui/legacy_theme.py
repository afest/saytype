"""Тема V5 для окон, чья логика осталась прежней: импорт файла (`FileImportDialog`)
и мастер первого запуска (`FirstRunWizard`).

Эти окна задают себе стиль сами (`BASE_DIALOG_QSS`, `wizard._STYLE`) — общий
`APP_QSS` его не перекрывает. Здесь — тот же набор objectName'ов в токенах V5:
белый фон, прямоугольные кнопки, синий акцент, линии `#E8EBF2`. Поведение окон
не меняется: подменяется только стиль, плюс несколько известных inline-стилей
(полоса прогресса, карточка-подсказка).
"""
from __future__ import annotations

from PySide6.QtWidgets import QFrame, QLabel, QProgressBar, QWidget

from .tokens import Color, Font

_BODY = "font-family:'Segoe UI Variable Text','Segoe UI',sans-serif;"

DIALOG_QSS = f"""
QDialog {{ background:{Color.BG}; }}
QLabel {{ background:transparent; color:{Color.INK}; {_BODY} font-size:{Font.BODY}px; }}
QLabel#frow_label {{ font-size:{Font.LABEL}px; font-weight:400; }}
QLabel#frow_hint, QLabel#footer_note, QLabel#step_hint, QLabel#step_counter {{
    color:{Color.MUTED}; font-size:{Font.HINT}px; }}
QLabel#step_title {{ font-size:20px; font-weight:400; letter-spacing:-0.3px; }}
QLabel#step_text {{ font-size:{Font.BTN}px; color:{Color.MUTED}; }}
QLineEdit, QLineEdit#settings_input_mono, QComboBox, QKeySequenceEdit {{
    {_BODY} background:{Color.BG}; border:1px solid {Color.FIELD_BORDER}; border-radius:0;
    padding:0 12px; color:{Color.INK}; font-size:{Font.BODY}px; min-height:40px; max-height:42px; }}
QLineEdit:focus, QComboBox:focus, QKeySequenceEdit:focus {{ border-color:{Color.FIELD_FOCUS}; }}
QComboBox QAbstractItemView {{ background:{Color.BG}; color:{Color.INK}; border:1px solid {Color.FIELD_BORDER};
    selection-background-color:{Color.TINT}; selection-color:{Color.INK}; outline:0; }}
QPushButton, QPushButton#btn_outline {{
    {_BODY} background:transparent; border:1px solid {Color.BTN_BORDER}; border-radius:0;
    padding:0 15px; color:{Color.INK}; font-size:{Font.BTN}px; font-weight:400; min-height:38px; max-height:40px; }}
QPushButton:hover, QPushButton#btn_outline:hover {{ background:{Color.BTN_BG_HOVER}; border-color:{Color.BTN_BORDER_HOVER}; }}
QPushButton:disabled, QPushButton#btn_outline:disabled {{ color:#A4AAB8; border-color:#E9ECF3; background:transparent; }}
QPushButton#btn_primary, QPushButton:default {{
    background:{Color.ACCENT}; border:1px solid {Color.ACCENT}; color:#FFFFFF; padding:0 18px; }}
QPushButton#btn_primary:hover, QPushButton:default:hover {{ background:{Color.ACCENT_HOVER}; border-color:{Color.ACCENT_HOVER}; }}
QPushButton#btn_primary:disabled {{ background:#9AA5F2; border-color:#9AA5F2; color:#FFFFFF; }}
QCheckBox {{ color:{Color.INK}; {_BODY} font-size:{Font.BTN}px; spacing:10px; }}
QCheckBox::indicator {{ width:16px; height:16px; border:1px solid {Color.BTN_BORDER_HOVER}; border-radius:0;
    background:{Color.BG}; }}
QCheckBox::indicator:checked {{ background:{Color.ACCENT}; border-color:{Color.ACCENT}; }}
QProgressBar {{ background:{Color.PROGRESS_TRACK}; border:0; border-radius:0; height:5px; color:transparent; }}
QProgressBar::chunk {{ background:{Color.ACCENT}; border-radius:0; }}
QWidget#footer, QWidget#settings_footer {{ background:transparent; border-top:1px solid {Color.LINE}; }}
"""

_BAR_QSS = (f"QProgressBar {{ background:{Color.PROGRESS_TRACK}; border:none; border-radius:0; }}"
            f"QProgressBar::chunk {{ background:{Color.ACCENT}; border-radius:0; }}")
_CARD_QSS = f"QFrame {{ background:{Color.BG}; border:1px solid {Color.LINE}; border-radius:0; }}"


def apply_v5_dialog_theme(dialog: QWidget) -> None:
    """Перекрасить прежний диалог в V5, не трогая его логику."""
    dialog.setStyleSheet(DIALOG_QSS)
    # Палитра окна остаётся от прежнего стиля (замер: фон FileImportDialog
    # держался #F6F6F7 даже при «* {background:white}») — переполировать явно.
    dialog.style().unpolish(dialog)
    dialog.style().polish(dialog)
    for bar in dialog.findChildren(QProgressBar):
        if bar.styleSheet():
            bar.setStyleSheet(_BAR_QSS)
    for frame in dialog.findChildren(QFrame):
        if type(frame) is QFrame and "border-radius" in frame.styleSheet():
            frame.setStyleSheet(_CARD_QSS)
    for label in dialog.findChildren(QLabel):
        ss = label.styleSheet()
        if "font-weight: 600" in ss:
            label.setStyleSheet(ss.replace("font-weight: 600", "font-weight: 500"))


def v5_active() -> bool:
    import os
    return os.environ.get("SAYTYPE_UI", "v5").strip().lower() != "legacy"
