"""Episode equivalence, including zero IDs, disappearance and rich-only changes."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from dt_cloud.chstore import coalesce
from dt_cloud.chstore.client import Ch
from dt_cloud.chstore.coalesce import pair_query, window_query
from dt_cloud.cli import main

from chserver import ch_db, ch_url  # noqa: F401 — fixtures


def test_pair_sql_emits_presence_guarded_versions() -> None:
    query = pair_query(["SELECT * FROM old", "SELECT * FROM new"], ["pre", "b"], ["b"], ["2026-10-04", "2026-10-05"])
    expected = """
        SELECT v.2 AS pre, v.3 AS b, v.4 AS vf, v.5 AS vt FROM (
            SELECT arrayJoin(arrayFilter(v -> v.1, [
                tuple(o.present, o.pre, o.b, toDateTime('2026-10-04', 'UTC'),
                    if(n.present AND NOT (tuple(o.b) != tuple(n.b)),
                        toDateTime('2106-01-01 00:00:00', 'UTC'), toDateTime('2026-10-05', 'UTC'))),
                tuple(n.present AND (NOT o.present OR tuple(o.b) != tuple(n.b)),
                    n.pre, n.b, toDateTime('2026-10-05', 'UTC'), toDateTime('2106-01-01 00:00:00', 'UTC'))
            ])) AS v
            FROM (SELECT *, toUInt8(1) AS present FROM (SELECT * FROM old)) o
            FULL OUTER JOIN (SELECT *, toUInt8(1) AS present FROM (SELECT * FROM new)) n ON o.pre = n.pre
        )
    """
    # Whitespace is formatting, but every SQL token/operator is specified.
    assert " ".join(query.split()) == " ".join(expected.split())


def test_window_sql_preserves_episode_boundaries() -> None:
    query = window_query(["SELECT * FROM old", "SELECT * FROM new"], ["pre", "b"], ["b"], ["2026-10-04", "2026-10-05"])
    expected = """
        SELECT pre, any(b) AS b,
            [toDateTime('2026-10-04', 'UTC'), toDateTime('2026-10-05', 'UTC'), toDateTime('2106-01-01 00:00:00', 'UTC')][min(tick) + 1] AS vf,
            [toDateTime('2026-10-04', 'UTC'), toDateTime('2026-10-05', 'UTC'), toDateTime('2106-01-01 00:00:00', 'UTC')][max(tick) + 2] AS vt
        FROM (SELECT *, sum(fresh) OVER (PARTITION BY pre ORDER BY tick) AS episode FROM (
            SELECT *, tick = 0 OR previous.1 + 1 != tick OR previous.2 != tuple(b) AS fresh FROM (
                SELECT *, lagInFrame(tuple(tick, tuple(b))) OVER (
                    PARTITION BY pre ORDER BY tick ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS previous
                FROM (SELECT * FROM old UNION ALL SELECT * FROM new)))) GROUP BY pre, episode
    """
    assert " ".join(query.split()) == " ".join(expected.split())


@pytest.mark.parametrize("parts,instants", [
    (["old"], ["2026-10-04"]),
    (["old", "new"], ["2026-10-04"]),
    (["old", "new"], ["2026-10-04", "2026-10-04"]),
    (["old", "new"], ["2026-10-05", "2026-10-04"]),
    (["old", "new", "later"], ["2026-10-03", "2026-10-04", "2026-10-05"]),
])
def test_pair_refuses_other_temporal_domains(parts: list[str], instants: list[str]) -> None:
    with pytest.raises(ValueError) as caught:
        pair_query(parts, ["pre", "b"], ["b"], instants)
    assert str(caught.value) == "pair coalescing requires exactly two increasing snapshot instants"


@pytest.mark.parametrize("columns,values", [([], ["b"]), (["b", "pre"], ["b"]), (["pre", "b"], [])])
def test_pair_requires_explicit_identity_and_values(columns: list[str], values: list[str]) -> None:
    with pytest.raises(ValueError) as caught:
        pair_query(["old", "new"], columns, values, ["2026-10-04", "2026-10-05"])
    assert str(caught.value) == "pair coalescing requires pre-led columns and comparison values"


def test_pair_and_window_emit_exact_rich_versions(ch_url: str, ch_db: str) -> None:  # noqa: F811
    ch = Ch(ch_url, db=ch_db, max_threads=2, max_block_size=2, join_algorithm="full_sorting_merge", join_use_nulls=0)
    try:
        for table in ("old", "new"):
            ch.exec(f"CREATE TABLE {table} (pre UInt32, path String, b Int64, ub Map(String, Int64), wts Float64) ENGINE = MergeTree ORDER BY pre")
        ch.exec("""INSERT INTO old VALUES
            (0,'root',10,{'a':10},4.5), (1,'changed',20,{'a':20},2),
            (2,'vanished',30,{},3), (4,'owner',40,{'a':40},4),
            (5,'stamp',50,{},5.25), (6,'zero',0,{},0)""")
        ch.exec("""INSERT INTO new VALUES
            (0,'root',10,{'a':10},4.5), (1,'changed',21,{'a':21},2),
            (3,'born',35,{},3.5), (4,'owner',40,{'b':40},4),
            (5,'stamp',50,{},5.5), (6,'zero',0,{},0)""")
        a, b, end = "2026-10-04 00:00:00", "2026-10-05 00:00:00", "2106-01-01 00:00:00"
        expected = [
            [0, "root", 10, {"a": 10}, 4.5, a, end],
            [1, "changed", 20, {"a": 20}, 2, a, b], [1, "changed", 21, {"a": 21}, 2, b, end],
            [2, "vanished", 30, {}, 3, a, b], [3, "born", 35, {}, 3.5, b, end],
            [4, "owner", 40, {"a": 40}, 4, a, b], [4, "owner", 40, {"b": 40}, 4, b, end],
            [5, "stamp", 50, {}, 5.25, a, b], [5, "stamp", 50, {}, 5.5, b, end],
            [6, "zero", 0, {}, 0, a, end],
        ]
        binary = []
        for make in (window_query, pair_query):
            query = make(["SELECT toUInt16(0) AS tick, * FROM old", "SELECT toUInt16(1) AS tick, * FROM new"],
                         ["pre", "path", "b", "ub", "wts"], ["b", "ub", "wts"], [a, b])
            ordered = f"SELECT * FROM ({query}) ORDER BY pre, vf"
            assert ch.json(ordered) == expected
            binary.append(b"".join(ch.stream(ordered, "RowBinary")))
        assert binary[0] == binary[1]
        assert [int(ch.scalar(f"SELECT count() FROM {table}")) for table in ("old", "new")] == [6, 6]
    finally:
        ch.close()


@pytest.mark.parametrize("chunks,exact", [
    ([b"a", b"bcd"], True),
    ([b"abcde"], False),
    ([b"abc"], False),
    ([b"ab", b"ce"], False),
])
def test_benchmark_compares_literal_bytes_across_chunk_boundaries(monkeypatch, tmp_path: Path, chunks: list[bytes], exact: bool) -> None:
    calls = []
    scalar_queries = []
    manifest = {"dates": ["2026-10-04", "2026-10-05"], "union_nodes": 8, "dbs": ["source_0", "source_1"], "prefix": ""}

    def scalar(query: str) -> str:
        scalar_queries.append(query)
        return json.dumps(manifest) if query == "SELECT doc FROM target.manifest" else str(65 << 30)

    def execute(query: str, **kwargs: object) -> None:
        calls.append((query, kwargs))

    def stream(query: str, fmt: str, *, settings: dict):
        calls.append((query, {"fmt": fmt, "settings": settings}))
        return iter([b"ab", b"cd"] if "window_plan" in query else chunks)

    monkeypatch.setattr(coalesce, "window_query", lambda *a: "SELECT window_plan")
    monkeypatch.setattr(coalesce, "pair_query", lambda *a: "SELECT pair_plan")
    ch = SimpleNamespace(scalar=scalar, exec=execute, stream=stream)
    rows = []
    kwargs = dict(table="nodes", batch_rows=3, threads=2, trials=2, temp_dir=tmp_path, emit=rows.append)
    if exact:
        coalesce.benchmark(ch, "target", (2,), **kwargs)
    else:
        with pytest.raises(ValueError) as caught:
            coalesce.benchmark(ch, "target", (2,), **kwargs)
        assert str(caught.value) == "coalesced rows differ at nodes range [2, 5)"
    assert scalar_queries == ["SELECT doc FROM target.manifest", "SELECT min(free_space) FROM system.disks"]
    assert [(r["lo"], r["hi"], r["order"], r["result_bytes"], r["exact"], r["publication"]) for r in rows] == (
        [(2, 5, ["window", "pair"], 4, True, False), (2, 5, ["pair", "window"], 4, True, False)] if exact
        else [(2, 5, ["window", "pair"], 4, False, False)]
    )
    settings = {"max_threads": 2, "max_block_size": 8192, "max_memory_usage": 8 << 30,
                "max_bytes_before_external_sort": 256 << 20, "max_bytes_ratio_before_external_sort": 0,
                "max_bytes_before_external_group_by": 256 << 20, "max_bytes_ratio_before_external_group_by": 0,
                "join_algorithm": "full_sorting_merge", "join_use_nulls": 0}
    expected = []
    for trial, order in enumerate((["window", "pair"], ["pair", "window"]) if exact else (["window", "pair"],)):
        label = f"coalesce:target:nodes:2:{trial}"
        expected.extend((f"SELECT {plan}_plan", {"fmt": "Null", "settings": {**settings, "log_comment": f"{label}:{plan}"}}) for plan in order)
        expected.extend((f"SELECT * FROM (SELECT {plan}_plan) ORDER BY pre, vf",
                         {"fmt": "RowBinary", "settings": {**settings, "log_comment": f"{label}:{plan}:verify"}}) for plan in ("window", "pair"))
    assert calls == expected
    assert list(tmp_path.iterdir()) == []


def test_benchmark_cli_forwards_bounds_and_refuses_overwrite(monkeypatch, tmp_path: Path) -> None:
    from dt_cloud.chstore import client

    events = []
    ch = SimpleNamespace(close=lambda: events.append("closed"))
    monkeypatch.setattr(client, "Ch", lambda *a, **kw: ch)

    def run(actual_ch, target: str, starts: tuple[int, ...], **kwargs: object) -> None:
        emit = kwargs.pop("emit")
        events.append((actual_ch is ch, target, starts, kwargs))
        emit({"exact": True, "publication": False})

    monkeypatch.setattr(coalesce, "benchmark", run)
    output = tmp_path / "result.jsonl"
    args = ["ch-narrow-coalesce-bench", "-s0", "-s4", "-b3", "-n1", "-t4", "-knodes", "-T", str(tmp_path), "-o", str(output), "target"]
    result = CliRunner().invoke(main, args)
    assert (result.exit_code, result.output) == (0, '{"exact": true, "publication": false}\n')
    expected = [(True, "target", (0, 4), {"table": "nodes", "batch_rows": 3, "threads": 4, "trials": 1, "temp_dir": tmp_path}), "closed"]
    assert events == expected
    assert output.read_text() == '{"exact": true, "publication": false}\n'
    result = CliRunner().invoke(main, args)
    assert isinstance(result.exception, FileExistsError)
    assert output.read_text() == '{"exact": true, "publication": false}\n'
    assert events == [*expected, "closed"]
