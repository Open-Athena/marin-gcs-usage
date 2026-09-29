#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["click", "pillow"]
# ///
"""Slack app icons for the usage bots (GCS Usage Bot, CoreWeave Usage Bot): one
family — a treemap mark, which is what both sites draw — in each cloud's
palette, with an optional short label in the largest tile so the two read
apart at Slack's 36 px avatar size.

  job/gen-bot-icons.py -o tmp/bot-icons          # every variant + contact.png
  job/gen-bot-icons.py -o out -b cw -s labeled   # one
"""
from pathlib import Path

from click import command, option

S = 1024   # working size
OUT = 512  # Slack wants a square 512–2000 px

PALETTES = {
    # Google Cloud Storage blues with the Google accent pair
    "gcs": {"tiles": ["#1A73E8", "#4285F4", "#8AB4F8", "#34A853", "#FBBC04", "#AECBFA"], "bg": "#0D1117", "label": "GCS"},
    # CoreWeave: indigo/violet, distinct from GCS's blue at a glance
    "cw": {"tiles": ["#5B21B6", "#7C3AED", "#A78BFA", "#2DD4BF", "#F472B6", "#C4B5FD"], "bg": "#0D1117", "label": "CW"},
}

# A fixed squarified-looking layout (x, y, w, h in unit square), largest first.
LAYOUT = [
    (0.00, 0.00, 0.58, 0.62),
    (0.58, 0.00, 0.42, 0.36),
    (0.00, 0.62, 0.34, 0.38),
    (0.58, 0.36, 0.42, 0.30),
    (0.34, 0.62, 0.24, 0.38),
    (0.58, 0.66, 0.42, 0.34),
]


def font(px: int):
    from PIL import ImageFont
    for f in ("/System/Library/Fonts/SFNSRounded.ttf", "/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(f, px)
        except OSError:
            continue
    return ImageFont.load_default()


def render(bot: str, style: str):
    from PIL import Image, ImageDraw
    pal = PALETTES[bot]
    img = Image.new("RGBA", (S, S), pal["bg"] if style != "light" else "#FFFFFF")
    d = ImageDraw.Draw(img)
    pad, gap = S * 0.10, S * 0.022
    span = S - 2 * pad
    for (x, y, w, h), color in zip(LAYOUT, pal["tiles"]):
        box = (pad + x * span + gap / 2, pad + y * span + gap / 2, pad + (x + w) * span - gap / 2, pad + (y + h) * span - gap / 2)
        d.rounded_rectangle(box, radius=S * 0.035, fill=color)
    if style in ("labeled", "light"):
        x, y, w, h = LAYOUT[0]
        cx, cy = pad + (x + w / 2) * span, pad + (y + h / 2) * span
        f = font(int(S * (0.20 if len(pal["label"]) <= 2 else 0.155)))
        d.text((cx, cy), pal["label"], font=f, fill="#FFFFFF", anchor="mm")
    return img.resize((OUT, OUT), Image.LANCZOS)


@command()
@option("-b", "--bot", default=None, help="gcs | cw (default: both)")
@option("-o", "--out", "out_dir", default="tmp/bot-icons", help="Output dir")
@option("-s", "--style", default=None, help="plain | labeled | light (default: all)")
def main(bot, out_dir, style):
    from PIL import Image
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    bots = [bot] if bot else list(PALETTES)
    styles = [style] if style else ["plain", "labeled", "light"]
    tiles = []
    for b in bots:
        for s in styles:
            img = render(b, s)
            img.save(out / f"{b}-{s}.png")
            tiles.append(img)
    # contact sheet: each icon at 512 and at Slack's 36 px avatar size
    cols = len(styles)
    sheet = Image.new("RGBA", (cols * 560, len(bots) * 620), "#F6F8FA")
    for i, img in enumerate(tiles):
        r, c = divmod(i, cols)
        sheet.paste(img, (c * 560 + 24, r * 620 + 24), img)
        small = img.resize((36, 36), Image.LANCZOS)
        sheet.paste(small, (c * 560 + 24, r * 620 + 548), small)
    sheet.save(out / "contact.png")
    print(out / "contact.png")


if __name__ == "__main__":
    main()
