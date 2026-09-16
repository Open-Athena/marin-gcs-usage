#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["click", "pillow"]
# ///
"""Generate GCS Usage Bot Slack icons from the GCS "Cloud Storage" mark.

Outputs (512px PNGs):
  gcs-app-{light,dark}.png  — static app icon (upload in the Slack app's Basic Information)
  gcs-digest.png            — per-message avatar for a normal digest (mark + :bar_chart: badge)
  gcs-breach.png            — per-message avatar for a ceiling/spike breach (mark + :rotating_light:)

Badge technique (mark + corner emoji, no backing circle) mirrors ~/c/rac/watchy's
gen-pfp.py; color emoji need macOS's Apple Color Emoji font.
"""
from pathlib import Path

from click import argument, command, option

S = 1024          # internal working size; outputs downscale to 512
OUT = 512
LIGHT = (255, 255, 255, 255)
DARK = (13, 17, 23, 255)      # GitHub dark, reads well in Slack dark mode


def emoji_glyph(ch: str, px: int):
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype("/System/Library/Fonts/Apple Color Emoji.ttc", 160)
    img = Image.new("RGBA", (320, 320), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((80, 80), ch, font=font, embedded_color=True)
    return img.crop(img.getbbox()).resize((px, px), Image.LANCZOS)


def base(mark_png: Path, bg: tuple, extent: float = 0.72):
    """The GCS mark centered on a solid square, scaled to `extent` of the side."""
    from PIL import Image
    mark = Image.open(mark_png).convert("RGBA")
    mark = mark.crop(mark.getchannel("A").getbbox())  # trim transparent margin
    box = int(S * extent)
    scale = min(box / mark.width, box / mark.height)
    mark = mark.resize((round(mark.width * scale), round(mark.height * scale)), Image.LANCZOS)
    out = Image.new("RGBA", (S, S), bg)
    out.alpha_composite(mark, ((S - mark.width) // 2, (S - mark.height) // 2))
    return out


def badge(img, ch: str):
    """Composite an emoji badge into the bottom-right corner (glyph content only occludes)."""
    from PIL import Image
    out = img.copy()
    c = S - int(S * 0.30)
    g = emoji_glyph(ch, int(S * 0.52))
    out.alpha_composite(g, (c - g.width // 2, c - g.height // 2))
    return out


def save(img, path: Path):
    img.convert("RGB").resize((OUT, OUT), __import__("PIL").Image.LANCZOS).save(path)
    return path


@command()
@option("-m", "--mark", type=Path, default=Path(__file__).parent / "assets/gcs-mark.png", help="GCS mark PNG (transparent)")
@option("-o", "--out-dir", type=Path, default=Path(__file__).parent / "icons", help="output dir")
def main(mark: Path, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    light, dark = base(mark, LIGHT), base(mark, DARK)
    made = [
        save(light, out_dir / "gcs-app-light.png"),
        save(dark, out_dir / "gcs-app-dark.png"),
        save(badge(light, "\U0001F4CA"), out_dir / "gcs-digest.png"),   # 📊
        save(badge(light, "\U0001F6A8"), out_dir / "gcs-breach.png"),   # 🚨
    ]
    for p in made:
        print(p)


if __name__ == "__main__":
    main()
