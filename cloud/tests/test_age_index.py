"""The per-path created-day age index (`dt_cloud.index.write_age_index`) and its
footer extraction (`index_footer` with no `usr` column) — specs/age-index.md."""
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud.index import AGE_INDEX, write_age_index
from dt_cloud.index_footer import extract, groups_blob

DAY = 86400


def _l2(tmp_path: Path, files: list[tuple[str, int, int, str]]) -> Path:
    """A synthetic layer-2 parquet (disk-tree import shape): rows of
    (path, size, mtime, kind)."""
    t = pa.table({
        "path": [f[0] for f in files],
        "size": [f[1] for f in files],
        "mtime": [f[2] for f in files],
        "kind": [f[3] for f in files],
        "depth": [f[0].count("/") + 1 for f in files],
    })
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / "l2.parquet"
    pq.write_table(t, p)
    return p


def _read(path: Path) -> list[tuple]:
    return duckdb.sql(
        f"SELECT path, depth, day, b, o FROM read_parquet('{path}') ORDER BY depth, path, day"
    ).fetchall()


def test_rollup_is_descendant_inclusive(tmp_path: Path):
    l2 = _l2(tmp_path, [
        ("marin/tmp/ttl=14d/a.bin", 100, 100 * DAY, "file"),
        ("marin/tmp/ttl=14d/b.bin", 200, 100 * DAY, "file"),  # same dir+day → summed
        ("marin/tmp/ttl=14d/c.bin", 50, 200 * DAY, "file"),   # same dir, later day
        ("marin/tmp/keep/d.bin", 10, 100 * DAY, "file"),
        ("root-obj.bin", 7, 200 * DAY, "file"),               # bucket-root direct child
        ("marin/tmp/ttl=14d", 0, 0, "dir"),                   # dir rows ignored
    ])
    con = duckdb.connect()
    write_age_index(con, [("BKT", str(l2))], tmp_path)
    assert _read(tmp_path / AGE_INDEX) == [
        # depth 0: the synthetic fleet root (all buckets summed per day)
        ("", 0, 100, 310, 3),
        ("", 0, 200, 57, 2),
        # depth 1: the bucket
        ("BKT", 1, 100, 310, 3),
        ("BKT", 1, 200, 57, 2),
        # depth 2: marin/
        ("BKT/marin", 2, 100, 310, 3),
        ("BKT/marin", 2, 200, 50, 1),
        # depth 3: marin/tmp
        ("BKT/marin/tmp", 3, 100, 310, 3),
        ("BKT/marin/tmp", 3, 200, 50, 1),
        # depth 4: the leaves
        ("BKT/marin/tmp/keep", 4, 100, 10, 1),
        ("BKT/marin/tmp/ttl=14d", 4, 100, 300, 2),
        ("BKT/marin/tmp/ttl=14d", 4, 200, 50, 1),
    ]


def test_floor_drops_small_prefixes_but_keeps_root(tmp_path: Path):
    # One big prefix (2 GiB) and one tiny one (100 B); the floor is the finest
    # coarse tier of the ~2 GiB fleet, so the tiny leaf drops and every big
    # ancestor (incl. the fleet root) stays.
    big = 2 * 1024**3
    l2 = _l2(tmp_path, [
        ("big/x.bin", big, 100 * DAY, "file"),
        ("tiny/y.bin", 100, 100 * DAY, "file"),
    ])
    con = duckdb.connect()
    s = write_age_index(con, [("BKT", str(l2))], tmp_path)
    paths = {r[0] for r in _read(tmp_path / AGE_INDEX)}
    assert s["floor"] > 100
    assert "BKT/tiny" not in paths          # below floor
    assert paths == {"", "BKT", "BKT/big"}  # root + bucket + the big leaf


def test_multi_bucket_root_sums_every_bucket(tmp_path: Path):
    la = _l2(tmp_path / "a", [("m/x.bin", 1000, 100 * DAY, "file")])
    lb = _l2(tmp_path / "b", [("m/y.bin", 2000, 100 * DAY, "file")])
    con = duckdb.connect()
    write_age_index(con, [("A", str(la)), ("B", str(lb))], tmp_path)
    rows = _read(tmp_path / AGE_INDEX)
    root = [r for r in rows if r[0] == ""]
    assert root == [("", 0, 100, 3000, 2)]  # 1000 (A) + 2000 (B), 2 objects


def test_footer_extract_without_usr_column(tmp_path: Path):
    l2 = _l2(tmp_path, [
        ("marin/a.bin", 1_000_000_000, 100 * DAY, "file"),
        ("marin/sub/c.bin", 500_000_000, 100 * DAY, "file"),
    ])
    con = duckdb.connect()
    s = write_age_index(con, [("BKT", str(l2))], tmp_path)
    schema, groups = extract(str(tmp_path / AGE_INDEX))
    assert [e["name"] for e in schema["schema"][1:]] == ["path", "depth", "day", "b", "o"]
    assert schema["floor_bytes"] == s["floor"]
    assert len(groups) == 1
    g = groups[0]
    assert (g["u_min"], g["u_max"]) == (None, None)  # no ownership column
    assert g["d_min"] == 0 and g["d_max"] == 3       # depths 0..3 present
    assert g["p_min"] == "" and g["p_max"] == "BKT/marin/sub"
    # the blob round-trips (u_min/u_max NULL slots included)
    assert isinstance(groups_blob(schema, groups), str)
