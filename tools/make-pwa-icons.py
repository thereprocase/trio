#!/usr/bin/env python3
"""Generate the dashboard's web-app icons.

Writes SVG sources and PNG renders into server/web/icons/ (the committed PNGs
are what the server serves; rerun this only to change the artwork):
  icon-192.png, icon-512.png                 rounded tile, "any" purpose
  icon-maskable-192.png, icon-maskable-512.png  full bleed, glyph scaled into
                                             the 80% safe zone launchers crop to
  apple-touch-icon.png (180)                 full bleed and opaque; iOS rounds
                                             the corners itself
  badge-96.png                               white silhouette on transparent,
                                             for the Android status bar

The artwork: a speech bubble drawn as four separate arcs, one colour per voice
(trio and quartet are conversations between several sessions), a tail in the
gap between two voices, and a spark at the centre, on a dark Gridline tile.

The SVGs are built with the standard library. Rendering the PNGs needs
`rsvg-convert` (librsvg) on the machine that regenerates them; the hub never
runs this script, so the server gains no dependency.

A hub can carry its own icon set (NTH_APP_ICON_DIR on nth-web), so one phone
can tell two installed hubs apart. --preset recolours the tile and halo and
--emblem adds a corner badge; render a set into its own directory and point
that hub at it.

Usage: python3 tools/make-pwa-icons.py [output_dir] [--preset NAME]
                                      [--glyph bubble|cross|star] [--emblem cross]
"""
import argparse
import math
import shutil
import subprocess
import sys
from pathlib import Path

# Bubble centre and radius in the 512-unit artboard.
CX, CY, R = 256, 244, 128

# Four voices, one per quadrant: (start angle, end angle, gradient start,
# gradient end). The gap at 135 degrees holds the tail.
ARCS = [
    (235, 305, "#5fe3b0", "#3fc8ff"),
    (325, 395, "#ffd36b", "#ff9f43"),
    (55, 118, "#ff6b8b", "#ff4f6a"),
    (152, 215, "#4fa8ff", "#7c6bff"),
]

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "server" / "web" / "icons"

# Tile palettes: (background gradient start, end, halo). "gridline" is the
# built-in icon.
PRESETS = {
    "gridline": ("#10261f", "#04090a", "#7cf5c8"),
    "ember": ("#3a1014", "#0b0405", "#ff8a5c"),
    "dusk": ("#1d1636", "#06040c", "#a98bff"),
    "ocean": ("#0b2038", "#03070d", "#5fb7ff"),
}

# Corner badges, drawn in glyph coordinates so the maskable icon keeps them
# inside the safe zone: (fill, ring) of a disc low on the right of the bubble.
EMBLEM_CENTRE, EMBLEM_RADIUS = (378, 362), 60
EMBLEMS = ("none", "cross")

# Whole-icon glyphs. "bubble" is the built-in four-voice speech bubble. The
# others draw rays from the centre, one voice colour per ray, over a faint
# copy of the bubble so every hub's icon still reads as a conversation:
# (ray angles in degrees, ray length, ray width).
GLYPHS = {
    "bubble": None,
    "cross": ((-90, 0, 90, 180), 150, 96),
    "star": ((-90, -30, 30, 90, 150, 210), 160, 66),
}

RENDERS = (
    ("icon.svg", "icon-512.png", 512),
    ("icon.svg", "icon-192.png", 192),
    ("icon-maskable.svg", "icon-maskable-512.png", 512),
    ("icon-maskable.svg", "icon-maskable-192.png", 192),
    ("apple-touch.svg", "apple-touch-icon.png", 180),
    ("badge.svg", "badge-96.png", 96),
)


def pt(angle, radius=R):
    """The point at `angle` degrees on a circle of `radius` around the centre."""
    return (CX + radius * math.cos(math.radians(angle)),
            CY + radius * math.sin(math.radians(angle)))


def arc(a0, a1):
    """SVG path data for one voice's arc, clockwise from a0 to a1."""
    (x0, y0), (x1, y1) = pt(a0), pt(a1)
    return f"M {x0:.1f} {y0:.1f} A {R} {R} 0 0 1 {x1:.1f} {y1:.1f}"


def spark(x, y, big, small):
    """An eight-point star: long points on the axes, short ones between."""
    points = []
    for k in range(8):
        reach = big if k % 2 == 0 else small
        angle = math.radians(-90 + k * 45)
        points.append(f"{x + reach * math.cos(angle):.1f},"
                      f"{y + reach * math.sin(angle):.1f}")
    return "M " + " L ".join(points) + " Z"


def tail_path():
    """(path data, base point, tip) of the tail between two voices."""
    a, b = pt(121, R - 4), pt(149, R - 4)
    tip = pt(135, R + 96)
    path = (f"M {a[0]:.1f} {a[1]:.1f} L {tip[0]:.1f} {tip[1]:.1f} "
            f"L {b[0]:.1f} {b[1]:.1f} Z")
    return path, a, tip


def mono_glyph():
    """The glyph as a single white silhouette, for the notification badge."""
    tail, _a, _tip = tail_path()
    arcs = "".join(f'<path d="{arc(a0, a1)}"/>' for a0, a1, *_ in ARCS)
    return (f'<g fill="none" stroke="#fff" stroke-width="34" stroke-linecap="round">{arcs}</g>'
            f'<path d="{tail}" fill="#fff" stroke="#fff" stroke-width="14" '
            f'stroke-linejoin="round"/>'
            f'<path d="{spark(CX, CY, 62, 14)}" fill="#fff"/>')


def colour_glyph():
    """(gradient defs, artwork) for the full-colour glyph: a blurred bloom
    copy underneath, then the crisp arcs, tail and spark on top."""
    tail, a, tip = tail_path()
    grads, paths = [], []
    for i, (a0, a1, c0, c1) in enumerate(ARCS):
        (x0, y0), (x1, y1) = pt(a0), pt(a1)
        grads.append(f'<linearGradient id="g{i}" gradientUnits="userSpaceOnUse" '
                     f'x1="{x0:.0f}" y1="{y0:.0f}" x2="{x1:.0f}" y2="{y1:.0f}">'
                     f'<stop offset="0" stop-color="{c0}"/>'
                     f'<stop offset="1" stop-color="{c1}"/></linearGradient>')
        paths.append(f'<path d="{arc(a0, a1)}" stroke="url(#g{i})"/>')
    tail_gradient = (f'<linearGradient id="tail" gradientUnits="userSpaceOnUse" '
                     f'x1="{a[0]:.0f}" y1="{a[1]:.0f}" x2="{tip[0]:.0f}" y2="{tip[1]:.0f}">'
                     f'<stop offset="0" stop-color="#ff4f6a"/>'
                     f'<stop offset="1" stop-color="#7c6bff"/></linearGradient>')
    body = (f'<g fill="none" stroke-width="{{w}}" stroke-linecap="round">{"".join(paths)}</g>'
            f'<path d="{tail}" fill="url(#tail)" stroke="url(#tail)" '
            f'stroke-width="{{tw}}" stroke-linejoin="round"/>')
    bloom = (f'<g filter="url(#bloom)" opacity="0.95">{body.format(w=34, tw=16)}'
             f'<path d="{spark(CX, CY, 66, 15)}" fill="#bfffe6"/></g>')
    crisp = (body.format(w=30, tw=12)
             + f'<path d="{spark(CX, CY, 62, 13)}" fill="url(#sparkfill)"/>')
    return "".join(grads) + tail_gradient, bloom + crisp


def emblem(kind):
    """A white cross on a red disc with a white ring, or nothing."""
    if kind != "cross":
        return ""
    (x, y), r = EMBLEM_CENTRE, EMBLEM_RADIUS
    arm, half = r * 0.62, r * 0.2
    cross = (f"M {x - half:.1f} {y - arm:.1f} H {x + half:.1f} V {y - half:.1f} "
             f"H {x + arm:.1f} V {y + half:.1f} H {x + half:.1f} V {y + arm:.1f} "
             f"H {x - half:.1f} V {y + half:.1f} H {x - arm:.1f} V {y - half:.1f} "
             f"H {x - half:.1f} Z")
    return (f'<circle cx="{x}" cy="{y}" r="{r + 10}" fill="#04090a" opacity="0.55"/>'
            f'<circle cx="{x}" cy="{y}" r="{r}" fill="#e8304a" stroke="#fff" stroke-width="9"/>'
            f'<path d="{cross}" fill="#fff"/>')


def ray_glyph(glyph):
    """(gradient defs, artwork) for a ray glyph: the bubble at low opacity,
    then bloomed rays in the voice colours and the spark on top."""
    angles, length, width = GLYPHS[glyph]
    bubble_defs, bubble_art = colour_glyph()
    grads, rays = [], []
    for i, angle in enumerate(angles):
        _a0, _a1, c0, c1 = ARCS[i % len(ARCS)]
        x1, y1 = pt(angle, length)
        grads.append(f'<linearGradient id="r{i}" gradientUnits="userSpaceOnUse" '
                     f'x1="{CX}" y1="{CY}" x2="{x1:.0f}" y2="{y1:.0f}">'
                     f'<stop offset="0" stop-color="{c0}"/>'
                     f'<stop offset="1" stop-color="{c1}"/></linearGradient>')
        rays.append(f'<line x1="{CX}" y1="{CY}" x2="{x1:.1f}" y2="{y1:.1f}" '
                    f'stroke="url(#r{i})"/>')
    body = f'<g stroke-width="{{w}}" stroke-linecap="round">{"".join(rays)}</g>'
    art = (f'<g opacity="0.24">{bubble_art}</g>'
           f'<g filter="url(#bloom)" opacity="0.9">{body.format(w=width + 6)}</g>'
           f'{body.format(w=width)}'
           f'<path d="{spark(CX, CY, 62, 13)}" fill="url(#sparkfill)"/>')
    return bubble_defs + "".join(grads), art


def mono_rays(glyph):
    """A ray glyph as a white silhouette, for the notification badge."""
    angles, length, width = GLYPHS[glyph]
    lines = "".join(f'<line x1="{CX}" y1="{CY}" x2="{pt(a, length)[0]:.1f}" '
                    f'y2="{pt(a, length)[1]:.1f}"/>' for a in angles)
    return f'<g stroke="#fff" stroke-width="{width}" stroke-linecap="round">{lines}</g>'


def tile(rounded, scale=1.0, preset="gridline", emblem_kind="none", glyph="bubble"):
    """The full icon: Gridline tile, halo, and the glyph scaled about the centre."""
    bg0, bg1, halo = PRESETS[preset]
    defs, art = colour_glyph() if GLYPHS[glyph] is None else ray_glyph(glyph)
    art += emblem(emblem_kind)
    grid = "".join(f'<path d="M {v} 0 V 512 M 0 {v} H 512"/>' for v in range(32, 512, 32))
    clip = f'<rect width="512" height="512" rx="{112 if rounded else 0}"/>'
    offset = 256 * (1 - scale)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs>\n'
        '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{bg0}"/><stop offset="1" stop-color="{bg1}"/>'
        '</linearGradient>\n'
        '<radialGradient id="halo" cx="0.5" cy="0.47" r="0.42">'
        f'<stop offset="0" stop-color="{halo}" stop-opacity="0.22"/>'
        f'<stop offset="1" stop-color="{halo}" stop-opacity="0"/></radialGradient>\n'
        '<radialGradient id="sparkfill" cx="0.5" cy="0.5" r="0.5">'
        '<stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#e6fff6"/>'
        '</radialGradient>\n'
        '<filter id="bloom" x="-30%" y="-30%" width="160%" height="160%">'
        '<feGaussianBlur stdDeviation="9"/></filter>\n'
        f'<clipPath id="tile">{clip}</clipPath>{defs}</defs>\n'
        '<g clip-path="url(#tile)"><rect width="512" height="512" fill="url(#bg)"/>\n'
        f'<g stroke="#fff" stroke-opacity="0.045" stroke-width="2">{grid}</g>'
        '<rect width="512" height="512" fill="url(#halo)"/></g>\n'
        f'<g transform="translate({offset:.1f} {offset:.1f}) scale({scale})">{art}</g></svg>'
    )


def mono_emblem(kind):
    """The emblem as a white ring with a solid cross, for the badge, so the
    status-bar icon tells hubs apart too."""
    if kind != "cross":
        return ""
    (x, y), r = EMBLEM_CENTRE, EMBLEM_RADIUS
    arm, half = r * 0.62, r * 0.2
    return (f'<circle cx="{x}" cy="{y}" r="{r}" fill="none" stroke="#fff" stroke-width="14"/>'
            f'<path d="M {x - half:.1f} {y - arm:.1f} H {x + half:.1f} V {y + arm:.1f} '
            f'H {x - half:.1f} Z M {x - arm:.1f} {y - half:.1f} H {x + arm:.1f} '
            f'V {y + half:.1f} H {x - arm:.1f} Z" fill="#fff"/>')


def badge(emblem_kind="none", glyph="bubble"):
    """The monochrome badge, scaled to keep the stroke ends inside the canvas.
    Android draws the badge from its alpha channel, so an emblem is cut out of
    the glyph with a mask (a painted black disc would show as solid)."""
    head = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
    place = '<g transform="translate(35.8 46.8) scale(0.86)">'
    shape = mono_glyph() if GLYPHS[glyph] is None else mono_rays(glyph)
    if emblem_kind == "none":
        return f'{head}{place}{shape}</g></svg>'
    (x, y), r = EMBLEM_CENTRE, EMBLEM_RADIUS
    cut = ('<defs><mask id="cut"><rect width="512" height="512" fill="#fff"/>'
           f'<circle cx="{x}" cy="{y}" r="{r + 14}" fill="#000"/></mask></defs>')
    return (f'{head}{cut}{place}<g mask="url(#cut)">{shape}</g>'
            f'{mono_emblem(emblem_kind)}</g></svg>')


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the dashboard's web-app icons.")
    parser.add_argument("output_dir", nargs="?", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--preset", choices=sorted(PRESETS), default="gridline")
    parser.add_argument("--emblem", choices=EMBLEMS, default="none")
    parser.add_argument("--glyph", choices=sorted(GLYPHS), default="bubble")
    args = parser.parse_args()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    look = dict(preset=args.preset, emblem_kind=args.emblem, glyph=args.glyph)
    sources = {
        "icon.svg": tile(True, **look),
        "icon-maskable.svg": tile(False, 0.78, **look),
        "apple-touch.svg": tile(False, 0.92, **look),
        "badge.svg": badge(args.emblem, args.glyph),
    }
    for name, svg in sources.items():
        (out / name).write_text(svg, encoding="utf-8")
    renderer = shutil.which("rsvg-convert")
    if not renderer:
        sys.exit("rsvg-convert not found: SVG sources written, PNGs left unchanged")
    for svg, png, size in RENDERS:
        subprocess.run([renderer, "-w", str(size), "-h", str(size),
                        str(out / svg), "-o", str(out / png)], check=True)
    print(f"wrote {len(sources)} SVG sources and {len(RENDERS)} PNGs to {out}")


if __name__ == "__main__":
    main()
