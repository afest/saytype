"""Минимальный разбор атрибута `d` SVG-пути в `QPainterPath`.

Нужен, чтобы рисовать цифры и иконки прототипа тем же контуром без модуля
QtSvgWidgets (он исключён из сборки). Поддерживаются команды M L H V C S Q T A Z
в абсолютной и относительной форме — ровно то, что встречается в прототипе.
"""
from __future__ import annotations

import math
import re
from functools import lru_cache

from PySide6.QtCore import QPointF
from PySide6.QtGui import QPainterPath

_TOKEN = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _tokens(d: str) -> list:
    out = []
    for tok in _TOKEN.findall(d):
        if tok.isalpha():
            out.append(tok)
        else:
            out.append(float(tok))
    return out


def _arc_to(path: QPainterPath, x1, y1, rx, ry, phi_deg, large, sweep, x2, y2) -> None:
    """Эллиптическая дуга SVG → кубические кривые (формулы из спецификации SVG, F.6)."""
    if rx == 0 or ry == 0:
        path.lineTo(x2, y2)
        return
    phi = math.radians(phi_deg)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2.0, (y1 - y2) / 2.0
    x1p = cos_phi * dx + sin_phi * dy
    y1p = -sin_phi * dx + cos_phi * dy
    rx, ry = abs(rx), abs(ry)
    lam = (x1p ** 2) / (rx ** 2) + (y1p ** 2) / (ry ** 2)
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx ** 2 * ry ** 2 - rx ** 2 * y1p ** 2 - ry ** 2 * x1p ** 2
    den = rx ** 2 * y1p ** 2 + ry ** 2 * x1p ** 2
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        coef = -coef
    cxp = coef * (rx * y1p / ry)
    cyp = coef * (-(ry * x1p) / rx)
    cx = cos_phi * cxp - sin_phi * cyp + (x1 + x2) / 2.0
    cy = sin_phi * cxp + cos_phi * cyp + (y1 + y2) / 2.0

    def angle(ux, uy, vx, vy):
        dot = ux * vx + uy * vy
        length = math.hypot(ux, uy) * math.hypot(vx, vy)
        a = math.acos(max(-1.0, min(1.0, dot / length))) if length else 0.0
        if ux * vy - uy * vx < 0:
            a = -a
        return a

    theta1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dtheta = angle((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dtheta > 0:
        dtheta -= 2 * math.pi
    elif sweep and dtheta < 0:
        dtheta += 2 * math.pi
    segments = max(1, int(math.ceil(abs(dtheta) / (math.pi / 2))))
    delta = dtheta / segments
    t = 4.0 / 3.0 * math.tan(delta / 4.0)
    th = theta1
    for _ in range(segments):
        cos1, sin1 = math.cos(th), math.sin(th)
        cos2, sin2 = math.cos(th + delta), math.sin(th + delta)
        p1 = (cos1 - t * sin1, sin1 + t * cos1)
        p2 = (cos2 + t * sin2, sin2 - t * cos2)
        e = (cos2, sin2)

        def tr(px, py):
            return (cos_phi * rx * px - sin_phi * ry * py + cx, sin_phi * rx * px + cos_phi * ry * py + cy)

        c1 = tr(*p1)
        c2 = tr(*p2)
        ep = tr(*e)
        path.cubicTo(QPointF(*c1), QPointF(*c2), QPointF(*ep))
        th += delta


@lru_cache(maxsize=512)
def parse_path(d: str) -> QPainterPath:
    """`d` → QPainterPath (кэшируется: одни и те же глифы рисуются тысячи раз)."""
    path = QPainterPath()
    toks = _tokens(d)
    i = 0
    cmd = None
    cx = cy = 0.0        # текущая точка
    sx = sy = 0.0        # начало субпути
    last_c = None        # последняя контрольная точка для S/T
    last_q = None

    def nxt():
        nonlocal i
        v = toks[i]
        i += 1
        return v

    while i < len(toks):
        t = toks[i]
        if isinstance(t, str):
            cmd = t
            i += 1
            if cmd in "Zz":
                path.closeSubpath()
                cx, cy = sx, sy
                last_c = last_q = None
                continue
        if cmd is None:
            break
        rel = cmd.islower()
        c = cmd.upper()
        if c == "M":
            x, y = nxt(), nxt()
            if rel:
                x, y = cx + x, cy + y
            path.moveTo(x, y)
            cx, cy = x, y
            sx, sy = x, y
            cmd = "l" if rel else "L"   # последующие пары — lineTo
            last_c = last_q = None
        elif c == "L":
            x, y = nxt(), nxt()
            if rel:
                x, y = cx + x, cy + y
            path.lineTo(x, y)
            cx, cy = x, y
            last_c = last_q = None
        elif c == "H":
            x = nxt()
            if rel:
                x = cx + x
            path.lineTo(x, cy)
            cx = x
            last_c = last_q = None
        elif c == "V":
            y = nxt()
            if rel:
                y = cy + y
            path.lineTo(cx, y)
            cy = y
            last_c = last_q = None
        elif c == "C":
            x1, y1, x2, y2, x, y = (nxt() for _ in range(6))
            if rel:
                x1, y1, x2, y2, x, y = cx + x1, cy + y1, cx + x2, cy + y2, cx + x, cy + y
            path.cubicTo(x1, y1, x2, y2, x, y)
            last_c = (x2, y2)
            last_q = None
            cx, cy = x, y
        elif c == "S":
            x2, y2, x, y = (nxt() for _ in range(4))
            if rel:
                x2, y2, x, y = cx + x2, cy + y2, cx + x, cy + y
            if last_c:
                x1, y1 = 2 * cx - last_c[0], 2 * cy - last_c[1]
            else:
                x1, y1 = cx, cy
            path.cubicTo(x1, y1, x2, y2, x, y)
            last_c = (x2, y2)
            last_q = None
            cx, cy = x, y
        elif c == "Q":
            x1, y1, x, y = (nxt() for _ in range(4))
            if rel:
                x1, y1, x, y = cx + x1, cy + y1, cx + x, cy + y
            path.quadTo(x1, y1, x, y)
            last_q = (x1, y1)
            last_c = None
            cx, cy = x, y
        elif c == "T":
            x, y = nxt(), nxt()
            if rel:
                x, y = cx + x, cy + y
            if last_q:
                x1, y1 = 2 * cx - last_q[0], 2 * cy - last_q[1]
            else:
                x1, y1 = cx, cy
            path.quadTo(x1, y1, x, y)
            last_q = (x1, y1)
            last_c = None
            cx, cy = x, y
        elif c == "A":
            rx, ry, rot, large, sweep, x, y = (nxt() for _ in range(7))
            if rel:
                x, y = cx + x, cy + y
            _arc_to(path, cx, cy, rx, ry, rot, bool(large), bool(sweep), x, y)
            cx, cy = x, y
            last_c = last_q = None
        else:
            break
    return path
