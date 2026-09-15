"""Specs for the CoreWeave digest MECHANISM (`gcs_usage.digest`): the pure
helpers (arrow degrees, scan ids, deltas, link tokens, state path) and the
monthly-thread converge against a fake Slack client over a local snapshot
tree. The message content (`op_body`/`reply`) is a placeholder being
workshopped (specs/cw-slack-digest.md), so it's exercised only through the
converge — no text is pinned here.

Hand-built month: a lead-in scan (Sun 9/6 12:00Z for the pure helpers; an
August one for the month-scoped IO tests) plus in-month 12-hourly scans (Mon
9/7 00:00Z, 9/7 12:00Z, Tue 9/8 00:00Z). Totals chosen so every derived number
is checkable by hand."""
import datetime as dt
import json
from datetime import date
from pathlib import Path

import pytest

from gcs_usage import digest as D

TIB = 1024**4
UTC = dt.timezone.utc


def _meta(tot_tib: float, objs: int) -> dict:
    return {"total_bytes": round(tot_tib * TIB), "total_objects": objs, "class_bytes": {}}


# lead-in 9/6 12:00 (base 900 / 9.00M), then +3 (903), −1 (902), +3 (905).
DATED_META = [
    ("2026-09-06T1200", _meta(900, 9_000_000)),
    ("2026-09-07T0000", _meta(903, 9_020_000)),
    ("2026-09-07T1200", _meta(902, 9_010_000)),
    ("2026-09-08T0000", _meta(905, 9_030_000)),
]
ROWS = D.rows_from_meta(DATED_META)[1:]  # slice the lead-in
SEP = date(2026, 9, 1)
# an August lead-in for the month-scoped IO tests (9/6 above is in-month, so
# `load_month` keeps it as a row)
LEAD = ("2026-08-31T1200", _meta(890, 8_900_000))


def test_deg_projection():
    # a 12-hourly reply projects its rate with mult=14; daily with 7 (gcs's numbers)
    assert [D.deg(p, 14) for p in (0.1, 0.3, 0.5, 1.8)] == [20, 40, 50, 70]
    assert [D.deg(p, 7) for p in (0.1, 0.3, 0.5, 1.8, 4.0)] == [10, 30, 40, 60, 70]


def test_deg_weekly_and_signs():
    assert [D.deg(p, 1) for p in (0.4, 1.0, 2.5)] == [10, 20, 30]
    assert D.deg(-1.8, 7) == -60
    assert D.deg(0.0, 1) == 0
    assert D.deg(100.0, 1) == 80  # capped


def test_scan_ts():
    assert D.scan_ts("2026-09-07T1201") == dt.datetime(2026, 9, 7, 12, 1, tzinfo=UTC)
    assert D.scan_ts("2026-09-07") == dt.datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="not a scan id"):
        D.scan_ts("260907")


def test_formatters():
    assert [D._tb(v) for v in (3.0, 0.0, -1.25)] == ["+3.0", "+0.0", "−1.2"]
    assert [D._objs(n) for n in (9_020_000, 38_428_773)] == ["9.02M", "38.43M"]
    assert [D._dobjs(d) for d in (20_000, 0, -10_000)] == ["+0.02M", "+0.00M", "−0.01M"]
    assert [D._pct(3.0, 903.0), D._pct(-1.0, 902.0), D._pct(5.0, 5.0)] == ["0.3", "0.1", "0.0"]
    assert D._pct_val(-1.0, 902.0) == pytest.approx(-0.110742, abs=1e-6)


def test_dlink_and_span():
    # the site's compact `?d=` token + `encodeSpan` look-back (site/src/scan.ts)
    assert [D._dlink(s) for s in ("2026-09-07T1201", "2026-09-07")] == ["260907-1201", "260907"]
    t0 = D.scan_ts("2026-09-06T1200")
    assert [D._span(t0, D.scan_ts(s)) for s in ("2026-09-08T0000", "2026-09-13T1200", "2026-09-07T0000", "2026-09-06T1200")] == [
        "1d12h", "7d", "12h", "0h",
    ]


def test_rows_from_meta_deltas():
    r0, r1, r2 = ROWS
    assert (r0.scan, r0.date, r0.tb, r0.objs, r0.dtb, r0.dobjs, r0.hours) == ("2026-09-07T0000", "2026-09-07", 903.0, 9_020_000, 3.0, 20_000, 12.0)
    assert (r1.scan, r1.tb, r1.objs, r1.dtb, r1.dobjs, r1.hours) == ("2026-09-07T1200", 902.0, 9_010_000, -1.0, -10_000, 12.0)
    assert (r2.scan, r2.tb, r2.objs, r2.dtb, r2.dobjs, r2.hours) == ("2026-09-08T0000", 905.0, 9_030_000, 3.0, 20_000, 12.0)
    # no lead-in → no deltas
    first = D.rows_from_meta(DATED_META[:1])[0]
    assert (first.dtb, first.dobjs, first.hours) == (None, None, None)


def test_reply_avatar_is_clock_normalised():
    # the arrow projects the scan's Δ% over its real interval: +3.0 on 900 in
    # 12 h → 0.33%·14 → deg40; the same delta over a day (a date-only id) → deg30
    assert D.reply(ROWS[0])[2] == f"{D.ICONS_BASE}/arrows/av_deg40.png?v={D.AVATAR_REV}"
    daily = D.rows_from_meta([("2026-09-06", _meta(900, 9_000_000)), ("2026-09-07", _meta(903, 9_020_000))])[1]
    assert D.reply(daily)[2] == f"{D.ICONS_BASE}/arrows/av_deg30.png?v={D.AVATAR_REV}"
    assert D.reply(ROWS[1])[2] == f"{D.ICONS_BASE}/arrows/av_deg-30.png?v={D.AVATAR_REV}"


def test_state_path():
    # namespaced under cw/ and keyed by channel: staging and prod never share a thread
    assert D._state_path("gs://b/snapshots/cw", SEP, "C1") == "gs://b/digest/cw/C1/2026-09.json"


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


def test_load_month_lead_in(tmp_path: Path):
    root = tmp_path / "snapshots" / "cw"
    _publish(root, [LEAD] + DATED_META[1:])
    # September's rows are exactly the in-month scans; the lead-in only seeds the first delta (8/31 12:00 → 9/7 00:00 = 156 h)
    rows = D.load_month(str(root), SEP)
    assert [r.scan for r in rows] == ["2026-09-07T0000", "2026-09-07T1200", "2026-09-08T0000"]
    assert (rows[0].dtb, rows[0].hours) == (13.0, 156.0)
    assert D.load_month(str(root), date(2026, 7, 1)) == []
    # a root with no scan before the month: the first row simply has no delta
    _publish(tmp_path / "fresh", DATED_META[1:])
    assert D.load_month(str(tmp_path / "fresh"), SEP)[0].dtb is None


def test_post_digest_converges(tmp_path: Path):
    root = tmp_path / "snapshots" / "cw"
    _publish(root, [LEAD] + DATED_META[1:3])
    state_file = tmp_path / "digest" / "cw" / "C1" / "2026-09.json"
    rows = D.load_month(str(root), SEP)
    fake = _FakeSlack()

    # fresh month, two scans in: OP under the month's sender, then one reply per scan in the thread
    state = D.post_digest(str(root), SEP, "xoxb", "C1", client=fake)
    plot = state["plot_name"]
    assert plot.startswith("plot-") and plot.endswith(".png")
    op_url_prefix = f"https://cw.gcs-usage-icons.pages.dev/{plot}?v="
    op = fake.calls[0]
    assert (op[0], op[2], op[3], op[4], op[5]) == ("post", None, "CoreWeave usage — September 2026", None, ":calendar:")
    assert op[1].startswith(D.op_body(rows, SEP, op_url_prefix).split("?v=")[0])
    assert fake.calls[1:] == [
        ("post", D.reply(r)[1], "m1", D.reply(r)[0], D.reply(r)[2], None) for r in rows
    ]
    assert state == {"plot_name": plot, "op_ts": "m1", "posted": {"2026-09-07T0000": "m2", "2026-09-07T1200": "m3"}}
    assert json.loads(state_file.read_text()) == state

    # next scan: OP edited in place, only the new scan replied, plot name stable
    _publish(root, DATED_META[3:])
    fake.calls.clear()
    state = D.post_digest(str(root), SEP, "xoxb", "C1", client=fake)
    rows = D.load_month(str(root), SEP)
    assert fake.calls[0][:2] == ("edit", "m1")
    assert fake.calls[1:] == [("post", D.reply(rows[2])[1], "m1", D.reply(rows[2])[0], D.reply(rows[2])[2], None)]
    assert state["plot_name"] == plot
    assert state["posted"] == {"2026-09-07T0000": "m2", "2026-09-07T1200": "m3", "2026-09-08T0000": "m4"}

    # same scans again: nothing but the OP refresh
    fake.calls.clear()
    D.post_digest(str(root), SEP, "xoxb", "C1", client=fake)
    assert [c[0] for c in fake.calls] == ["edit"]

    # another channel is its own thread (state keyed by channel)
    other = _FakeSlack()
    D.post_digest(str(root), SEP, "xoxb", "C2", client=other)
    assert [c[0] for c in other.calls] == ["post", "post", "post", "post"]
    assert (tmp_path / "digest" / "cw" / "C2" / "2026-09.json").exists()


def test_post_digest_empty_month(tmp_path: Path):
    root = tmp_path / "snapshots" / "cw"
    _publish(root, DATED_META)
    fake = _FakeSlack()
    assert D.post_digest(str(root), date(2026, 10, 1), "xoxb", "C1", client=fake) == {}
    assert fake.calls == []
