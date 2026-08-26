#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["click", "pillow"]
# ///
"""Directional Δ-arrow emojis for the daily digest (specs: #gcs-usage workshop).

A sweep of arrows encoding each day's size delta: signed angle a ∈ [-85°, +85°]
in math convention (0° = due E = flat/no change), negative = pointing S-ward =
shrinking (green), positive = N-ward = growing (red). Grey + headless at 0.
Emits SVG sources + supersampled PNGs (Slack custom emoji: ≤128px, transparent),
plus a contact-sheet PNG for quick review.

Names: d«signed angle» with `m` for minus (Slack emoji names bar `-`/`+`):
dm80 … dm10, d0, d10 … d80.
"""
import colorsys
import math
from pathlib import Path

from click import command, option

from PIL import Image, ImageDraw

SIZE = 128            # emoji canvas (Slack max)
SS = 4                # supersample factor for PNG antialiasing
MAX_A = 85            # angle at full-scale delta


def color(a: float) -> tuple[int, int, int]:
    """Green (shrink) ↔ grey (flat) ↔ red (grow), saturating with |a|."""
    t = min(1.0, abs(a) / MAX_A)
    hue = (140 if a < 0 else 4) / 360
    sat = 0.72 * t
    lit = 0.60 - 0.14 * t
    r, g, b = colorsys.hls_to_rgb(hue, lit, sat)
    return round(r * 255), round(g * 255), round(b * 255)


def geometry(a: float, size: float) -> tuple[tuple, tuple, list[tuple] | None]:
    """(tail, head, head-triangle) for signed math-angle `a`, y-up → y-down."""
    t = min(1.0, abs(a) / MAX_A)
    c = size / 2
    rad = math.radians(a)
    dx, dy = math.cos(rad), -math.sin(rad)  # svg/PIL y-down
    half = size * 0.30
    tail = (c - dx * half, c - dy * half)
    tip = (c + dx * half, c + dy * half)
    head_len = size * (0.10 + 0.14 * t) if abs(a) >= 5 else 0
    if not head_len:
        return tail, tip, None
    head_w = head_len * 0.85
    bx, by = tip[0] - dx * head_len, tip[1] - dy * head_len  # head base center
    px, py = -dy, dx  # perpendicular
    tri = [tip, (bx + px * head_w / 2, by + py * head_w / 2), (bx - px * head_w / 2, by - py * head_w / 2)]
    return tail, tip, tri


def svg(a: float) -> str:
    tail, tip, tri = geometry(a, SIZE)
    r, g, b = color(a)
    col = f"#{r:02x}{g:02x}{b:02x}"
    w = SIZE * 0.11
    # shaft stops short of the tip when there's a head (head covers the gap)
    end = tip if tri is None else ((tail[0] + tip[0]) / 2 + (tip[0] - tail[0]) * 0.28,
                                   (tail[1] + tip[1]) / 2 + (tip[1] - tail[1]) * 0.28)
    parts = [f'<line x1="{tail[0]:.1f}" y1="{tail[1]:.1f}" x2="{end[0]:.1f}" y2="{end[1]:.1f}" '
             f'stroke="{col}" stroke-width="{w:.1f}" stroke-linecap="round"/>']
    if tri is not None:
        pts = ' '.join(f"{x:.1f},{y:.1f}" for x, y in tri)
        parts.append(f'<polygon points="{pts}" fill="{col}"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}">' + ''.join(parts) + '</svg>\n')


def png(a: float) -> Image.Image:
    s = SIZE * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    tail, tip, tri = geometry(a, s)
    col = (*color(a), 255)
    w = round(s * 0.11)
    end = tip if tri is None else ((tail[0] + tip[0]) / 2 + (tip[0] - tail[0]) * 0.28,
                                   (tail[1] + tip[1]) / 2 + (tip[1] - tail[1]) * 0.28)
    d.line([tail, end], fill=col, width=w)
    for p in (tail, end) if tri is None else (tail,):
        d.ellipse([p[0] - w / 2, p[1] - w / 2, p[0] + w / 2, p[1] + w / 2], fill=col)
    if tri is not None:
        d.polygon(tri, fill=col)
    return img.resize((SIZE, SIZE), Image.LANCZOS)


def name(a: int) -> str:
    return f"d{'m' if a < 0 else ''}{abs(a)}"


@command()
@option("-m", "--max-angle", default=80, help="Sweep endpoint (±deg)")
@option("-o", "--out", type=Path, default=Path("slck/gcs-digest/emoji"), help="Output dir (svg/, png/, sheet.png)")
@option("-s", "--step", default=10, help="Degrees between arrows (5 for the full 35-emoji set)")
def main(
    max_angle: int,
    out: Path,
    step: int,
) -> None:
    angles = list(range(-max_angle, max_angle + 1, step))
    (out / "svg").mkdir(parents=True, exist_ok=True)
    (out / "png").mkdir(parents=True, exist_ok=True)
    imgs = []
    for a in angles:
        (out / "svg" / f"{name(a)}.svg").write_text(svg(a))
        im = png(a)
        im.save(out / "png" / f"{name(a)}.png")
        imgs.append((a, im))
    # contact sheet: sweep left→right on a light + dark row (Slack themes)
    pad, cell = 6, SIZE
    wsheet = pad + len(imgs) * (cell + pad)
    sheet = Image.new("RGB", (wsheet, 2 * (cell + pad) + pad + 18), "#f8f8f8")
    dark = Image.new("RGB", (wsheet, cell + pad), "#1a1d21")
    sheet.paste(dark, (0, cell + pad + 18))
    d = ImageDraw.Draw(sheet)
    for i, (a, im) in enumerate(imgs):
        x = pad + i * (cell + pad)
        d.text((x + 2, 2), f"{a:+d}°" if a else "0°", fill="#333")
        sheet.paste(im, (x, 18), im)
        sheet.paste(im, (x, cell + pad + 18 + pad // 2), im)
    sheet.save(out / "sheet.png")
    print(f"{len(angles)} arrows → {out}/ (svg/, png/, sheet.png)")


if __name__ == "__main__":
    main()
