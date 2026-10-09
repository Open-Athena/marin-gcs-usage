"""Read-only ingestion-plan experiments keep publication out of the harness."""

from hashlib import sha256
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from dt_cloud.chstore import ingest_bench as bench
from dt_cloud.chstore.ingest import Ingest, IngestError, range_settings
from dt_cloud.cli import main


@pytest.fixture
def inputs(monkeypatch):
    monkeypatch.setattr(bench, "scan_epochs", lambda ch: [
        ("2026-10-03", "2026-10-03 00:00:00", 2, 8, "2026-10-03 00:00:00"),
        ("2026-10-04", "2026-10-04 00:00:00", 2, 8, "2026-10-04 00:00:00"),
        ("2026-10-05", "2026-10-05 00:00:00", 2, 8, "2026-10-04 00:00:00"),
    ])
    monkeypatch.setattr(Ingest, "_source", lambda self: 8)
    monkeypatch.setattr(bench, "sample_bounds", lambda ch, n: ["depth < 2", "depth >= 2"])
    monkeypatch.setattr(Ingest, "pair_query", lambda self, prev, cond: f"SELECT '{prev.epoch}' AS epoch WHERE {cond}")
    calls = []

    def execute(query, *, fmt, settings):
        calls.append(("time", query, fmt, settings))

    def stream(query, fmt, settings):
        calls.append(("verify", query, fmt, settings))
        return iter([b"ab", b"cd"])

    return SimpleNamespace(exec=execute, stream=stream), calls


def test_read_only_plans_alternate_and_compare_complete_output(inputs):
    ch, calls = inputs
    rows = []
    bench.benchmark(ch, "2026-10-05", "retained.parquet", (1,), trials=2, emit=rows.append)
    order = ["legacy", "epoch", "epoch-1g", "epoch-merge", "epoch-merge", "epoch-1g", "epoch", "legacy"]
    assert [(r["plan"], r["trial"], r["range"], r["result_bytes"], r["sha256"], r["same_rows_fingerprint"], r["publication"]) for r in rows] == [
        (name, i // 4, 1, 4, sha256(b"abcd").hexdigest(), True, False) for i, name in enumerate(order)
    ]
    base = range_settings(4)
    settings_by_name = {
        "legacy": base, "epoch": base,
        "epoch-1g": {**base, "max_bytes_before_external_group_by": 1 << 30},
        "epoch-merge": {**base, "join_algorithm": "full_sorting_merge"},
    }
    expected = []
    for i, name in enumerate(order):
        epoch = "2026-10-03" if name == "legacy" else "2026-10-04"
        query = f"SELECT '{epoch} 00:00:00' AS epoch WHERE depth >= 2"
        label = f"ingest-plan:2026-10-05:1:{i // 4}:{name}"
        expected.extend([
            ("time", query, "Null", {**settings_by_name[name], "log_comment": label}),
            ("verify", f"SELECT * FROM ({query}) ORDER BY depth, path, usr", "RowBinary",
             {**settings_by_name[name], "log_comment": f"{label}:verify"}),
        ])
    assert calls == expected


def test_fingerprint_difference_fails_and_emits_evidence(inputs):
    ch, _ = inputs
    values = iter([b"first", b"changed"])
    ch.stream = lambda *a, **kw: iter([next(values)])
    rows = []
    with pytest.raises(IngestError) as caught:
        bench.benchmark(ch, "2026-10-05", "retained.parquet", (1,), trials=1, emit=rows.append)
    assert str(caught.value) == "changed-row fingerprint mismatch at range 1: epoch"
    assert [(r["plan"], r["same_rows_fingerprint"]) for r in rows] == [("legacy", True), ("epoch", False)]


@pytest.mark.parametrize("scan,indices,error", [
    ("missing", (0,), "benchmark requires a published scan with a published predecessor"),
    ("2026-10-03", (0,), "benchmark requires a published scan with a published predecessor"),
    ("2026-10-05", (2,), "range index exceeds the 2 sampled ranges"),
    ("2026-10-05", (0, 0), "provide distinct nonnegative range indices and positive samples/trials"),
])
def test_invalid_benchmark_never_runs_a_plan(inputs, scan, indices, error):
    ch, calls = inputs
    with pytest.raises((ValueError, IngestError)) as caught:
        bench.benchmark(ch, scan, "retained.parquet", indices)
    assert str(caught.value) == error
    assert calls == []


def test_source_count_guard(inputs, monkeypatch):
    ch, calls = inputs
    monkeypatch.setattr(Ingest, "_source", lambda self: 7)
    with pytest.raises(IngestError) as caught:
        bench.benchmark(ch, "2026-10-05", "retained.parquet", (0,))
    assert str(caught.value) == "source row count differs from the published scan; no benchmark run"
    assert calls == []


def test_cli_records_each_result_without_overwriting(monkeypatch, tmp_path):
    from dt_cloud.chstore import client

    events = []
    ch = SimpleNamespace(close=lambda: events.append("close"))
    monkeypatch.setattr(client, "Ch", lambda *a, **kw: ch)

    def run(actual_ch, scan, source, indices, **kwargs):
        events.append((actual_ch is ch, scan, source, indices, kwargs["trials"], kwargs["threads"], kwargs["samples"]))
        kwargs["emit"]({"publication": False})

    monkeypatch.setattr(bench, "benchmark", run)
    output = tmp_path / "result.jsonl"
    args = ["ch-ingest-plan-bench", "-d2026-10-05", "-i0", "-i2", "-n1", "-t2", "-s64", "-o", str(output), "retained.parquet"]
    result = CliRunner().invoke(main, args)
    assert (result.exit_code, result.output) == (0, '{"publication": false}\n')
    assert output.read_text() == '{"publication": false}\n'
    assert events == [(True, "2026-10-05", "retained.parquet", (0, 2), 1, 2, 64), "close"]
    result = CliRunner().invoke(main, args)
    assert isinstance(result.exception, FileExistsError)
    assert output.read_text() == '{"publication": false}\n'
    assert events == [(True, "2026-10-05", "retained.parquet", (0, 2), 1, 2, 64), "close", "close"]
