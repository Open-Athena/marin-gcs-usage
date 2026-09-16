"""End-to-end test of `write_webdata` on a tiny synthetic listing.

Sizes sit above FOLD_MIN_BYTES so no "(other ×N)" folding kicks in and the
tree can be asserted exactly.
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
    files = {"n": "(files)", "b": 30 * GB, "o": 1, "d": epoch_day(TS["d0702"]), "tm": {"unattributed": 30 * GB}}
    b1 = {
        "n": "b1",
        "b": 380 * GB,
        "o": 4,
        "d": wmean_day(*zip([100 * GB, 50 * GB, 200 * GB, 30 * GB], [TS["d0701"], TS["d0703"], TS["d0615"], TS["d0702"]], strict=True)),
        "cb": {"2": 200 * GB},
        "tm": {"data": 200 * GB, "infra": 150 * GB, "unattributed": 30 * GB},
        "sh": {"data": 200 * GB},
        "us": [["ryan-williams", 150 * GB]],
        "c": [datasets, users, files],
    }
    assert tree == {
        "n": "marin GCS",
        "b": 380 * GB,
        "o": 4,
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
