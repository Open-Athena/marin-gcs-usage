"""Shape-C monthly CoreWeave-usage digest -> a Slack thread (`gcs-usage digest`).

One thread per calendar month: an OP that's edited in place as the month
progresses (headline + a hosted plot), plus one reply per scan, each under its
own sender name + trend-arrow avatar. Posts via the `thrds` `SlackClient`
(per-message username/icon overrides need a bot token). Converge state lives
in a per-channel, per-month JSON in the data bucket.

This is the MECHANISM, ported from gcs's `digest.py` (specs/done/slack-digest-
shape-c.md there) with the CoreWeave deltas in specs/cw-slack-digest.md:
12-hourly scan ids, object counts (no storage-class dollars), a preview-branch
plot deploy, channel-keyed state, Slack only. The message CONTENT is a clean
seam — `op_body` / `reply` are placeholders while the headline/body framing is
workshopped (that spec's "Content" section); nothing else changes when the
real text lands.

Pure helpers (`deg`, `scan_ts`, `rows_from_meta`, the `_tb`/`_pct`/`_objs`
formatters, `_dlink`/`_span` link tokens) are unit-tested; `post_digest` is the
side-effecting shell (render+host plot, post/edit OP, post new replies, persist
state), tested against a fake client."""
from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import sys
from dataclasses import dataclass

TIB = 1024**4
# Weekly-halving arrow buckets: |dpct| >= THRESH[i] -> deg (i+1)*10 (capped 80).
THRESH = [0.39, 0.78, 1.5, 3.1, 6.25, 12.5, 25, 50]
MINUS = "−"  # matches the site's unicode minus
DEFAULT_URL = "https://cw-s3.oa.dev"
# The avatars are gcs's arrow set, served from the icons project's production
# alias; cw's plots go to its own preview branch (ICONS_BRANCH) so a cw deploy
# never replaces what that alias serves.
ICONS_BASE = "https://gcs-usage-icons.pages.dev"
ICONS_PROJECT = "gcs-usage-icons"
ICONS_BRANCH = "cw"
# bump when the av_deg glyphs change: Slack caches avatars per-URL at post
# time, so a stable URL serves MIXED generations after a redesign.
AVATAR_REV = 4
HOURS_PER_WEEK = 168.0
SCAN_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2}))?$")


def deg(pct_signed: float, mult: float = 1.0) -> int:
    """Signed arrow degree for a percent change, time-normalized by ``mult``.

    Anchored on a weekly halving (deg80 ~ +/-50%/week). A reply passes the
    weekly-equivalent multiplier for its scan interval (``168 / hours``: 7 for
    a daily feed, 14 for 12-hourly), a weekly rollup ``mult=1``, month-to-date
    ``7 / days_elapsed`` -- so every arrow means the same underlying rate."""
    a = abs(pct_signed) * mult
    d = 0
    for i, t in enumerate(THRESH):
        if a >= t:
            d = (i + 1) * 10
    d = min(80, d)
    return -d if pct_signed < 0 else d


def scan_ts(scan: str) -> dt.datetime:
    """A scan id's UTC instant; date-only ids read as midnight (site/src/scan.ts)."""
    m = SCAN_RE.match(scan)
    if not m:
        raise ValueError(f"not a scan id: {scan!r}")
    y, mo, d, hh, mm = m.groups()
    return dt.datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), tzinfo=dt.timezone.utc)


def _tb(v: float) -> str:
    return f"+{v:.1f}" if v >= 0 else f"{MINUS}{abs(v):.1f}"


def _objs(n: int) -> str:
    return f"{n / 1e6:.2f}M"


def _dobjs(d: int) -> str:
    return ("+" if d >= 0 else MINUS) + f"{abs(d) / 1e6:.2f}M"


def _pct(dtb: float, tb: float) -> str:
    prev = tb - dtb
    return f"{abs(dtb / prev * 100) if prev else 0:.1f}"


def _pct_val(dtb: float, tb: float) -> float:
    prev = tb - dtb
    return dtb / prev * 100 if prev else 0.0


def _dlink(scan: str) -> str:
    """The site's compact `?d=` token for a scan (`260915-0001`; date-only ids
    stay `260915`)."""
    y, mo, d, hh, mm = SCAN_RE.match(scan).groups()
    return f"{y[2:]}{mo}{d}" + (f"-{hh}{mm}" if hh else "")


def _span(a: dt.datetime, b: dt.datetime) -> str:
    """`?d=` look-back token for the interval a→b (`1d12h`, `7d`, `12h`),
    matching the site's `encodeSpan`."""
    secs = (b - a).total_seconds()
    days = int(secs // 86400)
    hours = round((secs - days * 86400) / 3600)
    return (f"{days}d" if days else "") + (f"{hours}h" if hours else "") or "0h"


@dataclass(frozen=True)
class Scan:
    """One scan's row: TiB total + object count, with deltas vs. the previous
    scan (``dtb``/``dobjs``/``hours`` are ``None`` only if no prior scan)."""

    scan: str
    tb: float
    objs: int
    dtb: float | None
    dobjs: int | None
    hours: float | None

    @property
    def date(self) -> str:
        return self.scan[:10]


def rows_from_meta(dated_meta: list[tuple[str, dict]]) -> list[Scan]:
    """Build ``Scan`` rows from ``(scan_id, meta.json)`` pairs in scan order.

    The first pair seeds the delta for the second; callers pass one scan of
    lead-in before the window they want, then slice it off."""
    out: list[Scan] = []
    ptb = pobjs = pts = None
    for scan, m in dated_meta:
        ts = scan_ts(scan)
        tb = m["total_bytes"] / TIB
        objs = int(m["total_objects"])
        out.append(
            Scan(
                scan=scan,
                tb=round(tb, 1),
                objs=objs,
                dtb=round(tb - ptb, 1) if ptb is not None else None,
                dobjs=objs - pobjs if pobjs is not None else None,
                hours=(ts - pts).total_seconds() / 3600 if pts is not None else None,
            )
        )
        ptb, pobjs, pts = tb, objs, ts
    return out


# ---- Content (PLACEHOLDER — specs/cw-slack-digest.md § Content) -------------
# The real headline/body framing (quota headroom / movers / sweep ledger) is
# being workshopped; these bodies exist so the converge mechanism runs end to
# end. Only `op_body` and `reply` change when the content lands.


def op_body(rows: list[Scan], month: dt.date, plot_url: str | None, site_url: str = DEFAULT_URL) -> str:
    """OP markdown (placeholder): latest total + objects + dashboard link, and
    the trailing plot image. The month/year title is NOT in the body -- it's
    folded into the OP's sender name by the poster. ``plot_url=None`` omits the
    image line."""
    last = rows[-1]
    lines = [f"**{last.tb:,.0f} TB** · {_objs(last.objs)} objects · [dashboard]({site_url}/)"]
    if plot_url is not None:
        lines += ["", f"![CoreWeave usage — {month:%B %Y}]({plot_url})"]
    return "\n".join(lines)


def reply(r: Scan, site_url: str = DEFAULT_URL) -> tuple[str, str, str]:
    """One scan's reply (placeholder) -> (sender_username, body, avatar_url).

    The SENDER is the scan's headline (plain text -- Slack renders no
    links/emoji/markdown there); two scans share a date, so the UTC time rides
    along. The BODY is one line ending in a link to the scan's Diff section.
    The avatar is the scan's colour-coded trend arrow, its Δ% projected over
    the hours since the previous scan to a weekly rate (URL carries AVATAR_REV
    -- Slack caches avatars per-URL, so glyph redesigns must bust it)."""
    _, _, _, hh, mm = SCAN_RE.match(r.scan).groups()
    d = dt.date.fromisoformat(r.date)
    dtb = r.dtb or 0
    when = f"{d.month}/{d.day}" + (f" {hh}:{mm}Z" if hh else "")
    sender = f"{when} — {r.tb:,.0f} TB ({_tb(dtb)}, {_pct(dtb, r.tb)}%)"
    # ↗︎ = NE arrow + text-presentation selector: renders as a font
    # glyph in link colour (bare ↗ gets emoji-ized by Slack into the
    # cartoonish :arrow_upper_right:)
    body = f"{_objs(r.objs)} objects ({_dobjs(r.dobjs or 0)}) [↗︎]({site_url}/?d={_dlink(r.scan)}#diff)"
    mult = HOURS_PER_WEEK / r.hours if r.hours else 7.0
    avatar = f"{ICONS_BASE}/arrows/av_deg{deg(_pct_val(dtb, r.tb), mult)}.png?v={AVATAR_REV}"
    return sender, body, avatar


# ---- IO (side-effecting) --------------------------------------------------


def _err(*a) -> None:
    print(*a, file=sys.stderr)


def load_month(root: str, month: dt.date) -> list[Scan]:
    """Per-scan ``Scan`` rows for ``month`` (UTC), read from ``root`` snapshots.

    ``root`` = ``gs://<bucket>/snapshots/cw``. Lists the scan ids (one
    ``meta.json`` per published scan; ids sort chronologically), keeps the
    month's plus one lead-in scan for the first delta, reads each scan's
    ``meta.json``, then slices the lead-in off."""
    import fsspec

    fs, _, _ = fsspec.get_fs_token_paths(root)
    scans = sorted(
        m.group(1)
        for p in fs.glob(f"{root.split('://', 1)[-1]}/*/meta.json")
        if (m := re.search(r"/(\d{4}-\d{2}-\d{2}(?:T\d{4})?)/meta\.json$", p))
    )
    pfx = f"{month:%Y-%m}-"
    in_month = [s for s in scans if s.startswith(pfx)]
    if not in_month:
        return []
    first_idx = scans.index(in_month[0])
    window = scans[max(0, first_idx - 1) : scans.index(in_month[-1]) + 1]
    dated_meta: list[tuple[str, dict]] = []
    for s in window:
        with fsspec.open(f"{root}/{s}/meta.json", "rt") as f:
            dated_meta.append((s, json.load(f)))
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


def _state_path(root: str, month: dt.date, channel: str) -> str:
    """Converge-state JSON for one month's thread: ``digest/cw/<channel>/<YYYY-MM>.json``
    — namespaced under ``cw/`` (gcs's prod state is ``digest/<YYYY-MM>.json``)
    and keyed by channel so a staging converge never masquerades as prod."""
    base = root.rsplit("/snapshots", 1)[0]
    return f"{base}/digest/cw/{channel}/{month:%Y-%m}.json"


def load_state(root: str, month: dt.date, channel: str) -> dict:
    import fsspec

    try:
        with fsspec.open(_state_path(root, month, channel), "rt") as f:
            return json.load(f)
    except (FileNotFoundError, OSError):
        return {}


def save_state(root: str, month: dt.date, channel: str, state: dict) -> None:
    import fsspec

    with fsspec.open(_state_path(root, month, channel), "wt", auto_mkdir=True) as f:
        json.dump(state, f, indent=2)


def render_plot(rows: list[Scan], month: dt.date, out_path) -> None:
    """Render the 2-panel PNG for ``rows`` to ``out_path`` (in-process; needs
    the `[plot]` extra — matplotlib)."""
    from pathlib import Path

    from .digest_plot import render

    render([{"scan": r.scan, "tb": r.tb, "objs": r.objs} for r in rows], Path(out_path), f"CoreWeave usage — {month:%B %Y}")


def post_digest(root, month, token, channel, site_url=DEFAULT_URL, icons_dir=None, deploy_plot=None, reply_delay=0.0, client=None) -> dict:
    """Converge the month's thread: render+host the plot, post/edit the OP, post
    one reply per not-yet-posted scan, persist and return state. ``icons_dir`` is
    where to write the PNG; ``deploy_plot(local_png, basename)`` publishes it and
    returns the host that serves it (or None → the branch alias). ``reply_delay``
    sleeps that many seconds between replies (>0 for a spaced backfill, so Slack
    doesn't collapse the per-reply sender chrome). ``client`` overrides the
    thrds ``SlackClient`` (tests)."""
    import time
    from pathlib import Path

    rows = load_month(root, month)
    if not rows:
        _err(f"digest: no scans for {month:%Y-%m}")
        return {}
    state = load_state(root, month, channel)
    if client is None:
        from thrds.slack import SlackClient

        client = SlackClient(token, channel)

    plot_name = state.get("plot_name") or f"plot-{secrets.token_hex(16)}.png"
    base = ICONS_BASE.replace("https://", f"https://{ICONS_BRANCH}.")
    if icons_dir is not None:
        local = Path(icons_dir) / plot_name
        render_plot(rows, month, local)
        if deploy_plot is not None:
            # the deployment-specific host serves the just-uploaded plot
            # immediately (no alias propagation race → no invalid_blocks)
            dep = deploy_plot(local, plot_name)
            if dep:
                base = dep
    plot_url = f"{base}/{plot_name}?v={int(dt.datetime.now(dt.timezone.utc).timestamp())}"
    state["plot_name"] = plot_name
    # A just-deployed Pages asset isn't instantly served at the branch alias; if
    # we post before it propagates, Slack's image-block validation 500s the whole
    # message with `invalid_blocks`. Poll until the URL is live (or give up + warn).
    if icons_dir is not None and deploy_plot is not None:
        _wait_reachable(plot_url)

    body = op_body(rows, month, plot_url, site_url)
    op_ts = state.get("op_ts")
    if op_ts:
        client.edit(op_ts, body)
        _err(f"digest: edited OP {op_ts} ({len(rows)} scans)")
    else:
        m = client.post(body, username=f"CoreWeave usage — {month:%B %Y}", icon_emoji=":calendar:")
        op_ts = m.id
        state["op_ts"] = op_ts
        _err(f"digest: posted OP {op_ts}")

    posted = state.setdefault("posted", {})
    todo = [r for r in rows if r.scan not in posted]
    for i, r in enumerate(todo):
        sender, rbody, avatar = reply(r, site_url)
        rm = client.post(rbody, thread_id=op_ts, username=sender, icon_url=avatar)
        posted[r.scan] = rm.id
        save_state(root, month, channel, state)   # persist after each → a spaced backfill is resumable
        _err(f"digest: reply {r.scan} -> {rm.id}")
        if reply_delay and i < len(todo) - 1:
            time.sleep(reply_delay)

    save_state(root, month, channel, state)
    return state
