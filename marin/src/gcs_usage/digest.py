"""Shape-C monthly GCS-usage digest -> a Slack thread (`gcs-usage digest`).

One thread per calendar month: an OP (month-to-date headline, per-week rollup
bullets, and a 2-panel mosaic plot) that's edited in place as the month
progresses, plus one reply per scan. Each reply's sender name is the scan's
headline (date . TB . delta), its body the $/mo + a linked arrow to the day's
scan, and its avatar a colour-coded trend arrow (`av_deg{N}.png?v=REV`). Posts
via the `thrds` `SlackClient` (per-message username/icon overrides need a bot
token). Converge state lives in a per-month JSON in the data bucket.

Design + rationale: specs/done/slack-digest-shape-c.md.

The pure content functions (`deg`, `op_body`, `reply`, `rows_from_meta`) hold
all the formatting and are unit-tested; `post_digest` is the thin side-effecting
shell (render+host plot, post/edit OP, post new replies, persist state)."""
from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import sys
from collections import OrderedDict
from dataclasses import dataclass

TIB = 1024**4
GIB = 1024**3
# US list $/GiB-mo by GCS storage class id (1 Standard / 2 Nearline / 3 Coldline / 4 Archive).
PRICE = {"1": 0.02, "2": 0.01, "3": 0.004, "4": 0.0012}
# Weekly-halving arrow buckets: |dpct| >= THRESH[i] -> deg (i+1)*10 (capped 80).
THRESH = [0.39, 0.78, 1.5, 3.1, 6.25, 12.5, 25, 50]
MINUS = "−"  # matches the site's unicode minus
DEFAULT_URL = "https://gcs.oa.dev"
ICONS_BASE = "https://gcs-usage-icons.pages.dev"
# bump when the av_deg glyphs change: Slack caches avatars per-URL at post
# time, so a stable URL serves MIXED generations after a redesign.
AVATAR_REV = 2


def deg(pct_signed: float, mult: float = 1.0) -> int:
    """Signed arrow degree for a percent change, time-normalized by ``mult``.

    Anchored on a weekly halving (deg80 ~ +/-50%/week). A daily reply passes
    ``mult=7`` (project the day's rate to a weekly-equivalent), a weekly bullet
    ``mult=1``, month-to-date ``mult=7/days_elapsed`` -- so a daily arrow and a
    weekly arrow mean the same underlying rate."""
    a = abs(pct_signed) * mult
    d = 0
    for i, t in enumerate(THRESH):
        if a >= t:
            d = (i + 1) * 10
    d = min(80, d)
    return -d if pct_signed < 0 else d


def _tb(v: float) -> str:
    return f"+{v:.1f}" if v >= 0 else f"{MINUS}{abs(v):.1f}"


def _usd(v: float) -> str:
    return ("+$" if v >= 0 else f"{MINUS}$") + f"{abs(v):,}"


def _pct(dtb: float, tb: float) -> str:
    prev = tb - dtb
    return f"{abs(dtb / prev * 100) if prev else 0:.1f}"


def _pct_val(dtb: float, tb: float) -> float:
    prev = tb - dtb
    return dtb / prev * 100 if prev else 0.0


def _yy(date: str) -> str:
    return date[2:].replace("-", "")


@dataclass(frozen=True)
class Scan:
    """One scan's row: TiB total + per-class TiB + $/mo, with deltas vs. the
    previous scan (``dtb``/``dcost`` are ``None`` only if no prior scan)."""

    date: str
    tb: float
    cost: int
    dtb: float | None
    dcost: int | None
    std: float
    near: float
    cold: float
    arch: float


def _cost(class_bytes: dict) -> float:
    return sum(class_bytes.get(c, 0) / GIB * PRICE[c] for c in PRICE)


def rows_from_meta(dated_meta: list[tuple[str, dict]]) -> list[Scan]:
    """Build ``Scan`` rows from ``(date, meta.json)`` pairs in date order.

    The first pair seeds the delta for the second; callers pass one scan of
    lead-in before the window they want, then slice it off."""
    out: list[Scan] = []
    ptb = pcost = None
    for date, m in dated_meta:
        tb = m["total_bytes"] / TIB
        cb = m["class_bytes"]
        cost = round(_cost(cb))
        out.append(
            Scan(
                date=date,
                tb=round(tb, 1),
                cost=cost,
                dtb=round(tb - ptb, 1) if ptb is not None else None,
                dcost=cost - pcost if pcost is not None else None,
                std=round(cb.get("1", 0) / TIB, 1),
                near=round(cb.get("2", 0) / TIB, 1),
                cold=round(cb.get("3", 0) / TIB, 1),
                arch=round(cb.get("4", 0) / TIB, 1),
            )
        )
        ptb, pcost = tb, cost
    return out


def op_body(rows: list[Scan], month: dt.date, plot_url: str, site_url: str = DEFAULT_URL) -> str:
    """OP markdown: month-to-date headline, per-week bullets, trailing plot image.

    The month/year title is NOT in the body -- it's folded into the OP's sender
    name by the poster."""
    base_tb = rows[0].tb - (rows[0].dtb or 0)
    base_cost = rows[0].cost - (rows[0].dcost or 0)
    mdtb = rows[-1].tb - base_tb
    mweekly = (mdtb / base_tb * 100 * 7 / len(rows)) if base_tb else 0
    lines = [
        f":arrow_deg{deg(mweekly)}: **{_tb(mdtb)} TB** month-to-date · [dashboard]({site_url}/)",
        "",
        "*Weekly summaries*",
    ]
    weeks: OrderedDict[dt.date, list[Scan]] = OrderedDict()
    for r in rows:
        d = dt.date.fromisoformat(r.date)
        mon = d - dt.timedelta(days=d.weekday())
        weeks.setdefault(mon, []).append(r)
    prev_end: Scan | None = None
    last_mon = list(weeks)[-1]
    for mon, ws in weeks.items():
        end = ws[-1]
        b_tb, b_cost = (prev_end.tb, prev_end.cost) if prev_end is not None else (base_tb, base_cost)
        wdtb = end.tb - b_tb
        wpct = wdtb / b_tb * 100 if b_tb else 0
        partial = " _(partial)_" if len(ws) < 7 and mon == last_mon else ""
        lines.append(
            f":arrow_deg{deg(wpct)}: [wk of {mon.month}/{mon.day}]({site_url}/?d={_yy(end.date)}){partial} — "
            f"**{end.tb:,.0f} TB** ({_tb(wdtb)}, {_pct(wdtb, end.tb)}%) · ${end.cost:,}/mo ({_usd(end.cost - b_cost)})"
        )
        prev_end = end
    lines += ["", f"![GCS usage — {month:%B %Y}]({plot_url})"]
    return "\n".join(lines)


def reply(r: Scan, site_url: str = DEFAULT_URL) -> tuple[str, str, str]:
    """One scan's reply -> (sender_username, body, avatar_url).

    Style B, mobile-first: the SENDER is the size headline (bold, plain text --
    Slack renders no links/emoji/markdown there), sized to not wrap on a phone;
    the BODY is one line: the cost + a linked \u2197 to the day's scan at EOL.
    The avatar is the day's colour-coded trend arrow (URL carries AVATAR_REV --
    Slack caches avatars per-URL, so glyph redesigns must bust it)."""
    d = dt.date.fromisoformat(r.date)
    dtb = r.dtb or 0
    dcost = r.dcost or 0
    sender = f"{d.month}/{d.day} — {r.tb:,.0f} TB ({_tb(dtb)}, {_pct(dtb, r.tb)}%)"
    body = f"${r.cost:,}/mo ({_usd(dcost)}) [\u2197]({site_url}/?d={_yy(r.date)})"
    avatar = f"{ICONS_BASE}/arrows/av_deg{deg(_pct_val(dtb, r.tb), 7)}.png?v={AVATAR_REV}"
    return sender, body, avatar


# ---- IO (side-effecting) --------------------------------------------------


def _err(*a) -> None:
    print(*a, file=sys.stderr)


def load_month(root: str, month: dt.date) -> list[Scan]:
    """Per-scan ``Scan`` rows for ``month`` (UTC), read from ``root`` snapshots.

    ``root`` = ``gs://<bucket>/snapshots``. Reads ``series.json`` for scan dates,
    keeps the month's dates plus one lead-in scan for the first delta, reads each
    scan's ``meta.json``, then slices the lead-in off."""
    import fsspec

    with fsspec.open(f"{root}/series.json", "rt") as f:
        dates = json.load(f)["dates"]
    pfx = f"{month:%Y-%m}-"
    in_month = [d for d in dates if d.startswith(pfx)]
    if not in_month:
        return []
    first_idx = dates.index(in_month[0])
    window = dates[max(0, first_idx - 1) : dates.index(in_month[-1]) + 1]
    dated_meta: list[tuple[str, dict]] = []
    for d in window:
        with fsspec.open(f"{root}/{d}/meta.json", "rt") as f:
            dated_meta.append((d, json.load(f)))
    rows = rows_from_meta(dated_meta)
    return rows[1:] if first_idx > 0 else rows


def _wait_reachable(url: str, timeout: float = 90, interval: float = 3) -> None:
    """Block until ``url`` serves 200 (Pages CDN propagation after a deploy)."""
    import time
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "gcs-usage-digest/1.0"})
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status == 200:
                    return
        except Exception:
            pass
        time.sleep(interval)
    _err(f"digest: WARN {url} not reachable after {timeout:.0f}s — posting anyway")


def _state_path(root: str, month: dt.date) -> str:
    base = root.rsplit("/snapshots", 1)[0]
    return f"{base}/digest/{month:%Y-%m}.json"


def load_state(root: str, month: dt.date) -> dict:
    import fsspec

    try:
        with fsspec.open(_state_path(root, month), "rt") as f:
            return json.load(f)
    except (FileNotFoundError, OSError):
        return {}


def save_state(root: str, month: dt.date, state: dict) -> None:
    import fsspec

    with fsspec.open(_state_path(root, month), "wt") as f:
        json.dump(state, f, indent=2)


def render_plot(rows: list[Scan], month: dt.date, out_path) -> None:
    """Render the mosaic PNG for ``rows`` to ``out_path`` (runs the uv script)."""
    import subprocess
    import tempfile
    from pathlib import Path

    tiers = [{"date": r.date, "std": r.std, "near": r.near, "cold": r.cold, "arch": r.arch} for r in rows]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        json.dump(tiers, tf)
        days_json = tf.name
    script = Path(__file__).resolve().parents[3] / "job" / "gen-digest-plot.py"
    subprocess.run(
        [str(script), "-d", days_json, "-o", str(out_path), "-t", f"GCS usage — {month:%B %Y}"],
        check=True,
    )
    os.unlink(days_json)


def post_digest(root, month, token, channel, site_url=DEFAULT_URL, icons_dir=None, deploy_plot=None, reply_delay=0.0) -> dict:
    """Converge the month's thread: render+host the plot, post/edit the OP, post
    one reply per not-yet-posted scan, persist and return state. ``icons_dir`` is
    where to write the PNG; ``deploy_plot(local_png, basename)`` publishes it.
    ``reply_delay`` sleeps that many seconds between replies (>0 for a spaced
    backfill, so Slack doesn't collapse the per-reply sender chrome)."""
    import time
    from pathlib import Path

    from thrds.slack import SlackClient

    rows = load_month(root, month)
    if not rows:
        _err(f"digest: no scans for {month:%Y-%m}")
        return {}
    state = load_state(root, month)
    client = SlackClient(token, channel)

    plot_name = state.get("plot_name") or f"plot-{secrets.token_hex(16)}.png"
    base = ICONS_BASE
    if icons_dir is not None:
        local = Path(icons_dir) / plot_name
        render_plot(rows, month, local)
        if deploy_plot is not None:
            # the deployment-specific host serves the just-uploaded plot
            # immediately (no root-alias propagation race → no invalid_blocks)
            dep = deploy_plot(local, plot_name)
            if dep:
                base = dep
    plot_url = f"{base}/{plot_name}?v={int(dt.datetime.now(dt.timezone.utc).timestamp())}"
    state["plot_name"] = plot_name
    # A just-deployed Pages asset isn't instantly served at the root alias; if we
    # post before it propagates, Slack's image-block validation 500s the whole
    # message with `invalid_blocks`. Poll until the URL is live (or give up + warn).
    if icons_dir is not None and deploy_plot is not None:
        _wait_reachable(plot_url)

    body = op_body(rows, month, plot_url, site_url)
    op_ts = state.get("op_ts")
    if op_ts:
        client.edit(op_ts, body)
        _err(f"digest: edited OP {op_ts} ({len(rows)} scans)")
    else:
        m = client.post(body, username=f"GCS usage — {month:%B %Y}", icon_emoji=":calendar:")
        op_ts = m.id
        state["op_ts"] = op_ts
        _err(f"digest: posted OP {op_ts}")

    posted = state.setdefault("posted", {})
    todo = [r for r in rows if r.date not in posted]
    for i, r in enumerate(todo):
        sender, rbody, avatar = reply(r, site_url)
        rm = client.post(rbody, thread_id=op_ts, username=sender, icon_url=avatar)
        posted[r.date] = rm.id
        save_state(root, month, state)   # persist after each → a spaced backfill is resumable
        _err(f"digest: reply {r.date} -> {rm.id}")
        if reply_delay and i < len(todo) - 1:
            time.sleep(reply_delay)

    save_state(root, month, state)
    return state
