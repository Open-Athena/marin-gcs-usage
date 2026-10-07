from collections import Counter
from collections.abc import Iterator
from json import dumps

import pytest

from dt_cloud.chstore.client import Ch, lit
from dt_cloud.chstore.hot_frequency import census

from chserver import ch_db, ch_url  # noqa: F401


NAMES = {0: "", 11: "AAAA", 22: "zarr.json", 33: "cfg.json", 44: "ab", 55: "cd", 66: "x🙂x", 77: "deadbeef0.npy", 88: "never.json"}
FREQUENCIES = {"2026-10-04": {0: 1, 11: 5, 22: 3, 33: 1, 44: 2, 55: 1, 66: 2, 77: 1},
               "2026-10-05": {0: 1, 11: 1, 33: 2, 66: 1, 88: 3}}
PATTERNS = ("AA", ".JSON", ".npy", "🙂", "x🙂", "X", "bc", "deadbee", "never", "AA")


@pytest.fixture(scope="module")
def snapshots(ch_db: str, ch_url: str) -> Iterator[tuple[str, dict[str, str]]]:
    ch = Ch(ch_url, db=ch_db)
    databases = {"2026-10-04": ch_db + "_before", "2026-10-05": ch_db + "_after"}
    try:
        ch.exec("CREATE TABLE names (nid UInt32,l String) ENGINE=MergeTree ORDER BY nid")
        ch.exec("INSERT INTO names VALUES " + ",".join(f"({nid},{lit(name)})" for nid, name in NAMES.items()))
        for date, database in databases.items():
            ch.exec(f"CREATE DATABASE {database}")
            ch.exec(f"CREATE TABLE {database}.nodes_by_name (nid UInt32,pre UInt32) ENGINE=MergeTree ORDER BY (nid,pre)")
            rows = [(nid, pre) for pre, nid in enumerate(nid for nid, count in FREQUENCIES[date].items() for _ in range(count))]
            ch.exec(f"INSERT INTO {database}.nodes_by_name VALUES " + ",".join(f"({nid},{pre})" for nid, pre in rows))
        ch.exec("CREATE TABLE history_manifest (doc String) ENGINE=Memory")
        ch.exec("INSERT INTO history_manifest VALUES (" + lit(dumps({"prefix": "", "dates": list(databases), "dbs": list(databases.values())})) + ")")
        yield ch_db, databases
    finally:
        ch.close()
        for database in reversed(list(databases.values())):
            ch.exec(f"DROP DATABASE IF EXISTS {database} SYNC")


def weighted_substrings(date: str, chars: int) -> dict[str, int]:
    counts = Counter()
    for nid, weight in FREQUENCIES[date].items():
        name = NAMES[nid].lower()
        grams = {name[i:i + chars] for i in range(max(0, len(name) - chars + 1))}
        counts.update({gram: weight for gram in grams})
    return dict(counts)


@pytest.mark.parametrize("threshold", [1, 3, 6, 17])
@pytest.mark.parametrize("max_chars", [1, 3, 7])
def test_complete_hot_layers_equal_independent_weighted_utf8_substrings(
    snapshots: tuple[str, dict[str, str]],
    ch_url: str,
    threshold: int,
    max_chars: int,
) -> None:
    target, databases = snapshots
    date = "2026-10-04"
    ch = Ch(ch_url, db=target)
    progress = []
    try:
        body = census(ch, target, date, threshold, max_chars, PATTERNS, progress=progress.append)
        hot = {chars: {gram: count for gram, count in weighted_substrings(date, chars).items() if count >= threshold}
               for chars in range(1, max_chars + 1)}
        assert {**body, "weighted_names_s": "<elapsed>",
                "lengths": [{**row, "elapsed_s": "<elapsed>"} for row in body["lengths"]]} == {
            "schema": "hot-frequency-v1", "target": target, "snapshot_db": databases[date], "date": date,
            "scope": "complete snapshot lowercase basename substrings; direct paths, not inherited coverage or occurrence windows",
            "threshold_paths": threshold, "max_chars": max_chars, "distinct_names": 8, "paths": 16,
            "weighted_names_s": "<elapsed>",
            "lengths": [{"chars": chars, "hot_patterns": len(values),
                         "hot_query_utf8_bytes": sum(len(gram.encode()) for gram in values),
                         "sum_hot_direct_matching_paths": sum(values.values()), "elapsed_s": "<elapsed>",
                         "pruned_by_empty_prefix": chars > 1 and not hot[chars - 1]}
                        for chars, values in hot.items()],
            "selected_patterns": [{"pattern": pattern.lower(), "hot": pattern.lower() in hot[len(pattern.lower())],
                                   "direct_matching_paths": hot[len(pattern.lower())].get(pattern.lower())}
                                  for pattern in PATTERNS if len(pattern.lower()) <= max_chars],
            "temporary_index": "session-owned; Ch.close cleans up", "persistent_index_created": False,
            "accepted_hot_pattern_cap": 500_000,
        }
        assert [{**row, "elapsed_s": "<elapsed>"} for row in progress] == [
            {"stage": "weighted-names", "distinct_names": 8, "paths": 16, "elapsed_s": "<elapsed>"},
            *[{"stage": "hot-substrings", "chars": chars, "hot_patterns": len(values), "elapsed_s": "<elapsed>"}
              for chars, values in hot.items()],
        ]
        assert ch.json("SELECT nid,l FROM names ORDER BY nid") == [[nid, name] for nid, name in NAMES.items()]
        assert [(row["pattern"], row["direct_matching_paths"]) for row in body["selected_patterns"] if row["pattern"] == "bc"] == (
            [("bc", None)] if max_chars >= 2 else []
        )
        assert len(ch._tmp) == 1 + sum(not row["pruned_by_empty_prefix"] for row in body["lengths"])
        if max_chars == 7:
            for chars, values in hot.items():
                tables = [table for table in ch._tmp if table.startswith(f"hot_frequency_{chars}_")]
                if chars > 1 and not hot[chars - 1]:
                    assert tables == []
                else:
                    assert len(tables) == 1
                    assert ch.json(f"SELECT gram,direct_matching_paths FROM {tables[0]} ORDER BY gram") == [
                        [gram, count] for gram, count in sorted(values.items())
                    ]
    finally:
        ch.close()
    assert ch._tmp == []


def test_selected_pattern_counts_are_snapshot_specific_and_cold_is_not_zero(snapshots: tuple[str, dict[str, str]], ch_url: str) -> None:
    target, _ = snapshots
    ch = Ch(ch_url, db=target)
    try:
        before = census(ch, target, "2026-10-04", 3, 7, ("AA", ".json", "x", "deadbee", "never"))
        after = census(ch, target, "2026-10-05", 3, 7, ("AA", ".json", "x", "deadbee", "never"))
        assert before["selected_patterns"] == [
            {"pattern": "aa", "hot": True, "direct_matching_paths": 5},
            {"pattern": ".json", "hot": True, "direct_matching_paths": 4},
            {"pattern": "x", "hot": False, "direct_matching_paths": None},
            {"pattern": "deadbee", "hot": False, "direct_matching_paths": None},
            {"pattern": "never", "hot": False, "direct_matching_paths": None},
        ]
        assert after["selected_patterns"] == [
            {"pattern": "aa", "hot": False, "direct_matching_paths": None},
            {"pattern": ".json", "hot": True, "direct_matching_paths": 5},
            {"pattern": "x", "hot": False, "direct_matching_paths": None},
            {"pattern": "deadbee", "hot": False, "direct_matching_paths": None},
            {"pattern": "never", "hot": True, "direct_matching_paths": 3},
        ]
        assert [(row["distinct_names"], row["paths"]) for row in (before, after)] == [(8, 16), (5, 8)]
    finally:
        ch.close()


@pytest.mark.parametrize("threshold,max_chars", [(0, 7), (-1, 7), (1.5, 7), (True, 7), (1, 0), (1, 33), (1, 1.5), (1, True)])
def test_invalid_threshold_or_depth_refuses_before_queries(threshold: int, max_chars: int) -> None:
    with pytest.raises(ValueError) as caught:
        census(None, "safe", "2026-10-05", threshold, max_chars)
    assert str(caught.value) == "hot-frequency census requires a positive integer threshold and 1..32 characters"


@pytest.mark.parametrize("patterns", [("",), ("a/b",), ("a\0b",), ("x" * 33,), ("aa",) * 17])
def test_invalid_selected_patterns_refuse_before_queries(patterns: tuple[str, ...]) -> None:
    with pytest.raises(ValueError) as caught:
        census(None, "safe", "2026-10-05", 1, patterns=patterns)
    assert str(caught.value) == "selected hot-frequency patterns require at most sixteen NUL/slash-free literals of 1..32 characters"


@pytest.mark.parametrize("manifest,error", [
    ({"prefix": "bucket", "dates": ["2026-10-05"], "dbs": ["snapshot"]}, "hot-frequency census requires a global frozen target"),
    ({"prefix": "", "dates": ["2026-10-04"], "dbs": ["snapshot"]}, "scan outside the frozen index"),
    ({"prefix": "", "dates": ["2026-10-05"], "dbs": ["not.safe"]}, "invalid experimental database name: 'not.safe'"),
])
def test_manifest_and_snapshot_identifiers_refuse_before_any_staging(manifest: dict, error: str) -> None:
    calls = []

    class Client:
        def scalar(self, sql: str) -> str:
            calls.append(sql)
            return dumps(manifest)

    with pytest.raises(ValueError) as caught:
        census(Client(), "safe", "2026-10-05", 1)
    assert str(caught.value) == error
    assert calls == ["SELECT doc FROM safe.history_manifest"]


def test_invalid_target_identifier_refuses_before_queries() -> None:
    with pytest.raises(ValueError) as caught:
        census(None, "not.safe", "2026-10-05", 1)
    assert str(caught.value) == "invalid experimental database name: 'not.safe'"


def test_vocabulary_path_separator_refuses_instead_of_crossing_basename_boundaries(ch_db: str, ch_url: str) -> None:
    target = ch_db + "_bad"
    ch = Ch(ch_url, db=ch_db)
    ch.exec(f"CREATE DATABASE {target}")
    try:
        ch.exec(f"CREATE TABLE {target}.names (nid UInt32,l String) ENGINE=Memory")
        ch.exec(f"INSERT INTO {target}.names VALUES (1,'ab/cd')")
        ch.exec(f"CREATE TABLE {target}.nodes_by_name (nid UInt32) ENGINE=Memory")
        ch.exec(f"INSERT INTO {target}.nodes_by_name VALUES (1)")
        ch.exec(f"CREATE TABLE {target}.history_manifest (doc String) ENGINE=Memory")
        ch.exec(f"INSERT INTO {target}.history_manifest VALUES ({lit(dumps({'prefix': '', 'dates': ['2026-10-05'], 'dbs': [target]}))})")
        with pytest.raises(ValueError) as caught:
            census(ch, target, "2026-10-05", 1)
        assert str(caught.value) == "snapshot basename vocabulary contains path separators"
        assert len(ch._tmp) == 1
    finally:
        ch.close()
        ch.exec(f"DROP DATABASE {target} SYNC")


def test_32_layers_and_all_threshold_cuts_equal_independent_counts(
    snapshots: tuple[str, dict[str, str]],
    ch_url: str,
) -> None:
    target, _ = snapshots
    date, cuts = "2026-10-04", (1, 3, 6, 17)
    ch = Ch(ch_url, db=target)
    exported = {}
    try:
        body = census(ch, target, date, 1, 32, ("deadbeef0.npy",), thresholds=(17, 3, 6, 3, 1),
                      on_hot_table=lambda chars, table: exported.update({chars: ch.json(f"SELECT gram,direct_matching_paths FROM {table} ORDER BY gram")}))
        assert body["thresholds_paths"] == list(cuts)
        assert body["accepted_hot_pattern_cap"] == 500_000
        assert [(row["chars"], row["hot_patterns"], row["threshold_counts"]) for row in body["lengths"]] == [
            (chars, len(weighted_substrings(date, chars)),
             [{"threshold_paths": cut, "hot_patterns": sum(count >= cut for count in weighted_substrings(date, chars).values())} for cut in cuts])
            for chars in range(1, 33)
        ]
        assert exported == {chars: [[gram, count] for gram, count in sorted(weighted_substrings(date, chars).items())]
                            for chars in range(1, 15)}
        assert body["selected_patterns"] == [{"pattern": "deadbeef0.npy", "hot": True, "direct_matching_paths": 1}]
        assert [row["pruned_by_empty_prefix"] for row in body["lengths"]] == [False] * 14 + [True] * 18
    finally:
        ch.close()


@pytest.mark.parametrize("kwargs,error", [
    ({"thresholds": (0,)}, "hot-frequency threshold cuts must be integers at least the minimum threshold"),
    ({"thresholds": (True,)}, "hot-frequency threshold cuts must be integers at least the minimum threshold"),
    ({"thresholds": (1.5,)}, "hot-frequency threshold cuts must be integers at least the minimum threshold"),
    ({"max_patterns": 0}, "hot-frequency accepted-pattern cap must be a positive integer"),
    ({"max_patterns": True}, "hot-frequency accepted-pattern cap must be a positive integer"),
])
def test_cut_and_cap_preflight_refuses_before_queries(kwargs: dict, error: str) -> None:
    with pytest.raises(ValueError) as caught:
        census(None, "safe", "2026-10-05", 1, **kwargs)
    assert str(caught.value) == error


def test_global_cap_stops_after_layer_before_export_or_following_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud.chstore import hot_frequency

    calls, exported, progress = [], [], []

    class Client:
        def scalar(self, sql: str) -> str:
            return dumps({"prefix": "", "dates": ["2026-10-05"], "dbs": ["snapshot"]})

        def tmp(self, name: str, sql: str, **kwargs: object) -> None:
            calls.append(("tmp", name))

        def json(self, sql: str) -> list:
            calls.append(("json", sql))
            if sql == "SELECT count(),sum(c),countIf(position(l,'/') > 0) FROM hot_frequency_names_fixture":
                return [[2, 10, 0]]
            return [[2, 4, 20]]

    monkeypatch.setattr(hot_frequency, "uuid4", lambda: type("Tag", (), {"hex": "fixture"})())
    monkeypatch.setattr(hot_frequency, "disk_reserve", lambda *args: None)
    with pytest.raises(RuntimeError) as caught:
        census(Client(), "safe", "2026-10-05", 1, 32, max_patterns=3, progress=progress.append,
               on_hot_table=lambda chars, table: exported.append((chars, table)))
    assert str(caught.value) == "hot-frequency accepted-pattern cap exceeded at length 2: 4 > 3; no complete census"
    assert calls == [
        ("tmp", "hot_frequency_names_fixture"),
        ("json", "SELECT count(),sum(c),countIf(position(l,'/') > 0) FROM hot_frequency_names_fixture"),
        ("tmp", "hot_frequency_1_fixture"),
        ("json", "SELECT count(),sum(length(gram)),sum(direct_matching_paths) FROM hot_frequency_1_fixture"),
        ("tmp", "hot_frequency_2_fixture"),
        ("json", "SELECT count(),sum(length(gram)),sum(direct_matching_paths) FROM hot_frequency_2_fixture"),
    ]
    assert exported == [(1, "hot_frequency_1_fixture")]
    assert [{**row, "elapsed_s": "<elapsed>"} for row in progress] == [
        {"stage": "weighted-names", "distinct_names": 2, "paths": 10, "elapsed_s": "<elapsed>"},
        {"stage": "hot-substrings", "chars": 1, "hot_patterns": 2, "elapsed_s": "<elapsed>"},
    ]
