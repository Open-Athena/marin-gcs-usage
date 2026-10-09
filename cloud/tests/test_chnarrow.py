"""Frozen multi-scan numbering: additions/deletions leave valid interval holes."""

import json
import struct
import uuid

import numpy as np
import pytest
from click.testing import CliRunner

from dt_cloud.chstore import ingest as ci
from dt_cloud.chstore import narrow
from dt_cloud.chstore import narrow_serve
from dt_cloud.chstore.client import Ch
from dt_cloud.chstore.serve import Store
from dt_cloud.cli import main

from chserver import ch_db, ch_url  # noqa: F401 — fixtures
from test_box import write_v2
from test_chstore import A, B
from test_chserve import diff, subtree


def test_interval_binary():
    rows = b"".join(narrow.interval_rows(np.array([0, 2, 1]), np.array([2, 2, 1])))
    assert np.frombuffer(rows, dtype=[("id", "<u4"), ("pre", "<u4"), ("post", "<u4")]).tolist() == [
        (0, 0, 2), (1, 2, 2), (2, 1, 1),
    ]


@pytest.mark.parametrize("root_depth", [0, 2])
@pytest.mark.parametrize("chunk_size", [1, 4, 5, 7, 100])
def test_sparse_directory_ancestors_stream(root_depth, chunk_size):
    source = b"".join(struct.pack("<IB", pre, root_depth + depth) for pre, depth in [
        (0, 0), (1, 1), (9, 2), (13, 2), (16, 3), (22, 1),
    ])
    chunks = (source[i:i + chunk_size] for i in range(0, len(source), chunk_size))
    actual = b"".join(narrow.preorder_ancestors(chunks, 6, 30, root_depth=root_depth))
    assert actual == b"".join(struct.pack("<IB", pre, len(ancestors)) + struct.pack(f"<{len(ancestors)}I", *ancestors) for pre, ancestors in [
        (0, [0]), (1, [0, 1]), (9, [0, 1, 9]), (13, [0, 1, 13]), (16, [0, 1, 13, 16]), (22, [0, 22]),
    ])


def test_ancestor_stream_varuint_lengths():
    source = b"".join(struct.pack("<IB", pre, pre) for pre in range(130))
    expected = b"".join(
        struct.pack("<I", pre) + (bytes([pre + 1]) if pre < 127 else bytes([((pre + 1) & 127) | 128, 1]))
        + struct.pack(f"<{pre + 1}I", *range(pre + 1)) for pre in range(130)
    )
    assert b"".join(narrow.preorder_ancestors([source], 130, 130, root_depth=0)) == expected
    assert list(narrow.preorder_ancestors([], 0, 1, root_depth=2)) == []


@pytest.mark.parametrize("rows,parent_rows,union_nodes,expected", [
    ([(1, 0)], 1, 2, "directory stream must start at the selected root"),
    ([(0, 0), (1, 0)], 2, 2, "directory stream has another root"),
    ([(0, 0), (2, 2)], 2, 3, "directory stream lacks an ancestor"),
    ([(0, 0), (0, 1)], 2, 2, "directory preorder keys must increase within the union"),
    ([(0, 0), (3, 1)], 2, 3, "directory preorder keys must increase within the union"),
    ([(0, 0)], 2, 3, "incomplete directory stream: 1 != 2 rows, 0 trailing bytes"),
    ([(0, 0), (1, 1)], 1, 3, "directory stream exceeds its expected row count"),
    ([], 2, 1, "directory stream bounds exceed the frozen key domain"),
])
def test_ancestor_stream_rejects_invalid_tree(
    rows: list[tuple[int, int]],
    parent_rows: int,
    union_nodes: int,
    expected: str,
) -> None:
    source = b"".join(struct.pack("<IB", *row) for row in rows)
    with pytest.raises(ValueError) as caught:
        list(narrow.preorder_ancestors([source], parent_rows, union_nodes, root_depth=0))
    assert str(caught.value) == expected


def test_ancestor_stream_rejects_truncated_record():
    with pytest.raises(ValueError) as caught:
        list(narrow.preorder_ancestors([b"x"], 1, 3, root_depth=0))
    assert str(caught.value) == "incomplete directory stream: 0 != 1 rows, 1 trailing bytes"


@pytest.mark.parametrize("parent_rows", [0, 2])
def test_stream_hierarchy_primes_before_insert(monkeypatch, parent_rows):
    from types import SimpleNamespace

    events = []
    manifest = {"prefix": "b/u1", "union_nodes": 7}
    source = struct.pack("<IBIB", 0, 2, 4, 3) if parent_rows else b""

    def scalar(sql):
        events.append(("scalar", sql))
        return json.dumps(manifest) if sql == "SELECT doc FROM narrow_test.manifest" else "0" if sql == "EXISTS TABLE narrow_test.hierarchy_stream" else str(parent_rows)

    def stream(sql, fmt):
        events.append(("stream", sql, fmt))
        yield source

    def insert(sql, rows, **kwargs):
        events.append(("insert", sql, b"".join(rows)))

    reader = SimpleNamespace(stream=stream, close=lambda: events.append(("close_reader",)))
    ch = SimpleNamespace(scalar=scalar, exec=lambda sql: events.append(("exec", sql)),
                         fork=lambda **kwargs: reader, insert=insert)
    monkeypatch.setattr(narrow.time, "monotonic", lambda: 10)
    assert narrow.stream_hierarchy(ch, "narrow_test") == {
        "directories": parent_rows, "table": "narrow_test.hierarchy_stream", "seconds": 0,
        "engine": "stream", "incremental": False,
    }
    assert events == [
        ("scalar", "EXISTS TABLE narrow_test.hierarchy_stream"),
        ("scalar", "SELECT doc FROM narrow_test.manifest"),
        ("scalar", "SELECT count() FROM narrow_test.parent_paths"),
        ("scalar", "SELECT count() FROM narrow_test.parents"),
        ("exec", "CREATE TABLE narrow_test.hierarchy_stream (pre UInt32, ancestors Array(UInt32)) ENGINE = MergeTree ORDER BY pre"),
        ("stream", "SELECT pre, toUInt8(if(path = '', 0, length(splitByChar('/', path)))) FROM narrow_test.parent_paths ORDER BY pre", "RowBinary"),
        *([("insert", "INSERT INTO narrow_test.hierarchy_stream FORMAT RowBinary", struct.pack("<IBIIBII", 0, 1, 0, 4, 2, 0, 4))] if parent_rows else []),
        ("scalar", "SELECT count() FROM narrow_test.hierarchy_stream"),
        ("close_reader",),
    ]


def test_single_snapshot_union():
    assert narrow.merge_snapshots("narrow_test", 1) == "SELECT depth, path, parent FROM narrow_test.snapshot_0"
    with pytest.raises(ValueError, match="at least one snapshot is required"):
        narrow.merge_snapshots("narrow_test", 0)


def test_dictionary_join_has_explicit_spill_and_block_limits() -> None:
    assert narrow.dictionary_join_settings() == {
        "max_threads": 2, "max_insert_threads": 1, "max_block_size": 8192,
        "join_algorithm": "grace_hash", "grace_hash_join_initial_buckets": 16,
        "max_bytes_before_external_join": 512 << 20, "max_bytes_ratio_before_external_join": 0,
        "max_bytes_in_join": 1 << 30, "join_overflow_mode": "throw",
        "query_plan_join_swap_table": "false",
    }


@pytest.mark.parametrize("logged,actual,error", [
    ([7, 4], [7, 4], None),
    ([7, None], [7, 4], "name recovery needs a completed matching query: names"),
    ([7, 4], [7, 3], "name recovery row count differs from completed query: names"),
    ([6, 4], [6, 4], "name recovery domain differs from completed intervals: [6, 4] / 7"),
])
def test_name_recovery_requires_completed_exact_queries(
    logged: list[int | None],
    actual: list[int],
    error: str | None,
) -> None:
    from types import SimpleNamespace

    calls = []
    statements = {
        "names_sorted": "CREATE TABLE narrow_test.names_sorted ENGINE = MergeTree ORDER BY l AS SELECT l FROM narrow_test.ids",
        "names": "CREATE TABLE narrow_test.names (nid UInt32, l String, INDEX tl l TYPE text(tokenizer = ngrams(3)))\n"
        "            ENGINE = MergeTree ORDER BY l AS SELECT toUInt32(rowNumberInAllBlocks()) AS nid, l\n"
        "            FROM (SELECT l FROM narrow_test.names_sorted GROUP BY l ORDER BY l)",
    }

    def scalar(sql: str) -> str | None:
        calls.append(sql)
        if sql.startswith("EXISTS TABLE "):
            return "1"
        i = (len(calls) - 1) // 3
        value = actual[i] if sql.startswith("SELECT count()") else logged[i]
        return None if value is None else str(value)

    ch = SimpleNamespace(db="source", scalar=scalar)
    if error is None:
        assert narrow.completed_name_tables(ch, "narrow_test", 7) == {"names_sorted": 7, "names": 4}
    else:
        with pytest.raises(ValueError) as caught:
            narrow.completed_name_tables(ch, "narrow_test", 7)
        assert str(caught.value) == error
    expected = []
    for i, (label, statement) in enumerate(statements.items()):
        expected += [f"EXISTS TABLE narrow_test.{label}",
                     "SELECT written_rows FROM system.query_log WHERE event_date >= today() - 7 "
                     "AND type = 'QueryFinish' AND current_database = 'source' "
                     f"AND log_comment = 'narrow:narrow_test:{label}' AND query = {narrow.lit(statement)} "
                     "ORDER BY event_time_microseconds DESC LIMIT 1"]
        if logged[i] is None:
            break
        expected.append(f"SELECT count() FROM narrow_test.{label}")
        if actual[i] != logged[i]:
            break
    assert calls == expected


def test_missing_parent_keys_uses_sorted_distinct_requirements() -> None:
    from types import SimpleNamespace

    calls = []

    def query(sql: str, **kwargs: object) -> list[list[object]]:
        calls.append((sql, kwargs))
        return [[2, "b/missing"]]

    ch = SimpleNamespace(json=query)
    assert narrow.missing_parent_keys(ch, "narrow_test") == [[2, "b/missing"]]
    assert calls == [(
        "SELECT k.depth, k.parent FROM narrow_test.parent_keys k\n"
        "        LEFT JOIN narrow_test.ids p ON k.depth = p.depth AND k.parent = p.path\n"
        "        WHERE isNull(p.id) LIMIT 10",
        {"settings": {
            "join_algorithm": "full_sorting_merge", "join_use_nulls": 1,
            "max_threads": 2, "max_block_size": 8192, "max_memory_usage": 8 << 30,
            "max_bytes_before_external_sort": 256 << 20,
            "max_bytes_ratio_before_external_sort": 0,
            "log_comment": "narrow:narrow_test:missing_parent_keys",
        }},
    )]


def test_missing_parent_keys_rejects_invalid_identifier() -> None:
    from types import SimpleNamespace

    with pytest.raises(ValueError) as caught:
        narrow.missing_parent_keys(SimpleNamespace(), "bad;query")
    assert str(caught.value) == "invalid experimental database name: 'bad;query'"


@pytest.mark.parametrize("mapped,error", [(2, None), (1, "parent mapping row count differs from required keys: 1 != 2")])
def test_missing_parent_keys_requires_complete_mapping(mapped: int, error: str | None) -> None:
    from types import SimpleNamespace

    calls = []

    def scalar(sql: str) -> str:
        calls.append(sql)
        return str(mapped) if sql == "SELECT count() FROM narrow_test.parents" else "2"

    ch = SimpleNamespace(json=lambda *a, **kw: [], scalar=scalar)
    if error is None:
        assert narrow.missing_parent_keys(ch, "narrow_test") == []
    else:
        with pytest.raises(ValueError) as caught:
            narrow.missing_parent_keys(ch, "narrow_test")
        assert str(caught.value) == error
    assert calls == ["SELECT count() FROM narrow_test.parent_keys", "SELECT count() FROM narrow_test.parents"]


def test_missing_parent_keys_exact_tree_validation(ch_url: str, ch_db: str) -> None:
    target = f"{ch_db}_parents"
    ch = Ch(ch_url, db=ch_db)
    ch.exec(f"CREATE DATABASE {target}")
    try:
        ch.exec(f"CREATE TABLE {target}.ids (id UInt32, depth UInt8, path String) ENGINE = MergeTree ORDER BY (depth, path)")
        ch.exec(f"CREATE TABLE {target}.parent_keys (depth UInt8, parent String) ENGINE = MergeTree ORDER BY (depth, parent)")
        ch.exec(f"CREATE TABLE {target}.parents (id UInt32, path String) ENGINE = MergeTree ORDER BY path")
        ch.exec(f"INSERT INTO {target}.ids VALUES (0, 0, ''), (1, 1, 'b'), (2, 2, 'b/p')")
        ch.exec(f"INSERT INTO {target}.parent_keys VALUES (0, ''), (1, 'b'), (2, 'b/p')")
        ch.exec(f"INSERT INTO {target}.parents VALUES (0, ''), (1, 'b'), (2, 'b/p')")
        assert narrow.missing_parent_keys(ch, target) == []
        ch.exec(f"INSERT INTO {target}.parent_keys VALUES (2, 'b/absent')")
        assert narrow.missing_parent_keys(ch, target) == [[2, "b/absent"]]
    finally:
        ch.exec(f"DROP DATABASE {target} SYNC")
        ch.close()


@pytest.mark.parametrize("missing", [[], [[2, "b/absent"]]])
def test_parent_check_cli_reports_exact_validation(monkeypatch: pytest.MonkeyPatch, missing: list[list[object]]) -> None:
    from types import SimpleNamespace

    calls = []
    client = SimpleNamespace(close=lambda: calls.append("close"))

    def connect(url: str, **kwargs: object) -> object:
        calls.append(("client", url, kwargs))
        return client

    def check(ch: Ch, target: str) -> list[list[object]]:
        assert ch is client
        calls.append(("check", target))
        return missing

    monkeypatch.setattr("dt_cloud.chstore.client.Ch", connect)
    monkeypatch.setattr(narrow, "missing_parent_keys", check)
    monkeypatch.setattr("time.monotonic", lambda: 10)
    result = CliRunner().invoke(main, ["ch-narrow-parent-check", "-U", "http://box", "narrow_test"])
    if missing:
        assert (result.exit_code, result.output, str(result.exception)) == (1, "", "union lacks parents: [[2, 'b/absent']]")
    else:
        assert (result.exit_code, json.loads(result.output)) == (0, {"target": "narrow_test", "missing": [], "seconds": 0})
    assert calls == [("client", "http://box", {"timeout": 7200}), ("check", "narrow_test"), "close"]


@pytest.mark.parametrize("exists,logged,actual,error", [
    (False, None, 7, None), (True, "6", 7, None),
    (True, None, 7, "snapshot recovery needs a completed matching query: snapshot_0"),
    (True, "6", 8, "snapshot recovery row count differs from completed queries: snapshot_0"),
])
def test_snapshot_recovery_requires_completed_exact_queries(
    exists: bool,
    logged: str | None,
    actual: int,
    error: str | None,
) -> None:
    from types import SimpleNamespace

    calls = []
    create, root = "CREATE TABLE exact_snapshot AS SELECT 1", "INSERT INTO exact_snapshot SELECT 1"

    def scalar(sql):
        calls.append(sql)
        if sql == "EXISTS TABLE narrow_test.snapshot_0":
            return str(int(exists))
        if sql == "SELECT count() FROM narrow_test.snapshot_0":
            return str(actual)
        return "1" if "snapshot_root_0" in sql else logged

    ch = SimpleNamespace(db="source", scalar=scalar)
    if error:
        with pytest.raises(ValueError) as caught:
            narrow.completed_snapshot(ch, "narrow_test", 0, create, root)
        assert str(caught.value) == error
    else:
        assert narrow.completed_snapshot(ch, "narrow_test", 0, create, root) is exists
    query = lambda label, sql: (
        "SELECT written_rows FROM system.query_log WHERE event_date >= today() - 7 "
        "AND type = 'QueryFinish' AND current_database = 'source' "
        f"AND log_comment = 'narrow:narrow_test:{label}' AND query = {narrow.lit(sql)} "
        "ORDER BY event_time_microseconds DESC LIMIT 1"
    )
    assert calls == ["EXISTS TABLE narrow_test.snapshot_0", *(
        [query("snapshot_0", create), *(
            [query("snapshot_root_0", root), "SELECT count() FROM narrow_test.snapshot_0"] if logged is not None else []
        )] if exists else []
    )]


def test_bounded_snapshot_cli_forwards_recovery(monkeypatch):
    calls = []

    def build(
        store: Store,
        target: str,
        prefix: str,
        dates: tuple[str, ...],
        **kwargs: object,
    ) -> dict:
        calls.append((store.db, target, prefix, dates, {k: v for k, v in kwargs.items() if k != "log"}))
        return {"snapshot_ranges": kwargs["snapshot_ranges"]}

    monkeypatch.setattr(narrow, "build", build)
    result = CliRunner().invoke(main, ["ch-narrow-build", "-b", "64", "-p", "", "-d", "2026-10-04", "-d", "2026-10-05", "-R", "snapshots-partial", "narrow_test"])
    assert (result.exit_code, result.output) == (0, '{"snapshot_ranges": 64}\n')
    assert calls == [("default", "narrow_test", "", ("2026-10-04", "2026-10-05"), {
        "max_nodes": 50_000_000, "interval_engine": "numpy", "min_free_bytes": 64 << 30,
        "union_engine": "group", "resume_from": "snapshots-partial", "snapshot_ranges": 64,
    })]


@pytest.mark.parametrize("failure", [None, "build", "audit"])
def test_build_cli_audits_only_after_success(monkeypatch, failure: str | None) -> None:
    from dt_cloud import cli

    calls = []

    def build(*args: object, **kwargs: object) -> dict:
        calls.append("build")
        if failure == "build":
            raise RuntimeError("incomplete construction")
        return {"union_nodes": 7}

    def audit(url: str, target: str) -> dict:
        calls.append(("audit", url, target))
        if failure == "audit":
            raise ValueError("invalid preorder")
        return {"checks": {"preorder_tree": True}}

    monkeypatch.setattr(narrow, "build", build)
    monkeypatch.setattr(narrow, "audit", audit)
    monkeypatch.setattr(cli, "err", lambda message: calls.append(("log", message)))
    result = CliRunner().invoke(main, ["ch-narrow-build", "-a", "-p", "b/u1", "-d", "2026-10-01", "narrow_test"])
    if failure == "build":
        assert result.exit_code == 1
        assert str(result.exception) == "incomplete construction"
        assert result.output == ""
        assert calls == ["build"]
    elif failure == "audit":
        assert result.exit_code == 1
        assert str(result.exception) == "invalid preorder"
        assert result.output == ""
        assert calls == [
            "build",
            ("log", "full-domain audit: starting after successful construction"),
            ("audit", "http://localhost:8123", "narrow_test"),
        ]
    else:
        assert (result.exit_code, json.loads(result.stdout)) == (0, {
            "union_nodes": 7, "audit": {"checks": {"preorder_tree": True}},
        })
        assert result.stderr == ""
        assert calls == [
            "build",
            ("log", "full-domain audit: starting after successful construction"),
            ("audit", "http://localhost:8123", "narrow_test"),
            ("log", "full-domain audit: passed"),
        ]


@pytest.mark.parametrize("existing,parents,free,reserve,error,expected_calls", [
    (True, 2, 100, 0, "experimental table already exists: narrow_test.hierarchy_stream", ["EXISTS TABLE narrow_test.hierarchy_stream"]),
    (False, 3, 100, 0, "parent_paths row count differs from the frozen directory parents", [
        "EXISTS TABLE narrow_test.hierarchy_stream", "SELECT doc FROM narrow_test.manifest",
        "SELECT count() FROM narrow_test.parent_paths", "SELECT count() FROM narrow_test.parents",
    ]),
    (False, 2, 50, 100, "before hierarchy_stream: 50 free bytes < reserve 100; partial build retained", [
        "EXISTS TABLE narrow_test.hierarchy_stream", "SELECT doc FROM narrow_test.manifest",
        "SELECT count() FROM narrow_test.parent_paths", "SELECT count() FROM narrow_test.parents",
        "SELECT min(free_space) FROM system.disks",
    ]),
])
def test_stream_hierarchy_guards_precede_writes(
    existing: bool,
    parents: int,
    free: int,
    reserve: int,
    error: str,
    expected_calls: list[str],
) -> None:
    from types import SimpleNamespace

    calls = []
    values = {
        "EXISTS TABLE narrow_test.hierarchy_stream": str(int(existing)),
        "SELECT doc FROM narrow_test.manifest": json.dumps({"prefix": "", "union_nodes": 7}),
        "SELECT count() FROM narrow_test.parent_paths": "2",
        "SELECT count() FROM narrow_test.parents": str(parents),
        "SELECT min(free_space) FROM system.disks": str(free),
    }

    def scalar(sql):
        calls.append(sql)
        return values[sql]

    with pytest.raises(ValueError) as caught:
        narrow.stream_hierarchy(SimpleNamespace(scalar=scalar), "narrow_test", min_free_bytes=reserve)
    assert str(caught.value) == error
    assert calls == expected_calls


def test_stream_hierarchy_cli_forwards_bounds(monkeypatch):
    from types import SimpleNamespace

    calls = []
    client = SimpleNamespace(close=lambda: calls.append("close"))

    def connect(url, **kwargs):
        calls.append(("client", url, kwargs))
        return client

    monkeypatch.setattr("dt_cloud.chstore.client.Ch", connect)

    def build(ch, target, **kwargs):
        assert ch is client
        calls.append((target, kwargs))
        return {"directories": 2}

    monkeypatch.setattr(narrow, "stream_hierarchy", build)
    result = CliRunner().invoke(main, ["ch-narrow-hierarchy-stream", "-f", "10", "-T", "hierarchy_ab", "-U", "http://box", "narrow_test"])
    assert (result.exit_code, result.output) == (0, '{"directories": 2}\n')
    assert calls == [("client", "http://box", {"timeout": 7200}), ("narrow_test", {"table": "hierarchy_ab", "min_free_bytes": 10 << 30}), "close"]


@pytest.mark.parametrize("numeric", [False, True])
def test_history_parent_source(numeric):
    bound = "pre >= 4 AND pre < 8"
    parents = (
        "SELECT pre, parent_pre FROM narrow_test.numeric_parents WHERE pre >= 4 AND pre < 8"
        if numeric else
        "SELECT parent, parent_pre FROM narrow_test.parent_ids WHERE parent IN (SELECT parent FROM (SELECT * FROM narrow_test_0.metadata WHERE pre >= 4 AND pre < 8))"
    )
    join = "m.pre = p.pre" if numeric else "m.parent = p.parent"
    assert narrow.history_parent_source("narrow_test", "narrow_test_0", "b", 0, bound, numeric) == (
        "SELECT toUInt16(0) AS tick, m.pre AS pre,\n"
        "        if(m.path = 'b', toInt64(-1), toInt64(p.parent_pre)) AS parent_pre,\n"
        "        m.depth AS depth, m.path AS path, m.parent AS parent, "
        "m.b AS b, m.o AS o, m.wts AS wts, m.wb AS wb, m.a AS a, m.c2 AS c2, m.c3 AS c3, m.c4 AS c4, m.ub AS ub, m.kind AS kind, m.nc AS nc\n"
        f"        FROM (SELECT * FROM narrow_test_0.metadata WHERE {bound}) m LEFT JOIN ({parents}) p ON {join}"
    )


@pytest.mark.parametrize("batch_rows,starts,error", [
    (0, (0,), "parent benchmark batch_rows must be positive"),
    (2, (), "parent benchmark needs nonnegative unique starts"),
    (2, (-1,), "parent benchmark needs nonnegative unique starts"),
    (2, (0, 0), "parent benchmark needs nonnegative unique starts"),
])
def test_parent_benchmark_rejects_bad_ranges(batch_rows, starts, error):
    with pytest.raises(ValueError) as caught:
        list(narrow.parent_benchmark("unused", "narrow_test", starts, batch_rows=batch_rows))
    assert str(caught.value) == error


def test_parent_benchmark_rejects_unknown_join():
    with pytest.raises(ValueError) as caught:
        list(narrow.parent_benchmark("unused", "narrow_test", (0,), numeric_join="hash"))
    assert str(caught.value) == "parent benchmark numeric_join must be grace_hash or full_sorting_merge"


def test_parent_benchmark_cli(monkeypatch, tmp_path):
    calls = []

    def benchmark(url, target, starts, **kwargs):
        calls.append((url, target, starts, kwargs))
        yield {"exact": True, "renders_tree": False}

    monkeypatch.setattr(narrow, "parent_benchmark", benchmark)
    out = tmp_path / "paired.jsonl"
    result = CliRunner().invoke(main, ["ch-narrow-parent-bench", "-b", "500", "-j", "full_sorting_merge", "-o", str(out), "-s", "0", "-s", "1000", "-U", "http://example", "narrow_test"])
    assert result.exit_code == 0, result.output
    assert result.output == '{"exact": true, "renders_tree": false}\n'
    assert out.read_text() == result.output
    assert calls == [("http://example", "narrow_test", (0, 1000), {"batch_rows": 500, "numeric_join": "full_sorting_merge"})]


def test_stream_intervals_split_records():
    source = b"".join(struct.pack("<IB", node, depth) for node, depth in [(0, 1), (1, 2), (3, 3), (2, 2), (4, 1)])
    chunks = [source[:2], source[2:11], source[11:17], source[17:]]
    result = b"".join(narrow.preorder_intervals(chunks, 5))
    assert np.frombuffer(result, dtype=[("id", "<u4"), ("pre", "<u4"), ("post", "<u4")]).tolist() == [
        (3, 2, 2), (1, 1, 2), (2, 3, 3), (0, 0, 3), (4, 4, 4),
    ]


@pytest.mark.parametrize("rows,count,error", [
    ([(0, 2)], 1, "invalid preorder row"),
    ([(0, 1), (1, 3)], 2, "invalid preorder row"),
    ([(1, 1)], 1, "invalid preorder row"),
    ([(0, 1), (0, 1)], 1, "invalid preorder row"),
    ([(0, 1)], 2, "incomplete preorder stream"),
])
def test_invalid_stream_intervals(rows, count, error):
    source = b"".join(struct.pack("<IB", node, depth) for node, depth in rows)
    with pytest.raises(ValueError, match=error):
        list(narrow.preorder_intervals([source], count))


def test_truncated_stream_intervals():
    with pytest.raises(ValueError, match="incomplete preorder stream"):
        list(narrow.preorder_intervals([struct.pack("<IB", 0, 1) + b"x"], 1))


def test_interval_stream_is_primed_before_insert_connection():
    from types import SimpleNamespace

    calls, uploads = [], []
    source = b"".join(struct.pack("<IB", node, depth) for node, depth in [(0, 1), (1, 2), (2, 3)])

    def stream(sql, fmt):
        calls.append("read")
        yield source

    def insert(sql, data, **kwargs):
        calls.append("insert")
        uploads.append(b"".join(data))

    reader = SimpleNamespace(stream=stream, close=lambda: calls.append("close"))
    ch = SimpleNamespace(scalar=lambda *a: "3", exec=lambda *a: calls.append("create"), fork=lambda **k: reader, insert=insert)
    result = narrow.stream_intervals(ch, "narrow_test", "b")
    assert calls == ["create", "read", "insert", "close"]
    assert uploads == [b"".join(struct.pack("<III", *row) for row in [(2, 2, 2), (1, 1, 2), (0, 0, 2)])]
    assert {k: v for k, v in result.items() if k != "seconds"} == {
        "nodes": 3, "table": "narrow_test.intervals_stream", "engine": "stream", "order_engine": "escaped",
    }


def test_disk_reserve_stops_before_creating_database():
    from types import SimpleNamespace

    calls = []

    def scalar(sql):
        calls.append(sql)
        return "50" if sql == "SELECT min(free_space) FROM system.disks" else "0"

    def unexpected(sql, **kwargs):
        raise AssertionError(f"disk guard must precede writes: {sql}")

    store = SimpleNamespace(db="default", session=lambda: SimpleNamespace(scalar=scalar, exec=unexpected),
                            scan=lambda day: SimpleNamespace(version=2))
    with pytest.raises(ValueError) as error:
        narrow.build(store, "guard_test", "b/u1", ("2026-10-01",), min_free_bytes=100)
    assert str(error.value) == "before create_guard_test: 50 free bytes < reserve 100; partial build retained"
    assert calls == ["EXISTS DATABASE guard_test", "EXISTS DATABASE guard_test_0", "SELECT min(free_space) FROM system.disks"]


@pytest.mark.parametrize("batch_rows", [0, -1])
def test_history_rejects_nonpositive_batches(batch_rows):
    from types import SimpleNamespace

    def unexpected():
        raise AssertionError("invalid batch size must precede database access")

    with pytest.raises(ValueError) as caught:
        narrow.history(SimpleNamespace(session=unexpected), "guard_test", batch_rows=batch_rows)
    assert str(caught.value) == "history batch_rows must be positive"


def test_history_disk_reserve_stops_before_creating_tables():
    from types import SimpleNamespace

    calls = []
    manifest = {"dates": ["2026-10-01"]}

    def scalar(sql):
        calls.append(sql)
        if sql == "SELECT doc FROM guard_test.manifest":
            return json.dumps(manifest)
        return "50" if sql == "SELECT min(free_space) FROM system.disks" else "0"

    def unexpected(sql, **kwargs):
        raise AssertionError(f"disk guard must precede writes: {sql}")

    store = SimpleNamespace(session=lambda: SimpleNamespace(scalar=scalar, exec=unexpected))
    with pytest.raises(ValueError) as error:
        narrow.history(store, "guard_test", min_free_bytes=100)
    assert str(error.value) == "before parent_ids: 50 free bytes < reserve 100; partial build retained"
    assert calls == ["SELECT doc FROM guard_test.manifest", "EXISTS DATABASE guard_test_h0", "SELECT min(free_space) FROM system.disks"]


def test_history_parent_ids_use_sorted_join_without_global_membership_set() -> None:
    from types import SimpleNamespace

    statements = []

    def scalar(sql: str) -> str:
        if sql == "SELECT doc FROM narrow_test.manifest":
            return json.dumps({"dates": ["2026-10-01"], "prefix": "b/u1"})
        return "0"

    def stop(sql: str, **kwargs: object) -> None:
        statements.append((sql, kwargs))
        raise RuntimeError("recorded first history statement")

    store = SimpleNamespace(session=lambda: SimpleNamespace(scalar=scalar, exec=stop))
    with pytest.raises(RuntimeError, match="^recorded first history statement$"):
        narrow.history(store, "narrow_test", log=lambda *args: None)
    assert statements == [(
        "CREATE TABLE IF NOT EXISTS narrow_test.parent_ids ENGINE = MergeTree ORDER BY parent AS\n"
        "        SELECT p.path AS parent, i.pre AS parent_pre FROM narrow_test.parents p INNER JOIN narrow_test.intervals i ON p.id = i.id",
        {"settings": {
            "max_memory_usage": 8 << 30, "max_threads": 2, "max_block_size": 8192,
            "max_bytes_before_external_sort": 256 << 20, "max_bytes_ratio_before_external_sort": 0,
            "max_bytes_before_external_group_by": 256 << 20, "max_bytes_ratio_before_external_group_by": 0,
            "join_algorithm": "full_sorting_merge", "grace_hash_join_initial_buckets": 16,
            "log_comment": "narrow:narrow_test:parent_ids",
        }},
    )]


@pytest.mark.parametrize("threads", [0, -1])
def test_history_rejects_invalid_build_threads(threads):
    with pytest.raises(ValueError) as caught:
        narrow.history(None, "narrow_test", build_threads=threads)
    assert str(caught.value) == "history build_threads must be positive"


def test_history_rejects_unknown_coalescer_before_connection() -> None:
    with pytest.raises(ValueError) as caught:
        narrow.history(None, "narrow_test", coalescer="unknown")
    assert str(caught.value) == "history coalescer must be window or pair"


@pytest.mark.parametrize("dates", [[], ["2026-10-01"], ["2026-10-01", "2026-10-01"], ["2026-10-01", "2026-10-02", "2026-10-03"]])
def test_pair_history_refuses_other_domains_before_writes(dates: list[str]) -> None:
    from types import SimpleNamespace

    calls = []

    def scalar(query: str) -> str:
        calls.append(query)
        return json.dumps({"dates": dates})

    store = SimpleNamespace(session=lambda: SimpleNamespace(scalar=scalar))
    with pytest.raises(ValueError) as caught:
        narrow.history(store, "narrow_test", coalescer="pair")
    assert str(caught.value) == "pair coalescing requires exactly two increasing snapshot instants"
    assert calls == ["SELECT doc FROM narrow_test.manifest"]


@pytest.mark.parametrize("numeric_parents", [False, True])
@pytest.mark.parametrize("numeric_ancestors", [False, True])
@pytest.mark.parametrize("coalescer", ["window", "pair"])
def test_history_cli_forwards_explicit_build_threads(monkeypatch, numeric_parents, numeric_ancestors, coalescer):
    calls = []

    def history(store, target, **kwargs):
        calls.append({"threads": store.threads, "target": target,
                      **{k: v for k, v in kwargs.items() if k != "log"}})
        return {"history_threads": kwargs["build_threads"]}

    monkeypatch.setattr(narrow, "history", history)
    result = CliRunner().invoke(main, ["ch-narrow-history", *(["-p"] if numeric_parents else []), *(["-a"] if numeric_ancestors else []), "-e", coalescer, "-b", "1000", "-f", "10", "-j", "4", "-t", "8", "narrow_test"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"history_threads": 4}
    assert calls == [{"threads": 8, "target": "narrow_test", "batch_rows": 1000,
                      "build_threads": 4, "min_free_bytes": 10 << 30, "numeric_parents": numeric_parents,
                      "numeric_ancestors": numeric_ancestors, "coalescer": coalescer, "resume_publication": False}]


def test_stream_tree_order_not_plain_strings(ch_url, ch_db):  # noqa: F811
    ch = Ch(ch_url, db=ch_db)
    try:
        ch.exec("CREATE TABLE ids (id UInt32, depth UInt8, path String) ENGINE = MergeTree ORDER BY id")
        ch.exec("INSERT INTO ids VALUES (0,1,'b'),(1,2,'b/a'),(2,2,'b/a-b'),(3,2,'b/á'),(4,3,'b/a/x'),(5,3,'b/a-b/y'),(6,2,concat('b/a',char(0))),(7,3,concat('b/a',char(0),'/x'))")
        result = narrow.stream_intervals(ch, ch_db, "b")
        assert {k: v for k, v in result.items() if k != "seconds"} == {
            "nodes": 8, "table": f"{ch_db}.intervals_stream", "engine": "stream", "order_engine": "escaped",
        }
        assert ch.json("SELECT id, pre, post FROM intervals_stream ORDER BY id") == [
            [0, 0, 7], [1, 1, 2], [2, 5, 6], [3, 7, 7], [4, 2, 2], [5, 6, 6], [6, 3, 4], [7, 4, 4],
        ]
        escaped = ch.json(f"SELECT path FROM ids ORDER BY {narrow.tree_order_expr()}")
        assert escaped == ch.json("SELECT path FROM ids ORDER BY splitByChar('/',path)")
        assert escaped == [[path] for path in ["b", "b/a", "b/a/x", "b/a\0", "b/a\0/x", "b/a-b", "b/a-b/y", "b/á"]]
        with pytest.raises(RuntimeError, match="already exists"):
            narrow.stream_intervals(ch, ch_db, "b")
    finally:
        ch.exec("DROP TABLE IF EXISTS intervals_stream")
        ch.exec("DROP TABLE IF EXISTS ids")
        ch.close()


@pytest.mark.parametrize("query,syntax,expected", [
    ("foo", "simple", True), (".npy", "simple", True), ("foo", "regex", False),
    ("/foo", "simple", False), ("foo/", "simple", False), ("foo/bar", "simple", False),
    ("foo*", "simple", False), ("foo -bar", "simple", False), ("foo bar", "simple", False),
    ("foo|bar", "simple", False), ("-foo", "simple", False),
])
def test_literal_name_candidates(query, syntax, expected):
    from dt_cloud.bench.ch import literal_name_only
    from dt_cloud.bench.query import parse

    assert literal_name_only(parse(query, syntax)) is expected


@pytest.mark.parametrize("path_free", [True, False])
def test_path_free_candidate_sql(path_free, monkeypatch):
    from dt_cloud.bench.ch import ChIndex

    ix = ChIndex(bounded_view="b/u1")
    ix._path_free_candidates = path_free
    calls = []
    monkeypatch.setattr(ix, "tmp", lambda name, sql: calls.append((name, " ".join(sql.split()))))
    ix._cands("l = 'foo'", "1 AS p, 0 AS n", 0, 9)
    columns = "pre, post, depth, b, o" + ("" if path_free else ", path")
    source = columns + ("" if path_free else ", lowerUTF8(path) AS lp")
    assert calls == [
        ("cn", "SELECT nid FROM names WHERE l = 'foo'"),
        ("cr", f"SELECT {columns}, 1 AS p, 0 AS n FROM ( SELECT {source} FROM nodes_by_name WHERE nid IN (SELECT nid FROM cn) AND pre > 0 AND pre <= 9)"),
    ]
    assert ix.root_paths_sql() == (
        "SELECT path FROM nodes_by_name WHERE nid IN (SELECT nid FROM cn) AND pre IN (SELECT pre FROM roots)" if path_free else
        "SELECT any(path) AS path FROM cr WHERE pre IN (SELECT pre FROM roots) GROUP BY pre"
    )


def test_hit_view_does_not_discover_its_descendants(monkeypatch):
    from dataclasses import asdict

    from dt_cloud.bench.ch import ChIndex
    from dt_cloud.bench.query import parse

    ix = ChIndex(bounded_view="b/u1")
    ix._views["b/u1"] = (3, 9, 2, 300, 4)
    calls = []
    monkeypatch.setattr(ix, "tmp", lambda name, sql: calls.append((name, sql)))

    def unexpected(*a, **kw):
        raise AssertionError("a hit view with no exclusions needs no candidate reads")

    monkeypatch.setattr(ix, "_cands", unexpected)
    got = asdict(ix.evaluate(parse("b/u1"), "b/u1"))
    got["stats"]["s"] = "<seconds>"
    assert got == {"hit": True, "roots": 1, "b": 300, "o": 4, "excluded": 0, "stats": {
        "view_shortcut": True, "path_free_candidates": False, "cands_s": 0.0, "roots_s": 0.0, "s": "<seconds>",
    }}
    assert calls == [("roots", "SELECT toUInt32(3) AS pre, toUInt32(9) AS post, toInt64(300) AS b, toInt64(4) AS o")]
    assert ix.roots_summary(100) == (["b/u1"], 1, None)


def test_bounded_index_avoids_global_scan(monkeypatch):
    from dt_cloud.bench.ch import ChIndex, Unsupported

    def unexpected(*args, **kwargs):
        raise AssertionError("bounded construction must not issue a global query")

    monkeypatch.setattr(ChIndex, "one", unexpected)
    ix = ChIndex(bounded_view="b/u1")
    assert ix.n is None
    assert ix._views == {}
    with pytest.raises(Unsupported, match="the bounded index only serves its declared view"):
        ix.view("")


@pytest.mark.parametrize("chunk_size", [1, 7, 9, 1024])
def test_complete_preorder_endpoints(chunk_size):
    rows = [(0, 4, 1), (1, 2, 2), (2, 2, 3), (3, 4, 2), (4, 4, 3)]
    data = b"".join(struct.pack("<IIB", *row) for row in rows)
    assert narrow.check_preorder((data[i:i + chunk_size] for i in range(0, len(data), chunk_size)), 5) is None


@pytest.mark.parametrize("rows,count,error", [
    ([(0, 1, 1), (1, 1, 3)], 2, "invalid preorder interval: pre=1, post=1, depth=3, position=1"),
    ([(0, 0, 1), (1, 1, 2)], 2, "invalid root endpoint: 0 != 1"),
    ([(0, 2, 1), (1, 2, 2), (2, 2, 2)], 3, "invalid subtree endpoint: 2 != 1"),
    ([(0, 2, 1), (1, 1, 2), (2, 2, 3)], 3, "invalid final subtree endpoint: 1 != 2"),
    ([(0, 1, 1)], 2, "incomplete interval audit: 1 != 2 rows, 0 trailing bytes"),
])
def test_invalid_preorder_endpoints(rows, count, error):
    with pytest.raises(ValueError) as caught:
        narrow.check_preorder([b"".join(struct.pack("<IIB", *row) for row in rows)], count)
    assert str(caught.value) == error


def test_truncated_preorder_endpoint():
    with pytest.raises(ValueError) as caught:
        narrow.check_preorder([b"x"], 1)
    assert str(caught.value) == "incomplete interval audit: 0 != 1 rows, 1 trailing bytes"


def test_progress_is_read_only(monkeypatch):
    from types import SimpleNamespace

    answers = iter([
        [["narrow:narrow_test:ids", "query-1", 2.5, 7, 3, 100, 50, 80, 2, 3000000]],
        [["narrow_test", "ids", 7, 90]],
        [["default", 1000, 2000]],
        [["narrow:narrow_test:ids", "2026-10-05 12:00:00", "QueryFinish", 1.5, 40]],
    ])
    closed = []
    client = SimpleNamespace(scalar=lambda sql: "1", json=lambda sql: next(answers), close=lambda: closed.append(True))
    monkeypatch.setattr(narrow, "Ch", lambda *a, **kw: client)
    assert narrow.progress("http://box", "narrow_test") == {
        "target": "narrow_test",
        "active": [{"label": "narrow:narrow_test:ids", "query_id": "query-1", "elapsed_s": 2.5,
                    "read_rows": 7, "written_rows": 3, "read_bytes": 100, "memory_bytes": 50,
                    "peak_memory_bytes": 80, "peak_threads": 2, "user_cpu_us": 3000000}],
        "tables": [{"database": "narrow_test", "table": "ids", "rows": 7, "bytes": 90}],
        "disks": [{"name": "default", "free_bytes": 1000, "total_bytes": 2000}],
        "recent_stages": [{"label": "narrow:narrow_test:ids", "at": "2026-10-05 12:00:00", "status": "QueryFinish", "seconds": 1.5, "memory_bytes": 40}],
    }
    assert closed == [True]


@pytest.mark.parametrize("os_bytes", [65536, None])
def test_query_profile_includes_historical_views_and_is_read_only(monkeypatch, os_bytes):
    from types import SimpleNamespace

    calls = []

    def read(sql):
        calls.append(sql)
        return [["narrow_test_h1", "2026-10-05 09:05:47", "QueryFinish", 8446, 61589717, 7065229722,
                 1191853424, 40059201, 0, 0, 123456, os_bytes, 789,
                 "CREATE TEMPORARY TABLE rn_b ENGINE = Memory AS SELECT 1"]]

    client = SimpleNamespace(json=read, close=lambda: calls.append("close"))
    monkeypatch.setattr(narrow, "Ch", lambda *a, **kw: client)
    assert narrow.query_profile("unused", "narrow_test", seconds=60, limit=2, min_ms=300) == {
        "target": "narrow_test", "lookback_s": 60, "limit": 2, "min_ms": 300,
        "queries": [{"database": "narrow_test_h1", "at": "2026-10-05 09:05:47", "status": "QueryFinish", "duration_ms": 8446,
                     "read_rows": 61589717, "read_bytes": 7065229722, "peak_memory_bytes": 1191853424,
                     "user_cpu_us": 40059201, "aggregation_spills": 0, "sort_spills": 0,
                     "file_read_bytes": 123456, "os_read_bytes": os_bytes, "read_wait_us": 789,
                     "sql": "CREATE TEMPORARY TABLE rn_b ENGINE = Memory AS SELECT 1"}],
    }
    assert calls == ["""SELECT current_database, toString(event_time), toString(type), query_duration_ms,
            read_rows, read_bytes, memory_usage, ProfileEvents['UserTimeMicroseconds'],
            ProfileEvents['ExternalAggregationWritePart'], ProfileEvents['ExternalSortWritePart'],
            ProfileEvents['ReadBufferFromFileDescriptorReadBytes'],
            if(mapContains(ProfileEvents, 'OSReadBytes'), ProfileEvents['OSReadBytes'], NULL),
            ProfileEvents['DiskReadElapsedMicroseconds'], query
            FROM system.query_log
            WHERE event_date >= today() - toUInt32(ceil(60 / 86400))
                AND event_time >= now() - INTERVAL 60 SECOND
                AND (current_database = 'narrow_test' OR startsWith(current_database, 'narrow_test_'))
                AND type != 'QueryStart' AND query_duration_ms >= 300
            ORDER BY event_time_microseconds DESC LIMIT 2""", "close"]


@pytest.mark.parametrize("kwargs", [{"seconds": 0}, {"limit": 0}, {"min_ms": -1}])
def test_query_profile_validates_before_server_access(kwargs):
    with pytest.raises(ValueError) as caught:
        narrow.query_profile("unused", "narrow_test", **kwargs)
    assert str(caught.value) == "profile seconds/limit must be positive and min_ms nonnegative"


def test_query_profile_cli_forwards_bounds(monkeypatch):
    calls = []

    def profile(url, target, **kwargs):
        calls.append((url, target, kwargs))
        return {"queries": []}

    monkeypatch.setattr(narrow, "query_profile", profile)
    got = CliRunner().invoke(main, ["ch-narrow-query-profile", "-U", "http://box", "-s", "60", "-n", "2", "-m", "300", "narrow_test"])
    assert got.exit_code == 0, got.output
    assert json.loads(got.output) == {"queries": []}
    assert calls == [("http://box", "narrow_test", {"seconds": 60, "limit": 2, "min_ms": 300})]


def test_response_summary():
    row = {"date": "2026-10-01", "previous": "2026-09-30", "trial": 0, "exact": True,
           "response_s": .5, "discovery_s": .25, "walk_s": .125, "baseline": {"response_s": 2.0}}
    assert narrow_serve.summarize([row]) == {"2026-09-30->2026-10-01/t0": {"n": 1, "exact": 1, "timings": {
        "response_s": {"p50": .5, "p90": .5, "max": .5},
        "discovery_s": {"p50": .25, "p90": .25, "max": .25},
        "walk_s": {"p50": .125, "p90": .125, "max": .125},
        "baseline_response_s": {"p50": 2.0, "p90": 2.0, "max": 2.0},
    }}}


@pytest.mark.parametrize("canonical", [False, True])
def test_response_timing_includes_initialization_and_cleanup(monkeypatch, canonical):
    from types import SimpleNamespace

    clock, calls = [0.0], []
    manifest = {"prefix": "b/u1", "dates": ["2026-10-01"], "dbs": ["narrow_test_h0"]}

    def advance(label, seconds, result=None):
        calls.append(label)
        clock[0] += seconds
        return result

    client = SimpleNamespace(
        settings={},
        scalar=lambda sql: advance("manifest", 2, json.dumps(manifest)),
        close=lambda: advance("close", 7),
        exec=lambda sql: advance(sql, 1),
    )
    store = SimpleNamespace(
        session=lambda: advance("session", 1, client),
        scan=lambda date: advance("scan", 2, None),
        root_label="marin GCS",
    )
    monkeypatch.setattr(narrow_serve, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(narrow_serve.cs, "Store", lambda *a, **kw: store)
    monkeypatch.setattr(narrow_serve, "prepare", lambda *a, **kw: advance("prepare", 3))
    monkeypatch.setattr(narrow_serve.cs, "filter_prepare", lambda *a, **kw: advance("prepare", 3))
    monkeypatch.setattr(narrow_serve.cs, "subtree_body", lambda *a, **kw: advance("serialize", 5, ['{"ok": true}']))
    if canonical:
        assert narrow_serve.compare_response(store, "2026-10-01", "b/u1", "hit") == {
            "body": {"ok": True}, "bytes": 12, "response_s": 18.0,
        }
        assert calls == ["session", "scan", "prepare", "serialize", "close"]
    else:
        assert narrow_serve.response("unused", "narrow_test", "2026-10-01", "hit") == {
            "body": {"ok": True}, "bytes": 12, "response_s": 22.0,
            "discovery_s": 6.0, "walk_s": 0.0, "renders_tree": True, "incremental": False,
        }
        assert calls == [
            "session", "manifest", "prepare", "serialize", "close",
            "DROP TEMPORARY TABLE IF EXISTS roots", "DROP TEMPORARY TABLE IF EXISTS ex",
            "DROP TEMPORARY TABLE IF EXISTS cr", "DROP TEMPORARY TABLE IF EXISTS cn",
        ]


def test_discovery_runs_pair_full_roots_and_payload_modes():
    before = {"prefix": "b", "date": "2026-10-01", "query": "hit", "query_text": "hit", "syntax": "simple",
              "trial": 0, "cold": True, "threads": 8, "exact": True, "renders_tree": False,
              "roots": ["b/hit"], "n": 1, "md5": None, "b": 40, "o": 2,
              "path_free": True, "name_index": True, "metadata_paths": True,
              "discovery_s": 1.0, "materialize_s": 2.0, "late_metadata_s": 4.0}
    after = {**before, "metadata_paths": False, "late_metadata_s": 1.0}
    assert narrow.compare_discovery_runs([before], [after]) == [{
        "prefix": "b", "date": "2026-10-01", "query": "hit", "trial": 0, "cold": True,
        "same_roots_and_totals": True, "threads_verified": True,
        "before_mode": {"path_free": True, "name_index": True, "metadata_paths": True, "threads": 8},
        "after_mode": {"path_free": True, "name_index": True, "metadata_paths": False, "threads": 8},
        "timings": {
            "discovery_s": {"before": 1.0, "after": 1.0, "speedup": 1.0},
            "materialize_s": {"before": 2.0, "after": 2.0, "speedup": 1.0},
            "late_metadata_s": {"before": 4.0, "after": 1.0, "speedup": 4.0},
        },
    }]


@pytest.mark.parametrize("change,error", [
    ({"threads": 4}, "discovery threads differs"),
    ({"query_text": "other"}, "discovery query_text differs"),
    ({"exact": False}, "both discovery runs must pass canonical identity comparison"),
    ({"roots": ["b/other"]}, "discovery root identities or totals differ"),
    ({"b": 41}, "discovery root identities or totals differ"),
    ({"roots": []}, "root list must be sorted, unique and complete"),
    ({"roots": None, "md5": None}, "uncapped root fingerprint is required"),
    ({"discovery_s": -1.0}, "discovery durations must be finite and nonnegative"),
    ({"discovery_s": float("nan")}, "discovery durations must be finite and nonnegative"),
    ({"discovery_s": float("inf")}, "discovery durations must be finite and nonnegative"),
])
def test_discovery_run_comparison_rejects_invalid_pairs(change, error):
    row = {"prefix": "b", "date": "2026-10-01", "query": "hit", "query_text": "hit", "syntax": "simple",
           "trial": 0, "cold": True, "threads": 8, "exact": True, "renders_tree": False,
           "roots": ["b/hit"], "n": 1, "md5": None, "b": 40, "o": 2,
           "discovery_s": 1.0, "materialize_s": 2.0, "late_metadata_s": 4.0}
    with pytest.raises(ValueError) as caught:
        narrow.compare_discovery_runs([row], [{**row, **change}])
    assert str(caught.value) == error + ": ('b', '2026-10-01', 'hit', 0, True)"


def test_discovery_comparison_requires_matching_nonempty_workloads():
    row = {"prefix": "b", "date": "2026-10-01", "query": "hit", "trial": 0, "cold": True}
    for before, after, expected in (
        ([], [], "an empty discovery run cannot be compared"),
        ([row, row], [row], "duplicate discovery case: ('b', '2026-10-01', 'hit', 0, True)"),
        ([row], [{**row, "cold": False}], "discovery workloads differ; dates, queries, trials and cache modes must match"),
    ):
        with pytest.raises(ValueError) as caught:
            narrow.compare_discovery_runs(before, after)
        assert str(caught.value) == expected


def test_discovery_comparison_pairs_streamed_fingerprints_and_zero_times():
    row = {"prefix": "b", "date": "2026-10-01", "query": "hit", "trial": 0, "cold": True,
           "exact": True, "renders_tree": False, "roots": None, "n": 1000000, "md5": "a" * 32,
           "b": 40, "o": 2, "discovery_s": 1.0, "materialize_s": 0.0}
    result = narrow.compare_discovery_runs([row], [row])
    assert result == [{
        "prefix": "b", "date": "2026-10-01", "query": "hit", "trial": 0, "cold": True,
        "same_roots_and_totals": True, "threads_verified": False,
        "before_mode": {"path_free": None, "name_index": None, "metadata_paths": None, "threads": None},
        "after_mode": {"path_free": None, "name_index": None, "metadata_paths": None, "threads": None},
        "timings": {"discovery_s": {"before": 1.0, "after": 1.0, "speedup": 1.0},
                    "materialize_s": {"before": 0.0, "after": 0.0, "speedup": None}},
    }]
    with pytest.raises(ValueError) as caught:
        narrow.compare_discovery_runs([row], [{**row, "md5": "b" * 32}])
    assert str(caught.value) == "discovery root identities or totals differ: ('b', '2026-10-01', 'hit', 0, True)"


def test_complete_response_runs_pair_cases_and_preserve_unknown_threads():
    before = {"prefix": "b", "date": "2026-10-01", "previous": None, "query": "hit", "trial": 0, "cold": True,
              "exact": True, "sha": "same", "path_free": True, "name_index": False, "parent_index": False,
              "response_s": 4.0, "discovery_s": 2.0, "walk_s": 1.0}
    after = {**before, "threads": 8, "name_index": True, "response_s": 2.0, "discovery_s": 1.0, "walk_s": .25}
    assert narrow_serve.compare_runs([before], [after]) == [{
        "prefix": "b", "date": "2026-10-01", "previous": None, "query": "hit", "trial": 0, "cold": True,
        "same_body": True, "threads_verified": False,
        "comparison_phase": "interleaved",
        "before_mode": {"path_free": True, "name_index": False, "name_index_variant": None, "parent_index": False, "ancestor_preaggregate": None, "root_join": None, "bounded_joins": None, "leaf_intervals": None, "visible_intervals": None, "fold_parent_pruning": None, "ancestor_bottom_up": None, "threads": None},
        "after_mode": {"path_free": True, "name_index": True, "name_index_variant": None, "parent_index": False, "ancestor_preaggregate": None, "root_join": None, "bounded_joins": None, "leaf_intervals": None, "visible_intervals": None, "fold_parent_pruning": None, "ancestor_bottom_up": None, "threads": 8},
        "timings": {"response_s": {"before": 4.0, "after": 2.0, "speedup": 2.0},
                    "discovery_s": {"before": 2.0, "after": 1.0, "speedup": 2.0},
                    "walk_s": {"before": 1.0, "after": .25, "speedup": 4.0}},
    }]


@pytest.mark.parametrize("change,error", [
    ({"cold": True}, "response workloads differ; dates, queries, trials and cache modes must match"),
    ({"date": "2026-09-30"}, "response workloads differ; dates, queries, trials and cache modes must match"),
    ({"threads": 4}, "response threads differs"),
    ({"query_text": "another"}, "response query_text differs"),
    ({"syntax": "regex"}, "response syntax differs"),
    ({"comparison_phase": "first"}, "response comparison_phase differs"),
    ({"sha": "different"}, "both response bodies must match canonical serving and each other"),
    ({"exact": False}, "both response bodies must match canonical serving and each other"),
    ({"response_s": 0}, "response durations must be positive"),
])
def test_response_run_comparison_rejects_invalid_pairs(change, error):
    row = {"prefix": "b", "date": "2026-10-01", "previous": None, "query": "hit", "trial": 0, "cold": False,
           "threads": 8, "query_text": "hit", "syntax": "simple", "exact": True, "sha": "same", "response_s": 1.0}
    with pytest.raises(ValueError) as caught:
        narrow_serve.compare_runs([row], [{**row, **change}])
    suffix = "" if error.startswith("response workloads") else ": ('b', '2026-10-01', None, 'hit', 0, False)"
    assert str(caught.value) == error + suffix


def test_response_run_comparison_rejects_empty_and_duplicate_runs():
    with pytest.raises(ValueError) as caught:
        narrow_serve.compare_runs([], [])
    assert str(caught.value) == "an empty response run cannot be compared"
    row = {"prefix": "b", "date": "2026-10-01", "previous": None, "query": "hit", "trial": 0, "cold": False}
    with pytest.raises(ValueError) as caught:
        narrow_serve.compare_runs([row, row], [row])
    assert str(caught.value) == "duplicate response case: ('b', '2026-10-01', None, 'hit', 0, False)"


@pytest.mark.parametrize("query,hit,table,columns", [
    ("hit", False, "rn_b", "m.b AS b, greatest(0, m.o) AS o, m.wts AS wts, greatest(0, m.wb) AS wb, m.a AS a, m.c2 AS c2, m.c3 AS c3, m.c4 AS c4, m.ub AS ub, m.kind AS kind, m.nc AS nc"),
    ("hit", True, "rt_b", "m.b AS b, m.o AS o, m.wts AS wts, m.wb AS wb, m.a AS a, m.c2 AS c2, m.c3 AS c3, m.c4 AS c4, m.ub AS ub, m.kind AS kind, m.nc AS nc"),
    ("hit -excluded", False, "rt_b", "m.b AS b, m.o AS o, m.wts AS wts, m.wb AS wb, m.a AS a, m.c2 AS c2, m.c3 AS c3, m.c4 AS c4, m.ub AS ub, m.kind AS kind, m.nc AS nc"),
])
@pytest.mark.parametrize("leaf_intervals", [False, True])
def test_positive_descendant_roots_are_materialized_directly(query, hit, table, columns, leaf_intervals):
    from types import SimpleNamespace

    from dt_cloud.bench.query import parse

    calls = []

    def temporary(name, sql, **kwargs):
        calls.append((name, " ".join(sql.split()), kwargs))

    assert narrow_serve.materialize_root_rows(
        SimpleNamespace(tmp=temporary), "narrow_test_h0", parse(query), "b",
        name_index=False, hit=hit, large_roots=True, leaf_intervals=leaf_intervals,
    ) == table
    post = "greatest(m.pre, coalesce(r.post, m.pre))" if leaf_intervals else "r.post"
    join = "LEFT JOIN (SELECT pre, post FROM roots WHERE post > pre)" if leaf_intervals else "INNER JOIN roots"
    assert calls == [(
        table,
        f"SELECT m.pre AS pre, {post} AS post, m.parent_pre AS parent_pre, m.path AS path, m.depth AS depth, {columns} "
        f"FROM (SELECT * FROM narrow_test_h0.metadata WHERE pre IN (SELECT pre FROM roots)) m {join} r ON m.pre = r.pre",
        {"disk": True, "ordered": False},
    )]


def test_indexed_single_trigrams_match_literal_names(ch_url, ch_db):
    from dt_cloud.bench.ch import name_sql
    from dt_cloud.bench.terms import NameTest
    from dt_cloud.chstore.client import lit

    ch = Ch(ch_url, db=ch_db)
    names = ["", "ab", "abc", "abcabc", "abcd", "abc_bcd", "a_b", "000", "10001", "漢字語", "漢字語abc", "漢abc字", "ab c", "000abc"]
    ch.exec("CREATE TABLE direct_trigram_names (l String, INDEX tl l TYPE text(tokenizer = ngrams(3))) ENGINE = MergeTree ORDER BY l")
    try:
        ch.exec("INSERT INTO direct_trigram_names VALUES " + ",".join(f"({lit(value)})" for value in names))
        expected = {
            "abc": ["000abc", "abc", "abc_bcd", "abcabc", "abcd", "漢abc字", "漢字語abc"],
            "000": ["000", "000abc", "10001"],
            "abcd": ["abcd"],
            "a_b": ["a_b"],
            "漢字語": ["漢字語", "漢字語abc"],
        }
        for needle, matches in expected.items():
            for direct in (False, True):
                predicate = name_sql(NameTest("contains", needle), trigram=direct)
                assert ch.json(f"SELECT l FROM direct_trigram_names WHERE {predicate} ORDER BY l") == [[value] for value in matches]
    finally:
        ch.exec("DROP TABLE direct_trigram_names SYNC")
        ch.close()


def test_bounded_global_view_looks_up_depth_zero(monkeypatch):
    from dt_cloud.bench.ch import ChIndex

    ix = ChIndex("unused", bounded_view="")
    calls = []

    def rows(sql):
        calls.append(sql)
        return [[0, 5, 0, 35, 3]]

    monkeypatch.setattr(ix, "rows", rows)
    assert ix.view("") == (0, 5, 0, 35, 3)
    assert calls == ["""SELECT pre, post, depth, b, o FROM nodes_by_name
                WHERE nid IN (SELECT nid FROM names WHERE l = '') AND depth = 0 AND path = ''"""]


def test_global_numeric_coverage_still_requires_selected_dates():
    from types import SimpleNamespace

    from dt_cloud.box.server import ChBox

    box = ChBox(SimpleNamespace(), narrow_target="narrow_test")
    assert box.narrow_covers("", "2026-10-01") is False
    box.narrow_manifest = {"prefix": "", "dates": ["2026-09-30", "2026-10-01"]}
    assert [box.narrow_covers(path, *dates) for path, dates in [
        ("", ["2026-10-01"]), ("b", ["2026-10-01"]), ("b/child", ["2026-09-30", "2026-10-01"]),
        ("b", ["2026-10-02"]), ("", ["2026-09-29", "2026-10-01"]),
    ]] == [True, True, True, False, False]


@pytest.mark.parametrize("query,eligible", [("000", True), ("ckpt -logs", False), ("ckpt/logs", False), ("*.bin", False)])
@pytest.mark.parametrize("hit", [True, False])
@pytest.mark.parametrize("name_index", [True, False])
def test_rich_metadata_name_ranges_require_literal_proof(query, eligible, hit, name_index):
    from dt_cloud.bench.query import parse

    expected = "SELECT * FROM narrow_test.metadata WHERE pre IN (SELECT pre FROM roots)"
    if eligible and name_index and not hit:
        expected = "SELECT * FROM narrow_test.metadata_by_name WHERE nid IN (SELECT nid FROM cn) AND pre IN (SELECT pre FROM roots)"
    assert narrow.root_metadata_source("narrow_test", parse(query), name_index=name_index, hit=hit) == expected


@pytest.mark.parametrize("variant,suffix", [(None, ""), ("g64", "_g64")])
def test_rich_name_index_disk_guard_precedes_table_creation(monkeypatch, variant, suffix):
    from types import SimpleNamespace

    calls = []

    def scalar(sql):
        calls.append(sql)
        if sql == "SELECT doc FROM history_manifest":
            return json.dumps({"dbs": ["narrow_test_h0"]})
        return "50" if sql == "SELECT min(free_space) FROM system.disks" else "0"

    def unexpected(*args, **kwargs):
        raise AssertionError("disk reserve must be checked before writing")

    monkeypatch.setattr(narrow, "Ch", lambda *a, **k: SimpleNamespace(scalar=scalar, exec=unexpected, close=lambda: calls.append("close")))
    with pytest.raises(ValueError) as caught:
        narrow.rich_name_index("ignored", "narrow_test", min_free_bytes=100, variant=variant)
    assert str(caught.value) == f"before metadata_history_by_name{suffix}: 50 free bytes < reserve 100; partial build retained"
    assert calls == ["SELECT doc FROM history_manifest", f"EXISTS TABLE metadata_history_by_name{suffix}", f"EXISTS TABLE rich_name_manifest{suffix}",
                     f"EXISTS TABLE narrow_test_h0.metadata_by_name{suffix}",
                     "SELECT min(free_space) FROM system.disks", "close"]


def test_rich_metadata_variant_is_an_isolated_access_path():
    from dt_cloud.bench.query import parse

    assert narrow.root_metadata_source("narrow_test", parse("000"), name_index=True, hit=False, name_index_variant="g64") == (
        "SELECT * FROM narrow_test.metadata_by_name_g64 WHERE nid IN (SELECT nid FROM cn) AND pre IN (SELECT pre FROM roots)"
    )
    assert narrow.root_metadata_source("narrow_test", parse("000 -omit"), name_index=True, hit=False, name_index_variant="g64") == (
        "SELECT * FROM narrow_test.metadata WHERE pre IN (SELECT pre FROM roots)"
    )


def test_rich_name_variant_cli(monkeypatch):
    calls = []

    def build(url, target, **kwargs):
        calls.append((url, target, kwargs))
        return {"variant": "g64", "production_cutover": False}

    monkeypatch.setattr(narrow, "rich_name_index", build)
    result = CliRunner().invoke(main, ["ch-narrow-rich-name-index", "-f", "80", "-g", "64", "-v", "g64", "narrow_test"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"variant": "g64", "production_cutover": False}
    assert calls == [("http://localhost:8123", "narrow_test", {"granularity": 64, "min_free_bytes": 80 << 30, "variant": "g64"})]


def test_rich_name_variant_requires_index_before_connection():
    with pytest.raises(ValueError) as caught:
        narrow_serve.response("unused", "narrow_test", "2026-10-01", "000", name_index_variant="g64")
    assert str(caught.value) == "a rich name-index variant requires name_index"


def test_response_rejects_foreign_name_variant_checkpoint_and_cleans_up(monkeypatch):
    from types import SimpleNamespace

    calls = []
    docs = {
        "SELECT doc FROM history_manifest": {},
        "SELECT doc FROM rich_name_manifest_g64": {
            "target": "other_target", "variant": "g64", "view": "metadata_by_name_g64", "table": "metadata_history_by_name_g64",
        },
    }

    def scalar(sql):
        calls.append(sql)
        return json.dumps(docs[sql])

    client = SimpleNamespace(settings={}, scalar=scalar, close=lambda: calls.append("close"), exec=calls.append)
    monkeypatch.setattr(narrow_serve.cs, "Store", lambda *a, **kw: SimpleNamespace(session=lambda: client))
    with pytest.raises(ValueError) as caught:
        narrow_serve.response("unused", "narrow_test", "2026-10-01", "000", name_index=True, name_index_variant="g64")
    assert str(caught.value) == "rich name-index variant checkpoint differs from the response target"
    assert calls == [
        "SELECT doc FROM history_manifest", "SELECT doc FROM rich_name_manifest_g64", "close",
        "DROP TEMPORARY TABLE IF EXISTS roots", "DROP TEMPORARY TABLE IF EXISTS ex",
        "DROP TEMPORARY TABLE IF EXISTS cr", "DROP TEMPORARY TABLE IF EXISTS cn",
    ]


@pytest.mark.parametrize("variant", ["", "g64;DROP", "../g64", "64", "G64"])
def test_rich_name_variant_validation_precedes_server_access(variant):
    with pytest.raises(ValueError) as caught:
        narrow.rich_name_index("unused", "narrow_test", variant=variant)
    assert str(caught.value) == f"invalid experimental database name: {variant!r}"


@pytest.mark.parametrize("granularity", [0, -1])
def test_rich_name_index_rejects_nonpositive_granularity(granularity):
    with pytest.raises(ValueError) as caught:
        narrow.rich_name_index("unused", "narrow_test", granularity=granularity)
    assert str(caught.value) == "rich name index granularity must be positive"


@pytest.mark.parametrize("field,label", [("narrow_name_index", "rich name"), ("narrow_parent_index", "directory parent")])
def test_optional_index_server_requires_numeric_target(field, label):
    from types import SimpleNamespace

    from dt_cloud.box.server import ChBox

    with pytest.raises(ValueError) as caught:
        ChBox(SimpleNamespace(), **{field: True}).start()
    assert str(caught.value) == f"the {label} index requires an experimental numeric target"


@pytest.mark.parametrize("marker_exists", [True, False])
@pytest.mark.parametrize("field,label,table", [
    ("narrow_name_index", "rich name", "rich_name_manifest"),
    ("narrow_parent_index", "directory parent", "parent_index_manifest"),
])
def test_optional_index_server_rejects_incomplete_or_foreign_checkpoint(marker_exists, field, label, table):
    from types import SimpleNamespace

    from dt_cloud.box.server import ChBox

    calls = []

    def scalar(sql):
        calls.append(sql)
        if sql == "SELECT doc FROM narrow_test.history_manifest":
            return json.dumps({"source_db": "default"})
        if sql == f"EXISTS TABLE narrow_test.{table}":
            return "1" if marker_exists else "0"
        return json.dumps({"target": "wrong_target"})

    ch = SimpleNamespace(scalar=scalar, close=lambda: calls.append("close"))
    box = ChBox(SimpleNamespace(db="default", session=lambda: ch), narrow_target="narrow_test", **{field: True})
    with pytest.raises(ValueError) as caught:
        box.start()
    assert str(caught.value) == (f"experimental {label} index checkpoint differs from the serving target" if marker_exists else
                                 f"experimental {label} index has no completed checkpoint")
    assert calls == ["SELECT doc FROM narrow_test.history_manifest", f"EXISTS TABLE narrow_test.{table}",
                     *([f"SELECT doc FROM narrow_test.{table}"] if marker_exists else []), "close"]
    assert box.narrow_manifest is None


@pytest.mark.parametrize("flag,label", [("-i", "rich-name"), ("-j", "directory-parent")])
def test_serve_cli_index_requires_target(flag, label):
    result = CliRunner().invoke(main, ["serve-query", "-A", "-e", "ch", flag, "unused"])
    assert result.exit_code == 2
    assert result.output.splitlines() == [
        "Usage: main serve-query [OPTIONS] ROOT",
        "Try 'main serve-query --help' for help.",
        "",
        f"Error: --narrow-{label}-index requires --engine ch and --narrow-target",
    ]


@pytest.mark.parametrize("plan", ["legacy", "visible"])
def test_serve_cli_forwards_both_index_flags(monkeypatch, plan):
    from dt_cloud.box import server

    calls = []

    def serve(box, **kwargs):
        calls.append((box.narrow_target, box.narrow_name_index, box.narrow_parent_index, box.narrow_plan, kwargs))

    monkeypatch.setattr(server, "serve", serve)
    result = CliRunner().invoke(main, ["serve-query", "-A", "-e", "ch", "-N", "narrow_test", "-ij", "-k", plan, "-p", "8087", "unused"])
    assert result.exit_code == 0, result.output
    assert result.output == ""
    assert calls == [("narrow_test", True, True, plan, {"bind": "0.0.0.0", "port": 8087, "token": None})]


def test_live_name_variant_requires_rich_index_before_connection() -> None:
    from types import SimpleNamespace

    from dt_cloud.box.server import ChBox

    with pytest.raises(ValueError) as caught:
        ChBox(SimpleNamespace(), narrow_target="narrow_test", narrow_name_variant="g64").start()
    assert str(caught.value) == "a rich name-index variant requires the rich name index"


@pytest.mark.parametrize("preloaded", [False, True])
@pytest.mark.parametrize("invalid", [None, "missing", "target", "variant", "view", "table"])
def test_live_name_variant_validates_checkpoint_and_health(invalid: str | None, preloaded: bool) -> None:
    from types import SimpleNamespace

    from dt_cloud.box.server import ChBox

    calls = []
    manifest = {"source_db": "default", "prefix": "b/u1", "dates": ["2026-10-01"]}
    marker = {"target": "narrow_test", "variant": "g64", "view": "metadata_by_name_g64", "table": "metadata_history_by_name_g64"}
    if invalid not in (None, "missing"):
        marker[invalid] = "wrong"

    def scalar(sql: str) -> str:
        calls.append(sql)
        if sql == "SELECT doc FROM narrow_test.history_manifest":
            return json.dumps(manifest)
        if sql == "EXISTS TABLE narrow_test.rich_name_manifest_g64":
            return "0" if invalid == "missing" else "1"
        if sql == "SELECT doc FROM narrow_test.rich_name_manifest_g64":
            return json.dumps(marker)
        raise AssertionError(f"unexpected query: {sql}")

    ch = SimpleNamespace(scalar=scalar, close=lambda: calls.append("close"))
    store = SimpleNamespace(db="default", session=lambda: ch, scans=lambda **kwargs: {})
    box = ChBox(store, narrow_target="narrow_test", narrow_name_index=True, narrow_name_variant="g64",
                narrow_manifest={"unvalidated": True} if preloaded else None)
    if invalid:
        with pytest.raises(ValueError) as caught:
            box.start()
        assert str(caught.value) == (
            "experimental rich name index has no completed checkpoint" if invalid == "missing" else
            "experimental rich name index checkpoint differs from the serving target" if invalid == "target" else
            "experimental rich name index variant checkpoint differs from the serving target"
        )
        assert box.narrow_manifest == ({"unvalidated": True} if preloaded else None)
    else:
        box.start()
        assert box.narrow_manifest == manifest
        assert box.health() == {
            "state": "ready", "engine": "ch", "scans": [], "narrow": {
                "target": "narrow_test", "prefix": "b/u1", "dates": ["2026-10-01"], "incremental": False,
                "name_index": True, "name_index_variant": "g64", "parent_index": False,
            },
        }
    assert calls == [
        "SELECT doc FROM narrow_test.history_manifest", "EXISTS TABLE narrow_test.rich_name_manifest_g64",
        *([] if invalid == "missing" else ["SELECT doc FROM narrow_test.rich_name_manifest_g64"]), "close",
    ]


@pytest.mark.parametrize("route", ["subtree", "diff"])
def test_live_routes_forward_name_variant(monkeypatch, route: str) -> None:
    from types import SimpleNamespace

    from dt_cloud.box import server

    calls = []

    def response(
        url: str,
        target: str,
        date: str,
        query: str,
        **kwargs: object,
    ) -> dict:
        calls.append((url, target, date, query, kwargs["name_index"], kwargs["name_index_variant"]))
        return {"body": {"selected_variant": kwargs["name_index_variant"]}}

    monkeypatch.setattr(narrow_serve, "response", response)
    store = SimpleNamespace(url="unused", syntax="simple", threads=2, root_label="root", scan=lambda date: object())
    box = server.ChBox(store, narrow_target="narrow_test", narrow_name_index=True, narrow_name_variant="g64",
                       narrow_manifest={"prefix": "b/u1", "dates": ["2026-09-30", "2026-10-01"]})
    qs = {"path": ["b/u1"], "q": ["000"]}
    if route == "subtree":
        qs["date"] = ["2026-10-01"]
        output = server.ch_subtree(box, qs)
    else:
        qs.update({"from": ["2026-09-30"], "to": ["2026-10-01"]})
        output = server.ch_diff(box, qs)
    assert json.loads("".join(output)) == {"selected_variant": "g64"}
    assert calls == [("unused", "narrow_test", "2026-10-01", "000", True, "g64")]


def test_serve_cli_forwards_name_variant(monkeypatch) -> None:
    from dt_cloud.box import server

    calls = []
    monkeypatch.setattr(server, "serve", lambda box, **kwargs: calls.append((box.narrow_target, box.narrow_name_index, box.narrow_name_variant, kwargs)))
    result = CliRunner().invoke(main, ["serve-query", "-A", "-e", "ch", "-N", "narrow_test", "-i", "-v", "g64", "unused"])
    assert (result.exit_code, result.output) == (0, "")
    assert calls == [("narrow_test", True, "g64", {"bind": "0.0.0.0", "port": 8080, "token": None})]


def test_serve_cli_name_variant_requires_rich_index() -> None:
    result = CliRunner().invoke(main, ["serve-query", "-A", "-e", "ch", "-N", "narrow_test", "-v", "g64", "unused"])
    assert result.exit_code == 2
    assert result.output.splitlines() == [
        "Usage: main serve-query [OPTIONS] ROOT",
        "Try 'main serve-query --help' for help.",
        "",
        "Error: --narrow-rich-name-variant requires --narrow-rich-name-index",
    ]


@pytest.mark.parametrize("variant", ["", "g64;DROP", "../g64", "64", "G64"])
def test_live_name_variant_validates_identifier_before_connection(variant: str) -> None:
    from types import SimpleNamespace

    from dt_cloud.box.server import ChBox

    with pytest.raises(ValueError) as caught:
        ChBox(SimpleNamespace(), narrow_target="narrow_test", narrow_name_index=True, narrow_name_variant=variant).start()
    assert str(caught.value) == f"invalid experimental database name: {variant!r}"


@pytest.mark.parametrize("path_free", [True, False])
@pytest.mark.parametrize("name_index,variant", [(True, None), (True, "g64"), (False, None)])
@pytest.mark.parametrize("parent_index", [True, False])
@pytest.mark.parametrize("ancestor_preaggregate", [True, False])
def test_response_cold_comparison_resets_both_engines(
    monkeypatch: pytest.MonkeyPatch,
    path_free: bool,
    name_index: bool,
    variant: str | None,
    parent_index: bool,
    ancestor_preaggregate: bool,
) -> None:
    from dt_cloud.bench import queryset
    from dt_cloud.bench.queryset import Case
    from dt_cloud.chstore import bench

    manifest = {"source_db": "default", "prefix": "b/u1", "dates": ["2026-10-01"]}
    monkeypatch.setattr(Ch, "scalar", lambda *a: json.dumps(manifest))
    monkeypatch.setattr(queryset, "load", lambda *a: [Case("absent", "absent", "simple", ("",), "")])
    calls = []
    monkeypatch.setattr(bench, "drop_caches", lambda url: calls.append("drop"))
    body = {"tier": "none", "tree": {"b": 0}}

    def response(*args, **kwargs):
        calls.append(("response", kwargs["path_free"], kwargs["name_index"], kwargs["parent_index"], kwargs["name_index_variant"], kwargs["ancestor_preaggregate"]))
        return {"body": body, "response_s": .5, "discovery_s": .25, "walk_s": .125, "bytes": 32}

    def compare(*args, **kwargs):
        calls.append("compare")
        return {"body": body, "response_s": 2.0, "bytes": 32}

    monkeypatch.setattr(narrow_serve, "response", response)
    monkeypatch.setattr(narrow_serve, "compare_response", compare)
    result = CliRunner().invoke(main, ["ch-narrow-response-bench", *(["-a"] if ancestor_preaggregate else []), *([] if path_free else ["-P"]), *(["-r"] if name_index else []), *(["-v", variant] if variant else []), *(["-j"] if parent_index else []), "-cC", "-d", "2026-10-01", "-n", "1", "-Q", "ignored", "narrow_test"])
    assert result.exit_code == 0, result.output
    assert calls == ["drop", ("response", path_free, name_index, parent_index, variant, ancestor_preaggregate), "drop", "compare"]
    row = json.loads(result.output)
    assert {k: v for k, v in row.items() if k != "sha"} == {
        "date": "2026-10-01", "previous": None, "query": "absent", "query_text": "absent", "syntax": "simple",
        "trial": 0, "prefix": "b/u1", "cold": True, "threads": 8,
        "response_s": .5, "discovery_s": .25, "walk_s": .125, "bytes": 32, "exact": True, "baseline": {"response_s": 2.0, "bytes": 32},
        "path_free": path_free, "name_index": name_index, "name_index_variant": variant, "parent_index": parent_index,
        "ancestor_preaggregate": ancestor_preaggregate,
        "comparison_phase": "interleaved",
    }
    assert row["sha"] == bench.normalize(json.dumps(body).encode())


@pytest.mark.parametrize("path_free", [True, False])
@pytest.mark.parametrize("name_index", [True, False])
@pytest.mark.parametrize("metadata_paths", [True, False])
def test_discovery_cold_comparison_resets_both_engines_each_trial(monkeypatch, path_free, name_index, metadata_paths):
    from dt_cloud.bench import queryset
    from dt_cloud.bench.queryset import Case
    from dt_cloud.chstore import bench

    manifest = {"source_db": "default", "prefix": "b/u1", "dates": ["2026-10-01"], "dbs": ["narrow_test_0"]}
    monkeypatch.setattr(Ch, "scalar", lambda *a: json.dumps(manifest))
    monkeypatch.setattr(queryset, "load", lambda *a: [Case("absent", "absent", "simple", ("",), "")])
    calls = []
    monkeypatch.setattr(bench, "drop_caches", lambda url: calls.append("drop"))
    result = {"roots": [], "n": 0, "md5": None, "b": 0, "o": 0,
              "discovery_s": .25, "materialize_s": .125, "late_metadata_s": .125}

    def evaluate(*args, **kwargs):
        calls.append(("evaluate", kwargs))
        return result.copy()

    def compare(*args, timings):
        calls.append("compare")
        timings.update(discovery_s=1.0, materialize_s=.125)
        return {k: result[k] for k in ("roots", "n", "md5", "b", "o")}

    monkeypatch.setattr(narrow, "evaluate", evaluate)
    monkeypatch.setattr(narrow, "compare", compare)
    got = CliRunner().invoke(main, ["ch-narrow-bench", *([] if path_free else ["-P"]), *(["-r"] if name_index else []), *([] if metadata_paths else ["-M"]), "-cC", "-n", "2", "-Q", "ignored", "narrow_test"])
    assert got.exit_code == 0, got.output
    assert calls == ["drop", ("evaluate", {"path_free": path_free, "name_index": name_index, "metadata_paths": metadata_paths}), "drop", "compare"] * 2
    assert [json.loads(line) for line in got.output.splitlines()] == [
        {"date": "2026-10-01", "query": "absent", "query_text": "absent", "syntax": "simple", "trial": trial, "prefix": "b/u1", "path_free": path_free, "cold": True, "name_index": name_index, "threads": 8, "metadata_paths": metadata_paths,
         **{k: v for k, v in result.items() if k != "roots"}, "exact": True,
         "baseline": {"discovery_s": 1.0, "materialize_s": .125}}
        for trial in range(2)
    ]


@pytest.mark.parametrize("metadata_paths", [True, False])
def test_payload_projection_keeps_discovery_and_exclusions(monkeypatch, metadata_paths):
    from types import SimpleNamespace

    calls = []
    result = SimpleNamespace(hit=False, roots=1, excluded=1, b=100, o=2, stats={"s": .25})
    ix = SimpleNamespace(settings={}, evaluate=lambda *a: result, roots_summary=lambda *a: (["b/hit"], 1, None))
    client = SimpleNamespace(exec=lambda sql, fmt=None: calls.append((sql, fmt)))
    monkeypatch.setattr(narrow, "ChIndex", lambda *a, **kw: ix)
    monkeypatch.setattr(narrow, "Ch", lambda *a, **kw: client)
    got = narrow.evaluate("unused", "narrow_test", "b", "hit -omit", metadata_paths=metadata_paths)
    source = "SELECT * FROM narrow_test.metadata WHERE pre IN (SELECT pre FROM roots)"
    assert calls == [
        (source if metadata_paths else f"SELECT * EXCEPT (path) FROM ({source})", "Null"),
        (f"SELECT {'*' if metadata_paths else '* EXCEPT (path)'} FROM metadata WHERE pre IN (SELECT pre FROM ex)", "Null"),
        ("DROP TEMPORARY TABLE IF EXISTS roots", None),
        ("DROP TEMPORARY TABLE IF EXISTS ex", None),
        ("DROP TEMPORARY TABLE IF EXISTS cr", None),
        ("DROP TEMPORARY TABLE IF EXISTS cn", None),
    ]
    assert {k: v for k, v in got.items() if k not in ("materialize_s", "late_metadata_s")} == {
        "roots": ["b/hit"], "n": 1, "md5": None, "b": 100, "o": 2, "excluded": 1,
        "discovery_s": .25, "stages": {"s": .25}, "renders_tree": False,
    }


def test_signature_and_summary():
    row = {"date": "2026-10-01", "trial": 0, "roots": [], "n": 0, "md5": None, "b": 0, "o": 0,
           "discovery_s": .25, "materialize_s": .125, "late_metadata_s": .125, "exact": True, "truth_exact": True,
           "baseline": {"discovery_s": 1.0}}
    assert narrow.signature(row) == {"n": 0, "md5": "d41d8cd98f00b204e9800998ecf8427e", "b": 0, "o": 0}
    assert narrow.summarize([row]) == {"2026-10-01/t0": {"n": 1, "exact": 1, "truth_checked": 1, "truth_exact": 1, "timings": {
        "discovery_s": {"p50": .25, "p90": .25, "max": .25},
        "materialize_s": {"p50": .125, "p90": .125, "max": .125},
        "late_metadata_s": {"p50": .125, "p90": .125, "max": .125},
        "combined_s": {"p50": .5, "p90": .5, "max": .5},
        "baseline_discovery_s": {"p50": 1.0, "p90": 1.0, "max": 1.0},
    }}}


def test_summary_cli_stdin():
    empty = {"date": "2026-10-01", "trial": 0, "n": 0, "exact": True,
             "discovery_s": 1.0, "materialize_s": 0.0, "late_metadata_s": 0.0}
    hit = {**empty, "n": 1, "discovery_s": .5}
    got = CliRunner().invoke(main, ["ch-narrow-summary", "-n", "-"], input=json.dumps(empty) + "\n" + json.dumps(hit) + "\n")
    assert got.exit_code == 0, got.output
    assert json.loads(got.output) == {"2026-10-01/t0": {"n": 1, "exact": 1, "truth_checked": 0, "truth_exact": 0, "timings": {
        "discovery_s": {"p50": .5, "p90": .5, "max": .5},
        "materialize_s": {"p50": 0.0, "p90": 0.0, "max": 0.0},
        "late_metadata_s": {"p50": 0.0, "p90": 0.0, "max": 0.0},
        "combined_s": {"p50": .5, "p90": .5, "max": .5},
    }}}


@pytest.mark.parametrize("covered", [True, False])
def test_bench_cli_independent_truth(monkeypatch, tmp_path, covered):
    from dt_cloud.bench import queryset
    from dt_cloud.bench.queryset import Case

    manifest = {"source_db": "default", "prefix": "b/u1", "dates": ["2026-10-01"], "dbs": ["narrow_test_0"]}
    monkeypatch.setattr(Ch, "scalar", lambda *a: json.dumps(manifest))
    monkeypatch.setattr(queryset, "load", lambda *a: [Case("absent", "absent", "simple", ("",), "")])
    result = {"roots": [], "n": 0, "md5": None, "b": 0, "o": 0,
              "discovery_s": .25, "materialize_s": .125, "late_metadata_s": .125}
    def evaluate(*args, **kwargs):
        assert kwargs == {"path_free": True, "name_index": False, "metadata_paths": True}
        return result.copy()

    monkeypatch.setattr(narrow, "evaluate", evaluate)

    def compare(*args, timings):
        timings.update(discovery_s=1.0, materialize_s=.125)
        return {k: result[k] for k in ("roots", "n", "md5", "b", "o")}

    monkeypatch.setattr(narrow, "compare", compare)
    (tmp_path / "summary.json").write_text(json.dumps({"queries": [{"id": "absent", "views": [{
        "view": "b/u1" if covered else "b", "roots": 0, "md5": "d41d8cd98f00b204e9800998ecf8427e", "bytes": 0, "objects": 0,
    }]}]}) + "\n")
    got = CliRunner().invoke(main, ["ch-narrow-bench", "-c", "-n", "1", "-Q", "ignored", "-T", f"2026-10-01={tmp_path}", "narrow_test"])
    assert got.exit_code == 0, got.output
    assert json.loads(got.output) == {
        "date": "2026-10-01", "query": "absent", "query_text": "absent", "syntax": "simple", "trial": 0, "prefix": "b/u1", "n": 0, "md5": None, "b": 0, "o": 0, "path_free": True, "cold": False, "name_index": False, "threads": 8, "metadata_paths": True,
        "discovery_s": .25, "materialize_s": .125, "late_metadata_s": .125,
        "exact": True, "baseline": {"discovery_s": 1.0, "materialize_s": .125}, "truth_covered": covered,
        **({"truth_exact": True} if covered else {}),
    }


@pytest.mark.parametrize("value", ["default; DROP DATABASE default", "../bad", "default.x", ""])
def test_database_identifier(value):
    with pytest.raises(ValueError, match="invalid experimental database name"):
        narrow.identifier(value)


@pytest.mark.parametrize("root_depth", [0, 2])
def test_numeric_parents_stream_in_depth_space_not_path_order(root_depth):
    import struct

    data = b"".join(struct.pack("<IB", pre, root_depth + depth) for pre, depth in [(0, 0), (1, 1), (2, 2), (3, 2), (4, 1), (5, 2)])
    chunks = (data[offset:offset + 3] for offset in range(0, len(data), 3))
    result = b"".join(narrow.preorder_parents(chunks, 6, root_depth=root_depth))
    assert list(struct.iter_unpack("<Iq", result)) == [(0, -1), (1, 0), (2, 1), (3, 1), (4, 0), (5, 4)]


@pytest.mark.parametrize("rows,count,error", [
    ([(1, 0)], 1, "noncontiguous preorder key: 1 != 0"),
    ([(0, 1)], 1, "first row must be the selected root"),
    ([(0, 0), (1, 2)], 2, "preorder depth has no parent: 2"),
    ([(0, 0), (1, 0)], 2, "only one selected root is allowed"),
    ([(0, 0)], 2, "numeric parent rows lost: 1 != 2"),
])
def test_numeric_parent_stream_rejects_invalid_tree(rows, count, error):
    import struct

    chunks = [b"".join(struct.pack("<IB", *row) for row in rows)]
    with pytest.raises(ValueError) as caught:
        list(narrow.preorder_parents(chunks, count, root_depth=0))
    assert str(caught.value) == error


def test_numeric_parent_stream_rejects_partial_record():
    with pytest.raises(ValueError) as caught:
        list(narrow.preorder_parents([b"\0"], 1, root_depth=0))
    assert str(caught.value) == "truncated numeric parent input"


@pytest.mark.parametrize("numeric_parents", [False, True])
@pytest.mark.parametrize("coalescer", ["window", "pair"])
def test_two_scan_union(ch_url, ch_db, tmp_path, monkeypatch, numeric_parents, coalescer):  # noqa: F811
    ch = Ch(ch_url, db=ch_db)
    for date, files in [("2026-09-30", A), ("2026-10-01", B)]:
        ci.Ingest(ch, date, write_v2(tmp_path / date, files)[0], threads=2, log=lambda *a: None).run()
    store = Store(ch_url, db=ch_db, threads=2)
    session = store.session
    metadata_joins = []
    dictionary_plans = []

    def small_blocks():
        client = session()
        client.settings["max_block_size"] = "2"
        original_exec = client.exec

        def observed_exec(sql, **kwargs):
            settings = kwargs.get("settings") or {}
            label = settings.get("log_comment", "").rsplit(":", 1)[-1]
            if label.startswith("history_metadata_"):
                metadata_joins.append((label, settings["join_algorithm"]))
            if label == "dictionary":
                dictionary_plans.append({k: settings[k] for k in narrow.dictionary_join_settings()})
            return original_exec(sql, **kwargs)

        client.exec = observed_exec
        return client

    monkeypatch.setattr(store, "session", small_blocks)
    target = f"narrow_test_{uuid.uuid4().hex[:10]}"
    try:
        manifest = narrow.build(store, target, "b/u1", ("2026-09-30", "2026-10-01"), union_engine="merge", log=lambda *a: None)
        assert dictionary_plans == [narrow.dictionary_join_settings()]
        assert {k: manifest[k] for k in ("prefix", "dates", "union_nodes", "incremental", "renders_tree")} == {
            "prefix": "b/u1", "dates": ("2026-09-30", "2026-10-01"), "union_nodes": 7, "incremental": False, "renders_tree": False,
        }
        assert ch.json(f"SELECT id, path FROM {target}.ids ORDER BY id") == [
            [0, "b/u1"], [1, "b/u1/ckpt"], [2, "b/u1/logs"], [3, "b/u1/ckpt/a.bin"],
            [4, "b/u1/ckpt/b.bin"], [5, "b/u1/ckpt/z.bin"], [6, "b/u1/logs/x.txt"],
        ]
        assert ch.json(f"SELECT nid, l FROM {target}.names ORDER BY nid") == [
            [0, "a.bin"], [1, "b.bin"], [2, "ckpt"], [3, "logs"], [4, "u1"], [5, "x.txt"], [6, "z.bin"],
        ]
        narrow.stream_intervals(ch, target, "b/u1")
        assert ch.json(f"SELECT id, pre, post FROM {target}.intervals_stream ORDER BY id") == ch.json(f"SELECT id, pre, post FROM {target}.intervals ORDER BY id")
        historical = narrow.history(store, target, batch_rows=2, numeric_parents=numeric_parents, coalescer=coalescer, log=lambda *a: None)
        assert historical["history_coalescer"] == coalescer
        assert metadata_joins == [(f"history_metadata_{suffix}", "full_sorting_merge" if numeric_parents or coalescer == "pair" else "grace_hash")
                                  for suffix in ("empty", 0, 2, 4, 6)]
        if numeric_parents:
            assert ch.json(f"SELECT pre, parent_pre FROM {target}.numeric_parents ORDER BY pre") == [[0, -1], [1, 0], [2, 1], [3, 1], [4, 1], [5, 0], [6, 5]]
            from dt_cloud.chstore.coalesce import benchmark as coalesce_benchmark

            coalesced = []
            coalesce_benchmark(ch, target, (0, 4), batch_rows=3, trials=1, temp_dir=tmp_path / "coalesce", emit=coalesced.append)
            assert [(r["lo"], r["hi"], r["table"], r["exact"], r["publication"], r["order"]) for r in coalesced] == [
                (0, 3, "metadata", True, False, ["window", "pair"]),
                (4, 7, "metadata", True, False, ["pair", "window"]),
            ]
            assert list((tmp_path / "coalesce").iterdir()) == []
            paired = list(narrow.parent_benchmark(ch_url, target, (0, 4), batch_rows=3, numeric_join="full_sorting_merge"))
            assert [(r["string_join"], r["numeric_join"]) for r in paired] == [("grace_hash", "full_sorting_merge")] * 4
            assert [{k: r[k] for k in ("date", "lo", "hi", "rows", "threads", "cold", "exact", "renders_tree", "order")} for r in paired] == [
                {"date": "2026-09-30", "lo": 0, "hi": 3, "rows": 3, "threads": 2, "cold": False, "exact": True, "renders_tree": False, "order": ["string", "numeric"]},
                {"date": "2026-09-30", "lo": 4, "hi": 7, "rows": 2, "threads": 2, "cold": False, "exact": True, "renders_tree": False, "order": ["numeric", "string"]},
                {"date": "2026-10-01", "lo": 0, "hi": 3, "rows": 3, "threads": 2, "cold": False, "exact": True, "renders_tree": False, "order": ["numeric", "string"]},
                {"date": "2026-10-01", "lo": 4, "hi": 7, "rows": 3, "threads": 2, "cold": False, "exact": True, "renders_tree": False, "order": ["string", "numeric"]},
            ]
        named = narrow.rich_name_index(ch_url, target)
        variant = narrow.rich_name_index(ch_url, target, granularity=2, variant="g64")
        assert {k: v for k, v in variant.items() if k != "seconds"} == {
            "target": target, "versions": 9, "granularity": 2, "production_cutover": False,
            "variant": "g64", "table": "metadata_history_by_name_g64", "view": "metadata_by_name_g64",
        }
        assert ch.json(f"SELECT * FROM {target}.metadata_history_by_name_g64 ORDER BY nid, pre, vf") == ch.json(
            f"SELECT * FROM {target}.metadata_history_by_name ORDER BY nid, pre, vf"
        )
        parents = narrow.directory_parent_index(ch_url, target)
        assert {k: v for k, v in parents.items() if k != "seconds"} == {
            "target": target, "directories": 3, "production_cutover": False,
        }
        assert ch.json(f"SELECT pre, parent_pre FROM {target}.directory_parents ORDER BY pre") == [[0, -1], [1, 0], [5, 0]]
        with pytest.raises(ValueError, match="experimental table already exists"):
            narrow.directory_parent_index(ch_url, target)
        assert {k: v for k, v in named.items() if k != "seconds"} == {
            "target": target, "versions": 9, "granularity": 8192, "production_cutover": False,
        }
        with pytest.raises(ValueError, match="experimental table already exists"):
            narrow.rich_name_index(ch_url, target)
        assert ch.json(f"SELECT pre, ancestors FROM {target}.hierarchy ORDER BY pre") == [
            [0, [0]], [1, [0, 1]], [5, [0, 5]],
        ]
        streamed = narrow.stream_hierarchy(ch, target)
        assert streamed["directories"] == 3
        assert ch.json(f"SELECT pre, ancestors FROM {target}.hierarchy_stream ORDER BY pre") == [
            [0, [0]], [1, [0, 1]], [5, [0, 5]],
        ]
        assert {k: historical[k] for k in ("history", "version_rows", "asof_coverage", "renders_tree")} == {
            "history": True, "version_rows": {"nodes": 9, "metadata": 9}, "asof_coverage": "selected scans only", "renders_tree": True,
        }
        assert narrow.audit(ch_url, target) == {
            "union_nodes": 7, "names": 7, "snapshots": {"2026-09-30": 6, "2026-10-01": 6},
            "checks": {"path_ids": True, "interval_ids": True, "preorder_ids": True, "name_ids": True,
                       "interval_bounds": True, "root_span": True, "preorder_tree": True, "dictionary_rows": True, "snapshot_rows": True},
        }
        from dt_cloud.bench.truth import md5_paths

        paths = [r[0] for r in ch.json(f"SELECT path FROM {target}.dictionary ORDER BY path")]
        assert narrow.path_fingerprint(ch, f"SELECT path FROM {target}.dictionary") == (7, md5_paths(paths))
        cases = [
            ("ckpt", [["b/u1/ckpt"], ["b/u1/ckpt"]], [150, 170], [2, 2]),
            (".bin", [["b/u1/ckpt/a.bin", "b/u1/ckpt/b.bin"], ["b/u1/ckpt/a.bin", "b/u1/ckpt/z.bin"]], [150, 170], [2, 2]),
            (".bin -b.bin", [["b/u1/ckpt/a.bin"], ["b/u1/ckpt/a.bin", "b/u1/ckpt/z.bin"]], [100, 170], [1, 2]),
            ("ckpt/", [["b/u1/ckpt/a.bin", "b/u1/ckpt/b.bin"], ["b/u1/ckpt/a.bin", "b/u1/ckpt/z.bin"]], [150, 170], [2, 2]),
            ("/ckpt/[^/]*\\.bin$/", [["b/u1/ckpt/a.bin", "b/u1/ckpt/b.bin"], ["b/u1/ckpt/a.bin", "b/u1/ckpt/z.bin"]], [150, 170], [2, 2]),
            ("b/u1 -logs", [["b/u1"], ["b/u1"]], [150, 170], [2, 2]),
            ("absent", [[], []], [0, 0], [0, 0]),
        ]
        for query, roots, sizes, objects in cases:
            for i, date in enumerate(manifest["dates"]):
                got = narrow.evaluate(ch_url, manifest["dbs"][i], "b/u1", query, threads=2)
                actual = {k: got[k] for k in ("roots", "n", "md5", "b", "o")}
                assert actual == {"roots": roots[i], "n": len(roots[i]), "md5": None, "b": sizes[i], "o": objects[i]}
                assert actual == narrow.compare(store, date, "b/u1", query)
                versioned = narrow.evaluate(ch_url, historical["dbs"][i], "b/u1", query, threads=2, name_index=True)
                assert {k: versioned[k] for k in ("roots", "n", "md5", "b", "o")} == actual
                projected = narrow.evaluate(ch_url, historical["dbs"][i], "b/u1", query, threads=2, name_index=True, metadata_paths=False)
                assert {k: projected[k] for k in ("roots", "n", "md5", "b", "o")} == actual
                rendered = narrow_serve.response(ch_url, target, date, query, threads=2, w=1280)
                assert rendered["body"] == subtree(store, date, "b/u1", query)
                assert narrow_serve.response(ch_url, target, date, query, threads=2, w=1280, name_index=True)["body"] == rendered["body"]
                assert narrow_serve.response(ch_url, target, date, query, threads=2, w=1280, name_index=True, name_index_variant="g64")["body"] == rendered["body"]
                assert narrow_serve.response(ch_url, target, date, query, threads=2, w=1280, name_index=True, parent_index=True)["body"] == rendered["body"]
                assert narrow_serve.response(ch_url, target, date, query, threads=2, w=1280, name_index=True, ancestor_preaggregate=True)["body"] == rendered["body"]
                if numeric_parents and coalescer == "pair":
                    assert narrow_serve.response(ch_url, target, date, query, threads=2, w=1280, name_index=True, bounded_joins=True, root_join="hash", leaf_intervals=True)["body"] == rendered["body"]
                    assert narrow_serve.response(ch_url, target, date, query, threads=2, w=1280, name_index=True, bounded_joins=True)["body"] == rendered["body"]
                    for root_join in ("hash", "grace_hash", "full_sorting_merge"):
                        assert narrow_serve.response(ch_url, target, date, query, threads=2, w=1280, name_index=True, root_join=root_join)["body"] == rendered["body"]
            rendered_diff = narrow_serve.response(ch_url, target, "2026-10-01", query, previous="2026-09-30", threads=2, w=1280)
            assert rendered_diff["body"] == diff(store, "2026-09-30", "2026-10-01", "b/u1", query)
            assert narrow_serve.response(ch_url, target, "2026-10-01", query, previous="2026-09-30", threads=2, w=1280, name_index=True)["body"] == rendered_diff["body"]
            assert narrow_serve.response(ch_url, target, "2026-10-01", query, previous="2026-09-30", threads=2, w=1280, name_index=True, name_index_variant="g64")["body"] == rendered_diff["body"]
            assert narrow_serve.response(ch_url, target, "2026-10-01", query, previous="2026-09-30", threads=2, w=1280, name_index=True, parent_index=True)["body"] == rendered_diff["body"]
            assert narrow_serve.response(ch_url, target, "2026-10-01", query, previous="2026-09-30", threads=2, w=1280, name_index=True, ancestor_preaggregate=True)["body"] == rendered_diff["body"]
            if numeric_parents and coalescer == "pair":
                assert narrow_serve.response(ch_url, target, "2026-10-01", query, previous="2026-09-30", threads=2, w=1280, name_index=True, bounded_joins=True, root_join="hash", leaf_intervals=True)["body"] == rendered_diff["body"]
                assert narrow_serve.response(ch_url, target, "2026-10-01", query, previous="2026-09-30", threads=2, w=1280, name_index=True, bounded_joins=True)["body"] == rendered_diff["body"]
                for root_join in ("hash", "grace_hash", "full_sorting_merge"):
                    assert narrow_serve.response(ch_url, target, "2026-10-01", query, previous="2026-09-30", threads=2, w=1280, name_index=True, root_join=root_join)["body"] == rendered_diff["body"]
        for view in ("b/u1/ckpt", "b/u1/ckpt/a.bin", "b/u1/logs"):
            for date in manifest["dates"]:
                for query in (".bin", "ckpt -b.bin", "absent"):
                    actual = narrow_serve.response(ch_url, target, date, query, path=view, threads=2, w=1280)["body"]
                    assert actual == subtree(store, date, view, query)
            actual = narrow_serve.response(ch_url, target, "2026-10-01", ".bin", path=view, previous="2026-09-30", threads=2, w=1280)["body"]
            assert actual == diff(store, "2026-09-30", "2026-10-01", view, ".bin")
        for view in ("b/u1/ckpt/b.bin", "b/u1/ckpt/z.bin"):
            actual = narrow_serve.response(ch_url, target, "2026-10-01", ".bin", path=view, previous="2026-09-30", threads=2, w=1280)["body"]
            assert actual == diff(store, "2026-09-30", "2026-10-01", view, ".bin")
        from dt_cloud.chstore.serve import NotFound

        with pytest.raises(NotFound):
            narrow_serve.response(ch_url, target, "2026-10-01", ".bin", path="b/u1/missing")
        with pytest.raises(NotFound):
            narrow_serve.response(ch_url, target, "2026-10-01", ".bin", path="b/u1/missing", previous="2026-09-30")
        with pytest.raises(ValueError, match="outside the experimental prefix"):
            narrow_serve.response(ch_url, target, "2026-10-01", ".bin", path="b/u10")
        from urllib.parse import urlencode

        from dt_cloud.box.server import ChBox
        from test_chserve import get, start

        box = ChBox(store, narrow_target=target, narrow_name_index=True, narrow_parent_index=True,
                    narrow_name_variant="g64" if numeric_parents else None,
                    narrow_plan="visible" if numeric_parents and coalescer == "pair" else "legacy")
        box.start()
        assert [box.narrow_covers("b/u1", "2026-10-01"), box.narrow_covers("b", "2026-10-01"),
                box.narrow_covers("b/u1", "2026-10-02"), box.narrow_covers("b/u1/ckpt", "2026-10-01"),
                box.narrow_covers("b/u10", "2026-10-01")] == [True, False, False, True, False]
        url, httpd = start(box)
        try:
            for query_path, expected in (
                ("/api/subtree?" + urlencode(dict(date="2026-10-01", path="b/u1", q="ckpt/", w=1280, h=896, matchLimit=1)),
                 subtree(store, "2026-10-01", "b/u1", "ckpt/", match_limit=1)),
                ("/api/diff?" + urlencode(dict([("from", "2026-09-30"), ("to", "2026-10-01"), ("path", "b/u1"), ("q", "ckpt"), ("w", 1280), ("h", 896)])),
                 diff(store, "2026-09-30", "2026-10-01", "b/u1", "ckpt")),
                ("/api/subtree?" + urlencode(dict(date="2026-10-01", path="b/u1", w=1280, h=896)),
                 subtree(store, "2026-10-01", "b/u1", None)),
                ("/api/subtree?" + urlencode(dict(date="2026-10-01", path="b", q="ckpt", w=1280, h=896)),
                 subtree(store, "2026-10-01", "b", "ckpt")),
                ("/api/subtree?" + urlencode(dict(date="2026-10-01", path="b/u1/ckpt", q=".bin", w=1280, h=896)),
                 subtree(store, "2026-10-01", "b/u1/ckpt", ".bin")),
                ("/api/diff?" + urlencode(dict([("from", "2026-09-30"), ("to", "2026-10-01"), ("path", "b/u1/ckpt/z.bin"), ("q", ".bin"), ("w", 1280), ("h", 896)])),
                 diff(store, "2026-09-30", "2026-10-01", "b/u1/ckpt/z.bin", ".bin")),
            ):
                status, engine, body = get(url + query_path)
                assert (status, engine, json.loads(body)) == (200, "box", expected)
            assert get(url + "/api/subtree?date=2026-10-01&path=b/u1&q=ckpt", token=None) == (401, "box", "unauthorized")
            assert get(url + "/api/subtree?date=2026-10-01&path=b/u1/missing&q=.bin") == (404, "box", "path not found")
        finally:
            httpd.shutdown()
        from dt_cloud.chstore import serve

        monkeypatch.setattr(serve, "HARD_CAP", 1)
        folded = narrow_serve.response(ch_url, target, "2026-10-01", "ckpt/", threads=2, w=1280, min_area=800000)
        assert folded["body"] == subtree(store, "2026-10-01", "b/u1", "ckpt/", min_area=800000)
        assert [folded["body"]["folded"], folded["body"]["matches"]] == [2, ["b/u1/ckpt/a.bin", "b/u1/ckpt/z.bin"]]
        grouped = narrow_serve.response(ch_url, target, "2026-10-01", "ckpt/", threads=2, w=1280,
                                        min_area=800000, ancestor_preaggregate=True)
        assert grouped["body"] == folded["body"]
        assert grouped["ancestor_grouping"] == {"b": {"roots": 2, "parents": 1}}
        assert narrow_serve.response(ch_url, target, "2026-10-01", "ckpt/", threads=2, w=1280,
                                     min_area=800000, bounded_joins=True)["body"] == folded["body"]
        for plan in ({"visible_intervals": True}, {"fold_parent_pruning": True}, {"visible_intervals": True, "fold_parent_pruning": True},
                     {"ancestor_bottom_up": True}, {"ancestor_bottom_up": True, "visible_intervals": True, "fold_parent_pruning": True}):
            assert narrow_serve.response(ch_url, target, "2026-10-01", "ckpt/", threads=2, w=1280,
                                         min_area=800000, bounded_joins=True, leaf_intervals=True, **plan)["body"] == folded["body"]
            assert narrow_serve.response(ch_url, target, "2026-10-01", "ckpt/", previous="2026-09-30", threads=2, w=1280,
                                         min_area=800000, bounded_joins=True, leaf_intervals=True, **plan)["body"] == diff(store, "2026-09-30", "2026-10-01", "b/u1", "ckpt/", min_area=800000)
        with pytest.raises(ValueError, match="experimental database already exists"):
            narrow.build(store, target, "b/u1", ("2026-09-30", "2026-10-01"))
        with pytest.raises(ValueError, match="snapshots resume refuses later table"):
            narrow.build(store, target, "b/u1", ("2026-09-30", "2026-10-01"), resume_from="snapshots")
        with pytest.raises(ValueError, match="snapshot checkpoint does not match"):
            narrow.build(store, target, "b/u1", ("2026-10-01", "2026-09-30"), resume_from="snapshots")
    finally:
        admin = Ch(ch_url, session=False)
        for db in (f"{target}_0", f"{target}_1", f"{target}_h0", f"{target}_h1", target):
            admin.exec(f"DROP DATABASE IF EXISTS {db} SYNC")


@pytest.mark.parametrize("stage,resume", [("snapshot_1", "snapshots-partial"), ("paths", "snapshots"), ("ids", "paths"), ("names_sorted", "intervals"), ("dictionary", "names"), ("metadata", "dictionary")])
def test_snapshot_checkpoint_resume(ch_url, ch_db, tmp_path, monkeypatch, stage, resume):  # noqa: F811
    dates = ("2026-09-30", "2026-10-01")
    for date, files in zip(dates, (A, B), strict=True):
        ci.Ingest(Ch(ch_url, db=ch_db), date, write_v2(tmp_path / date, files)[0], threads=2, log=lambda *a: None).run()
    store = Store(ch_url, db=ch_db, threads=2)
    target = f"narrow_test_{uuid.uuid4().hex[:10]}"
    execute = Ch.exec

    def fail_union(self, sql, **kwargs):
        failure_db = f"{target}_0" if stage == "metadata" else target
        if sql.startswith(f"CREATE TABLE {failure_db}.{stage} "):
            raise RuntimeError("injected stage failure")
        return execute(self, sql, **kwargs)

    try:
        monkeypatch.setattr(Ch, "exec", fail_union)
        with pytest.raises(RuntimeError, match="injected stage failure"):
            narrow.build(store, target, "b/u1", dates, union_engine="merge", interval_engine="stream", log=lambda *a: None)
        monkeypatch.setattr(Ch, "exec", execute)
        admin = Ch(ch_url, db=target)
        if resume in ("snapshots-partial", "names"):
            admin.exec("SYSTEM FLUSH LOGS")
        if resume == "snapshots-partial":
            assert [admin.scalar("EXISTS TABLE snapshot_manifest"), admin.scalar("SELECT count() FROM snapshot_0"), admin.scalar("EXISTS TABLE snapshot_1")] == ["0", "6", "0"]
            with pytest.raises(ValueError) as caught:
                narrow.build(store, target, "b/u1", tuple(reversed(dates)), resume_from=resume, snapshot_ranges=2)
            assert str(caught.value) == "snapshot recovery needs a completed matching query: snapshot_0"
        else:
            assert json.loads(admin.scalar("SELECT doc FROM snapshot_manifest")) == {
                "source_db": ch_db, "prefix": "b/u1", "dates": list(dates), "snapshot_counts": [6, 6],
            }
        result = narrow.build(store, target, "b/u1", dates, resume_from=resume, snapshot_ranges=2 if resume == "snapshots-partial" else 0,
                              union_engine="merge", interval_engine="stream", log=lambda *a: None)
        assert [result["union_nodes"], result["union_engine"], result["interval_engine"]] == [7, "merge", "stream"]
        if resume == "snapshots-partial":
            assert [result["snapshot_ranges"], result["reused_snapshots"]] == [2, [0]]
            assert sorted(k for k in result["timings"] if k.startswith("snapshot_0")) == []
        else:
            assert sorted(k for k in result["timings"] if k.startswith("snapshot")) == []
        if resume == "dictionary":
            assert sorted(k for k in result["timings"] if k in ("names_sorted", "names", "dictionary")) == []
        if resume == "names":
            assert sorted(k for k in result["timings"] if k in ("names_sorted", "names", "dictionary")) == ["dictionary"]
        narrow.history(store, target, log=lambda *a: None)
        for date in dates:
            assert narrow_serve.response(ch_url, target, date, "ckpt", threads=2, w=1280)["body"] == subtree(store, date, "b/u1", "ckpt")
    finally:
        monkeypatch.setattr(Ch, "exec", execute)
        admin = Ch(ch_url, session=False)
        for db in (f"{target}_0", f"{target}_1", f"{target}_h0", f"{target}_h1", target):
            admin.exec(f"DROP DATABASE IF EXISTS {db} SYNC")


def test_reappearance_and_rich_only_changes(ch_url, ch_db, tmp_path):  # noqa: F811
    # A deleted blob comes back with the same scalar values; then only mtime
    # changes. Scalar lifetimes must stay compact while rich metadata versions.
    changed = [(p, u, size, mt + 1 if p == "b/u1/ckpt/a.bin" else mt, lr, cls) for p, u, size, mt, lr, cls in A]
    dates = ("2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03")
    ch = Ch(ch_url, db=ch_db)
    for date, files in zip(dates, (A, B, A, changed), strict=True):
        ci.Ingest(ch, date, write_v2(tmp_path / date, files)[0], threads=2, log=lambda *a: None).run()
    store = Store(ch_url, db=ch_db, threads=2)
    target = f"narrow_test_{uuid.uuid4().hex[:10]}"
    try:
        narrow.build(store, target, "b/u1", dates, interval_engine="stream", union_engine="merge", log=lambda *a: None)
        manifest = narrow.history(store, target, log=lambda *a: None)
        assert manifest["version_rows"] == {"nodes": 12, "metadata": 15}
        admin = Ch(ch_url, db=target)
        assert admin.json("SELECT path, toString(vf), toString(vt) FROM nodes_history WHERE endsWith(path, '/b.bin') ORDER BY vf") == [
            ["b/u1/ckpt/b.bin", "2026-09-30 00:00:00", "2026-10-01 00:00:00"],
            ["b/u1/ckpt/b.bin", "2026-10-02 00:00:00", "2106-01-01 00:00:00"],
        ]
        for i, date in enumerate(dates):
            result = narrow.evaluate(ch_url, manifest["dbs"][i], "b/u1", "b.bin", threads=2)
            assert {k: result[k] for k in ("roots", "n", "b", "o")} == {
                "roots": [] if i == 1 else ["b/u1/ckpt/b.bin"], "n": 0 if i == 1 else 1, "b": 0 if i == 1 else 50, "o": 0 if i == 1 else 1,
            }
        for date in dates[-2:]:
            assert narrow_serve.response(ch_url, target, date, "ckpt", threads=2, w=1280)["body"] == subtree(store, date, "b/u1", "ckpt")
    finally:
        admin = Ch(ch_url, session=False)
        for db in [*[f"{target}_{i}" for i in range(4)], *[f"{target}_h{i}" for i in range(4)], target]:
            admin.exec(f"DROP DATABASE IF EXISTS {db} SYNC")


def test_folded_visible_parents(ch_url, tmp_path, monkeypatch):  # noqa: F811
    from dt_cloud.chstore import serve

    files = [(path, "alice", size, 20009, None, None) for path, size in [
        ("b/fold/a/keep/hit.bin", 600),
        ("b/fold/a/tiny/deep/hit.bin", 1),
        ("b/fold/a/tiny/other/hit.bin", 1),
        ("b/fold/b/tiny/hit.bin", 1),
        ("b/fold/c/hit.bin", 1),
    ]]
    date = "2026-10-10"
    target = f"narrow_test_{uuid.uuid4().hex[:10]}"
    source = f"{target}_source"
    Ch(ch_url, session=False).exec(f"CREATE DATABASE {source}")
    try:
        ci.Ingest(Ch(ch_url, db=source), date, write_v2(tmp_path / date, files)[0], threads=2, log=lambda *a: None).run()
        store = Store(ch_url, db=source, threads=2)
        narrow.build(store, target, "b/fold", (date,), interval_engine="stream", log=lambda *a: None)
        narrow.history(store, target, log=lambda *a: None)
        narrow.directory_parent_index(ch_url, target)
        monkeypatch.setattr(serve, "HARD_CAP", 1)
        actual = narrow_serve.response(ch_url, target, date, ".bin", threads=2, w=1280, min_area=300000)["body"]
        assert actual == subtree(store, date, "b/fold", ".bin", min_area=300000)
        assert narrow_serve.response(ch_url, target, date, ".bin", threads=2, w=1280, min_area=300000, parent_index=True)["body"] == actual
        assert narrow_serve.response(ch_url, target, date, ".bin", threads=2, w=1280, min_area=300000, ancestor_preaggregate=True)["body"] == actual
        assert narrow_serve.response(ch_url, target, date, ".bin", threads=2, w=1280, min_area=300000, bounded_joins=True)["body"] == actual
        for plan in ({"visible_intervals": True}, {"fold_parent_pruning": True}, {"visible_intervals": True, "fold_parent_pruning": True},
                     {"ancestor_bottom_up": True}, {"ancestor_bottom_up": True, "visible_intervals": True, "fold_parent_pruning": True}):
            for parent_index in (False, True):
                assert narrow_serve.response(ch_url, target, date, ".bin", threads=2, w=1280, min_area=300000,
                                             bounded_joins=True, parent_index=parent_index, **plan)["body"] == actual
        assert [actual["folded"], actual["matches"], actual["tree"]["b"], actual["tree"]["o"]] == [
            4, ["b/fold/a/keep/hit.bin", "b/fold/a/tiny/deep/hit.bin", "b/fold/a/tiny/other/hit.bin", "b/fold/b/tiny/hit.bin", "b/fold/c/hit.bin"], 604, 5,
        ]
    finally:
        admin = Ch(ch_url, session=False)
        for db in (f"{target}_0", f"{target}_h0", target, source):
            admin.exec(f"DROP DATABASE IF EXISTS {db} SYNC")


@pytest.mark.parametrize("drop_bucket", [False, True])
@pytest.mark.parametrize("numeric_ancestors", [False, True])
@pytest.mark.parametrize("coalescer", ["window", "pair"])
def test_global_frozen_history_and_bucket_changes(ch_url, tmp_path, drop_bucket, numeric_ancestors, coalescer):  # noqa: F811
    dates = ("2026-09-30", "2026-10-01")
    files = [
        [("b/foo/hit.bin", "alice", 10, 20009, None, None),
         ("c/hit/child.bin", None, 20, 20009, None, None),
         ("c/plain/nope.txt", "bob", 5, 20009, None, None)],
        [("b/foo/hit.bin", "alice", 12, 20009, None, None),
         ("c/hit/new.bin", None, 7, 20009, None, None),
         ("c/plain/nope.txt", "bob", 5, 20009, None, None),
         ("d/new/hit.bin", None, 2, 20009, None, None)],
    ]
    if drop_bucket:
        files[1] = files[1][1:]
    target = f"narrow_test_{uuid.uuid4().hex[:10]}"
    source = f"{target}_source"
    admin = Ch(ch_url, session=False)
    admin.exec(f"CREATE DATABASE {source}")
    try:
        ch = Ch(ch_url, db=source)
        for date, rows in zip(dates, files, strict=True):
            ci.Ingest(ch, date, write_v2(tmp_path / date, rows)[0], threads=2, allow_drop=drop_bucket, log=lambda *a: None).run()
        store = Store(ch_url, db=source, threads=2)
        narrow.build(store, target, "", dates, interval_engine="stream", union_engine="merge", snapshot_ranges=2, log=lambda *a: None)
        historical = narrow.history(store, target, batch_rows=3, numeric_ancestors=numeric_ancestors, coalescer=coalescer, log=lambda *a: None)
        streamed = narrow.stream_hierarchy(admin, target)
        assert streamed["directories"] == int(admin.scalar(f"SELECT count() FROM {target}.hierarchy"))
        assert admin.json(f"SELECT pre, ancestors FROM {target}.hierarchy_stream ORDER BY pre") == admin.json(f"SELECT pre, ancestors FROM {target}.hierarchy ORDER BY pre")
        assert historical["hierarchy_engine"] == ("stream" if numeric_ancestors else "string-join")
        assert historical["history_coalescer"] == coalescer
        narrow.rich_name_index(ch_url, target, granularity=2)
        narrow.directory_parent_index(ch_url, target)
        assert admin.json(f"SELECT pre, ancestors FROM {target}.hierarchy WHERE pre = 0") == [[0, [0]]]
        assert [admin.json(f"SELECT depth, path, b, o, kind, nc FROM {db}.metadata WHERE pre = 0") for db in historical["dbs"]] == [
            [[0, "", 35, 3, "dir", 2]], [[0, "", 14, 3, "dir", 2]] if drop_bucket else [[0, "", 26, 4, "dir", 3]],
        ]
        audit = narrow.audit(ch_url, target)
        assert audit["checks"] == {
            "path_ids": True, "interval_ids": True, "preorder_ids": True, "name_ids": True,
            "interval_bounds": True, "root_span": True, "preorder_tree": True,
            "dictionary_rows": True, "snapshot_rows": True,
        }
        for query in ("hit", ".bin", "hit -child", "c/hit/", "-child", "-c/", "absent", "foo"):
            for date, db in zip(dates, historical["dbs"], strict=True):
                result = narrow.evaluate(ch_url, db, "", query, threads=2, name_index=True)
                assert {k: result[k] for k in ("roots", "n", "md5", "b", "o")} == narrow.compare(store, date, "", query)
                for path in ("", "c", "c/hit"):
                    actual = narrow_serve.response(ch_url, target, date, query, path=path, threads=2, w=1280, name_index=True, parent_index=True)["body"]
                    assert actual == subtree(store, date, path, query)
            for path in ("", "b", "c", "c/hit", "d"):
                actual = narrow_serve.response(ch_url, target, dates[1], query, previous=dates[0], path=path, threads=2, w=1280, name_index=True, parent_index=True)["body"]
                assert actual == diff(store, dates[0], dates[1], path, query)
        if drop_bucket:
            with pytest.raises(narrow_serve.cs.NotFound) as caught:
                narrow_serve.response(ch_url, target, dates[1], "hit", path="b", threads=2)
            assert caught.value.args == ("b",)
    finally:
        for db in (f"{target}_0", f"{target}_1", f"{target}_h0", f"{target}_h1", target, source):
            admin.exec(f"DROP DATABASE IF EXISTS {db} SYNC")
