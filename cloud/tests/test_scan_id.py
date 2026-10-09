"""`dt_cloud.scan_id`: scan ids (not dates) as the scan key, and the CLI's
acceptance of sub-daily ids (specs/scan-ids-not-dates.md)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from dt_cloud.cli import main
import datetime as dt

from dt_cloud.scan_id import META_PATH, check_order, check_scan_id, is_scan_id, latest_scan, resolve_slug, scan_epoch, scan_key, scan_label, scan_slug, scan_time, slug_prefix, snapshot_scans

SCANS = ["2026-10-08", "2026-10-09T0601", "2026-10-09T1802", "2026-10-09T1215"]


def test_is_scan_id():
    assert [v for v in [
        "2026-10-09", "2026-10-09T1200", "2026-10-09T0000", "2026-10-09T2359",
        "2026-10-09T12", "2026-10-09T2400", "2026-10-09T1260", "2026-02-30", "261009", "2026-10-9", "", None, 20261009,
    ] if is_scan_id(v)] == ["2026-10-09", "2026-10-09T1200", "2026-10-09T0000", "2026-10-09T2359"]


def test_check_scan_id_names_both_forms():
    assert check_scan_id("2026-10-09T1200") == "2026-10-09T1200"
    with pytest.raises(ValueError) as e:
        check_scan_id("2026-10-09 12:00")
    assert str(e.value) == "not a scan id: '2026-10-09 12:00' (want YYYY-MM-DD or YYYY-MM-DDTHHMM)"


def test_scan_time_and_slug():
    utc = dt.timezone.utc
    assert [scan_time(s) for s in ["2026-10-09", "2026-10-09T0601"]] == [dt.datetime(2026, 10, 9, tzinfo=utc), dt.datetime(2026, 10, 9, 6, 1, tzinfo=utc)]
    assert [scan_slug(s) for s in ["2026-10-09", "2026-10-09T0601"]] == ["2610090000", "2610090601"]
    for f in (scan_time, scan_slug):
        with pytest.raises(ValueError):
            f("261009")


def test_meta_path_extracts_the_scan_id():
    assert [m and m.group(1) for m in map(META_PATH.search, [
        "bkt/snapshots/2026-10-09/meta.json", "bkt/snapshots/2026-10-09T0601/meta.json", "bkt/snapshots/cw/meta.json", "bkt/snapshots/2026-10-09T06/meta.json",
    ])] == ["2026-10-09", "2026-10-09T0601", None, None]


def test_latest_scan_picks_the_latest_match():
    assert [latest_scan(p, SCANS) for p in ["2026-10-09", "2026-10-09T12", "2026-10-09T06", "2026-10-09T0601", "2026-10-08", "2026-10-07", "2026-10"]] == [
        "2026-10-09T1802", "2026-10-09T1215", "2026-10-09T0601", "2026-10-09T0601", "2026-10-08", None, "2026-10-09T1802",
    ]


def test_slug_prefix_and_resolve_slug():
    slugs = ["261009", "26100912", "2610091215", "261009-12", "261009-1215", "261009T12", "2026-10-09", "2026-10-09T12", "2026-10-09T1215",
             "26100903", "261010", "26100924", "20261009", "junk"]
    assert [slug_prefix(s) for s in slugs] == [
        "2026-10-09", "2026-10-09T12", "2026-10-09T1215", "2026-10-09T12", "2026-10-09T1215", "2026-10-09T12", "2026-10-09", "2026-10-09T12", "2026-10-09T1215",
        "2026-10-09T03", "2026-10-10", None, None, None,
    ]
    assert [resolve_slug(s, SCANS) for s in slugs] == [
        "2026-10-09T1802", "2026-10-09T1215", "2026-10-09T1215", "2026-10-09T1215", "2026-10-09T1215", "2026-10-09T1215", "2026-10-09T1802", "2026-10-09T1215", "2026-10-09T1215",
        None, None, None, None, None,
    ]


def test_snapshot_scans_lists_sub_daily_dirs(tmp_path: Path):
    for name in ["2026-10-08", "2026-10-09T0601", "2026-10-09T1802", "not-a-scan", "2026-10-10"]:
        (tmp_path / name).mkdir()
        if name != "2026-10-10":  # no meta.json: not published
            (tmp_path / name / "meta.json").write_text("{}")
    assert snapshot_scans(tmp_path) == ["2026-10-09T1802", "2026-10-09T0601", "2026-10-08"]


@pytest.mark.parametrize("args", [
    ["index-dir", "BAD"],
    ["index-sync", "-b", "bkt", "-g", "g1", "BAD"],
    ["ch-ingest", "-d", "BAD", "src.parquet"],
    ["job", "submit-listing", "-d", "BAD"],
    ["path-index", "-d", "BAD", "-l", "x.parquet"],
])
def test_scan_id_args_refuse_non_ids(args: list[str]):
    r = CliRunner().invoke(main, args)
    assert r.exit_code == 2
    assert r.output.rstrip().split("\n")[-1].endswith("not a scan id: 'BAD' (want YYYY-MM-DD or YYYY-MM-DDTHHMM)")


def test_index_dir_accepts_a_sub_daily_id(monkeypatch):
    seen = []

    def index_dir(date, variant, store):
        seen.append((date, variant, store))
        return f"listing/{date}/index/g1"

    monkeypatch.setattr("dt_cloud.index_footer.index_dir", index_dir)
    r = CliRunner().invoke(main, ["index-dir", "2026-10-09T0601"])
    assert (r.exit_code, r.output, seen) == (0, "listing/2026-10-09T0601/index/g1\n", [("2026-10-09T0601", "path", "primary")])


def test_submit_listing_keys_by_scan_id(monkeypatch):
    calls = []
    monkeypatch.setattr("dt_cloud.batch.listing_regions", lambda: {})
    monkeypatch.setattr("dt_cloud.batch.listing_job_spec", lambda date, buckets, **kw: {"date": date, "buckets": buckets})
    monkeypatch.setattr("dt_cloud.batch.submit_job", lambda spec, region: calls.append((spec, region)) or "job-1")
    r = CliRunner().invoke(main, ["job", "submit-listing", "-d", "2026-10-09T0601", "-b", "b1"])
    assert (r.exit_code, r.stdout) == (0, "job-1\n")
    assert calls == [({"date": "2026-10-09T0601", "buckets": ["b1"]}, "us-central1")]


@pytest.mark.parametrize("v, ok", [
    ("2026-10-09", True), ("2026-10-09T1236", True), ("2026-10-09T0000", True),
    ("2026-02-30", False), ("2026-10-09T2460", False), ("2026-10-09T12", False), ("261009", False), (None, False),
])
def test_is_scan_id_static_cases(v, ok):
    assert is_scan_id(v) == ok


def test_order_mixes_forms_by_time():
    """gcs's move to scan ids: a bare date sorts as its midnight, before that day's minute ids."""
    ids = ["2026-10-10", "2026-10-09T1236", "2026-10-09", "2026-10-10T0601"]
    assert check_order(ids) == ["2026-10-09", "2026-10-09T1236", "2026-10-10", "2026-10-10T0601"]
    assert [scan_label(scan_epoch(i)) for i in check_order(ids)] == check_order(ids)


def test_order_refuses_two_ids_at_one_instant():
    with pytest.raises(ValueError) as e:
        check_order(["2026-10-09T0000", "2026-10-09"])
    assert str(e.value) == "scans 2026-10-09 and 2026-10-09T0000: ids out of time order (stamps 1791504000, 1791504000)"


def test_date_only_and_timed_scan_on_one_day():
    # gcs 10/9: the date-only run and 12:36Z. The day slug is the latest; each
    # scan's exact slug (`scan_slug`) resolves to exactly it.
    scans = ["2026-10-08", "2026-10-09", "2026-10-09T1236"]
    assert [scan_slug(s) for s in scans] == ["2610080000", "2610090000", "2610091236"]
    assert [resolve_slug(scan_slug(s), scans) for s in scans] == scans
    assert [resolve_slug(s, scans) for s in ["261009", "2026-10-09", "26100900", "2610090000", "26100912", "26100904"]] == [
        "2026-10-09T1236", "2026-10-09T1236", "2026-10-09", "2026-10-09", "2026-10-09T1236", None,
    ]
    assert scan_key("2026-10-09") == "2026-10-09T0000"
    assert scan_key("2026-10-09T1236") == "2026-10-09T1236"
    # the key never collides with a real scan: a date and its T0000 are refused together
    with pytest.raises(ValueError):
        check_order(["2026-10-09", "2026-10-09T0000"])
