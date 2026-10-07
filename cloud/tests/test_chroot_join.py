"""Opt-in rich-root joins retain exact payloads and existing serving limits."""

import json

import pytest
from click.testing import CliRunner

from dt_cloud.bench import queryset
from dt_cloud.bench.query import parse
from dt_cloud.bench.queryset import Case
from dt_cloud.chstore import narrow_serve
from dt_cloud.chstore.client import Ch
from dt_cloud.cli import main

from chserver import ch_db, ch_url  # noqa: F401


@pytest.mark.parametrize("plan", narrow_serve.ROOT_JOINS)
def test_root_join_settings(plan: str) -> None:
    expected = {} if plan == "default" else {
        "join_algorithm": plan, "query_plan_join_swap_table": "false", "max_block_size": 8192,
        "max_bytes_in_join": 0, "max_bytes_before_external_join": 0, "max_bytes_ratio_before_external_join": 0,
    }
    if plan == "grace_hash":
        expected.update(grace_hash_join_initial_buckets=16, max_bytes_in_join=512 << 20,
                        max_bytes_before_external_join=512 << 20, max_bytes_ratio_before_external_join=0,
                        join_overflow_mode="throw")
    assert narrow_serve.root_join_settings(plan) == expected


def test_invalid_root_join_before_connection() -> None:
    with pytest.raises(ValueError) as caught:
        narrow_serve.response("unused", "target", "2026-10-05", "needle", root_join="unknown")
    assert str(caught.value) == "unknown root join plan: unknown"


@pytest.mark.parametrize("ids,expected,error", [
    ([], ["a", "b"], None), (["b"], ["b"], None), (["b", "a"], ["a", "b"], None),
    (["missing"], [], "Error: unknown query IDs: missing"),
])
def test_response_query_selection(monkeypatch, ids, expected, error) -> None:
    monkeypatch.setattr(Ch, "scalar", lambda *args: json.dumps({"source_db": "default", "prefix": "", "dates": ["2026-10-05"]}))
    monkeypatch.setattr(queryset, "load", lambda *args: [Case(k, k, "simple", ("",), "") for k in ("a", "b")])
    calls = []

    def response(url, target, date, query, **kwargs):
        calls.append(query)
        return {"body": {"ok": True}, "response_s": .5}

    monkeypatch.setattr(narrow_serve, "response", response)
    result = CliRunner().invoke(main, ["ch-narrow-response-bench", "-n", "1", "-d", "2026-10-05", "-Q", "unused",
                                      *[arg for ident in ids for arg in ("-q", ident)], "target"])
    assert result.exit_code == (2 if error else 0), result.output
    assert calls == expected
    if error:
        assert result.stderr.splitlines()[-1] == error
    else:
        assert [json.loads(line)["query"] for line in result.stdout.splitlines()] == expected


@pytest.mark.parametrize("plan", narrow_serve.ROOT_JOINS)
@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("leaf_intervals", [False, True])
@pytest.mark.parametrize("extra,flags", [
    ({}, []), ({"visible_intervals": True}, ["-k"]), ({"fold_parent_pruning": True}, ["-e"]),
    ({"visible_intervals": True, "fold_parent_pruning": True}, ["-ke"]),
    ({"ancestor_bottom_up": True}, ["-u"]),
])
def test_root_join_cli(
    monkeypatch,
    plan: str,
    bounded: bool,
    leaf_intervals: bool,
    extra: dict,
    flags: list[str],
) -> None:
    manifest = {"source_db": "default", "prefix": "", "dates": ["2026-10-05"]}
    monkeypatch.setattr(Ch, "scalar", lambda *args: json.dumps(manifest))
    monkeypatch.setattr(queryset, "load", lambda *args: [Case("q", "needle", "simple", ("",), "")])
    calls = []

    def response(*args, **kwargs):
        calls.append(kwargs)
        return {"body": {"ok": True}, "response_s": .5}

    monkeypatch.setattr(narrow_serve, "response", response)
    result = CliRunner().invoke(main, ["ch-narrow-response-bench", *flags, *(["-B"] if bounded else []), *(["-l"] if leaf_intervals else []), "-J", plan, "-n", "1", "-d", "2026-10-05", "-Q", "unused", "target"])
    assert result.exit_code == 0, result.output
    assert calls == [{"previous": None, "syntax": "simple", "threads": 8, "path_free": True,
                      "name_index": False, "parent_index": False, "name_index_variant": None,
                      "ancestor_preaggregate": False, **({"root_join": plan} if plan != "default" else {}),
                      **({"bounded_joins": True} if bounded else {}), **({"leaf_intervals": True} if leaf_intervals else {}), **extra}]
    assert json.loads(result.stdout).get("root_join", "default") == plan
    assert json.loads(result.stdout).get("bounded_joins", False) == bounded
    assert json.loads(result.stdout).get("leaf_intervals", False) == leaf_intervals
    assert {key: json.loads(result.stdout).get(key, False) for key in ("visible_intervals", "fold_parent_pruning", "ancestor_bottom_up")} == {
        key: extra.get(key, False) for key in ("visible_intervals", "fold_parent_pruning", "ancestor_bottom_up")
    }


@pytest.mark.parametrize("plan", narrow_serve.ROOT_JOINS)
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("leaf_intervals", [False, True])
def test_root_join_payload(  # noqa: F811
    ch_url: str,
    ch_db: str,
    plan: str,
    empty: bool,
    leaf_intervals: bool,
) -> None:
    ch = Ch(ch_url, db=ch_db, max_threads=2, max_memory_usage=8 << 30,
            max_bytes_before_external_sort=256 << 20, max_bytes_ratio_before_external_sort=0)
    ch.settings.update({key: str(value) for key, value in narrow_serve.root_join_settings("grace_hash").items()})
    try:
        ch.exec("""CREATE TABLE IF NOT EXISTS metadata ENGINE = Memory AS SELECT
            toUInt32(number) AS pre, toInt64(0) AS parent_pre, concat('root/', toString(number)) AS path,
            toUInt8(2) AS depth, toInt64(number + 2) AS b, toInt64(-1) AS o,
            toFloat64(number + 1) / 2 AS wts, toFloat64(-1) AS wb, toInt64(number) AS a,
            toInt64(number) AS c2, toInt64(0) AS c3, toInt64(1) AS c4,
            map('u', toInt64(number + 2)) AS ub, toUInt8(0) AS kind, toInt64(-1) AS nc
            FROM numbers(4)""")
        ch.tmp("roots", "SELECT toUInt32(number) AS pre, toUInt32(if(number = 1, 2, number)) AS post FROM numbers(3)" + (" WHERE 0" if empty else ""))
        assert narrow_serve.materialize_root_rows(ch, ch_db, parse("needle"), "b", name_index=False,
                                                  hit=False, large_roots=True, root_join=plan, leaf_intervals=leaf_intervals) == "rn_b"
        assert ch.json("SELECT * FROM rn_b ORDER BY pre") == ([] if empty else [
            [0, 0, 0, "root/0", 2, 2, 0, .5, 0, 0, 0, 0, 1, {"u": 2}, 0, -1],
            [1, 2, 0, "root/1", 2, 3, 0, 1, 0, 1, 1, 0, 1, {"u": 3}, 0, -1],
            [2, 2, 0, "root/2", 2, 4, 0, 1.5, 0, 2, 2, 0, 1, {"u": 4}, 0, -1],
        ])
        assert ch.settings["max_memory_usage"] == str(8 << 30)
    finally:
        ch.close()
