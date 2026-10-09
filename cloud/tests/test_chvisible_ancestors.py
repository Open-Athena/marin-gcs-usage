"""Visible interval maps preserve inclusive ancestry, gaps and parent identity."""

from bisect import bisect_right
from itertools import combinations
from types import SimpleNamespace

import pytest

from dt_cloud.chstore import visible_ancestors as va
from dt_cloud.chstore.client import Ch
from dt_cloud.chstore.narrow_serve import root_join_settings

from chserver import ch_db, ch_url  # noqa: F401


def test_visible_regions() -> None:
    assert list(va.regions([(5, 7), (1, 4), (2, 2), (3, 4)])) == [
        (0, ()), (1, (1,)), (2, (1, 2)), (3, (1, 3)), (5, (5,)), (8, ()),
    ]
    assert list(va.regions([])) == [(0, ())]
    assert list(va.regions([(0, 0), (0xFFFFFFFF, 0xFFFFFFFF)])) == [
        (0, (0,)), (1, ()), (0xFFFFFFFF, (0xFFFFFFFF,)), (0x100000000, ()),
    ]


def test_every_visible_subset_matches_interval_oracle() -> None:
    forest = [(0, 9), (1, 4), (2, 2), (3, 4), (4, 4), (5, 8), (6, 8), (7, 7), (8, 8), (9, 9)]
    for count in range(len(forest) + 1):
        for selected in combinations(forest, count):
            rows = list(va.regions(selected))
            positions = [position for position, _ in rows]
            actual = [rows[bisect_right(positions, parent) - 1][1] for parent in range(11)]
            expected = [tuple(pre for pre, post in selected if pre <= parent <= post) for parent in range(11)]
            assert actual == expected


@pytest.mark.parametrize("intervals,error", [
    ([(1, 2), (1, 3)], "invalid or duplicate visible preorder interval"),
    ([(-1, 2)], "invalid or duplicate visible preorder interval"),
    ([(2, 1)], "invalid or duplicate visible preorder interval"),
    ([(0, 0x100000000)], "invalid or duplicate visible preorder interval"),
    ([(1, 4), (2, 5)], "crossing visible preorder intervals"),
])
def test_invalid_visible_regions(intervals, error: str) -> None:
    with pytest.raises(ValueError) as caught:
        list(va.regions(intervals))
    assert str(caught.value) == error


@pytest.mark.parametrize("limit", ["MAX_INTERVALS", "MAX_MEMBERSHIPS"])
def test_oversized_maps_fall_back_before_materialization(monkeypatch, limit: str) -> None:
    monkeypatch.setattr(va, limit, 1)
    writes = []
    ch = SimpleNamespace(json=lambda sql: [(1, 4), (2, 2)], tmp=lambda *args: writes.append(args))
    assert va.source(ch, "frozen_h0", "b", 10.) is None
    assert writes == []


def test_visible_source_asof_parent_boundaries(ch_url, ch_db):  # noqa: F811
    ch = Ch(ch_url, db=ch_db, max_threads=2, **root_join_settings("hash"))
    try:
        ch.exec("CREATE TABLE nodes (pre UInt32, post UInt32) ENGINE = Memory")
        ch.exec("INSERT INTO nodes VALUES (1, 4), (2, 2), (5, 7), (8, 9)")
        ch.tmp("anc0_b", "SELECT pre, toInt64(if(pre = 8, 1, 10)) AS b FROM nodes")
        ch.tmp("rn_b", "SELECT toInt64(number) - 1 AS parent_pre, toInt64(number + 2) AS b FROM numbers(11)")
        selected = va.source(ch, ch_db, "b", 10.)
        assert ch.json(f"SELECT parent_pre, ancestor, b FROM {selected} ORDER BY parent_pre, ancestor") == [
            [1, 1, 4], [2, 1, 5], [2, 2, 5], [3, 1, 6], [4, 1, 7],
            [5, 5, 8], [6, 5, 9], [7, 5, 10],
        ]
        assert ch.json("SELECT shard, start, ancestors FROM visible_ranges_b ORDER BY start") == [
            [0, 0, []], [0, 1, [1]], [0, 2, [1, 2]], [0, 3, [1]], [0, 5, [5]], [0, 8, []],
        ]
    finally:
        ch.close()
