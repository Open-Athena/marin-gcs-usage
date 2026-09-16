"""Specs for the CoreWeave digest (`gcs_usage.digest`): the pure helpers, the
framing-A content (OP + both reply variants, exact strings), the daily keying
rule, and the monthly-thread converge against a fake Slack client over a local
snapshot tree.

Hand-built series (12-hourly, 00:00Z / 12:00Z): a lead day Mon 8/31 (700,
705 TiB) then Tue 9/1 (710, 713) and Wed 9/2 (712, 715) — one ISO week, so
the bullet label is unambiguous. Quota = 10^15 B = 909.4947 TiB."""
import datetime as dt
import json
from datetime import date
from pathlib import Path

import pytest

from gcs_usage import digest as D

TIB = 1024**4
UTC = dt.timezone.utc
SITE = "https://cw-s3.oa.dev"
AV = "https://gcs-usage-icons.pages.dev/arrows/av_deg"
SEP = date(2026, 9, 1)


def _meta(tot_tib: float, objs: int = 1_000_000) -> dict:
    return {"total_bytes": round(tot_tib * TIB), "total_objects": objs, "class_bytes": {}}


LEAD = [("2026-08-31T0000", _meta(700)), ("2026-08-31T1200", _meta(705))]
SEPT = [("2026-09-01T0000", _meta(710)), ("2026-09-01T1200", _meta(713)), ("2026-09-02T0000", _meta(712)), ("2026-09-02T1200", _meta(715))]
_all = D.rows_from_meta(LEAD + SEPT)
MONTH = D.Month(lead=_all[:2], rows=_all[2:])


def test_quota_constant():
    # 1 PB decimal, which the site's "910 TiB" comments round
    assert D.QUOTA_BYTES == 10**15
    assert round(D.QUOTA_TIB, 2) == 909.49
    assert (D._quota(715.0), D._free(715.0), D._free(D.QUOTA_TIB)) == ("78.6% of 1 PB", "194.5 TiB free", "0.0 TiB free")


def test_deg_projection():
    # a daily reply projects with 168/24 = 7; a 12-hourly interval with 14
    assert [D.deg(p, 7) for p in (0.1, 0.3, 0.5, 1.8, 4.0)] == [10, 30, 40, 60, 70]
    assert [D.deg(p, 14) for p in (0.1, 0.3, 0.5, 1.8)] == [20, 40, 50, 70]
    assert [D.deg(p, 1) for p in (0.4, 1.0, 2.5)] == [10, 20, 30]
    assert (D.deg(-1.8, 7), D.deg(0.0, 1), D.deg(100.0, 1)) == (-60, 0, 80)


def test_scan_ts():
    assert D.scan_ts("2026-09-07T1201") == dt.datetime(2026, 9, 7, 12, 1, tzinfo=UTC)
    assert D.scan_ts("2026-09-07") == dt.datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="not a scan id"):
        D.scan_ts("260907")


def test_formatters_and_links():
    assert [D._tb(v) for v in (3.0, 0.0, -1.25)] == ["+3.0", "+0.0", "−1.2"]
    assert [D._pct(3.0, 903.0), D._pct(-1.0, 902.0), D._pct(5.0, 5.0)] == ["0.3", "0.1", "0.0"]
    assert [D._dlink(s) for s in ("2026-09-07T1201", "2026-09-07")] == ["260907-1201", "260907"]
    t0 = D.scan_ts("2026-09-06T1200")
    assert [D._span(t0, D.scan_ts(s)) for s in ("2026-09-08T0000", "2026-09-13T1200", "2026-09-07T0000", "2026-09-06T1200", "2026-09-07T1159")] == ["1d12h", "7d", "12h", "0h", "1d"]
    assert D._diff_url("2026-09-02T1200", D.scan_ts("2026-08-31T1200"), SITE) == f"{SITE}/?d=260902-1200-2d#diff"
    assert D._diff_url("2026-09-02T1200", None, SITE) == f"{SITE}/?d=260902-1200#diff"


def test_rows_from_meta_deltas():
    r = MONTH.rows[0]
    assert (r.scan, r.date, r.tb, r.dtb, r.hours) == ("2026-09-01T0000", "2026-09-01", 710.0, 5.0, 12.0)
    assert (MONTH.lead[0].dtb, MONTH.lead[0].hours) == (None, None)
    assert MONTH.base.scan == "2026-08-31T1200"
    assert D.Month(lead=[], rows=_all[2:]).base.scan == "2026-09-01T0000"


def test_day_rows_variants():
    # sender: a day is its FIRST scan, Δ vs the prior day's first (midnight-to-midnight)
    assert D.day_rows(MONTH, "sender") == [
        D.DayRow("2026-09-01", "2026-09-01T0000", 710.0, 10.0, 24.0, D.scan_ts("2026-08-31T0000")),
        D.DayRow("2026-09-02", "2026-09-02T0000", 712.0, 2.0, 24.0, D.scan_ts("2026-09-01T0000")),
    ]
    # body: a day is its LAST scan so far, Δ vs the prior day's last
    assert D.day_rows(MONTH, "body") == [
        D.DayRow("2026-09-01", "2026-09-01T1200", 713.0, 8.0, 24.0, D.scan_ts("2026-08-31T1200")),
        D.DayRow("2026-09-02", "2026-09-02T1200", 715.0, 2.0, 24.0, D.scan_ts("2026-09-01T1200")),
    ]
    # a half-landed day on `body`: its reply is that day's 00:00 scan, 12 h after the prior day's last
    half = D.Month(lead=MONTH.lead, rows=MONTH.rows[:3])
    assert D.day_rows(half, "body")[1] == D.DayRow("2026-09-02", "2026-09-02T0000", 712.0, -1.0, 12.0, D.scan_ts("2026-09-01T1200"))
    # first month ever: no prior for day 1
    assert D.day_rows(D.Month(lead=[], rows=MONTH.rows), "sender")[0] == D.DayRow("2026-09-01", "2026-09-01T0000", 710.0, None, None, None)
    with pytest.raises(ValueError, match="variant must be one of"):
        D.day_rows(MONTH, "x")


def test_op_body():
    # month-to-date +10.0 on 705 over 2 days → 1.42%·3.5 = 5.0%/wk → deg40;
    # the (partial) week +10.0 → 1.4% → deg20, linked over 8/31 12:00 → 9/2 12:00 = 2d
    assert D.op_body(MONTH, SEP, "https://x/p.png").split("\n") == [
        f":arrow_deg40: **+10.0 TiB** [month-to-date]({SITE}/?d=260902-1200-2d#diff) · 715 TiB · 78.6% of 1 PB · [dashboard]({SITE}/)",
        "",
        "*Weekly summaries*",
        f":arrow_deg20: [wk of 8/31]({SITE}/?d=260902-1200-2d#diff) _(partial)_: **+10.0 TiB** → 715 TiB · 78.6% of 1 PB",
        "",
        "![CoreWeave usage — September 2026](https://x/p.png)",
    ]
    assert D.op_body(MONTH, SEP, None).split("\n")[-1].startswith(":arrow_deg20: [wk of 8/31]")


def test_op_body_two_weeks():
    # a completed week (Sunday scanned) is not partial; the next week's bullet is Δ vs that week's end
    metas = LEAD + [(f"2026-09-{d:02d}T{h}", _meta(700 + i)) for i, (d, h) in enumerate(((d, h) for d in range(1, 8) for h in ("0000", "1200")), start=1)]
    rows = D.rows_from_meta(metas)
    month = D.Month(lead=rows[:2], rows=rows[2:])
    bullets = D.op_body(month, SEP, None).split("\n")[3:]
    assert bullets == [
        f":arrow_deg20: [wk of 8/31]({SITE}/?d=260906-1200-6d#diff): **+7.0 TiB** → 712 TiB · 78.3% of 1 PB",
        f":arrow_deg0: [wk of 9/7]({SITE}/?d=260907-1200-1d#diff) _(partial)_: **+2.0 TiB** → 714 TiB · 78.5% of 1 PB",
    ]


def test_reply_sender_variant():
    d1, d2 = D.day_rows(MONTH, "sender")
    # +10.0 on 700 in 24 h → 1.43%·7 = 10%/wk → deg50; +2.0 on 710 → 0.28%·7 = 2.0% → deg30
    assert D.reply(d1, "sender") == D.Reply(
        "9/1 — 710 TiB (+10.0, 1.4%)",
        f"78.1% of 1 PB · 199.5 TiB free [↗︎]({SITE}/?d=260901-0000-1d#diff)",
        icon_url=f"{AV}50.png?v=4",
    )
    assert D.reply(d2, "sender") == D.Reply(
        "9/2 — 712 TiB (+2.0, 0.3%)",
        f"78.3% of 1 PB · 197.5 TiB free [↗︎]({SITE}/?d=260902-0000-1d#diff)",
        icon_url=f"{AV}30.png?v=4",
    )


def test_reply_body_variant():
    d1, d2 = D.day_rows(MONTH, "body")
    # +8.0 on 705 in 24 h → 1.13%·7 = 7.9%/wk → deg50; +2.0 on 713 → deg30
    assert D.reply(d1, "body") == D.Reply(
        "CoreWeave usage",
        f":arrow_deg50: [9/1]({SITE}/?d=260901-1200-1d#diff) — **713 TiB (+8.0, 1.1%)** · 78.4% of 1 PB · 196.5 TiB free",
        icon_emoji=":calendar:",
    )
    assert D.reply(d2, "body") == D.Reply(
        "CoreWeave usage",
        f":arrow_deg30: [9/2]({SITE}/?d=260902-1200-1d#diff) — **715 TiB (+2.0, 0.3%)** · 78.6% of 1 PB · 194.5 TiB free",
        icon_emoji=":calendar:",
    )


def test_reply_first_day_ever():
    # no prior scan: zero delta, flat arrow, link without a look-back
    day = D.day_rows(D.Month(lead=[], rows=MONTH.rows[:1]), "sender")[0]
    assert D.reply(day, "sender") == D.Reply("9/1 — 710 TiB (+0.0, 0.0%)", f"78.1% of 1 PB · 199.5 TiB free [↗︎]({SITE}/?d=260901-0000#diff)", icon_url=f"{AV}0.png?v=4")


def test_state_path():
    # namespaced under cw/, keyed by channel AND variant: staging A/B and prod never share a thread
    assert D._state_path("gs://b/snapshots/cw", SEP, "C1", "sender") == "gs://b/digest/cw/C1/sender/2026-09.json"


# ---- converge mechanism ----------------------------------------------------


class _Msg:
    def __init__(self, id: str):
        self.id = id


class _FakeSlack:
    """Records `post`/`edit` calls as tuples; message ids are sequential."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.n = 0

    def post(self, content, thread_id=None, *, username=None, icon_url=None, icon_emoji=None):
        self.n += 1
        self.calls.append(("post", content, thread_id, username, icon_url, icon_emoji))
        return _Msg(f"m{self.n}")

    def edit(self, ts, content):
        self.calls.append(("edit", ts, content))
        return _Msg(ts)


def _publish(root: Path, dated_meta) -> None:
    for scan, m in dated_meta:
        d = root / scan
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps(m))


def test_load_month(tmp_path: Path):
    root = tmp_path / "snapshots" / "cw"
    _publish(root, [("2026-08-30T1200", _meta(690))] + LEAD + SEPT)
    month = D.load_month(str(root), SEP)
    # lead = every scan of the last pre-month day (not 8/30); rows = the in-month scans
    assert [r.scan for r in month.lead] == ["2026-08-31T0000", "2026-08-31T1200"]
    assert [r.scan for r in month.rows] == [s for s, _ in SEPT]
    assert (month.lead[0].dtb, month.rows[0].dtb, month.rows[0].hours) == (None, 5.0, 12.0)  # deltas only within the loaded window
    assert D.load_month(str(root), date(2026, 7, 1)) is None
    _publish(tmp_path / "fresh", SEPT)
    assert D.load_month(str(tmp_path / "fresh"), SEP).lead == []


def _post(c):
    return (c[0], c[2], c[3], c[4], c[5]) if c[0] == "post" else c[:2]


def test_post_digest_sender_variant(tmp_path: Path):
    root = tmp_path / "snapshots" / "cw"
    _publish(root, LEAD + SEPT[:1])
    fake = _FakeSlack()
    # 9/1 00:00 lands: OP under the month sender + one reply for 9/1 (headline as sender, arrow avatar)
    state = D.post_digest(str(root), SEP, "xoxb", "C1", "sender", client=fake)
    plot = state["plot_name"]
    assert plot.startswith("plot-") and plot.endswith(".png")
    assert [_post(c) for c in fake.calls] == [
        ("post", None, "CoreWeave usage — September 2026", None, ":calendar:"),
        ("post", "m1", "9/1 — 710 TiB (+10.0, 1.4%)", f"{AV}50.png?v=4", None),
    ]
    assert fake.calls[0][1].startswith(":arrow_deg") and f"https://cw.gcs-usage-icons.pages.dev/{plot}?v=" in fake.calls[0][1]
    assert state == {"plot_name": plot, "variant": "sender", "op_ts": "m1", "posted": {"2026-09-01": {"ts": "m2", "scan": "2026-09-01T0000"}}}
    assert json.loads((tmp_path / "digest" / "cw" / "C1" / "sender" / "2026-09.json").read_text()) == state

    # 9/1 12:00 lands: only the OP is refreshed — no reply, no edit
    _publish(root, SEPT[1:2])
    fake.calls.clear()
    state = D.post_digest(str(root), SEP, "xoxb", "C1", "sender", client=fake)
    assert [_post(c) for c in fake.calls] == [("edit", "m1")]
    assert state["posted"] == {"2026-09-01": {"ts": "m2", "scan": "2026-09-01T0000"}}

    # 9/2 00:00 lands: a new day → a new reply
    _publish(root, SEPT[2:3])
    fake.calls.clear()
    state = D.post_digest(str(root), SEP, "xoxb", "C1", "sender", client=fake)
    assert [_post(c) for c in fake.calls] == [("edit", "m1"), ("post", "m1", "9/2 — 712 TiB (+2.0, 0.3%)", f"{AV}30.png?v=4", None)]
    assert state["posted"]["2026-09-02"] == {"ts": "m3", "scan": "2026-09-02T0000"}
    assert state["plot_name"] == plot


def test_post_digest_body_variant(tmp_path: Path):
    root = tmp_path / "snapshots" / "cw"
    _publish(root, LEAD + SEPT[:1])
    fake = _FakeSlack()
    # 9/1 00:00: OP + the day's reply under the static sender, headline in the body (Δ vs 8/31's last = 12 h)
    D.post_digest(str(root), SEP, "xoxb", "C1", "body", client=fake)
    assert [_post(c) for c in fake.calls] == [
        ("post", None, "CoreWeave usage — September 2026", None, ":calendar:"),
        ("post", "m1", "CoreWeave usage", None, ":calendar:"),
    ]
    assert fake.calls[1][1] == f":arrow_deg50: [9/1]({SITE}/?d=260901-0000-12h#diff) — **710 TiB (+5.0, 0.7%)** · 78.1% of 1 PB · 199.5 TiB free"

    # 9/1 12:00 lands: the OP AND the day's reply are edited to the latest scan (now a 24 h Δ)
    _publish(root, SEPT[1:2])
    fake.calls.clear()
    state = D.post_digest(str(root), SEP, "xoxb", "C1", "body", client=fake)
    assert fake.calls[0][:2] == ("edit", "m1")
    assert fake.calls[1] == ("edit", "m2", f":arrow_deg50: [9/1]({SITE}/?d=260901-1200-1d#diff) — **713 TiB (+8.0, 1.1%)** · 78.4% of 1 PB · 196.5 TiB free")
    assert state["posted"] == {"2026-09-01": {"ts": "m2", "scan": "2026-09-01T1200"}}

    # same scans again: nothing but the OP refresh (the reply already reflects the latest scan)
    fake.calls.clear()
    D.post_digest(str(root), SEP, "xoxb", "C1", "body", client=fake)
    assert [_post(c) for c in fake.calls] == [("edit", "m1")]

    # both variants in one channel keep separate threads/state
    other = _FakeSlack()
    D.post_digest(str(root), SEP, "xoxb", "C1", "sender", client=other)
    assert [c[0] for c in other.calls] == ["post", "post"]
    assert (tmp_path / "digest" / "cw" / "C1" / "sender" / "2026-09.json").exists()


def test_post_digest_empty_month(tmp_path: Path):
    root = tmp_path / "snapshots" / "cw"
    _publish(root, LEAD + SEPT)
    fake = _FakeSlack()
    assert D.post_digest(str(root), date(2026, 10, 1), "xoxb", "C1", client=fake) == {}
    assert fake.calls == []
