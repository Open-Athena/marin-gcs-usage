from collections.abc import Iterator
from json import dumps

import pytest

from dt_cloud.chstore.client import Ch, lit
from dt_cloud.chstore.query_census import pattern_frequency_census

from chserver import ch_db, ch_url  # noqa: F401


NAMES = [
    [11, "aaaa"], [22, "zarr.json"], [33, "cfg.json"], [44, "dead.txt"],
    [55, "5feceb66.json"], [66, "never.json"], [77, "a%b_"],
]
BEFORE_ROWS = [
    [11, 0], [11, 1], [11, 2], [11, 3], [11, 4],
    [22, 5], [22, 6], [22, 7], [33, 8], [44, 9], [77, 10], [77, 11],
]
AFTER_ROWS = [
    [11, 100], [11, 101], [33, 102],
    [55, 103], [55, 104], [55, 105], [55, 106], [77, 107],
]


@pytest.fixture(scope="module")
def pattern_snapshots(ch_db: str, ch_url: str) -> Iterator[tuple[Ch, str, str, str]]:
    ch = Ch(ch_url, db=ch_db)
    before, after = ch_db + "_before", ch_db + "_after"
    ch.exec(f"CREATE DATABASE {before}")
    ch.exec(f"CREATE DATABASE {after}")
    try:
        ch.exec(f"CREATE TABLE {ch_db}.names (nid UInt32,l String) ENGINE=MergeTree ORDER BY l")
        ch.exec(f"INSERT INTO {ch_db}.names VALUES " + ",".join(f"({nid},{lit(name)})" for nid, name in NAMES))
        for database, rows in ((before, BEFORE_ROWS), (after, AFTER_ROWS)):
            ch.exec(f"CREATE TABLE {database}.nodes_by_name (nid UInt32,pre UInt32) ENGINE=MergeTree ORDER BY (nid,pre)")
            ch.exec(f"INSERT INTO {database}.nodes_by_name VALUES " + ",".join(f"({nid},{pre})" for nid, pre in rows))
        ch.exec(f"CREATE TABLE {ch_db}.history_manifest (doc String) ENGINE=Memory")
        ch.exec(f"INSERT INTO {ch_db}.history_manifest VALUES ({lit(dumps({'dates': ['2026-10-04', '2026-10-05'], 'dbs': [before, after]}))})")
        yield ch, ch_db, before, after
    finally:
        ch.close()
        ch.exec(f"DROP DATABASE {after} SYNC")
        ch.exec(f"DROP DATABASE {before} SYNC")


@pytest.mark.parametrize("date,names,paths,expected_patterns", [
    ("2026-10-04", 5, 12, [
        {"pattern": "aa", "names": 1, "direct_matching_paths": 5},
        {"pattern": ".json", "names": 2, "direct_matching_paths": 4},
        {"pattern": "5fe", "names": 0, "direct_matching_paths": 0},
        {"pattern": "%", "names": 1, "direct_matching_paths": 2},
        {"pattern": "_", "names": 1, "direct_matching_paths": 2},
        {"pattern": "absent", "names": 0, "direct_matching_paths": 0},
        {"pattern": "never", "names": 0, "direct_matching_paths": 0},
    ]),
    ("2026-10-05", 4, 8, [
        {"pattern": "aa", "names": 1, "direct_matching_paths": 2},
        {"pattern": ".json", "names": 2, "direct_matching_paths": 5},
        {"pattern": "5fe", "names": 1, "direct_matching_paths": 4},
        {"pattern": "%", "names": 1, "direct_matching_paths": 1},
        {"pattern": "_", "names": 1, "direct_matching_paths": 1},
        {"pattern": "absent", "names": 0, "direct_matching_paths": 0},
        {"pattern": "never", "names": 0, "direct_matching_paths": 0},
    ]),
])
def test_selected_patterns_count_names_once_and_weight_by_snapshot_path_frequency(
    pattern_snapshots: tuple[Ch, str, str, str],
    date: str,
    names: int,
    paths: int,
    expected_patterns: list[dict],
) -> None:
    ch, target, before, after = pattern_snapshots
    body = pattern_frequency_census(ch, target, date, ("AA", ".JSON", "5fe", "%", "_", "absent", "never"))
    assert body | {"elapsed_s": "<elapsed>"} == {
        "scope": "exact selected name-substring frequencies; no inherited coverage or aggregate payload",
        "date": date, "distinct_names": names, "paths": paths,
        "patterns": expected_patterns, "elapsed_s": "<elapsed>", "stored_index_created": False,
    }
    assert body["elapsed_s"] >= 0
    assert ch._tmp == []
    assert ch.scalar(f"SELECT sorting_key FROM system.tables WHERE database={lit(target)} AND name='names'") == "l"
    assert ch.json(f"SELECT nid,l FROM {target}.names ORDER BY nid") == NAMES
    assert ch.json(f"SELECT nid,pre FROM {before}.nodes_by_name ORDER BY nid,pre") == BEFORE_ROWS
    assert ch.json(f"SELECT nid,pre FROM {after}.nodes_by_name ORDER BY nid,pre") == AFTER_ROWS
    assert ch.json(f"SELECT database,name FROM system.tables WHERE database IN ({lit(target)},{lit(before)},{lit(after)}) ORDER BY database,name") == [
        [target, "history_manifest"], [target, "names"], [after, "nodes_by_name"], [before, "nodes_by_name"],
    ]


def test_sixteen_patterns_are_accepted_and_normalized_without_deduplicating_requested_positions(pattern_snapshots: tuple[Ch, str, str, str]) -> None:
    ch, target, _, _ = pattern_snapshots
    assert pattern_frequency_census(ch, target, "2026-10-05", ("AA",) * 16) | {"elapsed_s": "<elapsed>"} == {
        "scope": "exact selected name-substring frequencies; no inherited coverage or aggregate payload",
        "date": "2026-10-05", "distinct_names": 4, "paths": 8,
        "patterns": [{"pattern": "aa", "names": 1, "direct_matching_paths": 2}] * 16,
        "elapsed_s": "<elapsed>", "stored_index_created": False,
    }


@pytest.mark.parametrize("patterns", [(), ("",), ("a/b",), ("/",), ("abcdefgh",), ("aa", ""), ("aa",) * 17])
def test_invalid_patterns_refuse_before_database_queries(patterns: tuple[str, ...]) -> None:
    with pytest.raises(ValueError) as caught:
        pattern_frequency_census(None, "safe", "2026-10-05", patterns)
    assert str(caught.value) == "one through sixteen name-only literals of 1..7 characters required"
