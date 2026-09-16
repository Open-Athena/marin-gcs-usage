"""Specs for the cache warm-up plan (`dt_cloud.warm`): exact request lists."""
from dt_cloud import warm as W

DATES = [f"2026-08-{d:02d}" for d in range(10, 32)] + [f"2026-09-{d:02d}" for d in range(1, 16)]


def test_nearest_prior_resolves_like_the_site():
    # 7 days back from 9/15 is 9/8 exactly; 30 days back is 8/16
    assert W.nearest_prior(DATES, "2026-09-15", 7) == "2026-09-08"
    assert W.nearest_prior(DATES, "2026-09-15", 30) == "2026-08-16"
    # a missing scan resolves to the nearest neighbour, never the scan itself
    dates = [d for d in DATES if d != "2026-09-08"]
    assert W.nearest_prior(dates, "2026-09-15", 7) in ("2026-09-07", "2026-09-09")
    assert W.nearest_prior(DATES, "2026-09-15", 0) == "2026-09-14"
    assert W.nearest_prior(["2026-09-15"], "2026-09-15", 1) is None


def test_plan_one_width():
    assert W.plan("2026-09-15", DATES, widths=(1280,), spans=(1, 7), paths=("",)) == [
        "/api/subtree?date=2026-09-15&path=&w=1280&h=768",
        "/api/diff?from=2026-09-14&to=2026-09-15&path=&w=1280&h=768&summary=1",
        "/api/diff?from=2026-09-14&to=2026-09-15&path=&w=1280&h=768",
        "/api/diff?from=2026-09-08&to=2026-09-15&path=&w=1280&h=768&summary=1",
        "/api/diff?from=2026-09-08&to=2026-09-15&path=&w=1280&h=768",
    ]


def test_plan_dedupes_pairs_and_counts():
    # the previous-scan pair and the 1d chip are the same pair: 5 spans + prev → 5 distinct pairs;
    # × (root + 6 buckets) × 5 widths
    paths = W.plan("2026-09-15", DATES)
    assert len(paths) == len(W.WIDTHS) * len(W.PATHS) * (1 + 2 * 5)
    assert paths[0] == "/api/subtree?date=2026-09-15&path=&w=512&h=307"
    assert paths[11] == "/api/subtree?date=2026-09-15&path=marin-us-central2&w=512&h=307"
    assert paths[-1] == "/api/diff?from=2026-08-16&to=2026-09-15&path=marin-us-east1&w=1920&h=1152"
