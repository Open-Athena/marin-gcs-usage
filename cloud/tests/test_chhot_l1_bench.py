from io import StringIO
from json import dumps
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from dt_cloud.chstore import hot_l1_bench
from test_chhot_l1_batch_catalog import stream_artifact


TAG = "hot_l1_bench_" + "b" * 32
PROFILE_SQL = ("SELECT count(),max(memory_usage),sum(read_rows),sum(read_bytes),sum(query_duration_ms) "
               f"FROM system.query_log WHERE log_comment='{TAG}' AND type='QueryFinish'")
PHASE_PROFILE_SQL = ('SELECT log_comment,count(),max(memory_usage),sum(read_rows),sum(read_bytes),sum(query_duration_ms) '
                     f"FROM system.query_log WHERE log_comment IN ('{TAG}','{TAG}_validation','{TAG}_control') "
                     "AND type='QueryFinish' GROUP BY log_comment")
PHASE_PROFILE = {'build': {'statements': 1, 'tracked_peak_memory_bytes': 100, 'read_rows': 20, 'read_bytes': 30, 'summed_query_ms': 40},
                 'validation': {'statements': 1, 'tracked_peak_memory_bytes': 123, 'read_rows': 25, 'read_bytes': 37, 'summed_query_ms': 46},
                 'control': {'statements': 1, 'tracked_peak_memory_bytes': 17, 'read_rows': 0, 'read_bytes': 0, 'summed_query_ms': 3}}


@pytest.fixture
def mocked_bench(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(
        calls=[], client=None, stderr=StringIO(), fail=None,
        error=RuntimeError("offline benchmark failed"), build_action=None,
        body={"schema": "hot-l1-v1", "pattern": ".json", "root": {"b": 8, "o": 3},
              "matching_nonleaf_rows": 0, "all_buckets_covered": False},
        rss={"samples": 2, "sampled_peak_total_rss_bytes": 1000}, phase_tags=[],
        phase_rows=[[TAG, 1, 100, 20, 30, 40], [TAG + '_validation', 1, 123, 25, 37, 46], [TAG + '_control', 1, 17, 0, 0, 3]],
    )

    def check(stage: str) -> None:
        if state.fail == stage:
            raise state.error

    class Client:
        def __init__(self, url: str, **settings: object) -> None:
            state.calls.append(("create", url, settings))
            state.client = self
            self.settings = {'log_comment': settings['log_comment']}

        def exec(self, sql: str) -> None:
            state.calls.append(("exec", sql))
            state.phase_tags.append(('control', self.settings['log_comment']))
            check("flush")

        def json(self, sql: str) -> list:
            state.calls.append(("json", " ".join(sql.split())))
            state.phase_tags.append(('profile', self.settings['log_comment']))
            check("profile")
            if ' '.join(sql.split()) == PHASE_PROFILE_SQL:
                return state.phase_rows
            return [[3, 123, 45, 67, 89]]

        def close(self) -> None:
            state.calls.append(("close",))

    class Monitor:
        def __init__(self, pids: tuple[int, ...], out: Path) -> None:
            state.calls.append(("monitor-create", pids, out))
            check("monitor-create")

        def __enter__(self) -> "Monitor":
            state.calls.append(("monitor-enter",))
            check("monitor-enter")
            return self

        def __exit__(self, *args: object) -> None:
            state.calls.append(("monitor-exit", tuple(arg is not None for arg in args)))
            check("monitor-exit")

        def summary(self) -> dict:
            state.calls.append(("monitor-summary",))
            check("monitor-summary")
            return state.rss

    def build(
        ch: Client,
        target: str,
        date: str,
        pattern: str,
        **caps: object,
    ) -> dict:
        state.calls.append(("build", ch, target, date, pattern, *([caps] if caps else [])))
        state.phase_tags.append(('build', ch.settings['log_comment']))
        check("build")
        if state.build_action is not None:
            state.build_action()
        return dict(state.body)

    def oracle(ch: Client, body: dict) -> bool:
        state.calls.append(("oracle", ch, body))
        state.phase_tags.append(('oracle', ch.settings['log_comment']))
        check("oracle")
        return True

    times = iter([10.0, 10.5])
    monkeypatch.setattr(hot_l1_bench, "Ch", Client)
    monkeypatch.setattr(hot_l1_bench, "build", build)
    monkeypatch.setattr(hot_l1_bench, "oracle", oracle)
    monkeypatch.setattr(hot_l1_bench, "monotonic", lambda: next(times))
    monkeypatch.setattr(hot_l1_bench, "uuid4", lambda: SimpleNamespace(hex="b" * 32))
    monkeypatch.setattr(hot_l1_bench, "stderr", state.stderr)
    state.monitor = Monitor
    return state


def client_settings(
    memory: int = 8,
    seconds: int = 300,
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


@pytest.mark.parametrize("nonleaf,covered", [(0, False), (2, False), (0, True)])
def test_leaf_and_mixed_directory_frontier_oracle_and_exact_artifact(
    mocked_bench: SimpleNamespace,
    tmp_path: Path,
    nonleaf: int,
    covered: bool,
) -> None:
    out = tmp_path / "report.json"
    mocked_bench.body.update(matching_nonleaf_rows=nonleaf, all_buckets_covered=covered)
    result = hot_l1_bench.bench("http://node", "fleet", "2026-10-05", ".JSON", out)
    limits = {"memory_gib": 8, "seconds": 300, "spill_gib": 8, "threads": 4}
    expected = {
        "schema": "hot-l1-v1", "pattern": ".json", "root": {"b": 8, "o": 3},
        "matching_nonleaf_rows": nonleaf, "all_buckets_covered": covered,
        "validation": "complete independent full-path frontier scan", "oracle_s": .5, "limits": limits,
        "profile": {"statements": 3, "tracked_peak_memory_bytes": 123,
                    "read_rows": 45, "read_bytes": 67, "summed_query_ms": 89},
        "cache_state": "uncontrolled; offline construction, not serving latency",
    }
    assert result == expected
    assert out.read_text() == dumps(expected) + "\n"
    calls = [("create", "http://node", client_settings()),
             ("build", mocked_bench.client, "fleet", "2026-10-05", ".JSON"),
             ("oracle", mocked_bench.client, mocked_bench.body)]
    calls.extend([("exec", "SYSTEM FLUSH LOGS"), ("json", PROFILE_SQL), ("close",)])
    assert mocked_bench.calls == calls
    assert mocked_bench.stderr.getvalue() == dumps({"log_comment": TAG, "limits": limits}) + "\n"


def test_explicit_limits_and_rss_monitor_entire_build_oracle_before_profile(
    mocked_bench: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    out = tmp_path / "report.json"
    monkeypatch.setattr(hot_l1_bench, "RssMonitor", mocked_bench.monitor)
    result = hot_l1_bench.bench("http://node", "fleet", "2026-10-05", ".json", out,
                               memory_gib=2, seconds=600, spill_gib=16, pids=(11, 22))
    assert result == {
        "schema": "hot-l1-v1", "pattern": ".json", "root": {"b": 8, "o": 3},
        "matching_nonleaf_rows": 0, "all_buckets_covered": False,
        "validation": "complete independent full-path frontier scan", "oracle_s": .5,
        "limits": {"memory_gib": 2, "seconds": 600, "spill_gib": 16, "threads": 4},
        "profile": {"statements": 3, "tracked_peak_memory_bytes": 123,
                    "read_rows": 45, "read_bytes": 67, "summed_query_ms": 89},
        "cache_state": "uncontrolled; offline construction, not serving latency",
        "rss": {"samples": 2, "sampled_peak_total_rss_bytes": 1000},
    }
    assert out.read_text() == dumps(result) + "\n"
    assert mocked_bench.calls == [
        ("monitor-create", (11, 22), tmp_path / "report.rss.jsonl"),
        ("create", "http://node", client_settings(2, 600, 16)), ("monitor-enter",),
        ("build", mocked_bench.client, "fleet", "2026-10-05", ".json"),
        ("oracle", mocked_bench.client, mocked_bench.body),
        ("monitor-exit", (False, False, False)),
        ("exec", "SYSTEM FLUSH LOGS"), ("json", PROFILE_SQL), ("monitor-summary",), ("close",),
    ]


@pytest.mark.parametrize("kwargs", [
    {"memory_gib": 0}, {"memory_gib": 9}, {"seconds": 0}, {"seconds": 601},
    {"spill_gib": 0}, {"spill_gib": 17},
])
def test_invalid_limits_refuse_before_client_and_artifact_creation(
    mocked_bench: SimpleNamespace,
    tmp_path: Path,
    kwargs: dict,
) -> None:
    out = tmp_path / "report.json"
    with pytest.raises(ValueError) as caught:
        hot_l1_bench.bench("http://node", "fleet", "2026-10-05", ".json", out, **kwargs)
    assert str(caught.value) == "hot L1 census requires 1..8 GiB memory, 1..600 seconds and 1..16 GiB spill"
    assert mocked_bench.calls == []
    assert mocked_bench.stderr.getvalue() == ""
    assert list(tmp_path.iterdir()) == []


def test_existing_artifact_is_preserved_and_refused_before_client_creation(mocked_bench: SimpleNamespace, tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    out.write_text("existing\n")
    with pytest.raises(ValueError) as caught:
        hot_l1_bench.bench("http://node", "fleet", "2026-10-05", ".json", out)
    assert str(caught.value) == "hot L1 artifact already exists"
    assert out.read_text() == "existing\n"
    assert mocked_bench.calls == []
    assert mocked_bench.stderr.getvalue() == ""


@pytest.mark.parametrize("pids", [(0,), (-1,), (1, 1)])
def test_invalid_rss_pids_refuse_before_client_creation(
    mocked_bench: SimpleNamespace,
    tmp_path: Path,
    pids: tuple[int, ...],
) -> None:
    with pytest.raises(ValueError) as caught:
        hot_l1_bench.bench("http://node", "fleet", "2026-10-05", ".json", tmp_path / "report.json", pids=pids)
    assert str(caught.value) == "RSS PIDs must be positive and distinct"
    assert mocked_bench.calls == []
    assert mocked_bench.stderr.getvalue() == ""
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("stage", ["build", "oracle", "flush", "profile"])
def test_query_failures_propagate_close_client_and_do_not_create_artifact(
    mocked_bench: SimpleNamespace,
    tmp_path: Path,
    stage: str,
) -> None:
    mocked_bench.fail = stage
    with pytest.raises(RuntimeError) as caught:
        hot_l1_bench.bench("http://node", "fleet", "2026-10-05", ".json", tmp_path / "report.json")
    assert caught.value is mocked_bench.error
    expected = [("create", "http://node", client_settings()),
                ("build", mocked_bench.client, "fleet", "2026-10-05", ".json")]
    if stage != "build":
        expected.append(("oracle", mocked_bench.client, mocked_bench.body))
    if stage == "flush" or stage == "profile":
        expected.append(("exec", "SYSTEM FLUSH LOGS"))
    if stage == "profile":
        expected.append(("json", PROFILE_SQL))
    expected.append(("close",))
    assert mocked_bench.calls == expected
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("stage", ["monitor-create", "monitor-enter", "monitor-exit", "monitor-summary"])
def test_rss_failures_propagate_and_cleanup_depends_on_client_creation(
    mocked_bench: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
) -> None:
    mocked_bench.fail = stage
    monkeypatch.setattr(hot_l1_bench, "RssMonitor", mocked_bench.monitor)
    with pytest.raises(RuntimeError) as caught:
        hot_l1_bench.bench("http://node", "fleet", "2026-10-05", ".json", tmp_path / "report.json", pids=(1,))
    assert caught.value is mocked_bench.error
    expected = [("monitor-create", (1,), tmp_path / "report.rss.jsonl")]
    if stage != "monitor-create":
        expected.extend([("create", "http://node", client_settings()), ("monitor-enter",)])
    if stage == "monitor-exit" or stage == "monitor-summary":
        expected.extend([
            ("build", mocked_bench.client, "fleet", "2026-10-05", ".json"),
            ("oracle", mocked_bench.client, mocked_bench.body), ("monitor-exit", (False, False, False)),
        ])
    if stage == "monitor-summary":
        expected.extend([("exec", "SYSTEM FLUSH LOGS"), ("json", PROFILE_SQL), ("monitor-summary",)])
    if stage != "monitor-create":
        expected.append(("close",))
    assert mocked_bench.calls == expected
    assert list(tmp_path.iterdir()) == []


def batch_reference(
    tmp_path: Path,
    mocked_bench: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict]:
    body = stream_artifact()
    path = tmp_path / "accepted-native.json"
    path.write_text(dumps(body) + "\n")
    row = body["results"][0]
    mocked_bench.body = {**mocked_bench.body, "target": body["target"], "date": body["date"],
                         "snapshot_db": body["snapshot_db"], "root": deepcopy(row["root"]),
                         "buckets": deepcopy(row["buckets"]),
                         "stages": {"vocabulary_s": .1, "directory_roots_s": .2, "aggregate_s": .3}, "build_s": .6}
    times = iter([1.0, 1.25, 10.0, 10.5])
    monkeypatch.setattr(hot_l1_bench, "monotonic", lambda: next(times))
    return path, body


def test_native_reference_checks_complete_zero_byte_bucket_and_preserves_separate_stage_timings(
    mocked_bench: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path, reference = batch_reference(tmp_path, mocked_bench, monkeypatch)
    out = tmp_path / "seam.json"
    result = hot_l1_bench.bench("http://node", "fleet", "2026-10-05", ".JSON", out,
                               seconds=60, reference_batch=path)
    assert result == {
        **mocked_bench.body,
        "validation": "exact agreement with accepted native batch reference; not independent full-source oracle",
        "validation_s": .5, "reference_load_s": .25,
        "reference_batch": {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest(),
                            "bytes": path.stat().st_size, "artifact_schema": "hot-l1-batch-stream-v1",
                            "snapshot_db": "snapshot_20261005", "validation": reference["validation"]},
        "limits": {"memory_gib": 8, "seconds": 60, "spill_gib": 8, "threads": 4},
        "profile": {"statements": 3, "tracked_peak_memory_bytes": 123,
                    "read_rows": 45, "read_bytes": 67, "summed_query_ms": 89},
        "cache_state": "uncontrolled; offline construction, not serving latency",
    }
    assert out.read_text() == dumps(result) + "\n"
    assert mocked_bench.calls == [
        ("create", "http://node", client_settings(seconds=60)),
        ("build", mocked_bench.client, "fleet", "2026-10-05", ".JSON"),
        ("exec", "SYSTEM FLUSH LOGS"), ("json", PROFILE_SQL), ("close",),
    ]


@pytest.mark.parametrize("change", [
    lambda b: b["root"].update(o=6),
    lambda b: (b["buckets"][0].update(o=2), b["buckets"][1].update(o=3)),
    lambda b: b["buckets"].pop(),
    lambda b: b["buckets"][1].update(post=9),
    lambda b: b.update(snapshot_db="wrong_snapshot"),
    lambda b: b.update(pattern=".npy"),
])
def test_reference_mismatch_refuses_no_oracle_no_artifact_and_closes_client(
    mocked_bench: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    change: object,
) -> None:
    path, _ = batch_reference(tmp_path, mocked_bench, monkeypatch)
    change(mocked_bench.body)
    out = tmp_path / "seam.json"
    with pytest.raises(AssertionError) as caught:
        hot_l1_bench.bench("http://node", "fleet", "2026-10-05", ".json", out, reference_batch=path)
    assert str(caught.value) == "hot L1 aggregate disagrees with the complete accepted native batch reference"
    assert mocked_bench.calls == [("create", "http://node", client_settings()),
                                 ("build", mocked_bench.client, "fleet", "2026-10-05", ".json"), ("close",)]
    assert sorted(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("target,date,pattern,change,message", [
    ("other", "2026-10-05", ".json", lambda b: None, "hot L1 batch reference target differs from requested target"),
    ("fleet", "2026-10-04", ".json", lambda b: None, "hot L1 batch pattern/date is not registered; no scan fallback"),
    ("fleet", "2026-10-05", "cold", lambda b: None, "hot L1 batch pattern/date is not registered; no scan fallback"),
    ("fleet", "2026-10-05", ".json", lambda b: b.update(compiled_patterns=3), "batch catalog registry/result counts disagree"),
    ("fleet", "2026-10-05", ".json", lambda b: (b.update(schema="hot-l1-batch-sql-v1", engine="sql"), b.pop("native")),
     "hot L1 batch reference requires a completed native stream artifact"),
])
def test_reference_invalid_identity_or_incomplete_native_artifact_refuses_before_ch(
    mocked_bench: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target: str,
    date: str,
    pattern: str,
    change: object,
    message: str,
) -> None:
    path, body = batch_reference(tmp_path, mocked_bench, monkeypatch)
    change(body)
    path.write_text(dumps(body) + "\n")
    with pytest.raises(ValueError) as caught:
        hot_l1_bench.bench("http://node", target, date, pattern, tmp_path / "seam.json", reference_batch=path)
    assert str(caught.value) == message
    assert mocked_bench.calls == []
    assert mocked_bench.stderr.getvalue() == ""
    assert sorted(tmp_path.iterdir()) == [path]


def test_reference_is_pinned_before_build_changes_its_path(
    mocked_bench: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path, _ = batch_reference(tmp_path, mocked_bench, monkeypatch)
    raw = path.read_bytes()
    mocked_bench.build_action = lambda: path.write_text("truncated replacement\n")
    result = hot_l1_bench.bench("http://node", "fleet", "2026-10-05", ".json", tmp_path / "seam.json", reference_batch=path)
    assert result["reference_batch"]["sha256"] == sha256(raw).hexdigest()
    assert result["reference_batch"]["bytes"] == len(raw)
    assert path.read_text() == "truncated replacement\n"


@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("capped", [False, True])
def test_cli_reference_and_default_forwarding_exact_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    selected: bool,
    capped: bool,
) -> None:
    from dt_cloud.cli import main

    calls = []
    output = {"fixture": "reference" if selected else "oracle"}

    def bench(*args: object, **kwargs: object) -> dict:
        calls.append((args, kwargs))
        return output

    monkeypatch.setattr(hot_l1_bench, "bench", bench)
    out, reference = tmp_path / "seam.json", tmp_path / "native.json"
    args = ["ch-hot-l1-bench", "fleet", "-d", "2026-10-05", "-n", ".json", "-o", str(out), "-w", "60"]
    if selected:
        args.extend(["-r", str(reference)])
    if capped:
        args.extend(['-N', '200000', '-P', '100000', '-R', '100000'])
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0
    assert result.output == dumps(output) + "\n"
    assert calls == [(("http://localhost:8123", "fleet", "2026-10-05", ".json", out),
                      {"memory_gib": 8, "seconds": 60, "spill_gib": 8, "pids": (),
                       "reference_batch": reference if selected else None,
                       **({'max_names': 200000, 'max_postings': 100000, 'max_roots': 100000} if capped else {})})]


def test_bounded_bench_forwards_exact_caps_preserves_count_metadata_and_validation(mocked_bench: SimpleNamespace, tmp_path: Path) -> None:
    state = mocked_bench
    state.body.update(work_bounds={'max_names': 20, 'max_postings': 30, 'max_outer_roots': 10}, direct_matching_rows=12)
    out = tmp_path / 'bounded.json'
    body = hot_l1_bench.bench('http://node', 'fleet', '2026-10-05', '.json', out,
                              max_names=20, max_postings=30, max_roots=10)
    assert body == {**state.body, 'validation': 'complete independent full-path frontier scan', 'oracle_s': .5,
                   'limits': {'memory_gib': 8, 'seconds': 300, 'spill_gib': 8, 'threads': 4},
                   'profile': {'statements': 3, 'tracked_peak_memory_bytes': 123, 'read_rows': 45, 'read_bytes': 67, 'summed_query_ms': 89},
                   'cache_state': 'uncontrolled; offline construction, not serving latency', 'profile_by_phase': PHASE_PROFILE}
    assert out.read_text() == dumps(body) + '\n'
    assert state.calls == [('create', 'http://node', client_settings()),
                          ('build', state.client, 'fleet', '2026-10-05', '.json', {'max_names': 20, 'max_postings': 30, 'max_roots': 10}),
                          ('oracle', state.client, state.body), ('exec', 'SYSTEM FLUSH LOGS'), ('json', PHASE_PROFILE_SQL), ('close',)]
    assert state.phase_tags == [('build', TAG), ('oracle', TAG + '_validation'), ('control', TAG + '_control'), ('profile', TAG + '_control')]
    assert state.client.settings == {'log_comment': TAG}


def test_bounded_scan_free_reference_validation_has_zero_query_reads_and_separate_build_profile(mocked_bench: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path, _ = batch_reference(tmp_path, mocked_bench, monkeypatch)
    state = mocked_bench
    state.phase_rows = [[TAG + '_control', 1, 17, 0, 0, 3], [TAG, 2, 100, 45, 67, 86]]
    body = hot_l1_bench.bench('http://node', 'fleet', '2026-10-05', '.json', tmp_path / 'bounded.json', max_postings=100, reference_batch=path)
    assert body['profile'] == {'statements': 3, 'tracked_peak_memory_bytes': 100, 'read_rows': 45, 'read_bytes': 67, 'summed_query_ms': 89}
    assert body['profile_by_phase'] == {
        'build': {'statements': 2, 'tracked_peak_memory_bytes': 100, 'read_rows': 45, 'read_bytes': 67, 'summed_query_ms': 86},
        'validation': {'statements': 0, 'tracked_peak_memory_bytes': 0, 'read_rows': 0, 'read_bytes': 0, 'summed_query_ms': 0},
        'control': {'statements': 1, 'tracked_peak_memory_bytes': 17, 'read_rows': 0, 'read_bytes': 0, 'summed_query_ms': 3},
    }
    assert state.phase_tags == [('build', TAG), ('control', TAG + '_control'), ('profile', TAG + '_control')]
    assert (body['validation_s'], state.client.settings) == (.5, {'log_comment': TAG})


def test_bounded_oracle_failure_restores_owned_log_setting_and_closes_without_artifact(mocked_bench: SimpleNamespace, tmp_path: Path) -> None:
    state = mocked_bench
    state.fail = 'oracle'
    out = tmp_path / 'bounded.json'
    with pytest.raises(RuntimeError) as caught:
        hot_l1_bench.bench('http://node', 'fleet', '2026-10-05', '.json', out, max_postings=100)
    assert caught.value is state.error
    assert (state.phase_tags, state.client.settings, out.exists()) == ([('build', TAG), ('oracle', TAG + '_validation')], {'log_comment': TAG}, False)
    assert state.calls == [('create', 'http://node', client_settings()),
                          ('build', state.client, 'fleet', '2026-10-05', '.json', {'max_postings': 100}),
                          ('oracle', state.client, state.body), ('close',)]


@pytest.mark.parametrize('kwargs,message', [
    ({'max_names': True}, 'hot L1 vocabulary budget must be a positive integer when supplied'),
    ({'max_postings': 0}, 'hot L1 direct-posting budget must be a positive integer when supplied'),
    ({'max_roots': 1.5}, 'hot L1 outer-directory budget must be positive when supplied'),
])
def test_bench_invalid_work_guards_fail_before_reference_client_or_output(mocked_bench: SimpleNamespace, tmp_path: Path, kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError) as caught:
        hot_l1_bench.bench('http://node', 'fleet', '2026-10-05', '.json', tmp_path / 'out.json',
                           reference_batch=tmp_path / 'must-not-read.json', **kwargs)
    assert (str(caught.value), mocked_bench.calls, mocked_bench.stderr.getvalue(), list(tmp_path.iterdir())) == (message, [], '', [])


def test_exclusive_artifact_write_preserves_a_file_created_during_workload(mocked_bench: SimpleNamespace, tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    mocked_bench.build_action = lambda: out.write_text("concurrent writer\n")
    with pytest.raises(FileExistsError):
        hot_l1_bench.bench("http://node", "fleet", "2026-10-05", ".json", out)
    assert out.read_text() == "concurrent writer\n"
    assert mocked_bench.calls == [
        ("create", "http://node", client_settings()),
        ("build", mocked_bench.client, "fleet", "2026-10-05", ".json"),
        ("oracle", mocked_bench.client, mocked_bench.body),
        ("exec", "SYSTEM FLUSH LOGS"), ("json", PROFILE_SQL), ("close",),
    ]


@pytest.mark.parametrize("stage", ["build", "oracle"])
def test_rss_context_receives_workload_failure_and_client_is_closed(
    mocked_bench: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
) -> None:
    mocked_bench.fail = stage
    monkeypatch.setattr(hot_l1_bench, "RssMonitor", mocked_bench.monitor)
    with pytest.raises(RuntimeError) as caught:
        hot_l1_bench.bench("http://node", "fleet", "2026-10-05", ".json", tmp_path / "report.json", pids=(1,))
    assert caught.value is mocked_bench.error
    expected = [
        ("monitor-create", (1,), tmp_path / "report.rss.jsonl"),
        ("create", "http://node", client_settings()), ("monitor-enter",),
        ("build", mocked_bench.client, "fleet", "2026-10-05", ".json"),
    ]
    if stage == "oracle":
        expected.append(("oracle", mocked_bench.client, mocked_bench.body))
    expected.extend([("monitor-exit", (True, True, True)), ("close",)])
    assert mocked_bench.calls == expected
    assert list(tmp_path.iterdir()) == []
from copy import deepcopy
from hashlib import sha256
