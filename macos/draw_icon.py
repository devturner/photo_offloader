"""Photo Offloader's app icon, drawn from scratch: an SD card with a gold arrow lifting off it."""

import math
import sys

from PIL import Image, ImageDraw

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


def outlined(draw, points, fill):
    sp = scaled(points)
    draw.line(sp + sp[:2], fill=OUTLINE, width=W * S, joint="curve")
    draw.polygon(sp, fill=fill)


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


def draw_icon(size=1024):
    big = Image.new("RGBA", (1024 * S, 1024 * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    outlined(d, card_outline(), BLUE)
    # Gold contacts: four tall pads and a shorter one under the cut corner.
    for i, x in enumerate((268, 362, 456, 550, 644)):
        height = 170 if i < 4 else 130
        outlined(
            d,
            rounded_polygon(
                [(x, 164), (x + 64, 164), (x + 64, 164 + height), (x, 164 + height)], 12
            ),
            GOLD,
        )
    # The label, with the arrow on it.
    label = rounded_polygon([(256, 520), (768, 520), (768, 868), (256, 868)], 30)
    d.polygon(scaled(label), fill=LABEL)
    d.polygon(scaled(swoop()), fill=GOLD)
    return big.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    draw_icon().save(sys.argv[1] if len(sys.argv) > 1 else "macos/AppIcon.png")
