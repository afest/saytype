"""Нарисовать иконку приложения из знака V5 — assets/saytype.ico и PNG-эталоны.

Источник один — `saytype.ui.icons.app_tile_pixmap` (знак на плитке, цвета в
`tokens.Color.APP_TILE*`). Поменяли знак или цвет — перезапустить скрипт и
пересобрать: .ico идёт в exe, установщик Velopack, splash и ярлык на рабочем
столе (приложение копирует его в профиль при старте).

Каждый размер .ico рисуется отдельно, а не ужимается из 256: на 16–32 px так
резче.

Запуск из корня репозитория:
    python tools/make_app_icon.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image  # noqa: E402
from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _to_pil(pixmap) -> Image.Image:
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    pixmap.save(buf, "PNG")
    from io import BytesIO
    return Image.open(BytesIO(bytes(buf.data()))).convert("RGBA")


def main() -> None:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)  # noqa: F841 — QPixmap без него не рисуется
    from saytype.ui.icons import app_tile_pixmap
    from saytype.ui.tokens import Color

    assets = ROOT / "assets"
    frames = [_to_pil(app_tile_pixmap(s, Color.APP_TILE)) for s in ICO_SIZES]
    big = frames[-1]
    big.save(assets / "saytype.ico", format="ICO",
             sizes=[(s, s) for s in ICO_SIZES], append_images=frames[:-1])
    big.save(assets / "app.png")
    for name, color in (("idle", Color.APP_TILE), ("recording", Color.APP_TILE_RECORDING),
                        ("processing", Color.APP_TILE_PROCESSING)):
        _to_pil(app_tile_pixmap(32, color)).save(assets / f"{name}.png")
    print(f"готово: {assets / 'saytype.ico'} ({', '.join(map(str, ICO_SIZES))} px) + PNG-эталоны")


if __name__ == "__main__":
    main()
