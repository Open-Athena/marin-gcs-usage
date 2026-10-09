from collections.abc import Iterator
from json import dumps

import pytest

from dt_cloud.chstore.client import Ch, lit
from dt_cloud.chstore.query_census import frequency_census

from chserver import ch_db, ch_url  # noqa: F401


@pytest.fixture(scope="module")
def frequency_snapshots(ch_db: str, ch_url: str) -> Iterator[tuple[Ch, str, str, str]]:
    ch = Ch(ch_url, db=ch_db)
    before, after = ch_db + "_before", ch_db + "_after"
    ch.exec(f"CREATE DATABASE {before}")
    ch.exec(f"CREATE DATABASE {after}")
    try:
        for database in (before, after):
            ch.exec(f"CREATE TABLE {database}.nodes_by_name (nid UInt32,pre UInt32) ENGINE=MergeTree ORDER BY (nid,pre)")
        ch.exec(f"INSERT INTO {before}.nodes_by_name VALUES (11,0),(11,1),(11,2),(11,3),(11,4),(22,5),(22,6),(22,7),(33,8),(44,9)")
        ch.exec(f"INSERT INTO {after}.nodes_by_name VALUES (11,100),(11,101),(33,102),(55,103),(55,104),(55,105),(55,106)")
        ch.exec(f"CREATE TABLE {ch_db}.history_manifest (doc String) ENGINE=Memory")
        ch.exec(f"INSERT INTO {ch_db}.history_manifest VALUES ({lit(dumps({'dates': ['2026-10-04', '2026-10-05'], 'dbs': [before, after]}))})")
        yield ch, ch_db, before, after
    finally:
        ch.close()
        ch.exec(f"DROP DATABASE {after} SYNC")
        ch.exec(f"DROP DATABASE {before} SYNC")


@pytest.mark.parametrize("date,names,paths,largest,threshold_rows", [
    ("2026-10-04", 4, 10, 5, [
        {"paths_at_least": 1, "names": 4, "paths": 10},
        {"paths_at_least": 2, "names": 2, "paths": 8},
        {"paths_at_least": 3, "names": 2, "paths": 8},
        {"paths_at_least": 4, "names": 1, "paths": 5},
        {"paths_at_least": 5, "names": 1, "paths": 5},
        {"paths_at_least": 6, "names": 0, "paths": 0},
    ]),
    ("2026-10-05", 3, 7, 4, [
        {"paths_at_least": 1, "names": 3, "paths": 7},
        {"paths_at_least": 2, "names": 2, "paths": 6},
        {"paths_at_least": 3, "names": 1, "paths": 4},
        {"paths_at_least": 4, "names": 1, "paths": 4},
        {"paths_at_least": 5, "names": 0, "paths": 0},
        {"paths_at_least": 6, "names": 0, "paths": 0},
    ]),
])
def test_exact_frequency_histogram_uses_only_selected_snapshot(
    frequency_snapshots: tuple[Ch, str, str, str],
    date: str,
    names: int,
    paths: int,
    largest: int,
    threshold_rows: list[dict[str, int]],
) -> None:
    ch, target, before, after = frequency_snapshots
    body = frequency_census(ch, target, date, (1, 2, 3, 4, 5, 6))
    assert body | {"elapsed_s": "<elapsed>"} == {
        "scope": "exact snapshot basename frequency, not substring support or query latency",
        "date": date, "distinct_names": names, "paths": paths,
        "most_reused_name_paths": largest, "thresholds": threshold_rows,
        "elapsed_s": "<elapsed>", "stored_index_created": False,
    }
    assert body["elapsed_s"] >= 0
    assert ch._tmp == []
    assert ch.json(f"SELECT nid,pre FROM {before}.nodes_by_name ORDER BY nid,pre") == [
        [11, 0], [11, 1], [11, 2], [11, 3], [11, 4], [22, 5], [22, 6], [22, 7], [33, 8], [44, 9],
    ]
    assert ch.json(f"SELECT nid,pre FROM {after}.nodes_by_name ORDER BY nid,pre") == [
        [11, 100], [11, 101], [33, 102], [55, 103], [55, 104], [55, 105], [55, 106],
    ]
    assert ch.json(f"SELECT database,name FROM system.tables WHERE database IN ({lit(target)},{lit(before)},{lit(after)}) ORDER BY database,name") == [
        [target, "history_manifest"], [after, "nodes_by_name"], [before, "nodes_by_name"],
    ]


def test_threshold_order_and_repetitions_are_preserved(frequency_snapshots: tuple[Ch, str, str, str]) -> None:
    ch, target, _, _ = frequency_snapshots
    assert frequency_census(ch, target, "2026-10-04", (5, 1, 3, 5)) | {"elapsed_s": "<elapsed>"} == {
        "scope": "exact snapshot basename frequency, not substring support or query latency",
        "date": "2026-10-04", "distinct_names": 4, "paths": 10,
        "most_reused_name_paths": 5,
        "thresholds": [
            {"paths_at_least": 5, "names": 1, "paths": 5},
            {"paths_at_least": 1, "names": 4, "paths": 10},
            {"paths_at_least": 3, "names": 2, "paths": 8},
            {"paths_at_least": 5, "names": 1, "paths": 5},
        ],
        "elapsed_s": "<elapsed>", "stored_index_created": False,
    }


def test_ten_thresholds_are_accepted_at_the_budget_boundary(frequency_snapshots: tuple[Ch, str, str, str]) -> None:
    ch, target, _, _ = frequency_snapshots
    assert frequency_census(ch, target, "2026-10-05", (5,) * 10) | {"elapsed_s": "<elapsed>"} == {
        "scope": "exact snapshot basename frequency, not substring support or query latency",
        "date": "2026-10-05", "distinct_names": 3, "paths": 7,
        "most_reused_name_paths": 4,
        "thresholds": [{"paths_at_least": 5, "names": 0, "paths": 0}] * 10,
        "elapsed_s": "<elapsed>", "stored_index_created": False,
    }


@pytest.mark.parametrize("thresholds", [(), (0,), (-1,), (1, 0), (1, -2), tuple(range(1, 12))])
def test_invalid_thresholds_refuse_before_database_queries(thresholds: tuple[int, ...]) -> None:
    with pytest.raises(ValueError) as caught:
        frequency_census(None, "safe", "2026-10-05", thresholds)
    assert str(caught.value) == "one through ten positive thresholds required"
