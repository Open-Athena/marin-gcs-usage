"""Mark & sweep engine (Slice 1): plan model, versioning guard, manifest builder."""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from gcs_usage.sweep import (
    Plan,
    SweepError,
    build_manifest,
    load_plan,
    normalize_prefix,
    versioning_enabled,
)

BUCKET = "marin-us-east-02a"


def test_normalize_prefix() -> None:
    assert normalize_prefix(f"s3://{BUCKET}/marin/ckpt/", BUCKET) == "marin/ckpt/"
    assert normalize_prefix(f"s3://{BUCKET}/marin/ckpt", BUCKET) == "marin/ckpt/"
    assert normalize_prefix("marin/ckpt", BUCKET) == "marin/ckpt/"
    assert normalize_prefix("/marin/ckpt/", BUCKET) == "marin/ckpt/"


def test_plan_validate_rejects_bad() -> None:
    with pytest.raises(SweepError):
        Plan(name="empty", bucket=BUCKET, sweep=[]).validate()
    with pytest.raises(SweepError):
        Plan(name="no-slash", bucket=BUCKET, sweep=["marin/ckpt"]).validate()  # normalize adds slash; raw lacks it
    with pytest.raises(SweepError):
        Plan(name="dotdot", bucket=BUCKET, sweep=["../etc/"]).validate()


def test_load_plan_normalizes(tmp_path: Path) -> None:
    p = tmp_path / "plan.json"
    p.write_text(json.dumps({
        "plan_id": 7,
        "name": "old ckpts",
        "bucket": BUCKET,
        "sweep": [f"s3://{BUCKET}/marin/ckpt/", "marin/scratch"],
        "keep": [f"s3://{BUCKET}/marin/ckpt/keep/"],
    }))
    plan = load_plan(p)
    assert plan == Plan(
        name="old ckpts",
        bucket=BUCKET,
        sweep=["marin/ckpt/", "marin/scratch/"],
        keep=["marin/ckpt/keep/"],
        plan_id=7,
    )


class _FakeS3:
    def __init__(self, status: str | None) -> None:
        self._status = status

    def get_bucket_versioning(self, Bucket: str) -> dict:  # noqa: N803 (boto3 kwarg name)
        return {"Status": self._status} if self._status else {}


def test_versioning_guard() -> None:
    assert versioning_enabled(_FakeS3("Enabled"), BUCKET) is True
    assert versioning_enabled(_FakeS3("Suspended"), BUCKET) is False
    assert versioning_enabled(_FakeS3(None), BUCKET) is False


def _write_l2(path: Path, rows: list[tuple[str, int, int, str]]) -> None:
    """Write a minimal layer-2 parquet: (path, size, mtime, kind)."""
    tbl = pa.table({
        "path": [r[0] for r in rows],
        "size": pa.array([r[1] for r in rows], pa.int64()),
        "mtime": pa.array([r[2] for r in rows], pa.int64()),
        "kind": [r[3] for r in rows],
    })
    pq.write_table(tbl, path)


def _read_manifest(path: Path) -> list[tuple]:
    return duckdb.connect().execute(
        f"SELECT name, size_bytes, mtime, dir FROM read_parquet('{path}') ORDER BY name"
    ).fetchall()


def test_build_manifest_deepest_wins(tmp_path: Path) -> None:
    l2 = tmp_path / "l2.parquet"
    _write_l2(l2, [
        (".", 999, 0, "dir"),                     # bucket root — excluded (not a file)
        ("marin/ckpt", 500, 0, "dir"),            # dir row — excluded
        ("marin/ckpt/a", 100, 111, "file"),       # swept
        ("marin/ckpt/sub/d", 200, 222, "file"),   # swept (deeper under sweep prefix)
        ("marin/ckpt/keep/b", 400, 333, "file"),  # carved out by deeper keep prefix
        ("marin/other/c", 800, 444, "file"),      # not under any sweep prefix
    ])
    plan = Plan(name="p", bucket=BUCKET, sweep=["marin/ckpt/"], keep=["marin/ckpt/keep/"], plan_id=3)
    out = tmp_path / "run"
    summary = build_manifest(str(l2), plan, str(out))

    assert _read_manifest(out / "manifest" / f"{BUCKET}.parquet") == [
        ("marin/ckpt/a", 100, 111, "marin/ckpt/"),
        ("marin/ckpt/sub/d", 200, 222, "marin/ckpt/sub/"),
    ]
    assert summary == {
        "plan_id": 3,
        "name": "p",
        "bucket": BUCKET,
        "sweep": ["marin/ckpt/"],
        "keep": ["marin/ckpt/keep/"],
        "objects": 2,
        "bytes": 300,
        "manifest": str(out / "manifest" / f"{BUCKET}.parquet"),
    }
    assert json.loads((out / "plan-summary.json").read_text()) == summary


def test_build_manifest_no_keep(tmp_path: Path) -> None:
    l2 = tmp_path / "l2.parquet"
    _write_l2(l2, [
        ("marin/ckpt/a", 100, 111, "file"),
        ("marin/other/c", 800, 444, "file"),
    ])
    plan = Plan(name="p", bucket=BUCKET, sweep=["marin/ckpt/"], plan_id=1)
    out = tmp_path / "run"
    summary = build_manifest(str(l2), plan, str(out))
    assert _read_manifest(out / "manifest" / f"{BUCKET}.parquet") == [
        ("marin/ckpt/a", 100, 111, "marin/ckpt/"),
    ]
    assert (summary["objects"], summary["bytes"], summary["keep"]) == (1, 100, [])
