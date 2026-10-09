from collections.abc import Iterator
from dataclasses import replace
from json import dumps

import pytest

from dt_cloud.chstore.client import Ch, lit
from dt_cloud.chstore.coarse import CoarseRequest, NameIndex
from dt_cloud.chstore.coarse_pair import diff

from chserver import ch_db, ch_url  # noqa: F401


@pytest.fixture(scope="module")
def snapshots(ch_db: str, ch_url: str) -> Iterator[tuple[NameIndex, NameIndex]]:
    ch = Ch(ch_url, db=ch_db)
    after_db = ch_db + "_after"
    ch.exec(f"CREATE DATABASE {after_db}")
    geometry = [
        [(0, 13, 0, ""), (1, 2, 1, "gone"), (2, 2, 2, "gone/zarr.json"),
         (3, 6, 1, "hold"), (4, 4, 2, "hold/zarr.json"),
         (5, 6, 2, "hold/nest"), (6, 6, 3, "hold/nest/zarr.json"),
         (7, 9, 1, "small"), (8, 8, 2, "small/zarr.json"), (9, 9, 2, "small/noise.txt"),
         (10, 11, 1, "zero"), (11, 11, 2, "zero/zarr.json"),
         (12, 13, 1, "off"), (13, 13, 2, "off/zarr.json")],
        [(2048, 2063, 0, ""), (2049, 2050, 1, "new"), (2050, 2050, 2, "new/zarr.json"),
         (2051, 2054, 1, "hold"), (2052, 2052, 2, "hold/zarr.json"),
         (2053, 2054, 2, "hold/nest"), (2054, 2054, 3, "hold/nest/zarr.json"),
         (2055, 2057, 1, "small"), (2056, 2056, 2, "small/zarr.json"), (2057, 2057, 2, "small/noise.txt"),
         (2058, 2059, 1, "zero"), (2059, 2059, 2, "zero/zarr.json"),
         (2060, 2061, 1, "off"), (2061, 2061, 2, "off/zarr.json"),
         (2062, 2063, 1, "ghost"), (2063, 2063, 2, "ghost/zarr.json")],
    ]
    leaves = [
        {2: (100, 2), 4: (40, 1), 6: (0, 3), 8: (8, 1), 11: (0, 1), 13: (20, 1)},
        {2050: (100, 2), 2052: (25, 1), 2054: (0, 3), 2056: (8, 1), 2059: (0, 4), 2061: (35, 1)},
    ]
    try:
        for db, day, rows, values, nid in zip((ch_db, after_db), ("2026-10-04", "2026-10-05"), geometry, leaves, (11, 71)):
            ch.exec(f"CREATE TABLE {db}.dictionary (pre UInt32, post UInt32, depth UInt8, path String) ENGINE = Memory")
            ch.exec(f"INSERT INTO {db}.dictionary VALUES " + ",".join(f"({pre},{post},{depth},{lit(path)})" for pre, post, depth, path in rows))
            ch.exec(f"CREATE TABLE {db}.nodes (pre UInt32, post UInt32, path String, nid UInt32, b UInt64, o UInt64) ENGINE = Memory")
            payload = []
            for pre, post, _, path in rows:
                if path == "ghost" or path.startswith("ghost/"):
                    continue
                b = sum(value[0] for leaf, value in values.items() if pre <= leaf <= post)
                o = sum(value[1] for leaf, value in values.items() if pre <= leaf <= post)
                payload.append(f"({pre},{post},{lit(path)},{nid if pre in values else 0},{b},{o})")
            ch.exec(f"INSERT INTO {db}.nodes VALUES " + ",".join(payload))
            ch.exec(f"CREATE VIEW {db}.nodes_by_name AS SELECT * FROM {db}.nodes")
            ch.exec(f"CREATE TABLE {db}.names (nid UInt32,l String) ENGINE = Memory")
            ch.exec(f"INSERT INTO {db}.names VALUES ({nid},'zarr.json')")
            ch.exec(f"CREATE TABLE {db}.history_manifest (doc String) ENGINE = Memory")
            ch.exec(f"INSERT INTO {db}.history_manifest VALUES ({lit(dumps({'dates': [day], 'dbs': [db], 'prefix': ''}))})")
        yield (NameIndex.build(ch, ch_db, "2026-10-04", "zarr.json", 1024),
               NameIndex.build(ch, after_db, "2026-10-05", "zarr.json", 1024))
    finally:
        ch.exec(f"DROP DATABASE {after_db} SYNC")
        ch.close()


def complete_partition(
    ch: Ch,
    index: NameIndex,
    path: str,
) -> tuple[dict, dict[str, dict]]:
    """Fixture-only full path scan; independent of ranks/geometry/summaries."""
    rows = ch.json(f"SELECT path,b,o FROM {index.db}.nodes_by_name WHERE nid = {index.nid} ORDER BY path")
    selected = [(p, b, o) for p, b, o in rows if p == path or not path or p.startswith(path + "/")]
    total = {"b": sum(b for _, b, _ in selected), "o": sum(o for _, _, o in selected), "matches": len(selected)}
    children = {}
    for p, b, o in selected:
        if p == path:
            continue
        child = (path + "/" if path else "") + p[len(path) + (1 if path else 0):].split("/")[0]
        value = children.setdefault(child, {"b": 0, "o": 0, "matches": 0})
        value["b"] += b
        value["o"] += o
        value["matches"] += 1
    return total, children


def assert_complete_pair(
    ch: Ch,
    indexes: tuple[NameIndex, NameIndex],
    body: dict,
) -> None:
    partitions = [complete_partition(ch, index, body["path"]) for index in indexes]
    threshold = max(1, (max(total["b"] for total, _ in partitions) + body["child_budget"] - 1) // body["child_budget"])
    paths = sorted({p for _, children in partitions for p, value in children.items() if value["b"] >= threshold},
                   key=lambda p: (-max(children.get(p, {}).get("b", 0) for _, children in partitions), p))
    assert body["threshold_bytes"] == threshold
    for side, (total, children) in zip(("before", "after"), partitions):
        tree = body[side]["tree"]
        assert {key: tree[key] for key in total} == total
        assert [(c["path"], {key: c[key] for key in total}) for c in tree["children"]] == [
            (p, children.get(p, {"b": 0, "o": 0, "matches": 0})) for p in paths
        ]
        expected_other = {key: total[key] - sum(children.get(p, {}).get(key, 0) for p in paths) for key in total}
        if tree["leaf"]:
            expected_other = dict.fromkeys(total, 0)
        assert tree["other"] == expected_other
    assert body["delta"] == {key: partitions[1][0][key] - partitions[0][0][key] for key in partitions[0][0]}


def test_independent_ids_preserve_deleted_added_and_offsetting_children(snapshots: tuple[NameIndex, NameIndex], ch_url: str) -> None:
    ch = Ch(ch_url)
    try:
        body = diff(ch, *snapshots, "", 6)
        assert (body["schema"], body["threshold_bytes"], body["delta"]) == (
            "coarse-pair-v1", 28, {"b": 0, "o": 3, "matches": 0},
        )
        assert [[(c["path"], c["pre"], c["b"], c["o"], c["matches"], c["present"]) for c in body[side]["tree"]["children"]]
                for side in ("before", "after")] == [
            [("gone", 1, 100, 2, 1, True), ("new", None, 0, 0, 0, False),
             ("hold", 3, 40, 4, 2, True), ("off", 12, 20, 1, 1, True)],
            [("gone", None, 0, 0, 0, False), ("new", 2049, 100, 2, 1, True),
             ("hold", 2051, 25, 4, 2, True), ("off", 2060, 35, 1, 1, True)],
        ]
        assert [body[side]["tree"]["other"] for side in ("before", "after")] == [
            {"b": 8, "o": 2, "matches": 2}, {"b": 8, "o": 5, "matches": 2},
        ]
        assert_complete_pair(ch, snapshots, body)
    finally:
        ch.close()


@pytest.mark.parametrize("path", ["", "gone", "new", "hold", "small", "zero", "zero/zarr.json", "gone/zarr.json"])
@pytest.mark.parametrize("budget", [1, 2, 6, 128])
def test_every_partition_matches_complete_independent_path_oracles(
    snapshots: tuple[NameIndex, NameIndex],
    ch_url: str,
    path: str,
    budget: int,
) -> None:
    ch = Ch(ch_url)
    try:
        body = diff(ch, *snapshots, path, budget)
        assert_complete_pair(ch, snapshots, body)
        assert len(body["before"]["tree"]["children"]) <= 2 * budget
        assert [c["path"] for c in body["before"]["tree"]["children"]] == [c["path"] for c in body["after"]["tree"]["children"]]
    finally:
        ch.close()


def test_zero_byte_leaf_retains_counts_without_folding_itself(snapshots: tuple[NameIndex, NameIndex], ch_url: str) -> None:
    ch = Ch(ch_url)
    try:
        body = diff(ch, *snapshots, "zero/zarr.json", 2)
        assert body["delta"] == {"b": 0, "o": 3, "matches": 0}
        assert [body[side]["tree"] for side in ("before", "after")] == [
            {"pre": 11, "path": "zero/zarr.json", "label": "zarr.json", "b": 0, "o": 1,
             "matches": 1, "leaf": True, "children": [], "other": {"b": 0, "o": 0, "matches": 0}},
            {"pre": 2059, "path": "zero/zarr.json", "label": "zarr.json", "b": 0, "o": 4,
             "matches": 1, "leaf": True, "children": [], "other": {"b": 0, "o": 0, "matches": 0}},
        ]
    finally:
        ch.close()


@pytest.mark.parametrize("path", ["missing", "ghost"])
def test_neither_snapshot_present_refuses_including_dictionary_only_paths(
    snapshots: tuple[NameIndex, NameIndex],
    ch_url: str,
    path: str,
) -> None:
    ch = Ch(ch_url)
    try:
        with pytest.raises(CoarseRequest) as caught:
            diff(ch, *snapshots, path, 2)
        assert str(caught.value) == "path not present at either selected scan"
    finally:
        ch.close()


def test_pattern_name_tables_rebind_independent_name_ids(snapshots: tuple[NameIndex, NameIndex], ch_url: str) -> None:
    ch = Ch(ch_url)
    try:
        indexes = tuple(NameIndex.build_pattern(ch, index.target, index.date, ".json", block_rows=1024) for index in snapshots)
        assert [(index.name_table, list(index.name_ids)) for index in indexes] == [
            ("coarse_suffix_names_0", [11]), ("coarse_suffix_names_0", [71]),
        ]
        body = diff(ch, *indexes, "", 6)
        assert_complete_pair(ch, snapshots, body)
    finally:
        ch.close()


@pytest.mark.parametrize("budget", [0, 129])
def test_invalid_budget_refuses_before_database_reads(budget: int) -> None:
    index = NameIndex("fixture", "2026-10-04", "fixture", 1, "zarr.json", "", None, 1024, 0)
    with pytest.raises(CoarseRequest) as caught:
        diff(None, index, index, "", budget)
    assert str(caught.value) == "diff budget must be from 1 to 128 (at most twice that many children)"


@pytest.mark.parametrize("changed", [{"name": "different"}, {"root": "bucket"}, {"predicate_mode": "suffix"}])
def test_incompatible_predicates_refuse_before_database_reads(changed: dict) -> None:
    index = NameIndex("fixture", "2026-10-04", "fixture", 1, "zarr.json", "", None, 1024, 0)
    with pytest.raises(ValueError) as caught:
        diff(None, index, replace(index, **changed), "", 2)
    assert str(caught.value) == "independent coarse diff requires one root scope and predicate"
