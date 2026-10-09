"""Ancestor expansion plans preserve the complete rich aggregate payload."""

from types import SimpleNamespace

import pytest

from dt_cloud.chstore.narrow_serve import ancestor_source, scalar_ancestors_bottom_up
from dt_cloud.chstore.client import Ch
from dt_cloud.chstore.serve import SUM_AGG

from chserver import ch_db, ch_url  # noqa: F401 — fixtures


@pytest.mark.parametrize("view_depth", [0, 1])
@pytest.mark.parametrize("empty", [False, True])
def test_bottom_up_mixed_depths_and_zero_bytes(  # noqa: F811
    ch_url: str,
    ch_db: str,
    view_depth: int,
    empty: bool,
) -> None:
    ch = Ch(ch_url, db=ch_db, max_threads=2, max_memory_usage=8 << 30,
            max_bytes_before_external_group_by=256 << 20, max_bytes_ratio_before_external_group_by=0,
            max_bytes_before_external_sort=256 << 20, max_bytes_ratio_before_external_sort=0)
    try:
        ch.exec("""CREATE TABLE IF NOT EXISTS metadata ENGINE = Memory AS SELECT * FROM
            values('pre UInt32, parent_pre Int64', (0, -1), (1, 0), (2, 1), (4, 1), (5, 4), (7, 4), (9, 0), (10, 1), (11, 10))""")
        ch.tmp("rn_b", """SELECT * FROM values('pre UInt32, parent_pre Int64, depth UInt8, b Int64',
            (2, 1, 2, 3), (5, 4, 3, 7), (7, 4, 3, 0), (9, 0, 1, 11), (11, 10, 3, 0))"""
            + (" WHERE 0" if empty else " WHERE depth > 1" if view_depth else ""))
        actual = scalar_ancestors_bottom_up(ch, ch_db, "b", view_depth)
        expected = [] if empty else [{"depth": 2, "rows": 2}] + ([{"depth": 1, "rows": 1}] if view_depth == 0 else [])
        assert actual == expected
        assert ch.json("SELECT pre, b FROM anc0_b ORDER BY pre") == (
            [] if empty else ([[1, 10]] if view_depth == 0 else []) + [[4, 7], [10, 0]]
        )
    finally:
        ch.close()


@pytest.mark.parametrize("preaggregate", [False, True])
def test_ancestor_source(preaggregate: bool) -> None:
    calls = []
    ch = SimpleNamespace(tmp=lambda name, sql, **kwargs: calls.append((name, " ".join(sql.split()), kwargs)))
    source = ancestor_source(ch, "frozen_h0", "b", preaggregate=preaggregate)
    expected_calls = []
    table = "rn_b"
    if preaggregate:
        table = "ancestor_parents_b"
        expected_calls = [(
            table,
            "SELECT parent_pre, sum(b) AS b, sum(o) AS o, sum(wts) AS wts, sum(wb) AS wb, "
            "max(a) AS a, sum(c2) AS c2, sum(c3) AS c3, sum(c4) AS c4, sumMap(ub) AS ub "
            "FROM rn_b GROUP BY parent_pre",
            {"disk": True, "ordered": False},
        )]
    assert calls == expected_calls
    assert " ".join(source.split()) == (
        f"(SELECT r.*, arrayJoin(h.ancestors) AS ancestor FROM {table} r INNER JOIN ("
        f" SELECT pre, ancestors FROM frozen_h0.hierarchy WHERE pre IN (SELECT parent_pre FROM {table})) h ON r.parent_pre = h.pre)"
    )


@pytest.mark.parametrize("empty", [False, True])
def test_sibling_aggregation_payload(  # noqa: F811
    ch_url: str,
    ch_db: str,
    empty: bool,
) -> None:
    ch = Ch(ch_url, db=ch_db, max_threads=2)
    try:
        ch.exec("CREATE TABLE IF NOT EXISTS hierarchy (pre UInt32, ancestors Array(UInt32)) ENGINE = Memory")
        if not empty:
            ch.exec("INSERT INTO hierarchy VALUES (1, [0, 1]), (3, [0, 1, 3])")
        ch.tmp("rn_b", """SELECT toUInt32(if(number < 2, 1, 3)) AS parent_pre,
            toInt64(number + 2) AS b, toInt64(1) AS o, toFloat64(number + 1) / 2 AS wts,
            toFloat64(2) AS wb, toInt64(number) AS a, toInt64(number) AS c2,
            toInt64(0) AS c3, toInt64(1) AS c4, map('u', toInt64(number + 2)) AS ub
            FROM numbers(3)""" + (" WHERE 0" if empty else ""))
        expected = [] if empty else [
            [1, 9, 3, 3, 6, 2, 3, 0, 3, {"u": 9}],
            [3, 4, 1, 1.5, 2, 2, 2, 0, 1, {"u": 4}],
        ]
        for preaggregate in (False, True):
            source = ancestor_source(ch, ch_db, "b", preaggregate=preaggregate)
            assert ch.json(f"SELECT ancestor, {SUM_AGG} FROM {source} WHERE ancestor > 0 GROUP BY ancestor ORDER BY ancestor") == expected
    finally:
        ch.close()
