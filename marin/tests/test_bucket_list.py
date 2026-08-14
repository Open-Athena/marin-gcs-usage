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


def test_pack_chunks_balances_by_weight():
    from gcs_usage.bucket_list import pack_chunks

    weights = {"huge/": 100, "big/": 40, "mid/": 30, "small1/": 5, "small2/": 5}
    chunks = pack_chunks(["huge/", "big/", "mid/", "small1/", "small2/", "unknown/"], weights, 3)
    totals = sorted(sum(weights.get(p, 1) for p in c) for c in chunks)
    assert totals == [40, 41, 100]
    assert sorted(p for c in chunks for p in c) == ["big/", "huge/", "mid/", "small1/", "small2/", "unknown/"]


@pytest.fixture
def weights_parquet(tmp_path: Path) -> str:
    names = [f"hot/{c}" for c in "abcdefgh"] + ["cold/x", "cold/y"]
    path = tmp_path / "weights.parquet"
    pd.DataFrame({"name": names}).to_parquet(path, index=False)
    return str(path)


def test_split_hot_prefixes_ranges(weights_parquet):
    from gcs_usage.bucket_list import split_hot_prefixes

    # total 10 over 4 streams → ideal 2; hot/ (8) splits into 4 ranges at the
    # 25/50/75% name-quantiles of the parquet; cold/ (2) stays whole
    items, weights = split_hot_prefixes(
        ["hot/", "cold/"], {"hot/": 8, "cold/": 2}, weights_parquet, n_streams=4,
    )
    assert items == [
        "cold/",
        ("hot/", None, "hot/b"),
        ("hot/", "hot/b", "hot/d"),
        ("hot/", "hot/d", "hot/f"),
        ("hot/", "hot/f", None),
    ]
    assert weights == {
        "hot/": 8,
        "cold/": 2,
        ("hot/", None, "hot/b"): 2,
        ("hot/", "hot/b", "hot/d"): 2,
        ("hot/", "hot/d", "hot/f"): 2,
        ("hot/", "hot/f", None): 2,
    }


def test_split_hot_prefixes_no_split_below_ideal(weights_parquet):
    from gcs_usage.bucket_list import split_hot_prefixes

    # ideal = 10 // 2 = 5; hot/ (8) → k = ceil(8/5) = 2 ranges; cold/ whole
    items, weights = split_hot_prefixes(
        ["hot/", "cold/"], {"hot/": 8, "cold/": 2}, weights_parquet, n_streams=2,
    )
    assert items == ["cold/", ("hot/", None, "hot/d"), ("hot/", "hot/d", None)]
    assert weights == {
        "hot/": 8,
        "cold/": 2,
        ("hot/", None, "hot/d"): 4,
        ("hot/", "hot/d", None): 4,
    }


def test_split_hot_prefixes_degenerate_bounds(tmp_path: Path):
    from gcs_usage.bucket_list import split_hot_prefixes

    # all names identical → duplicate quantile bounds → prefix kept whole
    path = tmp_path / "weights.parquet"
    pd.DataFrame({"name": ["hot/same"] * 8}).to_parquet(path, index=False)
    items, weights = split_hot_prefixes(["hot/"], {"hot/": 8}, str(path), n_streams=4)
    assert items == ["hot/"]
    assert weights == {"hot/": 8}


def test_pack_chunks_accepts_range_items():
    from gcs_usage.bucket_list import pack_chunks

    r1, r2 = ("hot/", None, "hot/d"), ("hot/", "hot/d", None)
    weights = {r1: 4, r2: 4, "cold/": 2}
    chunks = pack_chunks([r1, r2, "cold/"], weights, 2)
    # equal-weight tie broken by str(): "'hot/d'" sorts before "None"
    assert chunks == [[r2, "cold/"], [r1]]


class StubFS:
    """ls() from a dict of path -> entries; models gcsfs quirks seen in prod."""

    def __init__(self, listings):
        self.listings = listings

    def ls(self, path, detail=True):
        return self.listings[path]


def d(name):
    return {"name": name, "type": "directory", "size": 0, "timeCreated": None, "storageClass": "DIRECTORY"}


def f(name, size=1, created="2026-07-01T00:00:00.000Z", cls="STANDARD"):
    return {"name": name, "type": "file", "size": size, "timeCreated": created, "storageClass": cls}


def test_discover_prefixes_plain():
    from gcs_usage.bucket_list import discover_prefixes

    fs = StubFS({
        "bkt": [f("bkt/root.txt", 3), d("bkt/a")],
        "bkt/a": [f("bkt/a/x.txt", 5), d("bkt/a/b")],
    })
    assert discover_prefixes(fs, "bkt") == (
        [("root.txt", 3, "2026-07-01T00:00:00.000Z", "STANDARD"),
         ("a/x.txt", 5, "2026-07-01T00:00:00.000Z", "STANDARD")],
        ["a/b/"],
        [],
    )


def test_discover_prefixes_bucket_self_entry():
    # marin-us-east5 shape: ls(bucket) contains the bucket itself as a directory
    from gcs_usage.bucket_list import discover_prefixes

    fs = StubFS({
        "bkt": [d("bkt"), d("bkt/a")],
        "bkt/a": [d("bkt/a/b")],
    })
    assert discover_prefixes(fs, "bkt") == ([], ["a/b/"], ["/"])


def test_discover_prefixes_dir_self_entry():
    # marin-us-central2 shape: placeholder-backed dir appears inside its own listing
    from gcs_usage.bucket_list import discover_prefixes

    fs = StubFS({
        "bkt": [d("bkt/tmp")],
        "bkt/tmp": [d("bkt/tmp"), d("bkt/tmp/zephyr"), f("bkt/tmp/note.txt", 7)],
    })
    assert discover_prefixes(fs, "bkt") == (
        [("tmp/note.txt", 7, "2026-07-01T00:00:00.000Z", "STANDARD")],
        ["tmp/zephyr/"],
        ["tmp/"],
    )
