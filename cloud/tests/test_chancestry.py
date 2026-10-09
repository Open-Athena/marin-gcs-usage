"""Stable keys need no DFS numbering, including newly appended descendants."""

import json

import pytest
from click.testing import CliRunner

from dt_cloud.chstore import ancestry, bench, narrow
from dt_cloud.chstore.ancestry import outer_roots_sql
from dt_cloud.chstore.client import Ch
from dt_cloud.cli import main

from chserver import ch_db, ch_url  # noqa: F401 — fixtures


def test_ancestor_query_is_numeric_and_parent_bounded():
    assert outer_roots_sql("test", "candidates", "hierarchy") == """WITH positive AS (SELECT id, parent_id, b, o FROM test.candidates),
        blocked AS (
            SELECT id FROM (
                SELECT id, ancestors FROM test.hierarchy WHERE id IN (SELECT parent_id FROM positive)
            ) ARRAY JOIN ancestors AS ancestor
            WHERE ancestor IN (SELECT id FROM positive) GROUP BY id
        )
        SELECT id, b, o FROM positive WHERE parent_id NOT IN (SELECT id FROM blocked)"""


@pytest.mark.parametrize("column", [0, 1, 2])
def test_ancestor_query_rejects_injected_identifiers(column):
    values = ["test", "candidates", "hierarchy"]
    values[column] = "bad; DROP TABLE candidates"
    with pytest.raises(ValueError, match="invalid experimental database name"):
        outer_roots_sql(*values)


def test_temporary_candidates_are_not_database_qualified():
    assert outer_roots_sql("test", "candidates", "hierarchy", temporary=True) == outer_roots_sql("test", "candidates", "hierarchy").replace("test.candidates", "candidates")


def test_selected_parent_hierarchy_can_be_session_bound():
    assert outer_roots_sql("test", "candidates", "hierarchy", temporary=True, temporary_hierarchy=True) == (
        outer_roots_sql("test", "candidates", "hierarchy").replace("test.candidates", "candidates").replace("test.hierarchy", "hierarchy")
    )


def test_ancestry_bench_resets_both_engines_and_keeps_full_artifact(monkeypatch, tmp_path):
    from dt_cloud.bench import queryset
    from dt_cloud.bench.queryset import Case

    monkeypatch.setattr(Ch, "scalar", lambda *a: json.dumps({"source_db": "default"}))
    monkeypatch.setattr(queryset, "load", lambda *a: [Case("hit", "hit", "simple", ("",), "")])
    calls = []
    result = {"date": "2026-10-01", "prefix": "b", "roots": ["b/hit"], "n": 1, "md5": None, "b": 100, "o": 2,
              "discovery_s": .25, "materialize_s": .125, "renders_tree": False, "incremental": False}
    monkeypatch.setattr(bench, "drop_caches", lambda url: calls.append("drop"))

    def evaluate(*args, threads):
        calls.append(("evaluate", threads))
        return result.copy()

    def compare(*args, timings):
        calls.append(("compare", args[0].threads))
        timings.update(discovery_s=1.0, materialize_s=.5)
        return {k: result[k] for k in ("roots", "n", "md5", "b", "o")}

    monkeypatch.setattr(ancestry, "evaluate", evaluate)
    monkeypatch.setattr(narrow, "compare", compare)
    out = tmp_path / "result.jsonl"
    got = CliRunner().invoke(main, ["ch-ancestry-bench", "-cC", "-n", "1", "-t", "4", "-Q", "ignored", "-o", str(out), "narrow_test"])
    assert got.exit_code == 0, got.output
    assert calls == ["drop", ("evaluate", 4), "drop", ("compare", 4)]
    expected = {"query": "hit", "trial": 0, "cold": True, "threads": 4, **result, "exact": True,
                "baseline": {"discovery_s": 1.0, "materialize_s": .5}}
    assert json.loads(out.read_text()) == expected
    assert json.loads(got.output) == {k: v for k, v in expected.items() if k != "roots"}


@pytest.mark.parametrize("threads", [0, -1])
def test_ancestry_session_rejects_invalid_threads(threads):
    with pytest.raises(ValueError) as caught:
        ancestry.session("unused", "narrow_test", threads=threads)
    assert str(caught.value) == "ancestry threads must be positive"


def test_ancestry_build_refuses_existing_table(monkeypatch):
    from types import SimpleNamespace

    calls = []

    def scalar(sql):
        calls.append(sql)
        return "1"

    monkeypatch.setattr(ancestry, "session", lambda *a: SimpleNamespace(scalar=scalar, close=lambda: calls.append("close")))
    with pytest.raises(ValueError) as caught:
        ancestry.build("ignored", "narrow_test", "2026-10-01")
    assert str(caught.value) == "experimental table already exists: narrow_test.ancestry_nodes"
    assert calls == ["EXISTS TABLE ancestry_nodes", "close"]


@pytest.mark.parametrize("query", ["hit -omit", "hit other", "hit|other", "dir/hit", "*.bin"])
def test_ancestry_rejects_unimplemented_query_shapes(query):
    from dt_cloud.bench.duck import Unsupported

    with pytest.raises(Unsupported, match="ancestry experiment only supports one unanchored literal without NOT"):
        ancestry.evaluate("unused", "narrow_test", "ancestry_nodes", query)


def test_sparse_ids_and_appended_descendants(ch_url, ch_db):  # noqa: F811
    ch = Ch(ch_url, db=ch_db)
    ch.exec("CREATE TABLE test_hierarchy (id UInt64, ancestors Array(UInt64)) ENGINE = Memory")
    ch.exec("INSERT INTO test_hierarchy VALUES (40,[40]),(6,[40,6]),(90,[40,6,90]),(2,[40,2])")
    ch.exec("CREATE TABLE candidates (id UInt64, parent_id Int64, b Int64, o Int64) ENGINE = Memory")
    # IDs deliberately do not follow depth-first order or parent-before-child.
    ch.exec("INSERT INTO candidates VALUES (6,40,100,2),(90,6,70,1),(17,90,70,1),(18,2,30,1)")
    sql = outer_roots_sql(ch_db, "candidates", "test_hierarchy")
    assert ch.json(sql + " ORDER BY id") == [[6, 100, 2], [18, 30, 1]]
    # A later directory and blob append without renumbering any existing key.
    ch.exec("INSERT INTO test_hierarchy VALUES (1000,[40,6,1000])")
    ch.exec("INSERT INTO candidates VALUES (1001,1000,5,1)")
    assert ch.json(sql + " ORDER BY id") == [[6, 100, 2], [18, 30, 1]]
    # A disjoint new root remains visible, while its new descendant is folded.
    ch.exec("INSERT INTO candidates VALUES (2,40,35,2)")
    assert ch.json(sql + " ORDER BY id") == [[2, 35, 2], [6, 100, 2]]
    ch.exec("INSERT INTO candidates VALUES (40,-1,135,4)")
    assert ch.json(sql + " ORDER BY id") == [[40, 135, 4]]
    ch.close()


def test_opaque_ancestry_keys_exceed_uint32(ch_url, ch_db):  # noqa: F811
    root = 1 << 40
    ch = Ch(ch_url, db=ch_db)
    try:
        ch.exec("CREATE TABLE wide_hierarchy (id UInt64, ancestors Array(UInt64)) ENGINE = Memory")
        ch.exec(f"INSERT INTO wide_hierarchy VALUES ({root},[{root}]),(7,[{root},7]),({root + 1},[{root},{root + 1}])")
        ch.exec("CREATE TABLE wide_candidates (id UInt64, parent_id Int64, b Int64, o Int64) ENGINE = Memory")
        ch.exec(f"INSERT INTO wide_candidates VALUES (7,{root},100,2),({root + 5},7,70,1),({root + 3},{root + 1},30,1)")
        sql = outer_roots_sql(ch_db, "wide_candidates", "wide_hierarchy")
        assert ch.json(sql + " ORDER BY id") == [[7, 100, 2], [root + 3, 30, 1]]
        ch.exec(f"INSERT INTO wide_candidates VALUES ({root},-1,130,3)")
        assert ch.json(sql + " ORDER BY id") == [[root, 130, 3]]
    finally:
        ch.close()


def test_ancestry_build_and_literal_results(ch_url, ch_db):  # noqa: F811
    ch = Ch(ch_url, db=ch_db)
    try:
        ch.exec("CREATE TABLE nodes (pre UInt32, nid UInt32, b Int64, o Int64) ENGINE = MergeTree ORDER BY pre")
        ch.exec("INSERT INTO nodes VALUES (0,0,130,3),(6,1,100,2),(90,2,70,1),(17,3,70,1),(2,4,30,1),(18,5,30,1)")
        ch.exec("CREATE TABLE metadata (pre UInt32, parent_pre Int64) ENGINE = MergeTree ORDER BY pre")
        ch.exec("INSERT INTO metadata VALUES (0,-1),(6,0),(90,6),(17,90),(2,0),(18,2)")
        ch.exec("CREATE TABLE names (nid UInt32,l String, INDEX tl l TYPE text(tokenizer = ngrams(3))) ENGINE = MergeTree ORDER BY l")
        ch.exec("INSERT INTO names VALUES (0,'b'),(1,'hit'),(2,'dir'),(3,'hit-a'),(4,'plain'),(5,'hit-b')")
        ch.exec("CREATE TABLE nodes_by_name (pre UInt32,nid UInt32,path String) ENGINE = MergeTree ORDER BY (nid,pre)")
        ch.exec("INSERT INTO nodes_by_name VALUES (0,0,'b'),(6,1,'b/hit'),(90,2,'b/hit/dir'),(17,3,'b/hit/dir/hit-a'),(2,4,'b/plain'),(18,5,'b/plain/hit-b')")
        ch.exec("CREATE TABLE hierarchy (pre UInt32,ancestors Array(UInt32)) ENGINE = Memory")
        ch.exec("INSERT INTO hierarchy VALUES (0,[0]),(6,[0,6]),(90,[0,6,90]),(2,[0,2])")
        ch.exec("CREATE TABLE history_manifest (doc String) ENGINE = TinyLog")
        manifest = {"prefix": "b", "dates": ["2026-10-01"], "dbs": [ch_db]}
        ch.exec(f"INSERT INTO history_manifest VALUES ('{json.dumps(manifest)}')")
        result = ancestry.build(ch_url, ch_db, "2026-10-01")
        assert {k: v for k, v in result.items() if k != "seconds"} == {
            "target": ch_db, "date": "2026-10-01", "prefix": "b", "source_db": ch_db,
            "table": "ancestry_nodes", "nodes": 6, "incremental": False,
        }
        result = ancestry.evaluate(ch_url, ch_db, "ancestry_nodes", "hit")
        assert {k: v for k, v in result.items() if k not in ("discovery_s", "materialize_s")} == {
            "date": "2026-10-01", "prefix": "b", "roots": ["b/hit", "b/plain/hit-b"], "n": 2,
            "md5": None, "b": 130, "o": 3, "renders_tree": False, "incremental": False,
        }
        result = ancestry.evaluate(ch_url, ch_db, "ancestry_nodes", "absent")
        assert {k: v for k, v in result.items() if k not in ("discovery_s", "materialize_s")} == {
            "date": "2026-10-01", "prefix": "b", "roots": [], "n": 0,
            "md5": None, "b": 0, "o": 0, "renders_tree": False, "incremental": False,
        }
    finally:
        ch.close()
