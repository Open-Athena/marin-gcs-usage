#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["click", "pillow"]
# ///
"""Digest OP card: the per-day table rendered as a PNG (typography Slack can't do).

Prototype for the monthly-thread OP image (shape C in the #gcs-usage-staging
workshop): date · size · ΔTB (colored) · $/mo · Δ$ (colored) rows + a size
sparkline. In prod this renders daily and is served from the site; the OP's
image block cache-busts `?v=<date>` on each `chat.update`.

Font: Menlo on macOS; the Batch image will need a bundled monospace (e.g.
JetBrains Mono) — path is an option for that reason.
"""
import json
from datetime import date as Date
from pathlib import Path

from click import command, option

from PIL import Image, ImageDraw, ImageFont

GREEN, RED, FG, DIM, BG, CARD = "#3fb950", "#f85149", "#e6edf3", "#8b949e", "#1a1d21", "#22262b"


@command()
@option("-d", "--days", "days_path", type=Path, default=Path("tmp/digest-days.json"), help="Per-day rows (date/tb/cost/dtb/dcost)")
@option("-f", "--font", "font_path", default="/System/Library/Fonts/Menlo.ttc", help="Monospace font file")
@option("-n", "--last", default=14, help="Rows to show (most recent)")
@option("-o", "--out", type=Path, default=Path("slck/gcs-digest/digest-card.png"), help="Output PNG")
def main(
    days_path: Path,
    font_path: str,
    last: int,
    out: Path,
) -> None:
    days = json.load(open(days_path))[-last:]
    S = 2  # supersample
    fs, pad, row_h = 15 * S, 18 * S, 24 * S
    f = ImageFont.truetype(font_path, fs)
    fb = ImageFont.truetype(font_path, fs, index=1)  # Menlo bold face
    W = 480 * S
    spark_h = 46 * S
    H = pad * 2 + row_h * (len(days) + 1) + spark_h + 10 * S
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([pad // 2, pad // 2, W - pad // 2, H - pad // 2], radius=10 * S, fill=CARD)

    x0, y = pad + 6 * S, pad + 2 * S
    d.text((x0, y), "GCS usage — August 2026", font=fb, fill=FG)
    d.text((W - pad - 6 * S - f.getlength("gcs.oa.dev"), y), "gcs.oa.dev", font=f, fill=DIM)
    y += row_h + 2 * S

    cols = [0, 90, 200, 280, 400]  # x-offsets (pre-scale) for date/size/Δ/$ /Δ$
    for r in days:
        dt = Date.fromisoformat(r["date"])
        dtb, dcost = r.get("dtb"), r.get("dcost")
        cells = [
            (dt.strftime("%a %-m/%-d"), DIM, f),
            (f"{r['tb']:>7,.0f} TB", FG, f),
            ("" if dtb is None else f"{dtb:+7.1f}", GREEN if (dtb or 0) < 0 else RED if (dtb or 0) > 0 else DIM, fb),
            (f"${r['cost']:>6,}/mo", FG, f),
            ("" if dcost is None else f"{dcost:+5,}", GREEN if (dcost or 0) < 0 else RED if (dcost or 0) > 0 else DIM, fb),
        ]
        for (txt, col, fnt), cx in zip(cells, cols):
            d.text((x0 + cx * S, y), txt, font=fnt, fill=col)
        y += row_h

    # sparkline of tb over the shown window
    tbs = [r["tb"] for r in days]
    lo, hi = min(tbs), max(tbs)
    sx0, sx1, sy0, sy1 = x0, W - pad - 6 * S, y + 6 * S, y + spark_h - 4 * S
    pts = [
        (sx0 + i * (sx1 - sx0) / max(1, len(tbs) - 1),
         sy1 - (t - lo) / max(1e-9, hi - lo) * (sy1 - sy0))
        for i, t in enumerate(tbs)
    ]
    d.line(pts, fill="#58a6ff", width=2 * S, joint="curve")
    d.ellipse([pts[-1][0] - 3 * S, pts[-1][1] - 3 * S, pts[-1][0] + 3 * S, pts[-1][1] + 3 * S], fill="#58a6ff")
    d.text((sx1 - f.getlength(f"{tbs[-1]:,.0f} TB"), sy0 - 14 * S), f"{tbs[-1]:,.0f} TB", font=f, fill=DIM)

    out.parent.mkdir(parents=True, exist_ok=True)
    img.resize((W // S, H // S), Image.LANCZOS).save(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
