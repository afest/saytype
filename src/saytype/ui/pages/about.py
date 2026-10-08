"""Страница «О приложении»: знак + wordmark, описание, версия, поддержка,
компоненты под LGPL со ссылками на исходники точной версии, тексты лицензий.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ... import profile
from ...transcribe_ui_window import DONATE_PHONE, _lgpl_components, open_donate_page
from ..icons import mark_pixmap
from ..shell import GuideCanvas, PageScroll
from ..tokens import Color, Font, Grid, head_font_plain, qc
from ..widgets import Button, Cell, Label, PageHead


class _Mark(QWidget):
    def __init__(self, size: int = 48, parent=None):
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)

    def paintEvent(self, ev) -> None:  # noqa: N802
        from PySide6.QtGui import QPainter
        p = QPainter(self)
        p.drawPixmap(0, 0, mark_pixmap(self._size, Color.MARK, self.devicePixelRatioF()))
        p.end()


class AboutPage(QWidget):
    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._w = window
        from ... import __version__
        try:
            from ... import updater
            version = updater.current_version() or __version__
        except Exception:  # noqa: BLE001
            version = __version__

        content = GuideCanvas()
        v = QVBoxLayout(content)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.head = PageHead("О приложении")
        v.addWidget(self.head)

        self.body = Cell()
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(Grid.PAD, 35, Grid.PAD, 35)
        bl.setSpacing(0)

        brand = QHBoxLayout()
        brand.setContentsMargins(0, 0, 0, 30)
        brand.setSpacing(15)
        brand.addWidget(_Mark(48), 0, Qt.AlignVCenter)
        word = QLabel("SayType")
        word.setFont(head_font_plain(20, 600, -0.8))
        word.setStyleSheet(f"color:{Color.INK}; background:transparent;")
        brand.addWidget(word, 0, Qt.AlignVCenter)
        brand.addStretch(1)
        bl.addLayout(brand)

        desc = Label("Диктовка, расшифровка файлов и запись созвонов с локальным распознаванием речи. "
                     "Код приложения — под лицензией MIT.", size=Font.BODY, color=Color.MUTED, wrap=True)
        desc.setMaximumWidth(600)
        bl.addWidget(desc)
        bl.addSpacing(10)
        bl.addWidget(Label(f"Версия {version}", size=Font.SMALL, color=Color.MUTED))
        bl.addSpacing(23)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.donate_btn = Button("Поддержать", icon="heart", variant="ghost")
        self.donate_btn.setToolTip(DONATE_PHONE)
        self.donate_btn.clicked.connect(lambda: open_donate_page(window))
        self.licenses_btn = Button("Тексты лицензий", icon="folder")
        lic = profile.licenses_dir()
        if not lic.exists():
            self.licenses_btn.setEnabled(False)
            self.licenses_btn.setToolTip(f"Папка не найдена: {lic}")
        self.licenses_btn.clicked.connect(self._open_licenses)
        row.addWidget(self.licenses_btn)
        row.addWidget(self.donate_btn)
        row.addStretch(1)
        bl.addLayout(row)
        bl.addSpacing(26)

        self.details_btn = Label("▸ Компоненты и лицензии", size=Font.SMALL, color=Color.MUTED)
        self.details_btn.setCursor(Qt.PointingHandCursor)
        self.details_btn.mousePressEvent = lambda ev: self._toggle_details()  # type: ignore[assignment]
        bl.addWidget(self.details_btn)
        parts = ["<b>Компоненты под LGPL-3.0</b><br>Поставляются отдельными файлами рядом с приложением, "
                 "а не вшиты в exe, поэтому их можно заменить своей сборкой той же версии."]
        for name, ver, url in _lgpl_components():
            parts.append(f"{name} — {ver}<br>Исходники: <a href='{url}' style='color:{Color.ACCENT}'>{url}</a>")
        self.details = QLabel("<br><br>".join(parts))
        self.details.setWordWrap(True)
        self.details.setOpenExternalLinks(True)
        self.details.setTextFormat(Qt.RichText)
        self.details.setStyleSheet(f"color:{Color.MUTED}; font-size:{Font.SMALL}px; background:transparent; margin-top:12px;")
        self.details.setMaximumWidth(650)
        self.details.hide()
        bl.addWidget(self.details)
        v.addWidget(self.body)
        v.addStretch(1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(PageScroll(content))

    def _toggle_details(self) -> None:
        vis = not self.details.isVisible()
        self.details.setVisible(vis)
        self.details_btn.setText(("▾ " if vis else "▸ ") + "Компоненты и лицензии")

    def _open_licenses(self) -> None:
        path = profile.licenses_dir()
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def set_window_width(self, w: int) -> None:
        self.head.set_window_width(w)
        pad = Grid.pad(w)
        self.body.layout().setContentsMargins(pad, 35, pad, 35)
