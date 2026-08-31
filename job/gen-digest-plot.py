#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["matplotlib>=3.8", "click>=8.1"]
# ///
"""Plot-only digest image for the Slack monthly thread (Shape C).

The OP's image is JUST the chart — the thing a Slack message can't render.
The tabular data (per-scan TB / Δ / $/mo / Δ$) lives in the thread's replies,
each led by a Δ-arrow emoji (Slack text has no per-value color). So this renders
a clean total-TB-over-time line on a GitHub-dark ground, nothing more.

Input: the same `digest-days.json` rows (`{date, tb, cost, dtb, dcost}`) the
card used. Output: a PNG sized for a Slack image block (cache-busted by thrds).
"""
from datetime import date as Date
from json import load
from pathlib import Path

from click import Path as ClickPath
from click import command, option

# GitHub-dark palette
BG = "#0d1117"
PANEL = "#161b22"
INK = "#c9d1d9"
DIM = "#8b949e"
LINE = "#58a6ff"
FILL = "#1f6feb"
GRID = "#21262d"


@command()
@option("-d", "--days", "days_path", type=ClickPath(exists=True, path_type=Path), default=Path("tmp/digest-days.json"), help="Per-scan rows (date/tb/cost/…)")
@option("-o", "--out", type=ClickPath(path_type=Path), default=Path("tmp/digest-plot.png"), help="Output PNG")
@option("-t", "--title", default=None, help="Title (default: 'GCS usage — <Month Year>' from the last date)")
def main(days_path: Path, out: Path, title: str | None) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    rows = load(open(days_path))
    xs = [Date.fromisoformat(r["date"]) for r in rows]
    ys = [r["tb"] for r in rows]
    title = title or f"GCS usage — {xs[-1]:%B %Y}"

    fig, ax = plt.subplots(figsize=(9, 3.6), dpi=200)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    ax.fill_between(xs, ys, min(ys) - (max(ys) - min(ys)) * 0.12, color=FILL, alpha=0.14, zorder=1)
    ax.plot(xs, ys, color=LINE, lw=2.2, zorder=3)
    ax.plot(xs[-1], ys[-1], "o", color=LINE, ms=6, zorder=4)
    # label the latest value at the last point
    ax.annotate(f"{ys[-1]:,.0f} TB", (xs[-1], ys[-1]), textcoords="offset points",
                xytext=(-6, 10), ha="right", color=INK, fontsize=12, fontweight="bold")

    ax.set_title(title, color=INK, fontsize=14, fontweight="bold", loc="left", pad=12)
    ax.text(1.0, 1.03, "gcs.oa.dev", transform=ax.transAxes, ha="right", va="bottom", color=DIM, fontsize=10)

    ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%-m/%-d"))
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:,.0f}")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(GRID)
    ax.tick_params(colors=DIM, labelsize=10)
    ax.grid(True, color=GRID, lw=0.8, alpha=0.7)
    ax.margins(x=0.02)

    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=BG)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
