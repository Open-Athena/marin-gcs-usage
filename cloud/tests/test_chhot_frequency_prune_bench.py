from collections import Counter
from collections.abc import Iterator
from io import StringIO
from json import dumps, loads
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import hot_frequency_prune_bench as module
from dt_cloud.chstore.client import Ch, lit
from dt_cloud.chstore.hot_frequency_report import SCOPE
from dt_cloud.chstore.hot_frequency_registry import UNION_SCHEMA
from dt_cloud.cli import main

from chserver import ch_db, ch_url  # noqa: F401

DATE = "2026-10-05"
TAG = "hot_frequency_prune_" + "a" * 32
NAMES = {0: "", 1: "A", 2: "AAAA", 3: "x🙂x", 4: "🙂🙂q", 5: "abxy", 6: "abxz", 7: "zz", 8: "xab", 9: "ab",
         10: "deadbeef0", 11: "deadbeef1", 12: "deadbeef2", 13: "aaaa"}
WEIGHTS = {"2026-10-04": {0: 1, 1: 2, 2: 5, 3: 4, 4: 3, 5: 1, 6: 2, 7: 1, 8: 1, 9: 2, 10: 1, 11: 1, 12: 1, 13: 2},
           DATE: {0: 1, 2: 1, 3: 2, 4: 1, 5: 3, 7: 1, 8: 1, 9: 2, 10: 2, 11: 1}}


def frequencies(date: str, length: int, threshold: int) -> tuple[tuple[str, int], ...]:
    counts = Counter()
    for nid, weight in WEIGHTS[date].items():
        name = NAMES[nid].lower()
        counts.update({gram: weight for gram in {name[i:i + length] for i in range(max(0, len(name) - length + 1))}})
    return tuple(sorted((gram, count) for gram, count in counts.items() if count >= threshold))


def artifacts(
    directory: Path,
    *,
    target: str = "fleet",
    db: str = "snapshot",
    date: str = DATE,
    threshold: int = 3,
    maximum: int = 9,
) -> tuple[Path, Path, dict]:
    census, queries = directory / "census.json", directory / "queries.jsonl"
    hot = {chars: frequencies(date, chars, threshold) for chars in range(1, maximum + 1)}
    rows = [{"schema": "hot-frequency-queries-v1", "target": target, "date": date,
             "threshold_paths": threshold, "max_chars": maximum},
            *[{"chars": chars, "pattern": gram, "direct_matching_paths": count} for chars, grams in hot.items() for gram, count in grams],
            {"complete": True, "patterns": sum(map(len, hot.values()))}]
    raw = "".join(dumps(row) + "\n" for row in rows)
    queries.write_text(raw)
    body = {"schema": "hot-frequency-v1", "scope": SCOPE, "persistent_index_created": False,
            "target": target, "date": date, "snapshot_db": db, "threshold_paths": threshold, "max_chars": maximum,
            "distinct_names": len(WEIGHTS[date]), "paths": sum(WEIGHTS[date].values()), "weighted_names_s": .1,
            "lengths": [{"chars": chars, "hot_patterns": len(values), "hot_query_utf8_bytes": sum(len(g.encode()) for g, _ in values),
                         "sum_hot_direct_matching_paths": sum(n for _, n in values), "elapsed_s": float(chars),
                         "pruned_by_empty_prefix": chars > 1 and not hot[chars - 1]} for chars, values in hot.items()],
            "queries": {"patterns": sum(map(len, hot.values())), "bytes": len(raw.encode()), "export_s": .1},
            "accepted_hot_pattern_cap": 500_000}
    census.write_text(dumps(body) + "\n")
    return census, queries, body


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(calls=[], error=None, action=None, stderr=StringIO())

    class Client:
        def __init__(self, url: str, **settings: object) -> None:
            state.calls.append(("create", url, settings))

        def exec(self, sql: str) -> None:
            state.calls.append(("exec", sql))

        def json(self, sql: str) -> list:
            state.calls.append(("profile", " ".join(sql.split())))
            return [[TAG + ":prune", 1, 123, 45, 67, 89, 1500000, 11]]

        def close(self) -> None:
            state.calls.append(("close",))

    def run(ch: Client, seed: dict, **options: object) -> dict:
        state.calls.append(("run", seed["prune_chars"], seed["max_chars"], options["log_tag"]))
        if state.error:
            raise state.error
        if state.action:
            state.action()
        return {"schema": "fixture", "complete": True}

    monkeypatch.setattr(module, "Ch", Client)
    monkeypatch.setattr(module, "run", run)
    monkeypatch.setattr(module, "stderr", state.stderr)
    monkeypatch.setattr(module, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    return state


def test_seed_default_next_length_and_entire_reference(tmp_path: Path) -> None:
    census, queries, _ = artifacts(tmp_path)
    seed = module.load_seed(census, queries, "fleet", DATE, 3, 2, None)
    assert (seed["max_chars"], seed["frequencies"], seed["control_layers"]) == (
        3, {2: frequencies(DATE, 2, 3), 3: frequencies(DATE, 3, 3)}, {k: float(k) for k in range(1, 10)},
    )


@pytest.mark.parametrize("kwargs", [{"memory_gib": 0}, {"memory_gib": 9}, {"memory_gib": True}, {"seconds": 0}, {"seconds": 601}, {"spill_gib": 0}, {"spill_gib": 9}])
def test_invalid_limits_refuse_before_client(fake: SimpleNamespace, tmp_path: Path, kwargs: dict) -> None:
    with pytest.raises(ValueError) as caught:
        module.bench("http://node", "fleet", DATE, 3, 2, tmp_path / "missing", tmp_path / "missing", tmp_path / "out", **kwargs)
    assert str(caught.value) == "pruning requires 1..8 GiB memory/spill and 1..600 seconds"
    assert (fake.calls, fake.stderr.getvalue(), list(tmp_path.iterdir())) == ([], "", [])


@pytest.mark.parametrize("overrides", [{"target": "different"}, {"date": "2026-10-04"}, {"threshold": 2}, {"prune_chars": 9}, {"max_chars": 10}, {"max_chars": 2}])
def test_invalid_identity_threshold_depth_refuse_before_client(fake: SimpleNamespace, tmp_path: Path, overrides: dict) -> None:
    census, queries, _ = artifacts(tmp_path)
    args = {"target": "fleet", "date": DATE, "threshold": 3, "prune_chars": 2, "max_chars": None, **overrides}
    with pytest.raises(ValueError):
        module.bench("http://node", census=census, queries=queries, out=tmp_path / "out", **args)
    assert (fake.calls, fake.stderr.getvalue(), (tmp_path / "out").exists()) == ([], "", False)


def test_truncated_export_refuses_before_client(fake: SimpleNamespace, tmp_path: Path) -> None:
    census, queries, _ = artifacts(tmp_path)
    queries.write_text("\n".join(queries.read_text().splitlines()[:-1]) + "\n")
    with pytest.raises(ValueError) as caught:
        module.bench("http://node", "fleet", DATE, 3, 2, census, queries, tmp_path / "out")
    assert str(caught.value) == "hot query export lacks a valid exact-count completion footer"
    assert fake.calls == []


def test_union_registry_refuses_before_client_or_provenance_parse(fake: SimpleNamespace, tmp_path: Path) -> None:
    census, queries, _ = artifacts(tmp_path)
    queries.write_text(dumps({"schema": UNION_SCHEMA}) + "\n")
    out = tmp_path / "out"
    with pytest.raises(ValueError) as caught:
        module.bench("http://node", "fleet", DATE, 3, 2, census, queries, out)
    assert str(caught.value) == "this operation requires a single-date hot-frequency-queries-v1 export"
    assert (fake.calls, fake.stderr.getvalue(), out.exists()) == ([], "", False)


@pytest.mark.parametrize("overrides", [False, True])
def test_limits_profile_artifact_and_cleanup(fake: SimpleNamespace, tmp_path: Path, overrides: bool) -> None:
    census, queries, _ = artifacts(tmp_path)
    out = tmp_path / "out.json"
    options = {"memory_gib": 2, "seconds": 30, "spill_gib": 4, "max_chars": 4} if overrides else {}
    result = module.bench("http://node", "fleet", DATE, 3, 2, census, queries, out, **options)
    memory, seconds, spill = (2, 30, 4) if overrides else (8, 600, 8)
    expected = {"schema": "fixture", "complete": True, "limits": {"memory_gib": memory, "seconds": seconds, "spill_gib": spill, "threads": 4},
                "profile": [{"stage": TAG + ":prune", "statements": 1, "tracked_peak_memory_bytes": 123,
                             "read_rows": 45, "read_bytes": 67, "summed_query_ms": 89, "cpu_s": 1.5, "external_processing_compressed_bytes": 11}]}
    assert result == expected
    assert out.read_text() == dumps(expected) + "\n"
    assert fake.calls == [
        ("create", "http://node", {"db": "fleet", "timeout": seconds + 60, "max_threads": 4,
                                  "max_memory_usage": memory << 30, "max_execution_time": seconds,
                                  "timeout_before_checking_execution_speed": 0, "timeout_overflow_mode": "throw",
                                  "max_bytes_before_external_sort": 256 << 20, "max_bytes_ratio_before_external_sort": 0,
                                  "max_bytes_before_external_group_by": 256 << 20, "max_bytes_ratio_before_external_group_by": 0,
                                  "max_temporary_data_on_disk_size_for_query": spill << 30, "log_comment": TAG}),
        ("run", 2, 4 if overrides else 3, TAG), ("exec", "SYSTEM FLUSH LOGS"),
        ("profile", "SELECT log_comment,count(),max(memory_usage),sum(read_rows),sum(read_bytes),sum(query_duration_ms), "
                    "sum(ProfileEvents['UserTimeMicroseconds']) + sum(ProfileEvents['SystemTimeMicroseconds']), "
                    "sum(ProfileEvents['ExternalProcessingCompressedBytesTotal']) "
                    f"FROM system.query_log WHERE startsWith(log_comment,'{TAG}') AND type='QueryFinish' GROUP BY log_comment ORDER BY log_comment"),
        ("close",),
    ]
    assert fake.stderr.getvalue() == dumps({"log_comment": TAG, "limits": expected["limits"]}) + "\n"


def test_failure_closes_without_artifact(fake: SimpleNamespace, tmp_path: Path) -> None:
    census, queries, _ = artifacts(tmp_path)
    fake.error = RuntimeError("same-cardinality literal mismatch")
    with pytest.raises(RuntimeError) as caught:
        module.bench("http://node", "fleet", DATE, 3, 2, census, queries, tmp_path / "out")
    assert str(caught.value) == "same-cardinality literal mismatch"
    assert (fake.calls[-1], (tmp_path / "out").exists()) == (("close",), False)


def test_no_overwrite_and_race_preserves_other_file(fake: SimpleNamespace, tmp_path: Path) -> None:
    census, queries, _ = artifacts(tmp_path)
    out = tmp_path / "out"
    out.write_text("existing\n")
    with pytest.raises(ValueError) as caught:
        module.bench("http://node", "fleet", DATE, 3, 2, census, queries, out)
    assert (str(caught.value), out.read_text(), fake.calls) == ("pruning artifact already exists", "existing\n", [])
    other = tmp_path / "raced"
    fake.action = lambda: other.write_text("raced\n")
    with pytest.raises(FileExistsError):
        module.bench("http://node", "fleet", DATE, 3, 2, census, queries, other)
    assert (other.read_text(), fake.calls[-1]) == ("raced\n", ("close",))


def test_invalid_rss_refuses_before_client(fake: SimpleNamespace, tmp_path: Path) -> None:
    census, queries, _ = artifacts(tmp_path)
    with pytest.raises(ValueError) as caught:
        module.bench("http://node", "fleet", DATE, 3, 2, census, queries, tmp_path / "out", pids=(11, 11))
    assert (str(caught.value), fake.calls) == ("RSS PIDs must be positive and distinct", [])


@pytest.mark.parametrize("failure", [False, True])
def test_optional_rss_context_and_error_cleanup(fake: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: bool) -> None:
    census, queries, _ = artifacts(tmp_path)
    out = tmp_path / "out.json"
    events = []

    class Monitor:
        def __init__(self, pids: tuple[int, ...], path: Path) -> None:
            events.append(("create", pids, path))

        def __enter__(self) -> "Monitor":
            events.append(("enter",))
            return self

        def __exit__(self, *args: object) -> None:
            events.append(("exit", tuple(value is not None for value in args)))

        def summary(self) -> dict:
            events.append(("summary",))
            return {"samples": 2, "sampled_peak_total_rss_bytes": 1234}

    monkeypatch.setattr(module, "RssMonitor", Monitor)
    if failure:
        fake.error = RuntimeError("failed pruning")
        with pytest.raises(RuntimeError) as caught:
            module.bench("http://node", "fleet", DATE, 3, 2, census, queries, out, pids=(11,))
        assert str(caught.value) == "failed pruning"
        assert out.exists() is False
    else:
        result = module.bench("http://node", "fleet", DATE, 3, 2, census, queries, out, pids=(11,))
        assert result["rss"] == {"samples": 2, "sampled_peak_total_rss_bytes": 1234}
        assert loads(out.read_text()) == result
    assert events == [("create", (11,), out.with_suffix(".rss.jsonl")), ("enter",), ("exit", (failure, failure, failure)),
                      *([] if failure else [("summary",)])]
    assert fake.calls[-1] == ("close",)


def test_cli_forwarding(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(module, "bench", lambda *args, **kwargs: calls.append((args, kwargs)) or {"complete": True})
    result = CliRunner().invoke(main, ["ch-hot-frequency-prune-bench", "fleet", "-a", "control.json", "-q", "queries.jsonl",
                                     "-d", DATE, "-t", "3", "-k", "2", "-l", "4", "-m", "2", "-s", "4", "-w", "30", "-o", "out.json", "-p", "11", "-U", "http://node"])
    assert (result.exit_code, result.output) == (0, '{"complete": true}\n')
    assert calls == [(("http://node", "fleet", DATE, 3, 2, Path("control.json"), Path("queries.jsonl"), Path("out.json")),
                     {"max_chars": 4, "memory_gib": 2, "seconds": 30, "spill_gib": 4, "pids": (11,)})]


def test_same_cardinality_different_literal_refuses_without_later_queries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    census, queries, _ = artifacts(tmp_path)
    seed = module.load_seed(census, queries, "fleet", DATE, 3, 2, 4)
    assert seed["frequencies"][3] == (("abx", 3), ("adb", 3), ("bee", 3), ("bxy", 3), ("dbe", 3), ("dea", 3), ("ead", 3), ("eef", 3))
    calls = []

    class Client:
        def scalar(self, sql: str) -> str:
            if sql == "SELECT doc FROM fleet.history_manifest":
                return dumps({"prefix": "", "dates": [DATE], "dbs": ["snapshot"]})
            return "8" if sql.startswith("SELECT count() FROM hot_prune_layer_") else str(30 << 30)

        def tmp(self, name: str, sql: str, **settings: object) -> None:
            calls.append((name.split("_" + "b" * 32)[0], " ".join(sql.split()), settings))

        def insert(self, sql: str, data: Iterator[bytes]) -> None:
            calls.append(("insert", [loads(row) for row in data]))

        def json(self, sql: str) -> list:
            return [[0, 0]] if sql.startswith("SELECT sum(bytes_on_disk)") else [["abz", 3], *[list(row) for row in seed["frequencies"][3][1:]]]

    monkeypatch.setattr(module, "uuid4", lambda: SimpleNamespace(hex="b" * 32))
    monkeypatch.setattr(module, "disk_reserve", lambda *args: None)
    monkeypatch.setattr(module, "_stats", lambda *args: {"distinct_names": seed["distinct_names"], "paths": seed["paths"]})
    with pytest.raises(RuntimeError) as caught:
        module.run(Client(), seed)
    assert str(caught.value) == "pruning exact reference mismatch at length 3; no accepted result"
    assert [call[0] for call in calls] == ["hot_prune_names", "hot_prune_seed", "insert", "hot_prune_candidates", "hot_prune_layer_3"]
    assert calls[3] == ("hot_prune_candidates",
                        "SELECT nid,l,c FROM hot_prune_names_" + "b" * 32 + " WHERE lengthUTF8(l) > 2 "
                        "AND arrayExists(g -> g IN (SELECT gram FROM hot_prune_seed_" + "b" * 32 + "), ngrams(l,2))",
                        {"disk": True, "ordered": False, "settings": None})


@pytest.fixture(scope="module")
def native_snapshots(ch_db: str, ch_url: str) -> Iterator[tuple[str, dict[str, str]]]:
    ch = Ch(ch_url, db=ch_db)
    databases = {day: ch_db + suffix for day, suffix in (("2026-10-04", "_a"), (DATE, "_b"))}
    try:
        ch.exec("CREATE TABLE names (nid UInt32,l String) ENGINE=MergeTree ORDER BY nid")
        ch.exec("INSERT INTO names VALUES " + ",".join(f"({nid},{lit(name)})" for nid, name in NAMES.items()))
        for day, database in databases.items():
            ch.exec(f"CREATE DATABASE {database}")
            ch.exec(f"CREATE TABLE {database}.nodes_by_name (nid UInt32,pre UInt32) ENGINE=MergeTree ORDER BY (nid,pre)")
            rows = [(nid, pre) for pre, nid in enumerate(nid for nid, count in WEIGHTS[day].items() for _ in range(count))]
            ch.exec(f"INSERT INTO {database}.nodes_by_name VALUES " + ",".join(f"({nid},{pre})" for nid, pre in rows))
        ch.exec("CREATE TABLE history_manifest (doc String) ENGINE=Memory")
        ch.exec("INSERT INTO history_manifest VALUES (" + lit(dumps({"prefix": "", "dates": list(databases), "dbs": list(databases.values())})) + ")")
        yield ch_db, databases
    finally:
        ch.close()
        for database in databases.values():
            ch.exec(f"DROP DATABASE IF EXISTS {database} SYNC")


@pytest.mark.parametrize("date,threshold,k,end", [("2026-10-04", 3, 2, 9), (DATE, 3, 2, 4), (DATE, 1, 1, 4), (DATE, 100, 2, 3)])
def test_native_pruned_candidates_and_every_hot_tuple_match_weighted_oracle(
    native_snapshots: tuple[str, dict[str, str]],
    ch_url: str,
    tmp_path: Path,
    date: str,
    threshold: int,
    k: int,
    end: int,
) -> None:
    target, databases = native_snapshots
    census, queries, _ = artifacts(tmp_path, target=target, db=databases[date], date=date, threshold=threshold)
    seed = module.load_seed(census, queries, target, date, threshold, k, end)
    ch = Ch(ch_url, db=target)
    try:
        body = module.run(ch, seed)
        hot = {gram for gram, _ in frequencies(date, k, threshold)}
        kept = [[nid, name.lower(), WEIGHTS[date][nid]] for nid, name in NAMES.items() if nid in WEIGHTS[date] and len(name.lower()) > k and
                any(name.lower()[i:i + k] in hot for i in range(max(0, len(name.lower()) - k + 1)))]
        table = [name for name in ch._tmp if name.startswith("hot_prune_candidates_")]
        assert len(table) == 1
        assert ch.json(f"SELECT nid,l,c FROM {table[0]} ORDER BY nid") == kept
        assert {key: body["retained"][key] for key in ("distinct_names", "paths", "name_utf8_bytes", "name_characters", "occurrence_windows")} == {
            "distinct_names": len(kept), "paths": sum(row[2] for row in kept), "name_utf8_bytes": sum(len(row[1].encode()) for row in kept),
            "name_characters": sum(len(row[1]) for row in kept),
            "occurrence_windows": [{"chars": chars, "windows": sum(max(0, len(row[1]) - chars + 1) for row in kept)} for chars in range(k + 1, end + 1)],
        }
        assert [(row["chars"], row["hot_patterns"], row["control_observed_elapsed_s"], row["validation"]) for row in body["lengths"]] == [
            (chars, len(frequencies(date, chars, threshold)), float(chars), "complete sorted literal/frequency tuple equality against accepted export")
            for chars in range(k + 1, end + 1)
        ]
        for chars in range(k + 1, end + 1):
            tables = [name for name in ch._tmp if name.startswith(f"hot_prune_layer_{chars}_")]
            assert len(tables) == 1
            assert ch.json(f"SELECT gram,direct_matching_paths FROM {tables[0]} ORDER BY gram") == [list(row) for row in frequencies(date, chars, threshold)]
        assert ch.json("SELECT nid,l FROM names ORDER BY nid") == [[nid, name] for nid, name in NAMES.items()]
    finally:
        ch.close()
    assert ch._tmp == []


def test_native_same_cardinality_wrong_frequency_refuses_before_next_layer(native_snapshots: tuple[str, dict[str, str]], ch_url: str, tmp_path: Path) -> None:
    target, databases = native_snapshots
    census, queries, _ = artifacts(tmp_path, target=target, db=databases[DATE])
    seed = module.load_seed(census, queries, target, DATE, 3, 2, 4)
    values = seed["frequencies"][3]
    seed["frequencies"][3] = ((values[0][0], values[0][1] + 1), *values[1:])
    ch = Ch(ch_url, db=target)
    try:
        with pytest.raises(RuntimeError) as caught:
            module.run(ch, seed)
        assert str(caught.value) == "pruning exact reference mismatch at length 3; no accepted result"
        assert [name for name in ch._tmp if name.startswith("hot_prune_layer_4_")] == []
    finally:
        ch.close()
