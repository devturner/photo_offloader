"""Photo Offloader's icon, drawn from scratch: an SD card with a gold arrow lifting off it.

    .venv/bin/python macos/draw_icon.py     writes macos/AppIcon.png, the app bundles' icon

The dashboard header shows the same shapes as inline SVG (``svg()``); a test checks that the copy
in photo_offloader/dashboard/web/index.html still matches.
"""

import math
import sys
from typing import List, Tuple

Point = Tuple[float, float]
Color = Tuple[int, int, int]

S = 4  # supersampling, for smooth edges
OUTLINE = (44, 46, 52)
BLUE = (66, 112, 168)
GOLD = (224, 195, 112)
LABEL = (44, 46, 52)
W = 30  # outline width


def scaled(points):
    return [(x * S, y * S) for x, y in points]


def rounded_polygon(points, radius, steps=12):
    """Round every corner of a polygon with a small arc."""
    out = []
    n = len(points)
    for i in range(n):
        p0, p1, p2 = points[i - 1], points[i], points[(i + 1) % n]
        v1 = (p0[0] - p1[0], p0[1] - p1[1])
        v2 = (p2[0] - p1[0], p2[1] - p1[1])
        l1, l2 = math.hypot(*v1), math.hypot(*v2)
        r = min(radius, l1 / 2, l2 / 2)
        a = (p1[0] + v1[0] / l1 * r, p1[1] + v1[1] / l1 * r)
        b = (p1[0] + v2[0] / l2 * r, p1[1] + v2[1] / l2 * r)
        for k in range(steps + 1):  # quadratic Bezier through the corner
            t = k / steps
            out.append(
                (
                    (1 - t) ** 2 * a[0] + 2 * (1 - t) * t * p1[0] + t**2 * b[0],
                    (1 - t) ** 2 * a[1] + 2 * (1 - t) * t * p1[1] + t**2 * b[1],
                )
            )
    return out


def card_outline():
    # A card with a cut top-right corner and a write-protect step on the left edge.
    left, right, top, bottom, cut = 196, 828, 112, 916, 150
    return rounded_polygon(
        [
            (left, top),
            (right - cut, top),
            (right, top + cut),
            (right, bottom),
            (left, bottom),
            (left, 470),
            (left - 22, 470),
            (left - 22, 330),
            (left, 330),
        ],
        34,
    )


def swoop():
    """A gold arrow that runs along the label, then curves up."""
    sw, head_w, head_h = 50, 176, 118
    x0, y0, xc, r, base = 300, 820, 540, 108, 680
    line = [(x0, y0), (xc, y0)]
    for i in range(1, 31):
        a = math.pi / 2 - (math.pi / 2) * i / 30
        line.append((xc + r * math.cos(a), y0 - r + r * math.sin(a)))
    line.append((xc + r, base))

    def offset(d):
        pts = []
        for i, (x, y) in enumerate(line):
            p, q = line[max(i - 1, 0)], line[min(i + 1, len(line) - 1)]
            dx, dy = q[0] - p[0], q[1] - p[1]
            n = math.hypot(dx, dy)
            pts.append((x + dy / n * d, y - dx / n * d))
        return pts

    tx = line[-1][0]
    head = [(tx + head_w / 2, base), (tx, base - head_h), (tx - head_w / 2, base)]
    return offset(-sw / 2) + head + offset(sw / 2)[::-1]


def shapes() -> List[Tuple[List[Point], Color, bool]]:
    """Every shape, back to front, as (points, fill, outlined)."""
    out = [(card_outline(), BLUE, True)]
    # Gold contacts: four tall pads and a shorter one under the cut corner.
    for i, x in enumerate((268, 362, 456, 550, 644)):
        height = 170 if i < 4 else 130
        pad = [(x, 164), (x + 64, 164), (x + 64, 164 + height), (x, 164 + height)]
        out.append((rounded_polygon(pad, 12), GOLD, True))
    # The label, with the arrow on it.
    out.append(
        (rounded_polygon([(256, 520), (768, 520), (768, 868), (256, 868)], 30), LABEL, False)
    )
    out.append((swoop(), GOLD, False))
    return out


def draw_icon(size=1024):
    from PIL import Image, ImageDraw  # only the PNG needs Pillow

    big = Image.new("RGBA", (1024 * S, 1024 * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    for points, fill, outline in shapes():
        sp = scaled(points)
        if outline:  # the fill covers the inner half of the line, as paint-order does in svg()
            d.line(sp + sp[:2], fill=OUTLINE, width=W * S, joint="curve")
        d.polygon(sp, fill=fill)
    return big.resize((size, size), Image.LANCZOS)


def svg() -> str:
    """The icon as one line of inline SVG, for the dashboard header."""

    def color(rgb: Color) -> str:
        return "#%02x%02x%02x" % rgb

    paths = []
    for points, fill, outline in shapes():
        d = "M" + "L".join(f"{x:.0f} {y:.0f}" for x, y in points) + "Z"
        stroke = (
            f' stroke="{color(OUTLINE)}" stroke-width="{W}" stroke-linejoin="round"'
            ' paint-order="stroke"'
            if outline
            else ""
        )
        paths.append(f'<path d="{d}" fill="{color(fill)}"{stroke}/>')
    return (
        '<svg class="app-icon" viewBox="0 0 1024 1024" aria-hidden="true">'
        + "".join(paths)
        + "</svg>"
    )


if __name__ == "__main__":
    draw_icon().save(sys.argv[1] if len(sys.argv) > 1 else "macos/AppIcon.png")
