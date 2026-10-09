from io import StringIO
from json import dumps, loads
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import hot_l1_batch_bench as module
from dt_cloud.chstore.hot_l1_catalog import SCOPE, VALIDATION


DATE = "2026-10-05"
TAG = "hot_l1_batch_" + "b" * 32
HEADER = {"schema": "hot-frequency-queries-v1", "target": "fleet", "date": DATE,
          "threshold_paths": 10, "max_chars": 7}
BUCKETS = [{"pre": 1, "post": 4, "path": "a", "b": 8, "o": 3},
           {"pre": 5, "post": 8, "path": "b", "b": 0, "o": 2}]
ROOT = {"b": 8, "o": 5}
PROFILE_SQL = ("SELECT count(),max(memory_usage),sum(read_rows),sum(read_bytes),sum(query_duration_ms) "
               f"FROM system.query_log WHERE log_comment='{TAG}' AND type='QueryFinish'")


def export(path: Path, patterns: tuple[str, ...] = (".json", ".npy")) -> list[dict]:
    rows = [dict(HEADER), *[{"chars": len(p), "pattern": p, "direct_matching_paths": 10} for p in patterns],
            {"complete": True, "patterns": len(patterns)}]
    write_rows(path, rows)
    return rows


def write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(dumps(row) + "\n" for row in rows))


@pytest.mark.parametrize("pattern", ["zarr.json", "å" * 32])
def test_long_completed_registry_loads_without_shortening_literal(tmp_path: Path, pattern: str) -> None:
    path = tmp_path / "long.jsonl"
    header = {**HEADER, "max_chars": 32}
    write_rows(path, [header, {"chars": len(pattern), "pattern": pattern, "direct_matching_paths": 10},
                     {"complete": True, "patterns": 1}])
    assert module.load_queries(path, "fleet", DATE) == (header, (pattern,))


def reference(path: Path, pattern: str, validation: str = VALIDATION) -> dict:
    body = {"schema": "hot-l1-v1", "target": "fleet", "date": DATE, "pattern": pattern,
            "exact": True, "incremental": False, "scope": SCOPE, "validation": validation,
            "root": ROOT, "buckets": BUCKETS, "matching_nonleaf_rows": 0, "all_buckets_covered": False}
    path.write_text(dumps(body) + "\n")
    return body


def settings(memory: int = 8, seconds: int = 600, spill: int = 8) -> dict:
    return {"db": "fleet", "timeout": seconds + 60, "max_threads": 4,
            "max_memory_usage": memory << 30, "max_execution_time": seconds,
            "timeout_before_checking_execution_speed": 0, "timeout_overflow_mode": "throw",
            "max_bytes_before_external_sort": 256 << 20, "max_bytes_ratio_before_external_sort": 0,
            "max_bytes_before_external_group_by": 256 << 20, "max_bytes_ratio_before_external_group_by": 0,
            "max_temporary_data_on_disk_size_for_query": spill << 30, "log_comment": TAG}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(calls=[], stderr=StringIO(), fail=None, change=None, client=None)

    class Client:
        def __init__(self, url: str, **kwargs: object) -> None:
            state.calls.append(("create", url, kwargs))
            state.client = self

        def exec(self, sql: str) -> None:
            state.calls.append(("exec", sql))
            if state.fail == "flush":
                raise RuntimeError("failed flush")

        def json(self, sql: str) -> list:
            state.calls.append(("json", " ".join(sql.split())))
            if state.fail == "profile":
                raise RuntimeError("failed profile")
            return [[3, 123, 45, 67, 89]]

        def close(self) -> None:
            state.calls.append(("close",))

    def build(ch: Client, target: str, date: str, patterns: tuple[str, ...]) -> dict:
        state.calls.append(("build", ch, target, date, patterns))
        if state.fail == "build":
            raise RuntimeError("failed build")
        body = {"schema": "hot-l1-batch-sql-v1", "target": target, "date": date,
                "exact": True, "incremental": False, "scope": SCOPE,
                "results": [{"predicate_id": i, "pattern": p, "root": dict(ROOT),
                             "buckets": [dict(row) for row in BUCKETS]} for i, p in enumerate(patterns, 1)]}
        if state.change is not None:
            state.change(body)
        state.body = body
        return body

    monkeypatch.setattr(module, "Ch", Client)
    monkeypatch.setattr(module, "build", build)
    monkeypatch.setattr(module, "uuid4", lambda: SimpleNamespace(hex="b" * 32))
    monkeypatch.setattr(module, "stderr", state.stderr)
    return state


def test_complete_artifact_default_budgets_and_both_reference_types(fake: SimpleNamespace, tmp_path: Path) -> None:
    queries, out = tmp_path / "queries.jsonl", tmp_path / "report.json"
    export(queries)
    refs = (tmp_path / "json.json", tmp_path / "npy.json")
    reference(refs[0], ".json")
    reference(refs[1], ".npy", module.LEAF_VALIDATION)
    result = module.bench("http://node", "fleet", DATE, queries, out, references=refs)
    limits = {"memory_gib": 8, "seconds": 600, "spill_gib": 8, "threads": 4}
    expected = {**fake.body, "engine": "sql", "queries": {"path": str(queries), "patterns": 2, "header": HEADER, "registry_date": DATE},
                "validation": {"description": "complete catalog structure; supplied trusted references checked, not an independent full-catalog scan",
                               "references": [{"path": str(refs[0]), "pattern": ".json", "validation": VALIDATION},
                                              {"path": str(refs[1]), "pattern": ".npy", "validation": module.LEAF_VALIDATION}],
                               "independently_scanned_entire_catalog": False},
                "limits": limits, "profile": {"statements": 3, "tracked_peak_memory_bytes": 123,
                                              "read_rows": 45, "read_bytes": 67, "summed_query_ms": 89},
                "cache_state": "uncontrolled; offline construction, not serving latency"}
    assert result == expected
    assert out.read_text() == dumps(expected) + "\n"
    assert fake.calls == [("create", "http://node", settings()), ("build", fake.client, "fleet", DATE, (".json", ".npy")),
                          ("exec", "SYSTEM FLUSH LOGS"), ("json", PROFILE_SQL), ("close",)]
    assert fake.stderr.getvalue() == dumps({"log_comment": TAG, "limits": limits}) + "\n"
    assert loads(refs[1].read_text())["validation"] == module.LEAF_VALIDATION


def test_no_256_cap_unicode_preserved_and_explicit_rss_budgets(fake: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    queries, out = tmp_path / "queries.jsonl", tmp_path / "report.json"
    patterns = tuple(str(i) for i in range(300)) + ("åro", "🙂", "\n")
    export(queries, patterns)

    class Monitor:
        def __init__(self, pids: tuple[int, ...], path: Path) -> None:
            fake.calls.append(("monitor", pids, path))

        def __enter__(self) -> None:
            fake.calls.append(("enter",))

        def __exit__(self, *args: object) -> None:
            fake.calls.append(("exit", tuple(arg is not None for arg in args)))

        def summary(self) -> dict:
            fake.calls.append(("summary",))
            return {"samples": 2, "sampled_peak_total_rss_bytes": 1000}

    monkeypatch.setattr(module, "RssMonitor", Monitor)
    result = module.bench("http://node", "fleet", DATE, queries, out, memory_gib=2, seconds=3600, spill_gib=16, pids=(11,))
    assert result["queries"] == {"path": str(queries), "patterns": 303, "header": HEADER, "registry_date": DATE}
    assert result["rss"] == {"samples": 2, "sampled_peak_total_rss_bytes": 1000}
    assert result["limits"] == {"memory_gib": 2, "seconds": 3600, "spill_gib": 16, "threads": 4}
    assert result["validation"] == {"description": "complete catalog structure; supplied trusted references checked, not an independent full-catalog scan",
                                    "references": [], "independently_scanned_entire_catalog": False}
    assert loads(out.read_text()) == result
    assert fake.calls == [("monitor", (11,), out.with_suffix(".rss.jsonl")), ("create", "http://node", settings(2, 3600, 16)),
                          ("enter",), ("build", fake.client, "fleet", DATE, patterns), ("exit", (False, False, False)),
                          ("exec", "SYSTEM FLUSH LOGS"), ("json", PROFILE_SQL), ("summary",), ("close",)]


@pytest.mark.parametrize("change", [
    lambda rows: rows.pop(),
    lambda rows: rows[-1].update(patterns=3),
    lambda rows: rows[-1].update(complete=False),
    lambda rows: rows.append({"complete": True, "patterns": 2}),
    lambda rows: rows[0].update(date="2026-10-04"),
    lambda rows: rows[0].update(threshold_paths=0),
    lambda rows: rows[0].update(max_chars=33),
])
def test_incomplete_or_wrong_export_refused_before_ch(fake: SimpleNamespace, tmp_path: Path, change: object) -> None:
    queries, out = tmp_path / "queries.jsonl", tmp_path / "report.json"
    rows = export(queries)
    change(rows)
    write_rows(queries, rows)
    with pytest.raises(ValueError):
        module.bench("http://node", "fleet", DATE, queries, out)
    assert fake.calls == []
    assert fake.stderr.getvalue() == ""
    assert out.exists() is False


@pytest.mark.parametrize("pattern,count,chars", [(".JSON", 10, 5), ("a/b", 10, 3), ("a\0", 10, 2),
                                               (".json", 9, 5), (".json", 10, 4), (".json", True, 5)])
def test_invalid_literals_frequency_or_character_counts_refused(fake: SimpleNamespace, tmp_path: Path, pattern: str, count: int, chars: int) -> None:
    queries = tmp_path / "queries.jsonl"
    rows = export(queries)
    rows[1] = {"pattern": pattern, "direct_matching_paths": count, "chars": chars}
    write_rows(queries, rows)
    with pytest.raises(ValueError) as caught:
        module.bench("http://node", "fleet", DATE, queries, tmp_path / "report.json")
    assert str(caught.value) == "hot query export literals must be unique, normalized, NUL/slash-free hot queries of lengths 1..32"
    assert fake.calls == []


def test_duplicate_query_and_duplicate_json_keys_refused(fake: SimpleNamespace, tmp_path: Path) -> None:
    queries = tmp_path / "queries.jsonl"
    export(queries, ("a", "a"))
    with pytest.raises(ValueError) as caught:
        module.load_queries(queries, "fleet", DATE)
    assert str(caught.value) == "hot query export literals must be unique, normalized, NUL/slash-free hot queries of lengths 1..32"
    queries.write_text('{"schema":"hot-frequency-queries-v1","schema":"hot-frequency-queries-v1"}\n')
    with pytest.raises(ValueError) as caught:
        module.load_queries(queries, "fleet", DATE)
    assert str(caught.value) == "hot L1 artifact contains duplicate JSON keys"
    assert fake.calls == []


@pytest.mark.parametrize("kwargs", [{"memory_gib": 0}, {"seconds": 3601}, {"spill_gib": 17}])
def test_budget_and_existing_output_preflight(fake: SimpleNamespace, tmp_path: Path, kwargs: dict) -> None:
    out = tmp_path / "report.json"
    with pytest.raises(ValueError) as caught:
        module.bench("http://node", "fleet", DATE, tmp_path / "absent", out, **kwargs)
    assert str(caught.value) == "hot L1 batch requires 1..8 GiB memory, 1..3600 seconds and 1..16 GiB spill"
    out.write_text("existing\n")
    with pytest.raises(ValueError) as caught:
        module.bench("http://node", "fleet", DATE, tmp_path / "absent", out)
    assert str(caught.value) == "hot L1 batch output artifacts must be new"
    assert out.read_text() == "existing\n"
    assert fake.calls == []


@pytest.mark.parametrize("change", [lambda body: body.update(matching_nonleaf_rows=1),
                                     lambda body: body.update(all_buckets_covered=True),
                                     lambda body: body.update(date="2026-10-04"),
                                     lambda body: body.update(pattern="absent"),
                                     lambda body: body.update(validation="not verified")])
def test_unacceptable_reference_refused_before_ch(fake: SimpleNamespace, tmp_path: Path, change: object) -> None:
    queries, ref = tmp_path / "queries.jsonl", tmp_path / "ref.json"
    export(queries)
    body = reference(ref, ".npy", module.LEAF_VALIDATION)
    change(body)
    ref.write_text(dumps(body))
    with pytest.raises(ValueError):
        module.bench("http://node", "fleet", DATE, queries, tmp_path / "report.json", references=(ref,))
    assert fake.calls == []


def test_reference_disagreement_even_with_equal_root_closes_without_artifact(fake: SimpleNamespace, tmp_path: Path) -> None:
    queries, ref, out = tmp_path / "queries.jsonl", tmp_path / "ref.json", tmp_path / "report.json"
    export(queries)
    reference(ref, ".json")

    def change(body: dict) -> None:
        body["results"][0]["buckets"][0]["o"] = 2
        body["results"][0]["buckets"][1]["o"] = 3

    fake.change = change
    with pytest.raises(AssertionError) as caught:
        module.bench("http://node", "fleet", DATE, queries, out, references=(ref,))
    assert str(caught.value) == f"native batch disagrees with complete reference: {ref}"
    assert fake.calls == [("create", "http://node", settings()), ("build", fake.client, "fleet", DATE, (".json", ".npy")), ("close",)]
    assert out.exists() is False


@pytest.mark.parametrize("stage", ["build", "flush", "profile", "incomplete"])
def test_failed_or_incomplete_build_closes_without_accepted_artifact(fake: SimpleNamespace, tmp_path: Path, stage: str) -> None:
    queries, out = tmp_path / "queries.jsonl", tmp_path / "report.json"
    export(queries)
    fake.fail = stage
    if stage == "incomplete":
        fake.change = lambda body: body["results"].pop()
    with pytest.raises(RuntimeError) as caught:
        module.bench("http://node", "fleet", DATE, queries, out)
    assert str(caught.value) == ("native batch did not return the complete ordered query catalog" if stage == "incomplete" else f"failed {stage}")
    expected = [("create", "http://node", settings()), ("build", fake.client, "fleet", DATE, (".json", ".npy"))]
    if stage == "flush" or stage == "profile":
        expected.append(("exec", "SYSTEM FLUSH LOGS"))
    if stage == "profile":
        expected.append(("json", PROFILE_SQL))
    expected.append(("close",))
    assert fake.calls == expected
    assert out.exists() is False


def test_completed_empty_catalog_is_valid_but_benchmark_refuses_before_ch(fake: SimpleNamespace, tmp_path: Path) -> None:
    queries = tmp_path / "queries.jsonl"
    export(queries, ())
    assert module.load_queries(queries, "fleet", DATE) == (HEADER, ())
    with pytest.raises(ValueError) as caught:
        module.bench("http://node", "fleet", DATE, queries, tmp_path / "report.json")
    assert str(caught.value) == "completed hot query catalog contains no queries"
    assert fake.calls == []


def test_explicit_registry_date_reuses_queries_for_older_scan_without_rewriting_provenance(fake: SimpleNamespace, tmp_path: Path) -> None:
    queries, out = tmp_path / "queries.jsonl", tmp_path / "report.json"
    export(queries)
    original = queries.read_text()
    result = module.bench("http://node", "fleet", "2026-10-04", queries, out, registry_date=DATE)
    assert result["date"] == "2026-10-04"
    assert result["queries"] == {"path": str(queries), "patterns": 2, "header": HEADER, "registry_date": DATE}
    assert queries.read_text() == original
    assert loads(out.read_text()) == result
    assert fake.calls == [("create", "http://node", settings()), ("build", fake.client, "fleet", "2026-10-04", (".json", ".npy")),
                          ("exec", "SYSTEM FLUSH LOGS"), ("json", PROFILE_SQL), ("close",)]


def test_output_write_failure_removes_only_owned_artifact_and_closes(fake: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    queries, out = tmp_path / "queries.jsonl", tmp_path / "report.json"
    export(queries)
    original = Path.open

    class BrokenOutput:
        def __init__(self) -> None:
            self.file = original(out, "x")

        def __enter__(self) -> "BrokenOutput":
            return self

        def write(self, value: str) -> None:
            self.file.write(value[:10])
            raise OSError("disk full")

        def __exit__(self, *args: object) -> None:
            self.file.close()

    def open_path(path: Path, *args: object, **kwargs: object) -> object:
        if path == out and args == ("x",):
            return BrokenOutput()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_path)
    with pytest.raises(OSError) as caught:
        module.bench("http://node", "fleet", DATE, queries, out)
    assert str(caught.value) == "disk full"
    assert fake.calls == [("create", "http://node", settings()), ("build", fake.client, "fleet", DATE, (".json", ".npy")),
                          ("exec", "SYSTEM FLUSH LOGS"), ("json", PROFILE_SQL), ("close",)]
    assert out.exists() is False
    assert module.load_queries(queries, "fleet", DATE) == (HEADER, (".json", ".npy"))


@pytest.mark.parametrize("overrides", [False, True])
def test_cli_exact_forwarding_and_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, overrides: bool) -> None:
    from dt_cloud.cli import main

    calls = []

    def bench(*args: object, **kwargs: object) -> dict:
        calls.append((args, kwargs))
        return {"schema": "batch-fixture", "compiled_patterns": 303}

    monkeypatch.setattr(module, "bench", bench)
    queries, out = tmp_path / "queries.jsonl", tmp_path / "report.json"
    refs = (tmp_path / "json.json", tmp_path / "npy.json")
    binary = tmp_path / "native"
    args = ["ch-hot-l1-batch-bench", "fleet", "-d", DATE, "-q", str(queries), "-o", str(out)]
    if overrides:
        args.extend(["-m", "2", "-p", "11", "-p", "22", "-r", str(refs[0]), "-r", str(refs[1]),
                     "-s", "16", "-w", "3600", "-U", "http://node", "-g", "2026-10-04", "-e", "stream", "-b", str(binary)])
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert (result.stdout, result.stderr) == (dumps({"schema": "batch-fixture", "compiled_patterns": 303}) + "\n", "")
    assert calls == [(("http://node" if overrides else "http://localhost:8123", "fleet", DATE, queries, out), {
        "memory_gib": 2 if overrides else 8, "seconds": 3600 if overrides else 600, "spill_gib": 16 if overrides else 8,
        "pids": (11, 22) if overrides else (), "references": refs if overrides else (),
        "registry_date": "2026-10-04" if overrides else None,
        "engine": "stream" if overrides else "sql", "binary": binary if overrides else None,
    })]


@pytest.mark.parametrize("failure", [False, True])
def test_explicit_stream_dispatch_reference_checks_and_child_failure_propagation(fake: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: bool) -> None:
    queries, out, binary, ref = (tmp_path / name for name in ("queries.jsonl", "report.json", "native", "ref.json"))
    export(queries)
    reference(ref, ".json")
    binary.write_text("private executable placeholder\n")
    monkeypatch.setattr(module, "access", lambda path, mode: True)
    body = {"schema": "hot-l1-batch-stream-v1", "target": "fleet", "date": DATE,
            "exact": True, "incremental": False, "scope": SCOPE,
            "results": [{"predicate_id": i, "pattern": pattern, "root": ROOT, "buckets": BUCKETS}
                        for i, pattern in enumerate((".json", ".npy"), 1)],
            "source_query_id": "own-source-query", "native": {"native_peak_rss_bytes": 1234}}

    def stream_build(ch: object, target: str, date: str, patterns: tuple[str, ...], *, binary: Path) -> dict:
        fake.calls.append(("stream-build", ch, target, date, patterns, binary))
        if failure:
            raise RuntimeError("native builder failed")
        return body

    monkeypatch.setattr(module, "build_stream", stream_build)
    kwargs = {"engine": "stream", "binary": binary, "references": (ref,)}
    expected_calls = [("create", "http://node", settings()),
                      ("stream-build", fake.client, "fleet", DATE, (".json", ".npy"), binary)]
    if failure:
        with pytest.raises(RuntimeError) as caught:
            module.bench("http://node", "fleet", DATE, queries, out, **kwargs)
        assert str(caught.value) == "native builder failed"
        expected_calls[1] = ("stream-build", fake.client, "fleet", DATE, (".json", ".npy"), binary)
        expected_calls.append(("close",))
        assert fake.calls == expected_calls
        assert out.exists() is False
        return
    result = module.bench("http://node", "fleet", DATE, queries, out, **kwargs)
    expected = {**body, "engine": "stream", "native_binary": str(binary.resolve()),
                "queries": {"path": str(queries), "patterns": 2, "header": HEADER, "registry_date": DATE},
                "validation": {"description": "complete catalog structure; supplied trusted references checked, not an independent full-catalog scan",
                               "references": [{"path": str(ref), "pattern": ".json", "validation": VALIDATION}],
                               "independently_scanned_entire_catalog": False},
                "limits": {"memory_gib": 8, "seconds": 600, "spill_gib": 8, "threads": 4},
                "profile": {"statements": 3, "tracked_peak_memory_bytes": 123, "read_rows": 45, "read_bytes": 67, "summed_query_ms": 89},
                "cache_state": "uncontrolled; offline construction, not serving latency"}
    assert result == expected
    assert loads(out.read_text()) == expected
    expected_calls[1] = ("stream-build", fake.client, "fleet", DATE, (".json", ".npy"), binary)
    expected_calls.extend([("exec", "SYSTEM FLUSH LOGS"), ("json", PROFILE_SQL), ("close",)])
    assert fake.calls == expected_calls


@pytest.mark.parametrize("engine,binary,message", [
    ("other", None, "hot L1 batch engine must be sql or stream"),
    ("stream", None, "stream engine requires an explicit existing executable"),
    ("stream", "absent", "stream engine requires an explicit existing executable"),
    ("sql", "native", "a native binary requires the stream engine"),
])
def test_engine_preflight_before_source_or_client(fake: SimpleNamespace, tmp_path: Path, engine: str, binary: str | None, message: str) -> None:
    with pytest.raises(ValueError) as caught:
        module.bench("http://node", "fleet", DATE, tmp_path / "absent-queries", tmp_path / "report.json",
                     engine=engine, binary=tmp_path / binary if binary else None)
    assert str(caught.value) == message
    assert fake.calls == []
    assert fake.stderr.getvalue() == ""
