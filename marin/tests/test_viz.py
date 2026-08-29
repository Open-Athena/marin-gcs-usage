"""End-to-end test of `write_webdata` on a tiny synthetic listing.

The tree is built at arbitrary depth (specs/tree-builder-unification.md): dirs
are linked parent→child with no d1..d4 cap, and a directory's own direct files
surface as an expandable `(other)` node (bytes the kept children don't account
for), not a `(files)` leaf. The fold floor is relative (`MIN_FRAC` × total); at
this fixture's scale nothing folds, so the tree is asserted exactly.
"""

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest

from gcs_usage.viz import write_webdata

IDENTITIES_YAML = """\
users:
  ryan-williams:
    aliases: [rw]
    team: infra
teams: [infra, data]
prefix_owners:
  - prefix: gs://b1/datasets/
    team: data
"""

GB = 10**9

TS = {
    "d0615": dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc),
    "d0701": dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc),
    "d0702": dt.datetime(2026, 7, 2, tzinfo=dt.timezone.utc),
    "d0703": dt.datetime(2026, 7, 3, tzinfo=dt.timezone.utc),
}

epoch_day = lambda ts: int(ts.timestamp() // 86400)  # noqa: E731

def wmean_day(*pairs: tuple[int, dt.datetime]) -> int:
    """Bytes-weighted mean created date in epoch days, mirroring `_date_of`."""
    return int(sum(b * ts.timestamp() for b, ts in pairs) / sum(b for b, _ in pairs) / 86400)


@pytest.fixture
def listing(tmp_path: Path) -> str:
    path = tmp_path / "listing.parquet"
    pd.DataFrame(
        {
            "bucket": ["b1"] * 4,
            "name": [
                "users/rw/ckpt/model.bin",
                "users/rw/ckpt/opt.bin",
                "datasets/raw/part0",
                "top.bin",
            ],
            "size_bytes": [100 * GB, 50 * GB, 200 * GB, 30 * GB],
            "created": [TS["d0701"], TS["d0703"], TS["d0615"], TS["d0702"]],
            "storage_class_id": [1, 1, 2, 1],
        }
    ).to_parquet(path)
    return str(path)


@pytest.fixture
def attribution(tmp_path: Path) -> str:
    path = tmp_path / "attribution.parquet"
    pd.DataFrame(
        {
            "prefix": ["gs://b1/users/rw/"],
            "user": ["rw"],
            "team": ["unknown"],
            "source": ["user-prefix"],
            "asof": [dt.date(2026, 7, 20)],
        }
    ).to_parquet(path)
    return str(path)


def test_write_webdata_attr(tmp_path: Path, listing: str, attribution: str):
    identities_path = tmp_path / "identities.yaml"
    identities_path.write_text(IDENTITIES_YAML)
    out = tmp_path / "out"

    meta = write_webdata((listing,), out, "2026-07-20", (attribution,), identities_path)

    assert meta == {
        "asof": "2026-07-20",
        "generated": dt.date.today().isoformat(),
        "total_bytes": 380 * GB,
        "total_objects": 4,
        "class_bytes": {1: 180 * GB, 2: 200 * GB},
        "fold_min_frac": 0.0002,
        "users": [{"u": "ryan-williams", "t": "infra", "b": 150 * GB}],
        "team_class_bytes": {
            "data": {2: 200 * GB},
            "infra": {1: 150 * GB},
            "unattributed": {1: 30 * GB},
        },
        "user_class_bytes": {"ryan-williams": {1: 150 * GB}},
    }

    tree = json.loads((out / "tree.json").read_text())
    ckpt = {
        "n": "ckpt",
        "b": 150 * GB,
        "o": 2,
        "d": wmean_day((100 * GB, TS["d0701"]), (50 * GB, TS["d0703"])),
        "tm": {"infra": 150 * GB},
        "us": [["ryan-williams", 150 * GB]],
    }
    rw = {**ckpt, "n": "rw", "c": [ckpt]}
    users = {**ckpt, "n": "users", "c": [rw]}
    datasets = {
        "n": "datasets",
        "b": 200 * GB,
        "o": 1,
        "d": epoch_day(TS["d0615"]),
        "cb": {"2": 200 * GB},
        "tm": {"data": 200 * GB},
        "sh": {"data": 200 * GB},
        "c": [
            {"n": "raw", "b": 200 * GB, "o": 1, "d": epoch_day(TS["d0615"]), "cb": {"2": 200 * GB}, "tm": {"data": 200 * GB}, "sh": {"data": 200 * GB}},
        ],
    }
    # top.bin is a direct file of b1: bytes the kept children (datasets, users)
    # don't cover, surfaced as an expandable (other) with its subtracted
    # attribution. f=0: no sub-floor dirs folded in, only the direct file.
    other = {"n": "(other)", "b": 30 * GB, "o": 1, "f": 0, "d": epoch_day(TS["d0702"]), "tm": {"unattributed": 30 * GB}}
    b1 = {
        "n": "b1",
        "b": 380 * GB,
        "o": 4,
        "d": wmean_day(*zip([100 * GB, 50 * GB, 200 * GB, 30 * GB], [TS["d0701"], TS["d0703"], TS["d0615"], TS["d0702"]], strict=True)),
        "cb": {"2": 200 * GB},
        "tm": {"data": 200 * GB, "infra": 150 * GB, "unattributed": 30 * GB},
        "sh": {"data": 200 * GB},
        "us": [["ryan-williams", 150 * GB]],
        "c": [datasets, users, other],
    }
    assert tree == {
        "n": "marin GCS",
        "b": 380 * GB,
        "o": 4,
        "d": b1["d"],
        "cb": {"2": 200 * GB},
        "tm": {"data": 200 * GB, "infra": 150 * GB, "unattributed": 30 * GB},
        "sh": {"data": 200 * GB},
        "us": [["ryan-williams", 150 * GB]],
        "c": [b1],
    }

    age = json.loads((out / "age.json").read_text())
    assert sorted(age, key=lambda r: (r["d"], r["d1"])) == [
        {"d": epoch_day(TS["d0615"]), "d1": "datasets", "t": "data", "b": 200 * GB, "o": 1},
        {"d": epoch_day(TS["d0701"]), "d1": "users", "t": "infra", "u": "ryan-williams", "b": 100 * GB, "o": 1},
        {"d": epoch_day(TS["d0702"]), "d1": "(files)", "t": "unattributed", "b": 30 * GB, "o": 1},
        {"d": epoch_day(TS["d0703"]), "d1": "users", "t": "infra", "u": "ryan-williams", "b": 50 * GB, "o": 1},
    ]


@pytest.fixture
def access(tmp_path: Path) -> str:
    """Layer-2a access agg: one read prefix (plus its ancestor rollup row)."""
    path = tmp_path / "access.parquet"
    pd.DataFrame(
        {
            "bucket": ["b1", "b1"],
            "path": ["users/rw/ckpt", "users"],
            "op": ["GET", "GET"],
            "last_ts": [TS["d0703"], TS["d0703"]],
            "n_ops": [3, 3],
            "bytes_out": [1000, 1000],
        }
    ).to_parquet(path)
    return str(path)


def test_age_rows_carry_last_read(tmp_path: Path, listing: str, attribution: str, access: str):
    identities_path = tmp_path / "identities.yaml"
    identities_path.write_text(IDENTITIES_YAML)
    out = tmp_path / "out"
    meta = write_webdata((listing,), out, "2026-07-20", (attribution,), identities_path, access=(access,))
    rd = epoch_day(TS["d0703"])
    assert meta["access"] == {"from": rd, "to": rd}
    # Strata whose dir was read carry `a` (its last-read epoch day); the rest
    # omit it — the site's read axis colors those "never read".
    age = json.loads((out / "age.json").read_text())
    assert sorted(age, key=lambda r: (r["d"], r["d1"])) == [
        {"d": epoch_day(TS["d0615"]), "d1": "datasets", "t": "data", "b": 200 * GB, "o": 1},
        {"d": epoch_day(TS["d0701"]), "d1": "users", "t": "infra", "u": "ryan-williams", "a": rd, "b": 100 * GB, "o": 1},
        {"d": epoch_day(TS["d0702"]), "d1": "(files)", "t": "unattributed", "b": 30 * GB, "o": 1},
        {"d": epoch_day(TS["d0703"]), "d1": "users", "t": "infra", "u": "ryan-williams", "a": rd, "b": 50 * GB, "o": 1},
    ]


def test_path_index_carries_read_day(tmp_path: Path, listing: str, attribution: str, access: str):
    """The floor-free path index carries subtree-MAX `a` (last-read epoch day):
    a read on `users/rw/ckpt` lights up every ancestor up to the bucket, while
    a never-read sibling (`datasets`) stays NULL."""
    identities_path = tmp_path / "identities.yaml"
    identities_path.write_text(IDENTITIES_YAML)
    out = tmp_path / "out"
    pidx = tmp_path / "path-index.parquet"
    write_webdata((listing,), out, "2026-07-20", (attribution,), identities_path, access=(access,), path_index=pidx)
    rd = epoch_day(TS["d0703"])
    df = pd.read_parquet(pidx)
    assert list(df.columns) == ["path", "depth", "team", "usr", "b", "o", "wts", "wb", "c2", "c3", "c4", "a"]
    # `a` is per (path) — collapse the team/usr slices to the path's max.
    a_by_path = df.groupby("path")["a"].max().to_dict()
    assert a_by_path["b1"] == rd            # bucket: max over everything under it
    assert a_by_path["b1/users"] == rd      # read subtree
    assert a_by_path["b1/users/rw/ckpt"] == rd
    assert pd.isna(a_by_path["b1/datasets"])  # never read → NULL


def test_write_webdata_plain(tmp_path: Path, listing: str):
    out = tmp_path / "out"
    meta = write_webdata((listing,), out, "2026-07-20")
    assert "users" not in meta
    assert meta["total_bytes"] == 380 * GB
    age = json.loads((out / "age.json").read_text())
    assert sorted(age, key=lambda r: (r["d"], r["d1"])) == [
        {"d": epoch_day(TS["d0615"]), "d1": "datasets", "b": 200 * GB, "o": 1},
        {"d": epoch_day(TS["d0701"]), "d1": "users", "b": 100 * GB, "o": 1},
        {"d": epoch_day(TS["d0702"]), "d1": "(files)", "b": 30 * GB, "o": 1},
        {"d": epoch_day(TS["d0703"]), "d1": "users", "b": 50 * GB, "o": 1},
    ]


def test_deeper_team_only_rule_overrides_user_prefix(tmp_path: Path):
    # A team-only prefix nested INSIDE a user prefix must win for its subtree
    # (deepest-prefix-wins is row-wise: the deeper row's NULL user must not be
    # skipped in favor of the shallower row's user).
    identities_path = tmp_path / "identities.yaml"
    identities_path.write_text(
        """\
users:
  ryan-williams:
    aliases: [rw]
    team: infra
teams: [infra, data]
prefix_owners:
  - prefix: gs://b1/users/rw/shared/
    team: data
"""
    )
    listing_path = tmp_path / "listing.parquet"
    pd.DataFrame(
        {
            "bucket": ["b1", "b1"],
            "name": ["users/rw/own/a.bin", "users/rw/shared/b.bin"],
            "size_bytes": [100 * GB, 60 * GB],
            "created": [TS["d0701"], TS["d0702"]],
            "storage_class_id": [1, 1],
        }
    ).to_parquet(listing_path)
    attribution_path = tmp_path / "attribution.parquet"
    pd.DataFrame(
        {
            "prefix": ["gs://b1/users/rw/"],
            "user": ["rw"],
            "team": ["unknown"],
            "source": ["user-prefix"],
            "asof": [dt.date(2026, 7, 20)],
        }
    ).to_parquet(attribution_path)
    out = tmp_path / "out"
    write_webdata((str(listing_path),), out, "2026-07-28", (str(attribution_path),), identities_path)
    tree = json.loads((out / "tree.json").read_text())
    b1 = tree["c"][0]
    assert b1["tm"] == {"infra": 100 * GB, "data": 60 * GB}
    assert b1["sh"] == {"data": 60 * GB}
    assert b1["us"] == [["ryan-williams", 100 * GB]]


def test_dir_cache_roundtrip(tmp_path: Path, listing: str, attribution: str):
    """Cold run writes the layer-2 cache; a warm run (same cache dir, listing
    unreadable to prove it isn't touched) produces byte-identical outputs."""
    identities_path = tmp_path / "identities.yaml"
    identities_path.write_text(IDENTITIES_YAML)
    cache = tmp_path / "dir-cache"

    cold_out = tmp_path / "cold"
    write_webdata((listing,), cold_out, "2026-07-20", (attribution,), identities_path, dir_cache=cache)
    assert sorted(p.name for p in cache.iterdir()) == ["age-days.parquet", "dir-stats.parquet"]

    # Warm: point the listing at a copy that we then corrupt — object rows must
    # not be read. (The listing arg is still parsed for schema, so keep a valid
    # parquet with different contents: one giant bogus row.)
    bogus = tmp_path / "bogus-listing.parquet"
    pd.DataFrame(
        {
            "bucket": ["b1"],
            "name": ["SHOULD_NOT_BE_READ"],
            "size_bytes": [1],
            "created": [TS["d0701"]],
            "storage_class_id": [1],
        }
    ).to_parquet(bogus)
    warm_out = tmp_path / "warm"
    write_webdata((str(bogus),), warm_out, "2026-07-20", (attribution,), identities_path, dir_cache=cache)

    for name in ("tree.json", "age.json", "meta.json"):
        assert (warm_out / name).read_bytes() == (cold_out / name).read_bytes()
    tree = json.loads((warm_out / "tree.json").read_text())
    assert tree["b"] == 380 * GB  # cache content won, bogus listing ignored
