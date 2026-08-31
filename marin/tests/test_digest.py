"""Specs for the Shape-C digest content functions (`gcs_usage.digest`).

Exact-equality against a hand-built month (no Slack, no GCS): a lead-in scan
(8/2) plus two in-month scans (8/3 Mon, 8/4 Tue — same ISO week, so the label is
unambiguous). Totals chosen so every derived string is checkable by hand."""
from datetime import date

from gcs_usage import digest as D

TIB = 1024**4


def _meta(tot_tib: float, std: float, near: float, cold: float, arch: float) -> dict:
    return {
        "total_bytes": round(tot_tib * TIB),
        "class_bytes": {"1": round(std * TIB), "2": round(near * TIB), "3": round(cold * TIB), "4": round(arch * TIB)},
    }


# lead-in 8/2 (base), then 8/3 (+30) and 8/4 (−20); class sums match totals.
DATED_META = [
    ("2026-08-02", _meta(3000, 300, 600, 1500, 600)),
    ("2026-08-03", _meta(3030, 330, 600, 1500, 600)),
    ("2026-08-04", _meta(3010, 310, 600, 1500, 600)),
]
ROWS = D.rows_from_meta(DATED_META)[1:]  # slice the lead-in


def test_deg_daily_projection():
    # daily reply uses mult=7 (project the day's rate to a weekly-equivalent)
    assert [D.deg(p, 7) for p in (0.1, 0.3, 0.5, 1.8, 4.0)] == [10, 30, 40, 60, 70]


def test_deg_weekly_and_signs():
    assert [D.deg(p, 1) for p in (0.4, 1.0, 2.5)] == [10, 20, 30]
    assert D.deg(-1.8, 7) == -60
    assert D.deg(0.0, 1) == 0
    assert D.deg(100.0, 1) == 80  # capped


def test_rows_from_meta_deltas():
    r0, r1 = ROWS
    assert (r0.date, r0.tb, r0.cost, r0.dtb, r0.dcost) == ("2026-08-03", 3030.0, 19784, 30.0, 615)
    assert (r0.std, r0.near, r0.cold, r0.arch) == (330.0, 600.0, 1500.0, 600.0)
    assert (r1.date, r1.tb, r1.cost, r1.dtb, r1.dcost) == ("2026-08-04", 3010.0, 19374, -20.0, -410)


def test_reply_grow():
    assert D.reply(ROWS[0]) == (
        "8/3 — 3,030 TB (+30.0, 1.0%)",
        "$19,784/mo (+$615) [\u2197](https://gcs.oa.dev/?d=260803)",
        "https://gcs-usage-icons.pages.dev/arrows/av_deg50.png?v=2",
    )


def test_reply_shrink():
    assert D.reply(ROWS[1]) == (
        "8/4 — 3,010 TB (−20.0, 0.7%)",
        "$19,374/mo (−$410) [\u2197](https://gcs.oa.dev/?d=260804)",
        "https://gcs-usage-icons.pages.dev/arrows/av_deg-40.png?v=2",
    )


def test_op_body():
    assert D.op_body(ROWS, date(2026, 8, 1), "https://x/p.png").split("\n") == [
        ":arrow_deg20: **+10.0 TB** month-to-date · [dashboard](https://gcs.oa.dev/)",
        "",
        "*Weekly summaries*",
        ":arrow_deg0: [wk of 8/3](https://gcs.oa.dev/?d=260804) _(partial)_ — **3,010 TB** (+10.0, 0.3%) · $19,374/mo (+$205)",
        "",
        "![GCS usage — August 2026](https://x/p.png)",
    ]


def test_op_body_full_week_not_partial():
    # a 7-scan week (Mon 8/3 .. Sun 8/9) is not flagged partial
    dm = [("2026-08-02", _meta(3000, 300, 600, 1500, 600))]
    for i, d in enumerate(range(3, 10)):
        dm.append((f"2026-08-0{d}", _meta(3000 + i, 300 + i, 600, 1500, 600)))
    rows = D.rows_from_meta(dm)[1:]
    bullet = D.op_body(rows, date(2026, 8, 1), "https://x/p.png").split("\n")[3]
    assert bullet.startswith(":arrow_deg0: [wk of 8/3](https://gcs.oa.dev/?d=260809) — ")
