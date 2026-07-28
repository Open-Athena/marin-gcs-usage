"""Tests for `list-bucket` output policies and row mapping (no network)."""

import datetime as dt
import json
from pathlib import Path

import fsspec
import pandas as pd
import pytest

from gcs_usage.bucket_list import SUCCESS_MARKER, entries_to_frame, resolve_existing

TS = dt.datetime(2026, 7, 28, tzinfo=dt.timezone.utc)


def test_entries_to_frame():
    frame = entries_to_frame("b1", [("x/a.bin", 5, TS, "STANDARD"), ("y/b.bin", 7, TS, "COLDLINE")])
    assert frame.to_dict("records") == [
        {"bucket": "b1", "name": "x/a.bin", "size_bytes": 5, "created": pd.Timestamp(TS), "storage_class_id": 1},
        {"bucket": "b1", "name": "y/b.bin", "size_bytes": 7, "created": pd.Timestamp(TS), "storage_class_id": 3},
    ]


@pytest.fixture
def out(tmp_path: Path):
    fs, root = fsspec.core.url_to_fs(str(tmp_path / "out"))
    fs.makedirs(root, exist_ok=True)
    return fs, root


def seed(fs, root: str, shards: int, marker: bool) -> None:
    for i in range(shards):
        fs.pipe(f"{root}/shard-{i}.parquet", b"x")
    if marker:
        fs.pipe(f"{root}/{SUCCESS_MARKER}", json.dumps({"objects": 42}).encode())


def listing_files(fs, root: str) -> list[str]:
    return sorted(p.rsplit("/", 1)[1] for p in fs.ls(root))


def test_empty_dir_proceeds(out):
    fs, root = out
    assert resolve_existing(fs, root, "error") is None


def test_error_policy_refuses_partial_and_complete(out):
    fs, root = out
    seed(fs, root, 2, marker=False)
    with pytest.raises(ValueError, match="partial run"):
        resolve_existing(fs, root, "error")
    seed(fs, root, 0, marker=True)
    with pytest.raises(ValueError, match="completion marker"):
        resolve_existing(fs, root, "error")


def test_reuse_returns_completed_payload_and_keeps_files(out):
    fs, root = out
    seed(fs, root, 2, marker=True)
    assert resolve_existing(fs, root, "reuse") == {"objects": 42}
    assert listing_files(fs, root) == [SUCCESS_MARKER, "shard-0.parquet", "shard-1.parquet"]


def test_reuse_clears_partial_run(out):
    fs, root = out
    seed(fs, root, 2, marker=False)
    assert resolve_existing(fs, root, "reuse") is None
    assert listing_files(fs, root) == []


def test_clear_removes_everything(out):
    fs, root = out
    seed(fs, root, 2, marker=True)
    assert resolve_existing(fs, root, "clear") is None
    assert listing_files(fs, root) == []


def test_dedupe_prefixes_drops_nested_and_dups():
    from gcs_usage.bucket_list import dedupe_prefixes

    kept, dropped = dedupe_prefixes(
        ["scratch/", "scratch/alice/", "scratch/bob/", "docs/x/", "docs/x/", "docsz/", "a/b/", "a/b/c/"]
    )
    assert kept == ["a/b/", "docs/x/", "docsz/", "scratch/"]
    assert dropped == [
        ("a/b/c/", "a/b/"),
        ("scratch/alice/", "scratch/"),
        ("scratch/bob/", "scratch/"),
    ]
