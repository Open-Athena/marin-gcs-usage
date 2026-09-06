"""Tests for the shared prefix-map load + deepest-prefix-wins lookup."""

import datetime as dt
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from gcs_usage.identity import load_identities
from gcs_usage.listing import prepare_listing
from gcs_usage.prefixes import deepest_lookup, load_prefix_map

IDENTITIES_YAML = """\
users:
  ryan-williams:
    aliases: [rw]
    team: infra
teams: [infra, data]
prefix_owners:
  - prefix: gs://b*/datasets/
    team: data
  - prefix: gs://b1/scratch/rw/
    user: ryan-williams
    team: infra
"""


@pytest.fixture
def identities(tmp_path: Path):
    path = tmp_path / "identities.yaml"
    path.write_text(IDENTITIES_YAML)
    return load_identities(path)


@pytest.fixture
def listing(tmp_path: Path) -> str:
    path = tmp_path / "listing.parquet"
    pd.DataFrame(
        {
            "bucket": ["b1", "b2", "c9"],
            "name": ["x/a.bin", "y/b.bin", "z/c.bin"],
            "size_bytes": [1, 1, 1],
        }
    ).to_parquet(path)
    return str(path)


@pytest.fixture
def attribution(tmp_path: Path) -> str:
    path = tmp_path / "attribution.parquet"
    pd.DataFrame(
        {
            "prefix": ["gs://b1/users/rw/", "gs://b1/datasets/finelog/"],
            "user": ["rw", None],
            "team": ["unknown", "data"],
            "source": ["user-prefix", "manual"],
            "asof": [dt.date(2026, 7, 20)] * 2,
        }
    ).to_parquet(path)
    return str(path)


def test_load_prefix_map(identities, listing: str, attribution: str):
    con = duckdb.connect()
    by_prefix = load_prefix_map(con, (attribution,), identities, prepare_listing(con, (listing,)))
    # parquet rows re-resolve raw users against current identities; wildcard
    # prefix_owners fan out over the listing's buckets (b* matches b1/b2, not c9)
    assert by_prefix == {
        "gs://b1/users/rw/": ("ryan-williams", "infra", "user-prefix"),
        "gs://b1/datasets/finelog/": (None, "data", "manual"),
        "gs://b1/datasets/": (None, "data", "manual"),
        "gs://b2/datasets/": (None, "data", "manual"),
        "gs://b1/scratch/rw/": ("ryan-williams", "infra", "manual"),
    }


def test_deepest_lookup_wins_and_misses(identities, listing: str, attribution: str):
    con = duckdb.connect()
    deepest = deepest_lookup(load_prefix_map(con, (attribution,), identities, prepare_listing(con, (listing,))))
    # deepest ancestor wins over shallower manual rows
    assert deepest("b1/datasets/finelog/part-0") == (None, "data", "manual")
    assert deepest("b1/datasets/other") == (None, "data", "manual")
    assert deepest("b1/users/rw/ckpt/step-1") == ("ryan-williams", "infra", "user-prefix")
    assert deepest("b1/unrelated/dir") is None
    assert deepest("c9/datasets/x") is None


GLOB_IDENTITIES_YAML = """\
users:
  calvin-xu:
    team: data
teams: [infra, data]
prefix_owners:
  - prefix: gs://b1/grug/swarm_*/
    user: calvin-xu
    team: data
"""


def test_path_glob_expands_against_listing(tmp_path: Path):
    """A `*` in the path part fans out over the listing's actual dirs at that
    depth (one segment per glob star — `swarm_*` must not swallow `moe_*` or
    reach deeper levels)."""
    ident_path = tmp_path / "identities.yaml"
    ident_path.write_text(GLOB_IDENTITIES_YAML)
    identities = load_identities(ident_path)
    listing = tmp_path / "glob-listing.parquet"
    pd.DataFrame(
        {
            "bucket": ["b1"] * 5 + ["b2"],
            "name": [
                "grug/swarm_fisher_000001-aa/ckpt.bin",
                "grug/swarm_fisher_000002-bb/opt/state.bin",
                "grug/moe_67b-cc/ckpt.bin",          # non-matching sibling
                "grug/swarm_deep/sub/x.bin",          # matches at level 2 only
                "other/swarm_fisher_000003-dd/x.bin", # wrong parent dir
                "grug/swarm_fisher_000004-ee/x.bin",  # wrong bucket
            ],
            "size_bytes": [1] * 6,
        }
    ).to_parquet(listing)
    con = duckdb.connect()
    by_prefix = load_prefix_map(con, (), identities, prepare_listing(con, (str(listing),)))
    assert by_prefix == {
        "gs://b1/grug/swarm_fisher_000001-aa/": ("calvin-xu", "data", "manual"),
        "gs://b1/grug/swarm_fisher_000002-bb/": ("calvin-xu", "data", "manual"),
        "gs://b1/grug/swarm_deep/": ("calvin-xu", "data", "manual"),
    }
    deepest = deepest_lookup(by_prefix)
    assert deepest("b1/grug/swarm_fisher_000002-bb/opt") == ("calvin-xu", "data", "manual")
    assert deepest("b1/grug/moe_67b-cc") is None
