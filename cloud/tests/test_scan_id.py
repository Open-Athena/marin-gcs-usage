"""`dt_cloud.scan_id`: scan ids (not dates) as the scan key, and the CLI's
acceptance of sub-daily ids (specs/scan-ids-not-dates.md)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from dt_cloud.cli import main
from dt_cloud.scan_id import check_scan_id, is_scan_id, latest_scan, snapshot_scans

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


def test_latest_scan_picks_the_latest_match():
    assert [latest_scan(p, SCANS) for p in ["2026-10-09", "2026-10-09T12", "2026-10-09T06", "2026-10-09T0601", "2026-10-08", "2026-10-07", "2026-10"]] == [
        "2026-10-09T1802", "2026-10-09T1215", "2026-10-09T0601", "2026-10-09T0601", "2026-10-08", None, "2026-10-09T1802",
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
