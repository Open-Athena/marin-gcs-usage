from io import StringIO
from json import dumps
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from dt_cloud.chstore import query_census
from dt_cloud.cli import main


QUERY_ID = "pattern_census_" + "a" * 32


@pytest.fixture
def mocked_census(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(calls=[], stderr=StringIO(), error=None, client=None,
                            profile_rows=[[123, 456, 789, 900, {"OSReadBytes": 10}]])

    class Client:
        def __init__(self, url: str, **settings: object) -> None:
            state.calls.append(("create", url, settings))
            state.client = self

        def exec(self, sql: str) -> None:
            state.calls.append(("exec", sql))

        def json(self, sql: str) -> list:
            state.calls.append(("json", " ".join(sql.split())))
            return state.profile_rows

        def close(self) -> None:
            state.calls.append(("close",))

    def census(
        ch: Client,
        target: str,
        date: str,
        patterns: tuple[str, ...],
        **kwargs: object,
    ) -> dict:
        state.calls.append(("census", ch, target, date, patterns, kwargs))
        if state.error is not None:
            raise state.error
        return {"paths": 5}

    monkeypatch.setattr(query_census, "Ch", Client)
    monkeypatch.setattr(query_census, "pattern_frequency_census", census)
    monkeypatch.setattr(query_census, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    monkeypatch.setattr(query_census, "stderr", state.stderr)
    return state


def settings(
    memory: int,
    seconds: int,
    spill: int,
) -> dict:
    return {"db": "fleet", "timeout": seconds + 60, "max_threads": 4,
            "max_memory_usage": memory << 30, "max_execution_time": seconds,
            "timeout_before_checking_execution_speed": 0, "timeout_overflow_mode": "throw",
            "max_temporary_data_on_disk_size_for_query": spill << 30}


@pytest.mark.parametrize("kwargs,memory,seconds,spill", [
    ({}, 8, 180, 8),
    ({"memory_gib": 2, "seconds": 600, "spill_gib": 16}, 2, 600, 16),
])
def test_default_and_explicit_offline_budgets_forward_exactly_and_close(
    mocked_census: SimpleNamespace,
    kwargs: dict,
    memory: int,
    seconds: int,
    spill: int,
) -> None:
    body = query_census.pattern_frequency_bench("http://node:8123", "fleet", "2026-10-05", ("AA", ".json"), **kwargs)
    limits = {"memory_gib": memory, "seconds": seconds, "spill_gib": spill, "threads": 4}
    assert body == {"paths": 5, "query_id": QUERY_ID, "limits": limits,
                    "cache_state": "uncontrolled; offline census, not serving latency"}
    assert mocked_census.calls == [
        ("create", "http://node:8123", settings(memory, seconds, spill)),
        ("census", mocked_census.client, "fleet", "2026-10-05", ("AA", ".json"), {"query_id": QUERY_ID}),
        ("close",),
    ]
    assert mocked_census.stderr.getvalue().splitlines() == [dumps({"query_id": QUERY_ID, "limits": limits})]


@pytest.mark.parametrize("kwargs", [
    {"memory_gib": 0}, {"memory_gib": 9}, {"seconds": 0}, {"seconds": 601},
    {"spill_gib": 0}, {"spill_gib": 17},
])
def test_invalid_offline_limits_refuse_before_creating_client(mocked_census: SimpleNamespace, kwargs: dict) -> None:
    with pytest.raises(ValueError) as caught:
        query_census.pattern_frequency_bench("http://node", "fleet", "2026-10-05", ("aa",), **kwargs)
    assert str(caught.value) == "offline census requires 1..8 GiB memory, 1..600 seconds and 1..16 GiB spill"
    assert mocked_census.calls == []
    assert mocked_census.stderr.getvalue() == ""


@pytest.mark.parametrize("kwargs", [{"pids": (1,)}, {"rss_out": Path("rss.jsonl")}])
def test_incomplete_rss_configuration_refuses_before_creating_client(mocked_census: SimpleNamespace, kwargs: dict) -> None:
    with pytest.raises(ValueError) as caught:
        query_census.pattern_frequency_bench("http://node", "fleet", "2026-10-05", ("aa",), **kwargs)
    assert str(caught.value) == "RSS monitoring requires both process IDs and an output path"
    assert mocked_census.calls == []
    assert mocked_census.stderr.getvalue() == ""


@pytest.mark.parametrize("pids", [(0,), (-1,), (1, 1)])
def test_invalid_rss_pids_refuse_before_creating_client(mocked_census: SimpleNamespace, pids: tuple[int, ...]) -> None:
    with pytest.raises(ValueError) as caught:
        query_census.pattern_frequency_bench("http://node", "fleet", "2026-10-05", ("aa",), pids=pids, rss_out=Path("rss.jsonl"))
    assert str(caught.value) == "RSS PIDs must be positive and distinct"
    assert mocked_census.calls == []
    assert mocked_census.stderr.getvalue() == ""


def test_monitor_constructor_failure_precedes_client_creation(mocked_census: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    failure = RuntimeError("monitor construction failed")

    def monitor(pids: tuple[int, ...], output: Path) -> None:
        assert (pids, output) == ((1,), Path("rss.jsonl"))
        raise failure

    monkeypatch.setattr(query_census, "RssMonitor", monitor)
    with pytest.raises(RuntimeError) as caught:
        query_census.pattern_frequency_bench("http://node", "fleet", "2026-10-05", ("aa",), pids=(1,), rss_out=Path("rss.jsonl"))
    assert caught.value is failure
    assert mocked_census.calls == []
    assert mocked_census.stderr.getvalue() == ""


def test_census_failure_propagates_original_exception_and_closes(mocked_census: SimpleNamespace) -> None:
    failure = RuntimeError("query budget exceeded")
    mocked_census.error = failure
    with pytest.raises(RuntimeError) as caught:
        query_census.pattern_frequency_bench("http://node", "fleet", "2026-10-05", ("aa",))
    assert caught.value is failure
    assert mocked_census.calls == [
        ("create", "http://node", settings(8, 180, 8)),
        ("census", mocked_census.client, "fleet", "2026-10-05", ("aa",), {"query_id": QUERY_ID}),
        ("close",),
    ]


def test_successful_rss_and_profile_cover_workload_then_close(
    mocked_census: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rss = {"samples": 2, "sampled_peak_total_rss_bytes": 1000}

    class Monitor:
        def __init__(self, pids: tuple[int, ...], output: Path) -> None:
            mocked_census.calls.append(("monitor-create", pids, output))

        def __enter__(self) -> "Monitor":
            mocked_census.calls.append(("monitor-enter",))
            return self

        def __exit__(self, *args: object) -> None:
            mocked_census.calls.append(("monitor-exit", args))

        def summary(self) -> dict:
            mocked_census.calls.append(("monitor-summary",))
            return rss

    monkeypatch.setattr(query_census, "RssMonitor", Monitor)
    body = query_census.pattern_frequency_bench("http://node", "fleet", "2026-10-05", ("aa",),
                                              pids=(1, 2), rss_out=Path("rss.jsonl"), profile=True)
    assert body == {
        "paths": 5, "query_id": QUERY_ID,
        "limits": {"memory_gib": 8, "seconds": 180, "spill_gib": 8, "threads": 4},
        "cache_state": "uncontrolled; offline census, not serving latency", "rss": rss,
        "profile": {"duration_ms": 123, "tracked_peak_memory_bytes": 456, "read_rows": 789,
                    "read_bytes": 900, "events": {"OSReadBytes": 10}},
    }
    assert mocked_census.calls == [
        ("monitor-create", (1, 2), Path("rss.jsonl")),
        ("create", "http://node", settings(8, 180, 8)),
        ("monitor-enter",),
        ("census", mocked_census.client, "fleet", "2026-10-05", ("aa",), {"query_id": QUERY_ID}),
        ("monitor-exit", (None, None, None)), ("monitor-summary",), ("exec", "SYSTEM FLUSH LOGS"),
        ("json", "SELECT query_duration_ms,memory_usage,read_rows,read_bytes,ProfileEvents FROM system.query_log "
                 f"WHERE query_id='{QUERY_ID}' AND type='QueryFinish' ORDER BY event_time DESC LIMIT 1"),
        ("close",),
    ]


@pytest.mark.parametrize("stage", ["enter", "exit", "summary"])
def test_monitor_lifecycle_failures_propagate_and_close_created_client(
    mocked_census: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    failure = RuntimeError(f"monitor {stage} failed")

    class Monitor:
        def __init__(self, pids: tuple[int, ...], output: Path) -> None:
            mocked_census.calls.append(("monitor-create", pids, output))

        def __enter__(self) -> "Monitor":
            mocked_census.calls.append(("monitor-enter",))
            if stage == "enter":
                raise failure
            return self

        def __exit__(self, *args: object) -> None:
            mocked_census.calls.append(("monitor-exit", args))
            if stage == "exit":
                raise failure

        def summary(self) -> dict:
            mocked_census.calls.append(("monitor-summary",))
            raise failure

    monkeypatch.setattr(query_census, "RssMonitor", Monitor)
    with pytest.raises(RuntimeError) as caught:
        query_census.pattern_frequency_bench("http://node", "fleet", "2026-10-05", ("aa",),
                                            pids=(1,), rss_out=Path("rss.jsonl"))
    assert caught.value is failure
    expected = [
        ("monitor-create", (1,), Path("rss.jsonl")),
        ("create", "http://node", settings(8, 180, 8)), ("monitor-enter",),
    ]
    if stage != "enter":
        expected.extend([
            ("census", mocked_census.client, "fleet", "2026-10-05", ("aa",), {"query_id": QUERY_ID}),
            ("monitor-exit", (None, None, None)),
        ])
    if stage == "summary":
        expected.append(("monitor-summary",))
    expected.append(("close",))
    assert mocked_census.calls == expected


def test_missing_success_profile_refuses_and_still_closes(mocked_census: SimpleNamespace) -> None:
    mocked_census.profile_rows = []
    with pytest.raises(RuntimeError) as caught:
        query_census.pattern_frequency_bench("http://node", "fleet", "2026-10-05", ("aa",), profile=True)
    assert str(caught.value) == "successful census query profile missing"
    assert mocked_census.calls == [
        ("create", "http://node", settings(8, 180, 8)),
        ("census", mocked_census.client, "fleet", "2026-10-05", ("aa",), {"query_id": QUERY_ID}),
        ("exec", "SYSTEM FLUSH LOGS"),
        ("json", "SELECT query_duration_ms,memory_usage,read_rows,read_bytes,ProfileEvents FROM system.query_log "
                 f"WHERE query_id='{QUERY_ID}' AND type='QueryFinish' ORDER BY event_time DESC LIMIT 1"),
        ("close",),
    ]


def test_census_statement_preserves_caller_spill_budget_and_query_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class Client:
        def scalar(self, sql: str) -> str:
            calls.append(("scalar", sql))
            return dumps({"dates": ["2026-10-05"], "dbs": ["snapshot"]})

        def json(self, sql: str, **kwargs: object) -> list:
            calls.append(("json", " ".join(sql.split()), kwargs))
            return [[2, 5, 1, 3, 1, 2]]

    times = iter([10.0, 10.25])
    monkeypatch.setattr(query_census, "monotonic", lambda: next(times))
    body = query_census.pattern_frequency_census(Client(), "fleet", "2026-10-05", ("AA", ".JSON"), query_id=QUERY_ID)
    assert body == {
        "scope": "exact selected name-substring frequencies; no inherited coverage or aggregate payload",
        "date": "2026-10-05", "distinct_names": 2, "paths": 5,
        "patterns": [{"pattern": "aa", "names": 1, "direct_matching_paths": 3},
                     {"pattern": ".json", "names": 1, "direct_matching_paths": 2}],
        "elapsed_s": .25, "stored_index_created": False,
    }
    assert calls == [
        ("scalar", "SELECT doc FROM fleet.history_manifest"),
        ("json", "SELECT count(),sum(f.c),countIf(bitTest(n.mask,0)),sumIf(f.c,bitTest(n.mask,0)),"
                 "countIf(bitTest(n.mask,1)),sumIf(f.c,bitTest(n.mask,1)) "
                 "FROM (SELECT nid,toUInt16(if(position(l,'aa') > 0,1,0)+if(position(l,'.json') > 0,2,0)) "
                 "mask FROM fleet.names ORDER BY nid) n "
                 "INNER JOIN (SELECT nid,count() c FROM snapshot.nodes_by_name GROUP BY nid ORDER BY nid) f ON n.nid=f.nid "
                 "SETTINGS join_algorithm='full_sorting_merge',optimize_aggregation_in_order=1, "
                 "query_plan_join_swap_table='false',max_rows_in_set_to_optimize_join=0, "
                 "max_block_size=8192,max_bytes_before_external_sort=67108864", {"settings": {"query_id": QUERY_ID}}),
    ]


@pytest.mark.parametrize("options,expected", [
    ([], {"memory_gib": 8, "seconds": 180, "spill_gib": 8, "pids": (), "rss_out": None, "profile": False}),
    (["-m", "2", "-o", "rss.jsonl", "-p", "11", "-p", "22", "-q", "-s", "16", "-w", "600"],
     {"memory_gib": 2, "seconds": 600, "spill_gib": 16, "pids": (11, 22), "rss_out": Path("rss.jsonl"), "profile": True}),
])
def test_cli_forwards_all_default_and_explicit_offline_controls(
    monkeypatch: pytest.MonkeyPatch,
    options: list[str],
    expected: dict,
) -> None:
    calls = []

    def bench(*args: object, **kwargs: object) -> dict:
        calls.append((args, kwargs))
        return {"verified": True}

    monkeypatch.setattr(query_census, "pattern_frequency_bench", bench)
    result = CliRunner().invoke(main, ["ch-pattern-frequency-census", "-d", "2026-10-05", "-n", "AA", "-n", ".json",
                                      "-U", "http://node:8123", *options, "fleet"])
    assert result.exit_code == 0, result.exception
    assert result.output == '{"verified": true}\n'
    assert calls == [(("http://node:8123", "fleet", "2026-10-05", ("AA", ".json")), expected)]
