"""Génère les icônes PNG de rdpm/ui/assets/ (sans dépendance : zlib + struct).

    python tools/make_icons.py

Chaque icône est une liste de formes vectorielles en coordonnées 0..1, rastérisée avec
suréchantillonnage (anticrénelage). Les PNG produits sont commités : ce script ne sert qu'à les
régénérer après une retouche.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "rdpm" / "ui" / "assets"
SS = 4  # sous-échantillons par axe


def rgba(hex_color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha


# --- formes : fonctions (x, y) -> bool --------------------------------------------------------
def rect(x0, y0, x1, y1, r=0.0):
    def inside(x, y):
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            return False
        if r <= 0:
            return True
        cx = min(max(x, x0 + r), x1 - r)
        cy = min(max(y, y0 + r), y1 - r)
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r
    return inside


def ellipse(cx, cy, rx, ry):
    return lambda x, y: ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0


def polygon(*pts):
    def inside(x, y):
        hit = False
        n = len(pts)
        for i in range(n):
            (xa, ya), (xb, yb) = pts[i], pts[(i + 1) % n]
            if (ya > y) != (yb > y) and x < (xb - xa) * (y - ya) / (yb - ya) + xa:
                hit = not hit
        return hit
    return inside


# --- icônes ------------------------------------------------------------------------------------
WIN_BLUE = rgba("#0078D4")
TUX_BLACK, TUX_WHITE, TUX_ORANGE = rgba("#1B1B1F"), rgba("#F7F7F2"), rgba("#F4A300")
HETZNER_RED, WHITE = rgba("#D50C2D"), rgba("#FFFFFF")
ACCENT = rgba("#3B5BDB")

ICONS = {
    # Logo Windows : quatre carreaux.
    "os-windows": [
        (rect(0.08, 0.08, 0.47, 0.47), WIN_BLUE), (rect(0.53, 0.08, 0.92, 0.47), WIN_BLUE),
        (rect(0.08, 0.53, 0.47, 0.92), WIN_BLUE), (rect(0.53, 0.53, 0.92, 0.92), WIN_BLUE),
    ],
    # Manchot stylisé (Tux) : corps noir, ventre blanc, yeux, bec et pattes orange.
    "os-linux": [
        (ellipse(0.50, 0.55, 0.33, 0.40), TUX_BLACK),
        (ellipse(0.50, 0.24, 0.21, 0.20), TUX_BLACK),
        (ellipse(0.50, 0.63, 0.22, 0.28), TUX_WHITE),
        (ellipse(0.42, 0.22, 0.065, 0.085), TUX_WHITE), (ellipse(0.58, 0.22, 0.065, 0.085), TUX_WHITE),
        (ellipse(0.43, 0.24, 0.03, 0.04), TUX_BLACK), (ellipse(0.57, 0.24, 0.03, 0.04), TUX_BLACK),
        (polygon((0.38, 0.33), (0.62, 0.33), (0.50, 0.42)), TUX_ORANGE),
        (ellipse(0.33, 0.91, 0.16, 0.07), TUX_ORANGE), (ellipse(0.67, 0.91, 0.16, 0.07), TUX_ORANGE),
    ],
    # Fournisseur Hetzner : carré rouge arrondi et « H » blanc.
    "provider-hetzner": [
        (rect(0.04, 0.04, 0.96, 0.96, 0.18), HETZNER_RED),
        (rect(0.26, 0.24, 0.40, 0.76), WHITE), (rect(0.60, 0.24, 0.74, 0.76), WHITE),
        (rect(0.26, 0.44, 0.74, 0.56), WHITE),
    ],
    # Application : écran blanc sur fond bleu (bureau dans le nuage).
    "app": [
        (rect(0.02, 0.02, 0.98, 0.98, 0.22), ACCENT),
        (rect(0.18, 0.22, 0.82, 0.66, 0.05), WHITE),
        (rect(0.23, 0.27, 0.77, 0.61), ACCENT),
        (rect(0.44, 0.66, 0.56, 0.76), WHITE),
        (rect(0.32, 0.75, 0.68, 0.81, 0.03), WHITE),
    ],
}
SIZES = {"os-windows": (16, 32), "os-linux": (16, 32), "provider-hetzner": (16, 32), "app": (16, 32, 64)}


def render(shapes, size: int) -> bytes:
    rows = []
    step = 1.0 / (size * SS)
    for py in range(size):
        row = bytearray([0])  # filtre PNG « None »
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(SS):
                y = (py * SS + sy + 0.5) * step
                for sx in range(SS):
                    x = (px * SS + sx + 0.5) * step
                    color = None
                    for inside, c in shapes:
                        if inside(x, y):
                            color = c
                    if color:
                        a = color[3]
                        acc[0] += color[0] * a
                        acc[1] += color[1] * a
                        acc[2] += color[2] * a
                        acc[3] += a
            alpha = acc[3] / (SS * SS)
            if acc[3]:
                row += bytes((round(acc[0] / acc[3]), round(acc[1] / acc[3]), round(acc[2] / acc[3]),
                              round(alpha)))
            else:
                row += bytes(4)
        rows.append(bytes(row))
    return png(size, size, b"".join(rows))


def png(width: int, height: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8 bits, RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + \
        chunk(b"IEND", b"")


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    for name, shapes in ICONS.items():
        for size in SIZES[name]:
            path = ASSETS / f"{name}-{size}.png"
            path.write_bytes(render(shapes, size))
            print(path.relative_to(ASSETS.parents[2]))


if __name__ == "__main__":
    main()
