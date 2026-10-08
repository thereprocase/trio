#!/usr/bin/env python3
"""Generate the dashboard's web-app icons with the Python standard library.

Writes PNGs into server/web/icons/ (the committed copies are what the server
serves; rerun this only to change the artwork):

  icon-192.png, icon-512.png                 "any" purpose, rounded square
  icon-maskable-192.png, icon-maskable-512.png  full bleed, glyph inside the
                                             80% safe zone launchers crop to
  apple-touch-icon.png (180)                 full bleed and opaque; iOS rounds
                                             the corners itself and renders any
                                             transparency as black
  badge-96.png                               white silhouette on transparent,
                                             for the Android status bar

The artwork is a chat bubble with three dots, drawn with signed distance
functions so edges are anti-aliased without supersampling. Colours come from
the light-1 tokens in server/web/css/00-tokens.css (--accent, --accent-strong).

No Pillow on purpose: the hub venv does not ship it and the dashboard adds no
dependencies. zlib + struct are enough to write a valid RGBA PNG.

Usage: python3 tools/make-pwa-icons.py [output_dir]
"""
from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

ACCENT = (0x3D, 0x7A, 0x63)          # --accent
ACCENT_STRONG = (0x31, 0x64, 0x51)   # --accent-strong
WHITE = (0xFF, 0xFF, 0xFF)

OUT_DIR = Path(__file__).resolve().parent.parent / "server" / "web" / "icons"


# ── signed distance functions, in unit-square coordinates ──

def sd_round_rect(px, py, cx, cy, hw, hh, r):
    qx = abs(px - cx) - (hw - r)
    qy = abs(py - cy) - (hh - r)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    return outside + inside - r


def sd_circle(px, py, cx, cy, r):
    return math.hypot(px - cx, py - cy) - r


def sd_triangle(px, py, a, b, c):
    """Exact signed distance to a triangle (negative inside)."""
    def sub(u, v):
        return (u[0] - v[0], u[1] - v[1])

    def dot(u, v):
        return u[0] * v[0] + u[1] * v[1]

    p = (px, py)
    e0, e1, e2 = sub(b, a), sub(c, b), sub(a, c)
    v0, v1, v2 = sub(p, a), sub(p, b), sub(p, c)

    def edge(v, e):
        t = max(0.0, min(1.0, dot(v, e) / dot(e, e)))
        return (v[0] - e[0] * t, v[1] - e[1] * t)

    pq0, pq1, pq2 = edge(v0, e0), edge(v1, e1), edge(v2, e2)
    s = 1.0 if e0[0] * e2[1] - e0[1] * e2[0] > 0 else -1.0
    # Component-wise minimum: nearest edge for the distance, and the point is
    # inside only when it is on the inner side of all three edges.
    dist_sq = min(dot(pq0, pq0), dot(pq1, pq1), dot(pq2, pq2))
    side = min(s * (v0[0] * e0[1] - v0[1] * e0[0]),
               s * (v1[0] * e1[1] - v1[1] * e1[0]),
               s * (v2[0] * e2[1] - v2[1] * e2[0]))
    return -math.sqrt(dist_sq) * (1.0 if side > 0 else -1.0)


DOTS = ((0.385, 0.455), (0.5, 0.455), (0.615, 0.455))


def glyph(px, py):
    """(bubble distance, dots distance) for the chat glyph at scale 1."""
    body = sd_round_rect(px, py, 0.5, 0.455, 0.275, 0.2, 0.09)
    tail = sd_triangle(px, py, (0.33, 0.56), (0.47, 0.6), (0.29, 0.775))
    dots = min(sd_circle(px, py, x, y, 0.037) for x, y in DOTS)
    return min(body, tail), dots


def coverage(dist, size):
    """Anti-aliased coverage of a shape from its distance, one pixel wide."""
    return max(0.0, min(1.0, 0.5 - dist * size))


def mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def render(size, *, background, rounded, glyph_scale, silhouette=False):
    """RGBA rows for one icon."""
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            px, py = (x + 0.5) / size, (y + 0.5) / size
            gx, gy = (px - 0.5) / glyph_scale + 0.5, (py - 0.5) / glyph_scale + 0.5
            bubble, dots = glyph(gx, gy)
            bubble_cov = coverage(bubble * glyph_scale, size)
            dots_cov = coverage(dots * glyph_scale, size)
            if silhouette:
                alpha = bubble_cov * (1.0 - dots_cov)
                row += bytes((*WHITE, round(alpha * 255)))
                continue
            # A gentle top-to-bottom shade keeps the flat accent from reading
            # as a placeholder square at launcher size.
            base = mix(ACCENT, ACCENT_STRONG, py) if background else ACCENT
            colour = mix(base, WHITE, bubble_cov)
            colour = mix(colour, ACCENT, dots_cov * bubble_cov)
            alpha = 1.0
            if rounded:
                alpha = coverage(sd_round_rect(px, py, 0.5, 0.5, 0.5, 0.5, 0.22), size)
            row += bytes((*colour, round(alpha * 255)))
        rows.append(bytes(row))
    return rows


def png_bytes(rows, size):
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + r for r in rows)   # filter type 0 on every row
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


ICONS = (
    ("icon-192.png", 192, dict(background=True, rounded=True, glyph_scale=1.0)),
    ("icon-512.png", 512, dict(background=True, rounded=True, glyph_scale=1.0)),
    ("icon-maskable-192.png", 192, dict(background=True, rounded=False, glyph_scale=0.86)),
    ("icon-maskable-512.png", 512, dict(background=True, rounded=False, glyph_scale=0.86)),
    ("apple-touch-icon.png", 180, dict(background=True, rounded=False, glyph_scale=0.95)),
    ("badge-96.png", 96, dict(background=False, rounded=False, glyph_scale=1.3, silhouette=True)),
)


def main(argv):
    out = Path(argv[1]) if len(argv) > 1 else OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    for name, size, opts in ICONS:
        (out / name).write_bytes(png_bytes(render(size, **opts), size))
        print(f"wrote {out / name} ({size}x{size})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
