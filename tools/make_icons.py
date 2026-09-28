"""Generate the LDTF icon set (stdlib only): SVG master, PNGs and multi-size ICOs for the tray and shortcuts.

Design ("D over a drive"): DTF's pale-blue tile with its black geometric D, standing on a dark drive with a status
light — DTF kept on your own disk. The light's colour is the app state in the tray: green = idle, blue = syncing,
red = error. Sizes up to 24 px use a simplified drawing (thicker D, bigger light, no slot).

    python tools/make_icons.py            ->  dtf_backup/assets/brand/
"""

from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "dtf_backup" / "assets" / "brand"
TILE, INK, SLOT = (0xD9, 0xF0, 0xFF), (0x0B, 0x0B, 0x0C), (0x4A, 0x51, 0x60)
LIGHTS = {"": (0x3D, 0xDC, 0x84), "-sync": (0x4C, 0x8D, 0xFF), "-error": (0xFF, 0x5A, 0x4F)}
PNG_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 180, 256)
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 256)

# Geometry on a 128-unit grid: ("box", x0, y0, x1, y1, (r_tl, r_tr, r_br, r_bl)) | ("circle", cx, cy, r)
FULL = [
    (TILE, ("box", 0, 0, 128, 128, (28, 28, 28, 28))),
    (INK, ("box", 28, 16, 100, 82, (0, 27, 27, 0))),         # D
    (TILE, ("box", 46, 33, 82, 65, (0, 9, 9, 0))),          # D counter
    (INK, ("box", 18, 92, 110, 114, (7, 7, 7, 7))),          # drive
    (SLOT, ("box", 30, 101, 62, 105, (2, 2, 2, 2))),         # drive slot
    ("LIGHT", ("circle", 97, 103, 5)),
]
SMALL = [
    (TILE, ("box", 0, 0, 128, 128, (26, 26, 26, 26))),
    (INK, ("box", 20, 12, 108, 78, (0, 30, 30, 0))),
    (TILE, ("box", 46, 32, 84, 58, (0, 7, 7, 0))),
    (INK, ("box", 12, 88, 116, 120, (10, 10, 10, 10))),
    ("LIGHT", ("circle", 96, 104, 10)),
]


def sd_box(px: float, py: float, x0: float, y0: float, x1: float, y1: float, r: tuple) -> float:
    cx, cy, hw, hh = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    dx, dy = px - cx, py - cy
    rad = (r[1] if dy < 0 else r[2]) if dx > 0 else (r[0] if dy < 0 else r[3])
    qx, qy = abs(dx) - hw + rad, abs(dy) - hh + rad
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - rad


def sd(shape: tuple, px: float, py: float) -> float:
    if shape[0] == "box":
        return sd_box(px, py, *shape[1:])
    _, cx, cy, r = shape
    return math.hypot(px - cx, py - cy) - r


def render(size: int, light: tuple) -> bytes:
    """RGBA pixels, row by row from the top."""
    layers = SMALL if size <= 24 else FULL
    unit = 128 / size                    # grid units per pixel
    out = bytearray()
    for y in range(size):
        for x in range(size):
            px, py = (x + 0.5) * unit, (y + 0.5) * unit
            r = g = b = a = 0.0
            for color, shape in layers:
                cov = min(1.0, max(0.0, 0.5 - sd(shape, px, py) / unit))
                if cov <= 0:
                    continue
                c = light if color == "LIGHT" else color
                # "over" compositing, premultiplied
                r = c[0] * cov + r * (1 - cov)
                g = c[1] * cov + g * (1 - cov)
                b = c[2] * cov + b * (1 - cov)
                a = cov + a * (1 - cov)
            if a > 0:
                out += bytes((round(r / a), round(g / a), round(b / a), round(a * 255)))
            else:
                out += b"\0\0\0\0"
    return bytes(out)


def png(size: int, rgba: bytes) -> bytes:
    raw = b"".join(b"\0" + rgba[y * size * 4:(y + 1) * size * 4] for y in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def dib(size: int, rgba: bytes) -> bytes:
    """Classic 32-bit BMP icon entry (bottom-up BGRA + empty AND mask): the most compatible format for small sizes."""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    rows = []
    for y in range(size - 1, -1, -1):
        row = rgba[y * size * 4:(y + 1) * size * 4]
        rows.append(b"".join(bytes((row[i + 2], row[i + 1], row[i], row[i + 3])) for i in range(0, len(row), 4)))
    mask_row = b"\0" * (((size + 31) // 32) * 4)
    return header + b"".join(rows) + mask_row * size


def ico(images: dict[int, bytes]) -> bytes:
    entries, blobs = [], []
    offset = 6 + 16 * len(images)
    for size in sorted(images):
        data = png(size, images[size]) if size >= 256 else dib(size, images[size])
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset))
        blobs.append(data)
        offset += len(data)
    return struct.pack("<HHH", 0, 1, len(images)) + b"".join(entries) + b"".join(blobs)


def svg(light: tuple) -> str:
    def box(x0: float, y0: float, x1: float, y1: float, r: tuple) -> str:
        tl, tr, br, bl = r
        return (f"M{x0 + tl} {y0}H{x1 - tr}" + (f"A{tr} {tr} 0 0 1 {x1} {y0 + tr}" if tr else "")
                + f"V{y1 - br}" + (f"A{br} {br} 0 0 1 {x1 - br} {y1}" if br else "")
                + f"H{x0 + bl}" + (f"A{bl} {bl} 0 0 1 {x0} {y1 - bl}" if bl else "")
                + f"V{y0 + tl}" + (f"A{tl} {tl} 0 0 1 {x0 + tl} {y0}" if tl else "") + "Z")

    def hexc(c: tuple) -> str:
        return "#%02x%02x%02x" % c
    tile, d, counter, drive, slot, lamp = FULL
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">'
            f'<path fill="{hexc(TILE)}" d="{box(*tile[1][1:])}"/>'
            f'<path fill="{hexc(INK)}" fill-rule="evenodd" d="{box(*d[1][1:])}{box(*counter[1][1:])}"/>'
            f'<path fill="{hexc(INK)}" d="{box(*drive[1][1:])}"/>'
            f'<path fill="{hexc(SLOT)}" d="{box(*slot[1][1:])}"/>'
            f'<circle cx="{lamp[1][1]}" cy="{lamp[1][2]}" r="{lamp[1][3]}" fill="{hexc(light)}"/></svg>\n')


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for suffix, light in LIGHTS.items():
        images = {s: render(s, light) for s in sorted(set(ICO_SIZES) | (set(PNG_SIZES) if not suffix else set()))}
        (OUT / f"ldtf{suffix}.ico").write_bytes(ico({s: images[s] for s in ICO_SIZES}))
        (OUT / f"ldtf{suffix}.svg").write_text(svg(light), encoding="utf-8")
        if not suffix:
            for s in PNG_SIZES:
                (OUT / f"ldtf-{s}.png").write_bytes(png(s, images[s]))
    print(f"icons -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
