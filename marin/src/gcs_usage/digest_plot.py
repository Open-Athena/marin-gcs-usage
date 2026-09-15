"""Digest OP plot for the Slack monthly thread (`gcs-usage digest`): a 2-panel
mosaic. Top = total-TiB line, y-autofit (change over time) + a dashed reference
line at the month-start total (inc/dec at a glance). Bottom = object count,
same treatment — CoreWeave has no storage tiers to stack (gcs's bottom panel
is a tier stackplot).

In-package (not a standalone `uv run` script under `job/`) so the Batch image
runs it: `digest.render_plot` imports and calls :func:`render` in-process (the
slim image has neither `uv` nor, without the `[plot]` extra, matplotlib).
Ad-hoc renders: `python -m gcs_usage.digest_plot -d … -o …`.

Input: per-scan `{scan, tb, objs}` rows (scan id, TiB, objects). Output: a PNG
sized for a Slack image block. The digest renders this per run and cache-busts
the OP's image URL so `chat.update` refetches."""
from json import load
from pathlib import Path

from click import Path as CP, command, option

BG = "#0d1117"
INK = "#c9d1d9"
DIM = "#8b949e"
GRID = "#21262d"
LINE = "#58a6ff"
FILL = "#1f6feb"
OBJ_LINE = "#e3b341"
OBJ_FILL = "#9e6a03"


def render(
    rows: list[dict],
    out: Path,
    title: str | None = None,
    redact: bool = False,
) -> None:
    """Render the 2-panel PNG for ``rows`` (each ``{scan, tb, objs}``) to
    ``out``. matplotlib imported lazily — the `[plot]` extra. ``redact`` drops
    every number (y tick labels, the call-outs) — the shape of the month
    without the sizes, for the public README."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    from .digest import scan_ts

    xs = [scan_ts(r["scan"]) for r in rows]
    tot = [r["tb"] for r in rows]
    objs = [r["objs"] / 1e6 for r in rows]
    title = title or f"CoreWeave usage — {xs[-1]:%B %Y}"
    # month-wide x frame: stable early in the month (a 1-scan month otherwise
    # degenerates — zero-width fill, tick-label explosion) and scans fill in
    # left→right as the month progresses.
    from calendar import monthrange
    from datetime import timedelta

    m0 = xs[-1].replace(day=1, hour=0, minute=0)
    m1 = m0.replace(day=monthrange(m0.year, m0.month)[1], hour=23, minute=59)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 4.6), dpi=200, height_ratios=[1, 1], sharex=True)
    fig.patch.set_facecolor(BG)
    for ax in (ax1, ax2):
        ax.set_facecolor(BG)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        for sp in ("left", "bottom"):
            ax.spines[sp].set_color(GRID)
        ax.tick_params(colors=DIM, labelsize=9)
        ax.margins(x=0.02)
        ax.grid(True, color=GRID, lw=0.7, alpha=0.5, axis="y")

    left_half = (xs[-1] - m0) < (m1 - xs[-1])
    xytext = (6, 7) if left_half else (-6, 7)
    ha = "left" if left_half else "right"

    def panel(ax, ys, line, fill, label):
        # dashed month-start reference, soft fill under the line, end marker
        ax.axhline(ys[0], color=DIM, lw=1, ls="--", alpha=0.6)
        lo, hi = min(ys), max(ys)
        ax.fill_between(xs, ys, lo - (hi - lo) * 0.15, color=fill, alpha=0.12)
        ax.plot(xs, ys, color=line, lw=2)
        ax.plot(xs[-1], ys[-1], "o", color=line, ms=5)
        if not redact:
            ax.annotate(label, (xs[-1], ys[-1]), textcoords="offset points", xytext=xytext, ha=ha, color=INK, fontsize=11, fontweight="bold")
        pad = (hi - lo) * 0.25 or max(hi * 0.002, 1.0)
        ax.set_ylim(lo - pad, hi + pad)

    panel(ax1, tot, LINE, FILL, f"{tot[-1]:,.0f} TiB")
    ax1.yaxis.set_major_formatter(lambda v, _: f"{v:,.0f}")
    ax1.set_title(title, color=INK, fontsize=14, fontweight="bold", loc="left", pad=10)
    ax1.text(1.0, 1.04, "cw-s3.oa.dev", transform=ax1.transAxes, ha="right", va="bottom", color=DIM, fontsize=9)

    panel(ax2, objs, OBJ_LINE, OBJ_FILL, f"{objs[-1]:,.2f}M objects")
    ax2.yaxis.set_major_formatter(lambda v, _: f"{v:,.1f}M")
    ax2.set_xlim(m0 - timedelta(hours=16), m1 + timedelta(hours=16))
    ax2.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%-m/%-d"))

    if redact:
        for ax in (ax1, ax2):
            ax.tick_params(axis="y", labelleft=False, left=False)
        ax2.text(0.0, -0.22, "sizes omitted — sign in at cw-s3.oa.dev for the numbers", transform=ax2.transAxes, ha="left", va="top", color=DIM, fontsize=8)
    fig.tight_layout(h_pad=0.6)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=BG)
    plt.close(fig)


@command()
@option("-d", "--rows", "rows_path", type=CP(exists=True, path_type=Path), default=Path("tmp/cw-scans.json"), help="Per-scan rows: {scan, tb, objs}")
@option("-o", "--out", type=CP(path_type=Path), default=Path("tmp/plot-cw.png"), help="Output PNG")
@option("-R", "--redact", is_flag=True, help="No numbers (y labels, call-outs) — the public README version")
@option("-t", "--title", default=None, help="Title (default: 'CoreWeave usage — <Month Year>')")
def main(rows_path: Path, out: Path, redact: bool, title: str | None) -> None:
    render(load(open(rows_path)), out, title, redact=redact)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
