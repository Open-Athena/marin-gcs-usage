from io import StringIO
from json import dumps
from pathlib import Path
from types import SimpleNamespace
from collections.abc import Callable
from collections.abc import Iterator

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import hot_frequency_bench
from dt_cloud.cli import main


TAG = "hot_frequency_" + "c" * 32
PROFILE_SQL = ("SELECT count(),max(memory_usage),sum(read_rows),sum(read_bytes),sum(query_duration_ms) "
               f"FROM system.query_log WHERE log_comment='{TAG}' AND type='QueryFinish'")
STAGE = {"length": 1, "hot_patterns": 12}


@pytest.fixture
def mocked_bench(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(calls=[], client=None, stderr=StringIO(), fail=None, action=None,
                            error=RuntimeError("census failed"),
                            body={"schema": "hot-frequency-v1", "threshold_paths": 10, "lengths": [STAGE]},
                            rss={"samples": 2, "sampled_peak_total_rss_bytes": 1000}, tables=[], streams={})

    def check(stage: str) -> None:
        if state.fail == stage:
            raise state.error

    class Client:
        def __init__(self, url: str, **settings: object) -> None:
            state.calls.append(("create", url, settings))
            state.client = self

        def exec(self, sql: str) -> None:
            state.calls.append(("exec", sql))
            check("flush")

        def json(self, sql: str) -> list:
            state.calls.append(("json", " ".join(sql.split())))
            check("profile")
            return [[3, 123, 45, 67, 89]]

        def stream(
            self,
            sql: str,
            *,
            fmt: str,
            settings: dict,
        ) -> Iterator[bytes]:
            state.calls.append(("stream", sql, fmt, settings))
            for chunk in state.streams[sql]:
                yield chunk
                check("stream")

        def close(self) -> None:
            state.calls.append(("close",))

    class Monitor:
        def __init__(self, pids: tuple[int, ...], out: Path) -> None:
            state.calls.append(("monitor-create", pids, out))

        def __enter__(self) -> "Monitor":
            state.calls.append(("monitor-enter",))
            return self

        def __exit__(self, *args: object) -> None:
            state.calls.append(("monitor-exit", tuple(arg is not None for arg in args)))

        def summary(self) -> dict:
            state.calls.append(("monitor-summary",))
            return state.rss

    def census(
        ch: Client,
        target: str,
        date: str,
        threshold: int,
        max_chars: int,
        *,
        patterns: tuple[str, ...],
        progress: Callable[[dict], None],
        on_hot_table: Callable[[int, str], None] | None = None,
        thresholds: tuple[int, ...] = (),
        max_patterns: int = 500_000,
    ) -> dict:
        state.calls.append(("census", ch, target, date, threshold, max_chars, patterns))
        state.census_options = (thresholds, max_patterns)
        progress(STAGE)
        check("census")
        if state.action is not None:
            state.action()
        if on_hot_table is not None:
            for chars, table in state.tables:
                on_hot_table(chars, table)
        return dict(state.body)

    monkeypatch.setattr(hot_frequency_bench, "Ch", Client)
    monkeypatch.setattr(hot_frequency_bench, "census", census)
    monkeypatch.setattr(hot_frequency_bench, "RssMonitor", Monitor)
    monkeypatch.setattr(hot_frequency_bench, "uuid4", lambda: SimpleNamespace(hex="c" * 32))
    monkeypatch.setattr(hot_frequency_bench, "stderr", state.stderr)
    times = iter([10., 10.125, 11., 11.125])
    monkeypatch.setattr(hot_frequency_bench, "monotonic", lambda: next(times))
    return state


def settings(
    memory: int = 8,
    seconds: int = 600,
    spill: int = 8,
) -> dict:
    return {
        "db": "fleet", "timeout": seconds + 60, "max_threads": 4,
        "max_memory_usage": memory << 30, "max_execution_time": seconds,
        "timeout_before_checking_execution_speed": 0, "timeout_overflow_mode": "throw",
        "max_bytes_before_external_sort": 256 << 20, "max_bytes_ratio_before_external_sort": 0,
        "max_bytes_before_external_group_by": 256 << 20, "max_bytes_ratio_before_external_group_by": 0,
        "max_temporary_data_on_disk_size_for_query": spill << 30, "log_comment": TAG,
    }


@pytest.mark.parametrize("overrides", [False, True])
def test_exact_artifact_limits_profile_progress_and_optional_rss(
    mocked_bench: SimpleNamespace,
    tmp_path: Path,
    overrides: bool,
) -> None:
    out = tmp_path / "report.json"
    kwargs = {"memory_gib": 2, "seconds": 17, "spill_gib": 16, "pids": (11, 22), "patterns": (".json", "abc")} if overrides else {}
    result = hot_frequency_bench.bench("http://node", "fleet", "2026-10-05", 10, 7, out, **kwargs)
    limits = {"memory_gib": 2 if overrides else 8, "seconds": 17 if overrides else 600,
              "spill_gib": 16 if overrides else 8, "threads": 4}
    expected = {
        "schema": "hot-frequency-v1", "threshold_paths": 10, "lengths": [STAGE], "limits": limits,
        "profile": {"statements": 3, "tracked_peak_memory_bytes": 123,
                    "read_rows": 45, "read_bytes": 67, "summed_query_ms": 89},
        "cache_state": "uncontrolled; offline census, not serving latency",
    }
    if overrides:
        expected["rss"] = {"samples": 2, "sampled_peak_total_rss_bytes": 1000}
    assert result == expected
    assert out.read_text() == dumps(expected) + "\n"
    calls = [("monitor-create", (11, 22), out.with_suffix(".rss.jsonl"))] if overrides else []
    calls.append(("create", "http://node", settings(2, 17, 16) if overrides else settings()))
    if overrides:
        calls.append(("monitor-enter",))
    calls.append(("census", mocked_bench.client, "fleet", "2026-10-05", 10, 7, (".json", "abc") if overrides else ()))
    if overrides:
        calls.append(("monitor-exit", (False, False, False)))
    calls.extend([("exec", "SYSTEM FLUSH LOGS"), ("json", PROFILE_SQL)])
    if overrides:
        calls.append(("monitor-summary",))
    calls.append(("close",))
    assert mocked_bench.calls == calls
    assert mocked_bench.stderr.getvalue().splitlines() == [dumps({"log_comment": TAG, "limits": limits}), dumps(STAGE)]


@pytest.mark.parametrize("kwargs", [{"memory_gib": 0}, {"memory_gib": 9}, {"seconds": 0}, {"seconds": 601}, {"spill_gib": 0}, {"spill_gib": 17}])
def test_budget_preflight_refuses_without_client_logs_or_files(mocked_bench: SimpleNamespace, tmp_path: Path, kwargs: dict) -> None:
    with pytest.raises(ValueError) as caught:
        hot_frequency_bench.bench("http://node", "fleet", "2026-10-05", 10, 7, tmp_path / "report.json", **kwargs)
    assert str(caught.value) == "hot frequency census requires 1..8 GiB memory, 1..600 seconds and 1..16 GiB spill"
    assert (mocked_bench.calls, mocked_bench.stderr.getvalue(), list(tmp_path.iterdir())) == ([], "", [])


@pytest.mark.parametrize("threshold,max_chars", [(0, 7), (10, 0), (10, 33)])
def test_threshold_and_length_preflight_refuses_before_client(mocked_bench: SimpleNamespace, tmp_path: Path, threshold: int, max_chars: int) -> None:
    with pytest.raises(ValueError) as caught:
        hot_frequency_bench.bench("http://node", "fleet", "2026-10-05", threshold, max_chars, tmp_path / "report.json")
    assert str(caught.value) == "hot-frequency census requires a positive integer threshold and 1..32 characters"
    assert (mocked_bench.calls, mocked_bench.stderr.getvalue(), list(tmp_path.iterdir())) == ([], "", [])


def test_existing_artifact_is_preserved_before_any_client_work(mocked_bench: SimpleNamespace, tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    out.write_text("existing\n")
    with pytest.raises(ValueError) as caught:
        hot_frequency_bench.bench("http://node", "fleet", "2026-10-05", 10, 7, out)
    assert str(caught.value) == "hot frequency artifact already exists"
    assert (out.read_text(), mocked_bench.calls, mocked_bench.stderr.getvalue()) == ("existing\n", [], "")


@pytest.mark.parametrize("stage", ["census", "flush", "profile"])
def test_failure_closes_client_exits_rss_and_never_writes_result(mocked_bench: SimpleNamespace, tmp_path: Path, stage: str) -> None:
    mocked_bench.fail = stage
    out = tmp_path / "report.json"
    with pytest.raises(RuntimeError) as caught:
        hot_frequency_bench.bench("http://node", "fleet", "2026-10-05", 10, 7, out, pids=(11,))
    assert caught.value is mocked_bench.error
    expected = [
        ("monitor-create", (11,), out.with_suffix(".rss.jsonl")), ("create", "http://node", settings()),
        ("monitor-enter",), ("census", mocked_bench.client, "fleet", "2026-10-05", 10, 7, ()),
        ("monitor-exit", (True, True, True) if stage == "census" else (False, False, False)),
    ]
    if stage != "census":
        expected.append(("exec", "SYSTEM FLUSH LOGS"))
    if stage == "profile":
        expected.append(("json", PROFILE_SQL))
    expected.append(("close",))
    assert mocked_bench.calls == expected
    assert list(tmp_path.iterdir()) == []


def test_exclusive_write_preserves_artifact_created_during_census(mocked_bench: SimpleNamespace, tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    mocked_bench.action = lambda: out.write_text("concurrent writer\n")
    with pytest.raises(FileExistsError):
        hot_frequency_bench.bench("http://node", "fleet", "2026-10-05", 10, 7, out)
    assert out.read_text() == "concurrent writer\n"
    assert mocked_bench.calls == [
        ("create", "http://node", settings()), ("census", mocked_bench.client, "fleet", "2026-10-05", 10, 7, ()),
        ("exec", "SYSTEM FLUSH LOGS"), ("json", PROFILE_SQL), ("close",),
    ]


@pytest.mark.parametrize("overrides", [False, True])
def test_cli_forwards_defaults_overrides_and_preserves_progress_stderr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, overrides: bool) -> None:
    calls = []

    def bench(*args: object, **kwargs: object) -> dict:
        from sys import stderr as current_stderr

        calls.append((args, kwargs))
        print(dumps(STAGE), file=current_stderr)
        return {"schema": "fixture"}

    monkeypatch.setattr(hot_frequency_bench, "bench", bench)
    out = tmp_path / "report.json"
    queries_out = tmp_path / "queries.jsonl"
    args = ["ch-hot-frequency-census", "fleet", "-d", "2026-10-05", "-t", "10", "-o", str(out)]
    if overrides:
        args.extend(["-c", "1234", "-h", "20", "-h", "30", "-k", "32", "-m", "2", "-n", ".json", "-n", "abc", "-p", "11", "-p", "22", "-q", str(queries_out), "-s", "16", "-w", "17", "-U", "http://node"])
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert (result.stdout, result.stderr) == (dumps({"schema": "fixture"}) + "\n", dumps(STAGE) + "\n")
    assert calls == [(("http://node" if overrides else "http://localhost:8123", "fleet", "2026-10-05", 10, 32 if overrides else 7, out), {
        "memory_gib": 2 if overrides else 8, "seconds": 17 if overrides else 600, "spill_gib": 16 if overrides else 8,
        "pids": (11, 22) if overrides else (), "patterns": (".json", "abc") if overrides else (),
        "queries_out": queries_out if overrides else None,
        "thresholds": (20, 30) if overrides else (), "max_patterns": 1234 if overrides else 500_000,
    })]


def export_fixture(state: SimpleNamespace) -> tuple[bytes, bytes, bytes]:
    header = (dumps({"schema": "hot-frequency-queries-v1", "target": "fleet", "date": "2026-10-05", "threshold_paths": 10, "max_chars": 7}) + "\n").encode()
    first = b'{"chars":1,"pattern":"x","direct_matching_paths":12}\n'
    second = b'{"chars":2,"pattern":"xx","direct_matching_paths":10}\n'
    state.body["lengths"] = [{"chars": 1, "hot_patterns": 1}, {"chars": 2, "hot_patterns": 1}]
    state.tables = [(1, "hot_1"), (2, "hot_2")]
    state.streams = {
        "SELECT 1 AS chars,gram AS pattern,direct_matching_paths FROM hot_1 ORDER BY gram": [first[:9], first[9:]],
        "SELECT 2 AS chars,gram AS pattern,direct_matching_paths FROM hot_2 ORDER BY gram": [second[:-1], second[-1:]],
    }
    return header, first, second


def test_complete_export_preserves_split_stream_records_and_checks_all_layer_counts(mocked_bench: SimpleNamespace, tmp_path: Path) -> None:
    header, first, second = export_fixture(mocked_bench)
    out, queries = tmp_path / "report.json", tmp_path / "queries.jsonl"
    result = hot_frequency_bench.bench("http://node", "fleet", "2026-10-05", 10, 7, out, queries_out=queries)
    expected_export = header + first + second + (dumps({"complete": True, "patterns": 2}) + "\n").encode()
    assert queries.read_bytes() == expected_export
    expected = {
        "schema": "hot-frequency-v1", "threshold_paths": 10,
        "lengths": [{"chars": 1, "hot_patterns": 1}, {"chars": 2, "hot_patterns": 1}],
        "queries": {"path": str(queries), "patterns": 2, "bytes": len(expected_export), "export_s": .25},
        "limits": {"memory_gib": 8, "seconds": 600, "spill_gib": 8, "threads": 4},
        "profile": {"statements": 3, "tracked_peak_memory_bytes": 123, "read_rows": 45, "read_bytes": 67, "summed_query_ms": 89},
        "cache_state": "uncontrolled; offline census, not serving latency",
    }
    assert result == expected
    assert out.read_text() == dumps(expected) + "\n"
    assert mocked_bench.calls == [
        ("create", "http://node", settings()), ("census", mocked_bench.client, "fleet", "2026-10-05", 10, 7, ()),
        ("stream", "SELECT 1 AS chars,gram AS pattern,direct_matching_paths FROM hot_1 ORDER BY gram", "JSONEachRow", {"output_format_json_quote_64bit_integers": 0}),
        ("stream", "SELECT 2 AS chars,gram AS pattern,direct_matching_paths FROM hot_2 ORDER BY gram", "JSONEachRow", {"output_format_json_quote_64bit_integers": 0}),
        ("exec", "SYSTEM FLUSH LOGS"), ("json", PROFILE_SQL), ("close",),
    ]


@pytest.mark.parametrize("failure", ["count", "stream"])
def test_failed_export_keeps_only_unaccepted_partial_bytes_without_completion_or_stats(mocked_bench: SimpleNamespace, tmp_path: Path, failure: str) -> None:
    header, first, second = export_fixture(mocked_bench)
    out, queries = tmp_path / "report.json", tmp_path / "queries.jsonl"
    if failure == "count":
        mocked_bench.body["lengths"][0]["hot_patterns"] = 2
    else:
        mocked_bench.fail = "stream"
    with pytest.raises(RuntimeError) as caught:
        hot_frequency_bench.bench("http://node", "fleet", "2026-10-05", 10, 7, out, queries_out=queries)
    assert str(caught.value) == ("hot query export count disagrees with complete census" if failure == "count" else "census failed")
    assert queries.read_bytes() == header + (first + second if failure == "count" else first[:9])
    assert sorted(tmp_path.iterdir()) == [queries]
    expected = [("create", "http://node", settings()), ("census", mocked_bench.client, "fleet", "2026-10-05", 10, 7, ()),
                ("stream", "SELECT 1 AS chars,gram AS pattern,direct_matching_paths FROM hot_1 ORDER BY gram", "JSONEachRow", {"output_format_json_quote_64bit_integers": 0})]
    if failure == "count":
        expected.append(("stream", "SELECT 2 AS chars,gram AS pattern,direct_matching_paths FROM hot_2 ORDER BY gram", "JSONEachRow", {"output_format_json_quote_64bit_integers": 0}))
    assert mocked_bench.calls == expected + [("close",)]


@pytest.mark.parametrize("kind", ["same", "existing"])
def test_export_target_preflight_refuses_before_client_and_preserves_existing_bytes(mocked_bench: SimpleNamespace, tmp_path: Path, kind: str) -> None:
    out = tmp_path / "report.json"
    queries = out if kind == "same" else tmp_path / "queries.jsonl"
    if kind == "existing":
        queries.write_bytes(b"existing\n")
    with pytest.raises(ValueError) as caught:
        hot_frequency_bench.bench("http://node", "fleet", "2026-10-05", 10, 7, out, queries_out=queries)
    assert str(caught.value) == "hot query export must be a distinct new artifact"
    assert (mocked_bench.calls, mocked_bench.stderr.getvalue()) == ([], "")
    assert sorted(tmp_path.iterdir()) == ([queries] if kind == "existing" else [])
    if kind == "existing":
        assert queries.read_bytes() == b"existing\n"


def test_export_write_failure_closes_client_and_keeps_header_only_without_footer_or_stats(
    mocked_bench: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    header, _, _ = export_fixture(mocked_bench)
    out, queries = tmp_path / "report.json", tmp_path / "queries.jsonl"
    original = Path.open
    error = OSError("fixture disk full")

    class BrokenWriter:
        def __init__(self) -> None:
            self.file = original(queries, "xb")
            self.writes = 0

        def __enter__(self) -> "BrokenWriter":
            return self

        def __exit__(self, *args: object) -> None:
            self.file.close()

        def write(self, value: bytes) -> int:
            self.writes += 1
            if self.writes == 2:
                raise error
            return self.file.write(value)

    def open_file(path: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        return BrokenWriter() if path == queries and mode == "xb" else original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(OSError) as caught:
        hot_frequency_bench.bench("http://node", "fleet", "2026-10-05", 10, 7, out, queries_out=queries)
    assert caught.value is error
    assert queries.read_bytes() == header
    assert sorted(tmp_path.iterdir()) == [queries]
    assert mocked_bench.calls == [
        ("create", "http://node", settings()), ("census", mocked_bench.client, "fleet", "2026-10-05", 10, 7, ()),
        ("stream", "SELECT 1 AS chars,gram AS pattern,direct_matching_paths FROM hot_1 ORDER BY gram", "JSONEachRow", {"output_format_json_quote_64bit_integers": 0}),
        ("close",),
    ]


def test_multiple_thresholds_and_cap_forward_once_to_one_census(mocked_bench: SimpleNamespace, tmp_path: Path) -> None:
    hot_frequency_bench.bench("http://node", "fleet", "2026-10-05", 10, 32, tmp_path / "report.json",
                              thresholds=(10, 30, 100), max_patterns=1234)
    assert mocked_bench.census_options == ((10, 30, 100), 1234)
    assert [call for call in mocked_bench.calls if call[0] == "census"] == [
        ("census", mocked_bench.client, "fleet", "2026-10-05", 10, 32, ()),
    ]


def test_each_layer_exports_before_census_returns_and_no_cap_failure_footer(
    mocked_bench: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    header, first, _ = export_fixture(mocked_bench)
    out, queries = tmp_path / "report.json", tmp_path / "queries.jsonl"
    events = []

    def census(ch: object, *args: object, on_hot_table: Callable[[int, str], None], **kwargs: object) -> dict:
        events.append("census-start")
        on_hot_table(1, "hot_1")
        assert queries.read_bytes() == header + first
        events.append("layer-exported")
        raise RuntimeError("hot-frequency accepted-pattern cap exceeded at length 2: 4 > 3; no complete census")

    monkeypatch.setattr(hot_frequency_bench, "census", census)
    with pytest.raises(RuntimeError) as caught:
        hot_frequency_bench.bench("http://node", "fleet", "2026-10-05", 10, 7, out, queries_out=queries, max_patterns=3)
    assert str(caught.value) == "hot-frequency accepted-pattern cap exceeded at length 2: 4 > 3; no complete census"
    assert events == ["census-start", "layer-exported"]
    assert queries.read_bytes() == header + first
    assert sorted(tmp_path.iterdir()) == [queries]
    assert mocked_bench.calls == [
        ("create", "http://node", settings()),
        ("stream", "SELECT 1 AS chars,gram AS pattern,direct_matching_paths FROM hot_1 ORDER BY gram", "JSONEachRow", {"output_format_json_quote_64bit_integers": 0}),
        ("close",),
    ]
