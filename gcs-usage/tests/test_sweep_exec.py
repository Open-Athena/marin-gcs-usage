"""Executor semantics against a stub GCS client (specs/sweep-executor.md §3)."""

from __future__ import annotations

import datetime as dt
import json
from contextlib import contextmanager
from dataclasses import dataclass, field

import pandas as pd
import pyarrow.parquet as pq

from gcs_usage.sweep_exec import execute_plan

T0 = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
T1 = dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc)


@dataclass
class FakeBlob:
    name: str
    size: int
    generation: int
    time_created: dt.datetime


@dataclass
class FakeBucketHandle:
    deletes: list = field(default_factory=list)

    def delete_blob(self, name, if_generation_match=None):
        self.deletes.append((name, if_generation_match))


@dataclass
class FakeClient:
    blobs: dict  # bucket -> [FakeBlob] (any order; listed sorted by name, recursively, like GCS)
    handle: FakeBucketHandle = field(default_factory=FakeBucketHandle)
    soft_days: int = 7
    listed: list = field(default_factory=list)  # prefixes asked for, in order

    def list_blobs(self, bucket, prefix=""):
        self.listed.append(prefix)
        return sorted((b for b in self.blobs.get(bucket, []) if b.name.startswith(prefix)), key=lambda b: b.name)

    def bucket(self, name):
        return self.handle

    def get_bucket(self, name):
        @dataclass
        class Pol:
            retention_duration_millis: int
        @dataclass
        class B:
            soft_delete_policy: Pol
        return B(Pol(self.soft_days * 86400 * 1000))

    @contextmanager
    def batch(self):
        yield


def _plan_dir(tmp_path):
    d = tmp_path / "plan"
    (d / "manifest").mkdir(parents=True)
    (d / "plan-summary.json").write_text(json.dumps({
        "date": "2026-09-01", "head": 7686,
        "buckets": {"b1": {"eligible": {"bytes": 60, "objects": 4}}},
    }))
    mf = pd.DataFrame([
        {"name": "a/x", "size_bytes": 10, "storage_class_id": 1, "created": T0, "dir": "a", "owner": "k", "sweepers": "k"},
        {"name": "a/y", "size_bytes": 20, "storage_class_id": 1, "created": T0, "dir": "a", "owner": "k", "sweepers": "k"},
        {"name": "a/z", "size_bytes": 30, "storage_class_id": 1, "created": T0, "dir": "a", "owner": "k", "sweepers": "k"},
        {"name": "b/w", "size_bytes": 40, "storage_class_id": 1, "created": T0, "dir": "b", "owner": "k", "sweepers": "k"},
    ])
    mf.to_parquet(d / "manifest" / "b1.parquet")
    return d


def _client():
    return FakeClient(blobs={"b1": [
        # a/x live+matching; a/y gone; a/z overwritten (created moved)
        FakeBlob("a/x", 10, 111, T0),
        FakeBlob("a/z", 33, 333, T1),
        # b/w live+matching, plus a NEW key → drift
        FakeBlob("b/w", 40, 444, T0),
        FakeBlob("b/new", 5, 555, T1),
    ]})


def _decisions(plan, mode):
    df = pq.read_table(f"{plan}/{mode}/b1.parquet").to_pandas()
    return sorted(map(tuple, df[["name", "decision", "generation"]].itertuples(index=False)))


def test_dry_run_decisions_and_drift_skip(tmp_path):
    plan = _plan_dir(tmp_path)
    client = _client()
    s = execute_plan(str(plan), client=client)
    assert client.handle.deletes == []  # dry-run touches nothing
    assert _decisions(plan, "would-delete") == [
        ("a/x", "delete", 111),
        ("a/y", "skipped_gone", 0),
        ("a/z", "skipped_overwritten", 333),
    ]
    b = s["buckets"]["b1"]
    assert b["decisions"] == {"delete": 1, "skipped_gone": 1, "skipped_overwritten": 1}
    assert b["delete_bytes"] == 10
    assert b["drift_dirs"] == [{"dir": "b", "new_objects": 1, "new_bytes": 5, "skipped_deletes": 1}]
    assert b["ledger_drift_dirs"] == []


def test_for_real_deletes_with_generation_match_and_drift_proceed(tmp_path):
    plan = _plan_dir(tmp_path)
    client = _client()
    s = execute_plan(str(plan), for_real=True, drift="proceed", client=client)
    assert sorted(client.handle.deletes) == [("a/x", 111), ("b/w", 444)]
    assert _decisions(plan, "deleted") == [
        ("a/x", "delete", 111),
        ("a/y", "skipped_gone", 0),
        ("a/z", "skipped_overwritten", 333),
        ("b/w", "delete", 444),
    ]
    assert s["buckets"]["b1"]["delete_bytes"] == 50
    assert s["buckets"]["b1"]["drift_dirs"] == [
        {"dir": "b", "new_objects": 1, "new_bytes": 5, "skipped_deletes": 0},
    ]


def test_for_real_refuses_without_soft_delete(tmp_path):
    import pytest
    plan = _plan_dir(tmp_path)
    client = _client()
    client.soft_days = 0
    with pytest.raises(SystemExit) as ei:
        execute_plan(str(plan), for_real=True, client=client)
    assert "soft delete retention 0d < required 7d" in str(ei.value)


def test_ledger_drift_reclassify_drops_dirs(tmp_path):
    plan = _plan_dir(tmp_path)
    client = _client()
    s = execute_plan(str(plan), client=client, reclassify=lambda b, dn, approved: "eligible" if dn == "b" else "conflict")
    assert s["buckets"]["b1"]["ledger_drift_dirs"] == ["a"]
    # only b was processed; it drifted (new key) → nothing would-delete
    assert s["buckets"]["b1"]["decisions"] == {}


def test_reclassify_receives_plan_approved_bands(tmp_path):
    # A plan built from approved bands must hand those bands to reclassify —
    # without them every band-approved dir reclassifies as deferred and the
    # whole plan silently no-ops as "ledger drift".
    plan = _plan_dir(tmp_path)
    summ = json.loads((plan / "plan-summary.json").read_text())
    summ["approved"] = ["gs://b1/a/"]
    (plan / "plan-summary.json").write_text(json.dumps(summ))
    client = _client()
    seen: list[tuple[str, str, tuple[str, ...]]] = []

    def reclassify(bucket, dn, approved):
        seen.append((bucket, dn, tuple(approved)))
        return "eligible"

    s = execute_plan(str(plan), client=client, reclassify=reclassify)
    assert sorted(seen) == [
        ("b1", "a", ("gs://b1/a/",)),
        ("b1", "b", ("gs://b1/a/",)),
    ]
    assert s["buckets"]["b1"]["ledger_drift_dirs"] == []
    assert s["buckets"]["b1"]["decisions"] == {"delete": 1, "skipped_gone": 1, "skipped_overwritten": 1}


def test_roots_are_band_children_and_prefix_free():
    from gcs_usage.sweep_exec import list_roots

    dirs = {"ckpt/r1/step-1", "ckpt/r1/step-2", "ckpt/r2", "scratch/k/a/b", "scratch/k", "raw/x/y"}
    # bands: ckpt/ (children r1, r2 become roots), scratch/k/ (eligible itself → one root), raw/x/y uncovered → its top segment
    roots = list_roots(dirs, ("gs://b1/ckpt/", "gs://b1/scratch/k/"), "b1")
    assert roots == ["ckpt/r1", "ckpt/r2", "raw", "scratch/k"]
    assert list_roots({""}, (), "b1") == [""]  # the bucket root itself: list everything
    assert list_roots({"a/b", "a"}, ("gs://b1/a/",), "b1") == ["a"]  # the band itself is eligible → swallows its children


def test_streamed_merge_buffers_nested_dirs_until_the_listing_passes_them(tmp_path):
    """Manifest dirs `a` and `a/c` under one root: `a`'s keys interleave with
    `a/c`'s in the listing; `a/c` gains a new key (drift → skipped) while `a`
    is clean and deletes; gone keys are found wherever the merge passes them."""
    d = tmp_path / "plan"
    (d / "manifest").mkdir(parents=True)
    (d / "plan-summary.json").write_text(json.dumps({
        "date": "2026-09-01", "head": 1, "approved": ["gs://b1/a/"],
        "buckets": {"b1": {"eligible": {"bytes": 1, "objects": 1}}},
    }))
    row = lambda name, dn, size=1: {"name": name, "size_bytes": size, "storage_class_id": 1, "created": T0, "dir": dn, "owner": None, "sweepers": "k"}
    pd.DataFrame([row("a/b.txt", "a"), row("a/c/q", "a/c"), row("a/c/r", "a/c"), row("a/d.txt", "a", 7), row("a/zz", "a")]).to_parquet(d / "manifest" / "b1.parquet")
    client = FakeClient(blobs={"b1": [
        FakeBlob("a/b.txt", 1, 11, T0),
        FakeBlob("a/c/new", 9, 99, T1),  # drift in a/c
        FakeBlob("a/c/q", 1, 12, T0),
        FakeBlob("a/c/r", 1, 13, T0),
        FakeBlob("a/d.txt", 7, 14, T0),
        FakeBlob("a/e/other", 3, 15, T1),  # a dir not in the manifest: ignored
    ]})
    s = execute_plan(str(d), for_real=True, client=client)
    assert client.listed == ["a/"]  # one recursive listing for the band
    assert sorted(client.handle.deletes) == [("a/b.txt", 11), ("a/d.txt", 14)]
    assert _decisions(d, "deleted") == [
        ("a/b.txt", "delete", 11),
        ("a/d.txt", "delete", 14),
        ("a/zz", "skipped_gone", 0),
    ]
    b = s["buckets"]["b1"]
    assert b["decisions"] == {"delete": 2, "skipped_gone": 1}
    assert b["delete_bytes"] == 8
    assert b["drift_dirs"] == [{"dir": "a/c", "new_objects": 1, "new_bytes": 9, "skipped_deletes": 2}]
    assert b["bands"] == {"gs://b1/a/": {"bytes": 8, "objects": 2, "gone": 1, "drift_new_objects": 1}}
