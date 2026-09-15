"""Digest OP plot for the Slack monthly thread (`gcs-usage digest`): the
month's sparkline of every scan (12-hourly points), y-axis pinned to the
1 PB quota — the used fill under the line and the headroom band above it make
the used/free split legible at a glance; a dashed reference at the month-start
total shows the month's direction. Single panel by design (framing A).

In-package (not a standalone `uv run` script under `job/`) so the Batch image
runs it: `digest.render_plot` imports and calls :func:`render` in-process (the
slim image has neither `uv` nor, without the `[plot]` extra, matplotlib).
Ad-hoc renders: `python -m gcs_usage.digest_plot -d … -o …`.

Input: per-scan `{scan, tb}` rows (scan id, TiB). Output: a PNG sized for a
Slack image block. The digest renders this per run and cache-busts the OP's
image URL so `chat.update` refetches."""
from json import load
from pathlib import Path

from click import Path as CP, command, option

BG = "#0d1117"
INK = "#c9d1d9"
DIM = "#8b949e"
GRID = "#21262d"
LINE = "#58a6ff"
FILL = "#1f6feb"
FREE = "#2ea043"
QUOTA = "#f85149"


def render(
    rows: list[dict],
    out: Path,
    title: str | None = None,
    redact: bool = False,
) -> None:
    """Render the quota sparkline PNG for ``rows`` (each ``{scan, tb}``) to
    ``out``. matplotlib imported lazily — the `[plot]` extra. ``redact`` drops
    the numbers (y tick labels, the call-out) — the shape of the month without
    the sizes, for the public README."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    from .digest import QUOTA_TIB, scan_ts

    xs = [scan_ts(r["scan"]) for r in rows]
    tot = [r["tb"] for r in rows]
    title = title or f"CoreWeave usage — {xs[-1]:%B %Y}"
    # month-wide x frame: stable early in the month (a 1-scan month otherwise
    # degenerates) and scans fill in left→right as the month progresses.
    from calendar import monthrange
    from datetime import timedelta

    m0 = xs[-1].replace(day=1, hour=0, minute=0)
    m1 = m0.replace(day=monthrange(m0.year, m0.month)[1], hour=23, minute=59)
    x0, x1 = m0 - timedelta(hours=16), m1 + timedelta(hours=16)

    fig, ax = plt.subplots(figsize=(9, 3.2), dpi=200)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color(GRID)
    ax.tick_params(colors=DIM, labelsize=9)
    ax.grid(True, color=GRID, lw=0.7, alpha=0.5, axis="y")

    # headroom band: everything between the usage line and the quota, across
    # the whole month frame so the free space reads as a shape, not a sliver
    ax.fill_between(xs, tot, QUOTA_TIB, color=FREE, alpha=0.10, hatch="///", edgecolor=FREE, linewidth=0)
    ax.fill_between(xs, 0, tot, color=FILL, alpha=0.25)
    ax.axhline(tot[0], color=DIM, lw=1, ls="--", alpha=0.6)
    ax.axhline(QUOTA_TIB, color=QUOTA, lw=1.4)
    ax.text(x1, QUOTA_TIB, "1 PB quota ", ha="right", va="bottom", color=QUOTA, fontsize=9, fontweight="bold")
    ax.plot(xs, tot, color=LINE, lw=2)
    ax.plot(xs, tot, ".", color=LINE, ms=3.5)
    ax.plot(xs[-1], tot[-1], "o", color=LINE, ms=5)
    if not redact:
        left_half = (xs[-1] - m0) < (m1 - xs[-1])
        ax.annotate(
            f"{tot[-1]:,.0f} TiB · {tot[-1] / QUOTA_TIB * 100:.0f}%",
            (xs[-1], tot[-1]), textcoords="offset points",
            xytext=(6, -14) if left_half else (-6, -14), ha="left" if left_half else "right",
            color=INK, fontsize=11, fontweight="bold",
        )
    ax.set_ylim(0, QUOTA_TIB * 1.06)
    ax.set_xlim(x0, x1)
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:,.0f}")
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%-m/%-d"))
    ax.set_title(title, color=INK, fontsize=14, fontweight="bold", loc="left", pad=10)
    ax.text(1.0, 1.04, "cw-s3.oa.dev · TiB", transform=ax.transAxes, ha="right", va="bottom", color=DIM, fontsize=9)
    if redact:
        ax.tick_params(axis="y", labelleft=False, left=False)
        ax.text(0.0, -0.22, "sizes omitted — sign in at cw-s3.oa.dev for the numbers", transform=ax.transAxes, ha="left", va="top", color=DIM, fontsize=8)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=BG)
    plt.close(fig)


@command()
@option("-d", "--rows", "rows_path", type=CP(exists=True, path_type=Path), default=Path("tmp/cw-scans.json"), help="Per-scan rows: {scan, tb}")
@option("-o", "--out", type=CP(path_type=Path), default=Path("tmp/plot-cw.png"), help="Output PNG")
@option("-R", "--redact", is_flag=True, help="No numbers (y labels, call-out) — the public README version")
@option("-t", "--title", default=None, help="Title (default: 'CoreWeave usage — <Month Year>')")
def main(rows_path: Path, out: Path, redact: bool, title: str | None) -> None:
    render(load(open(rows_path)), out, title, redact=redact)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
