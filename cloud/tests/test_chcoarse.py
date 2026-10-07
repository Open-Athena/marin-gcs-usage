from itertools import accumulate, product
from json import dumps

import pytest

from dt_cloud.chstore.client import Ch, lit
from dt_cloud.chstore.coarse import CoarseRequest, NameIndex, byte_ranks, diff, diff_oracle, oracle, select_rows
from dt_cloud.chstore.range_bench import NonLeafMatches, Prefix
from dt_cloud.chstore.scoped_pattern import build as build_scoped

from chserver import ch_db, ch_url  # noqa: F401


def test_quantiles_find_all_heavy_contiguous_groups() -> None:
    # Exhaust every small byte distribution and every adjacent partition.
    # Each group at/above the threshold must contain a selected byte rank.
    for weights in product(range(4), repeat=4):
        totals = list(accumulate(weights, initial=0))
        for budget in range(1, 6):
            threshold, ranks = byte_ranks(totals[-1], budget)
            assert len(ranks) <= budget
            selected = [bisect_index(totals, rank) for rank in ranks]
            for split in range(5):
                actual = [any(lo <= i < hi for i in selected) for lo, hi in [(0, split), (split, 4)]]
                required = [totals[hi] - totals[lo] >= threshold for lo, hi in [(0, split), (split, 4)]]
                assert [hit or not heavy for hit, heavy in zip(actual, required)] == [True, True]


def bisect_index(totals: list[int], rank: int) -> int:
    return next(i for i in range(len(totals) - 1) if totals[i] < rank <= totals[i + 1])


@pytest.mark.parametrize("pattern,expected", [(".JSON", (17, 4, 3)), ("zarr.json", (17, 4, 3)), ("%", (0, 0, 0)), ("_", (0, 0, 0))])
def test_subtree_first_pattern_is_complete_without_global_names(index, ch_db, ch_url, pattern, expected) -> None:
    ch = Ch(ch_url, db=ch_db)
    try:
        scoped = build_scoped(ch, ch_db, "2026-10-04", "a", pattern, max_nodes=5)
        body = scoped.view(ch, "a")
        tree = body["tree"]
        assert (tree["b"], tree["o"], tree["matches"]) == expected
        assert (scoped.root, scoped.name_ids, scoped.vocabulary_names, scoped.build_stages) == ("a", None, 0, {"union_nodes": 5})
        assert oracle(ch, scoped, body) is True
        with pytest.raises(CoarseRequest) as caught:
            scoped.view(ch, "b")
        assert str(caught.value) == "path outside the frozen index"
    finally:
        ch.close()


def test_subtree_first_pattern_refuses_budget_and_nonleaf(index, ch_db, ch_url) -> None:
    ch = Ch(ch_url, db=ch_db)
    try:
        with pytest.raises(CoarseRequest) as caught:
            build_scoped(ch, ch_db, "2026-10-04", "a", ".json", max_nodes=4)
        assert str(caught.value) == "scoped union interval exceeds its 4-node work budget"
        with pytest.raises(NonLeafMatches) as caught:
            build_scoped(ch, ch_db, "2026-10-04", "a", "nest", max_nodes=5)
        assert (caught.value.posting_rows, caught.value.nonleaf_rows) == (1, 1)
    finally:
        ch.close()


def test_select_sparse_blocks_with_zero_weights() -> None:
    prefix = Prefix([[0, 3, 5, 3], [2, 2, 0, 2], [4, 2, 9, 2]])
    assert select_rows(prefix, [1, 5, 6, 14], [[0, 0], [1, 5], [2, 0], [8, 0], [9, 0], [16, 0], [17, 9]], 4) == [1, 1, 17, 17]
    assert byte_ranks(0, 64) == (1, [])
    assert byte_ranks(10, 3) == (4, [4, 8])


@pytest.fixture(scope="module")
def index(ch_db, ch_url):
    ch = Ch(ch_url, db=ch_db)
    nodes = [
        (0, 12, 0, ""), (1, 5, 1, "a"), (2, 2, 2, "a/zarr.json"),
        (3, 4, 2, "a/nest"), (4, 4, 3, "a/nest/zarr.json"), (5, 5, 2, "a/extra.zarr.json"),
        (6, 11, 1, "b"), (7, 8, 2, "b/deeper"), (8, 8, 3, "b/deeper/zarr.json"),
        (9, 9, 2, "b/zarr.json"), (10, 10, 2, "b/unmatched"), (11, 11, 2, "b/unmatched2"),
        (12, 12, 1, "zarr.json"),
    ]
    values = {2: (9, 1), 4: (3, 2), 8: (18, 3), 9: (0, 1), 12: (6, 1)}
    rollups = {0: (41, 9), 1: (17, 4), 3: (3, 2), 6: (18, 4), 7: (18, 3), 5: (5, 1), **values}
    ch.exec("CREATE TABLE dictionary (pre UInt32, post UInt32, depth UInt8, path String) ENGINE = Memory")
    ch.exec("INSERT INTO dictionary VALUES " + ",".join(f"({pre},{post},{depth},{lit(path)})" for pre, post, depth, path in nodes))
    path_ids = {path: pre for pre, _, _, path in nodes}
    ch.exec("CREATE TABLE numeric_parents (pre UInt32, parent_pre Int64) ENGINE = Memory")
    ch.exec("INSERT INTO numeric_parents VALUES " + ",".join(
        f"({pre},{path_ids[path.rsplit('/', 1)[0] if '/' in path else ''] if path else -1})" for pre, _, _, path in nodes
    ))
    ch.exec("CREATE TABLE hierarchy (pre UInt32, ancestors Array(UInt32)) ENGINE = Memory")
    directory_ancestors = []
    for pre, post, _, path in nodes:
        if pre == post:
            continue
        segments = path.split("/") if path else []
        ancestors = [0] + [path_ids["/".join(segments[:i])] for i in range(1, len(segments) + 1)]
        directory_ancestors.append(f"({pre},{ancestors})")
    ch.exec("INSERT INTO hierarchy VALUES " + ",".join(directory_ancestors))
    ch.exec("CREATE TABLE nodes (pre UInt32, post UInt32, path String, nid UInt32, b UInt64, o UInt64) ENGINE = Memory")
    ch.exec("INSERT INTO nodes VALUES " + ",".join(
        f"({pre},{post},{lit(path)},{1 if pre in values else 2 if pre == 1 else 3 if pre == 5 else 4 if pre == 3 else 0},{rollups.get(pre, (0, 0))[0]},{rollups.get(pre, (0, 0))[1]})"
        for pre, post, _, path in nodes
    ))
    ch.exec("CREATE VIEW nodes_by_name AS SELECT * FROM nodes")
    ch.exec("""CREATE VIEW metadata_by_parent AS SELECT n.pre AS pre, n.path AS path, n.b AS b, n.o AS o, d.pre AS parent_pre
        FROM nodes n LEFT JOIN dictionary d ON d.path = if(position(n.path, '/') = 0, '', substring(n.path, 1, length(n.path) - position(reverse(n.path), '/')))""")
    ch.exec("CREATE TABLE names (nid UInt32, l String) ENGINE = Memory")
    ch.exec("INSERT INTO names VALUES (1,'zarr.json'), (2,'a'), (3,'extra.zarr.json'), (4,'nest')")
    ch.exec("CREATE TABLE history_manifest (doc String) ENGINE = Memory")
    after_db = ch_db + "_after"
    ch.exec(f"CREATE DATABASE {after_db}")
    ch.exec(f"CREATE TABLE {after_db}.nodes AS {ch_db}.nodes ENGINE = Memory")
    after_values = {8: (10, 2), 9: (20, 1), 12: (6, 1)}
    after_rollups = {0: (36, 4), 6: (30, 3), 7: (10, 2), **after_values}
    ch.exec(f"INSERT INTO {after_db}.nodes VALUES " + ",".join(
        f"({pre},{post},{lit(path)},{1 if pre in after_values else 0},{after_rollups.get(pre, (0, 0))[0]},{after_rollups.get(pre, (0, 0))[1]})"
        for pre, post, _, path in nodes if not 1 <= pre <= 5
    ))
    ch.exec(f"CREATE VIEW {after_db}.nodes_by_name AS SELECT * FROM {after_db}.nodes")
    ch.exec(f"""CREATE VIEW {after_db}.metadata_by_parent AS SELECT n.pre AS pre, n.path AS path, n.b AS b, n.o AS o, d.pre AS parent_pre
        FROM {after_db}.nodes n LEFT JOIN {ch_db}.dictionary d ON d.path = if(position(n.path, '/') = 0, '', substring(n.path, 1, length(n.path) - position(reverse(n.path), '/')))""")
    ch.exec(f"INSERT INTO history_manifest VALUES ({lit(dumps({'dates': ['2026-10-04', '2026-10-05'], 'dbs': [ch_db, after_db], 'prefix': ''}))})")
    try:
        yield NameIndex.build(ch, ch_db, "2026-10-04", "ZARR.JSON", 1024)
    finally:
        ch.exec(f"DROP DATABASE {after_db}")
        ch.close()


@pytest.mark.parametrize("budget,expected", [
    (1, []), (3, [("b", 18, 4, 2), ("a", 12, 3, 2)]),
    (6, [("b", 18, 4, 2), ("a", 12, 3, 2), ("zarr.json", 6, 1, 1)]),
])
def test_exact_root_partition(index, ch_db, ch_url, budget, expected) -> None:
    ch = Ch(ch_url, db=ch_db)
    try:
        body = index.view(ch, "", budget)
        tree = body["tree"]
        assert (tree["b"], tree["o"], tree["matches"]) == (36, 8, 5)
        assert [(child["path"], child["b"], child["o"], child["matches"]) for child in tree["children"]] == expected
        assert tree["other"] == {"b": 36 - sum(c[1] for c in expected), "o": 8 - sum(c[2] for c in expected),
                                 "matches": 5 - sum(c[3] for c in expected)}
        assert oracle(ch, index, body) is True
    finally:
        ch.close()


def test_drill_and_leaf(index, ch_db, ch_url) -> None:
    ch = Ch(ch_url, db=ch_db)
    try:
        tree = index.view(ch, "a", 2)["tree"]
        assert tree["children"] == [{"pre": 2, "path": "a/zarr.json", "label": "zarr.json", "b": 9, "o": 1, "matches": 1, "leaf": True}]
        assert tree["other"] == {"b": 3, "o": 2, "matches": 1}
        leaf = index.view(ch, "a/zarr.json", 2)["tree"]
        assert leaf == {"pre": 2, "path": "a/zarr.json", "label": "zarr.json", "b": 9, "o": 1, "matches": 1,
                        "leaf": True, "children": [], "other": {"b": 0, "o": 0, "matches": 0}}
        zero = index.view(ch, "b/zarr.json", 2)["tree"]
        assert zero == {"pre": 9, "path": "b/zarr.json", "label": "zarr.json", "b": 0, "o": 1, "matches": 1,
                        "leaf": True, "children": [], "other": {"b": 0, "o": 0, "matches": 0}}
    finally:
        ch.close()


def test_coarse_route_reuses_bounded_summary(index, ch_db, ch_url) -> None:
    from json import loads
    from types import SimpleNamespace

    from dt_cloud.box.server import ChBox, ch_coarse

    box = ChBox(SimpleNamespace(url=ch_url, threads=1), narrow_target=ch_db,
                narrow_manifest={"prefix": "", "dates": ["2026-10-04"]})
    qs = {"date": ["2026-10-04"], "name": ["ZARR.JSON"], "path": ["a"], "budget": ["2"]}
    first = loads("".join(ch_coarse(box, qs)))
    second = loads("".join(ch_coarse(box, qs)))
    assert [first["cache_hit"], second["cache_hit"]] == [False, True]
    assert len(box.coarse_indexes) == 1
    assert first["tree"] == second["tree"] == {
        "pre": 1, "path": "a", "label": "a", "b": 12, "o": 3, "matches": 2, "leaf": False,
        "children": [{"pre": 2, "path": "a/zarr.json", "label": "zarr.json", "b": 9, "o": 1, "matches": 1, "leaf": True}],
        "other": {"b": 3, "o": 2, "matches": 1},
    }


def test_coarse_route_refuses_nonleaf_names(index, ch_db, ch_url) -> None:
    from types import SimpleNamespace

    from dt_cloud.box.server import ChBox, HttpError, ch_coarse

    box = ChBox(SimpleNamespace(url=ch_url, threads=1), narrow_target=ch_db,
                narrow_manifest={"prefix": "", "dates": ["2026-10-04"]})
    with pytest.raises(HttpError) as caught:
        list(ch_coarse(box, {"date": ["2026-10-04"], "name": ["a"]}))
    assert (caught.value.status, caught.value.msg) == (501, "exact-name prototype requires only leaf matches")
    assert len(box.coarse_indexes) == 0


def test_coarse_diff_keeps_large_canceling_children_and_exact_remainder(index, ch_db, ch_url) -> None:
    ch = Ch(ch_url, db=ch_db)
    try:
        after = NameIndex.build(ch, ch_db, "2026-10-05", "zarr.json", 1024)
        body = diff(ch, index, after, "", 3)
        assert body["delta"] == {"b": 0, "o": -4, "matches": -2}
        assert (body["threshold_bytes"], body["max_children"]) == (12, 6)
        assert [[(c["path"], c["b"], c["o"], c["matches"]) for c in body[side]["tree"]["children"]]
                for side in ("before", "after")] == [
            [("b", 18, 4, 2), ("a", 12, 3, 2)], [("b", 30, 3, 2), ("a", 0, 0, 0)],
        ]
        assert [body[side]["tree"]["other"] for side in ("before", "after")] == [
            {"b": 6, "o": 1, "matches": 1}, {"b": 6, "o": 1, "matches": 1},
        ]
        assert diff_oracle(ch, index, after, body) is True
        deleted = diff(ch, index, after, "a", 2)
        assert [deleted[side]["present"] for side in ("before", "after")] == [True, False]
        assert deleted["delta"] == {"b": -12, "o": -3, "matches": -2}
        assert diff_oracle(ch, index, after, deleted) is True
        added = diff(ch, after, index, "a", 2)
        assert added["delta"] == {"b": 12, "o": 3, "matches": 2}
        assert diff_oracle(ch, after, index, added) is True
    finally:
        ch.close()


def test_coarse_diff_route_reuses_both_summaries(index, ch_db, ch_url) -> None:
    from json import loads
    from types import SimpleNamespace

    from dt_cloud.box.server import ChBox, HttpError, ch_coarse

    box = ChBox(SimpleNamespace(url=ch_url, threads=1), narrow_target=ch_db,
                narrow_manifest={"prefix": "", "dates": ["2026-10-04", "2026-10-05"]})
    qs = {"date": ["2026-10-05"], "date0": ["2026-10-04"], "name": ["zarr.json"], "budget": ["3"]}
    first = loads("".join(ch_coarse(box, qs)))
    second = loads("".join(ch_coarse(box, qs)))
    assert [first["cache_hit"], second["cache_hit"]] == [False, True]
    assert [first["before"]["cache_hit"], first["after"]["cache_hit"]] == [False, False]
    assert len(box.coarse_indexes) == 2
    assert first["delta"] == second["delta"] == {"b": 0, "o": -4, "matches": -2}
    assert first["before"]["tree"] == second["before"]["tree"]
    assert first["after"]["tree"] == second["after"]["tree"]
    with pytest.raises(HttpError) as caught:
        list(ch_coarse(box, {**qs, "budget": ["129"]}))
    assert (caught.value.status, caught.value.msg) == (400, "diff budget must be from 1 to 128 (at most twice that many children)")


@pytest.mark.parametrize("materialize", [False, True])
def test_coarse_suffix_disjoint_names_exact_partition(index, ch_db, ch_url, materialize) -> None:
    ch = Ch(ch_url, db=ch_db)
    try:
        suffix = NameIndex.build_pattern(ch, ch_db, "2026-10-04", ".JSON", materialize=materialize,
                                       block_rows=1024, min_free_bytes=0)
        assert (suffix.predicate_mode, suffix.vocabulary_names) == ("suffix", 2)
        body = suffix.view(ch, "", 3)
        assert (body["tree"]["b"], body["tree"]["o"], body["tree"]["matches"]) == (41, 9, 6)
        assert [(c["path"], c["b"], c["o"], c["matches"]) for c in body["tree"]["children"]] == [
            ("b", 18, 4, 2), ("a", 17, 4, 3),
        ]
        assert body["tree"]["other"] == {"b": 6, "o": 1, "matches": 1}
        assert oracle(ch, suffix, body) is True
    finally:
        ch.close()


@pytest.mark.parametrize("prepared_set", [False, True])
def test_pattern_summary_rebinds_after_original_session_is_closed(index, ch_db, ch_url, prepared_set) -> None:
    ch = Ch(ch_url, db=ch_db)
    try:
        pattern = NameIndex.build_pattern(ch, ch_db, "2026-10-04", ".json", match_mode="contains", block_rows=1024, prepared_set=prepared_set)
        expected = pattern.view(ch, "", 3)["tree"]
    finally:
        ch.close()
    fresh = Ch(ch_url, db=ch_db)
    try:
        pattern.prepare(fresh)
        assert pattern.view(fresh, "", 3)["tree"] == expected
        assert pattern.name_ids.tolist() == [1, 3]
    finally:
        fresh.close()


def test_substring_directory_match_is_a_counted_refusal(index, ch_db, ch_url) -> None:
    from dt_cloud.chstore.coarse import bench

    assert bench(ch_url, ch_db, "2026-10-04", "a", contains=True, paths=("",)) == {
        "target": ch_db, "date": "2026-10-04", "name": "a", "predicate_mode": "contains",
        "status": "unsupported nonleaf matches", "posting_rows": 7, "nonleaf_rows": 1,
        "leaf_rows": 6, "serving_changed": False,
    }


def test_coarse_pattern_route_caches_modes_independently(index, ch_db, ch_url) -> None:
    from json import loads
    from types import SimpleNamespace

    from dt_cloud.box.server import ChBox, ch_coarse

    box = ChBox(SimpleNamespace(url=ch_url, threads=1), narrow_target=ch_db,
                narrow_manifest={"prefix": "", "dates": ["2026-10-04"]})
    qs = {"date": ["2026-10-04"], "name": [".json"], "mode": ["contains"], "budget": ["3"]}
    first = loads("".join(ch_coarse(box, qs)))
    second = loads("".join(ch_coarse(box, qs)))
    suffix = loads("".join(ch_coarse(box, {**qs, "mode": ["suffix"]})))
    assert [first["cache_hit"], second["cache_hit"], suffix["cache_hit"]] == [False, True, False]
    assert len(box.coarse_indexes) == 2
    assert first["tree"] == second["tree"] == suffix["tree"]
    empty = loads("".join(ch_coarse(box, {**qs, "name": ["does-not-exist"]})))
    assert empty["tree"] == {"pre": 0, "path": "", "label": "all buckets", "b": 0, "o": 0, "matches": 0,
                             "leaf": False, "children": [], "other": {"b": 0, "o": 0, "matches": 0}}


def test_batched_refinement_has_one_global_threshold(index, ch_db, ch_url) -> None:
    from dt_cloud.chstore.coarse_walk import batch, walk, walk_oracle

    ch = Ch(ch_url, db=ch_db)
    try:
        bodies = batch(ch, index, ["b", "a"], 3, threshold=12)
        assert [(body["path"], body["threshold_bytes"], [(c["path"], c["b"]) for c in body["tree"]["children"]]) for body in bodies] == [
            ("b", 12, [("b/deeper", 18)]), ("a", 12, []),
        ]
        body = walk(ch, index, "", 3, 4)
        assert (body["tree_nodes"], body["frontier_batches"], body["threshold_bytes"]) == (5, 3, 12)
        assert body["tree"] == {
            "pre": 0, "path": "", "label": "all buckets", "b": 36, "o": 8, "matches": 5, "leaf": False,
            "children": [
                {"pre": 6, "path": "b", "label": "b", "b": 18, "o": 4, "matches": 2, "leaf": False,
                 "children": [{"pre": 7, "path": "b/deeper", "label": "deeper", "b": 18, "o": 3, "matches": 1, "leaf": False,
                               "children": [{"pre": 8, "path": "b/deeper/zarr.json", "label": "zarr.json", "b": 18, "o": 3, "matches": 1, "leaf": True}],
                               "other": {"b": 0, "o": 0, "matches": 0}}], "other": {"b": 0, "o": 1, "matches": 1}},
                {"pre": 1, "path": "a", "label": "a", "b": 12, "o": 3, "matches": 2, "leaf": False,
                 "children": [], "other": {"b": 12, "o": 3, "matches": 2}},
            ], "other": {"b": 6, "o": 1, "matches": 1},
        }
        assert walk_oracle(ch, index, body) == 4
    finally:
        ch.close()


@pytest.mark.parametrize("paths,budget,threshold,error", [
    (["", "a"], 3, 12, "frontier directory intervals overlap"),
    (["a", "b"], 2, 1, "global threshold exceeds the frontier quantile work budget"),
    (["a", "b"], 3, None, "multiple parents require one global byte threshold"),
])
def test_frontier_budget_and_disjointness(index, ch_db, ch_url, paths, budget, threshold, error) -> None:
    from dt_cloud.chstore.coarse import CoarseRequest
    from dt_cloud.chstore.coarse_walk import batch

    ch = Ch(ch_url, db=ch_db)
    try:
        with pytest.raises(CoarseRequest) as caught:
            batch(ch, index, paths, budget, threshold=threshold)
        assert str(caught.value) == error
    finally:
        ch.close()


def test_batched_diff_refines_common_children_with_absent_and_zero_sides(index, ch_db, ch_url) -> None:
    from dt_cloud.chstore.coarse_walk import walk_diff, walk_diff_oracle

    ch = Ch(ch_url, db=ch_db)
    try:
        after = NameIndex.build(ch, ch_db, "2026-10-05", "zarr.json", 1024)
        body = walk_diff(ch, index, after, "", 3, 4)
        assert (body["tree_nodes"], body["frontier_batches"], body["threshold_bytes"], body["delta"]) == (
            6, 3, 12, {"b": 0, "o": -4, "matches": -2},
        )
        assert [[(c["path"], c["b"], c["o"], c["matches"]) for c in body[side]["tree"]["children"][0]["children"]]
                for side in ("before", "after")] == [
            [("b/zarr.json", 0, 1, 1), ("b/deeper", 18, 3, 1)],
            [("b/zarr.json", 20, 1, 1), ("b/deeper", 10, 2, 1)],
        ]
        assert [[c["b"] for c in body[side]["tree"]["children"][0]["children"][1]["children"]] for side in ("before", "after")] == [[18], [10]]
        assert [body[side]["tree"]["children"][1]["other"] for side in ("before", "after")] == [
            {"b": 12, "o": 3, "matches": 2}, {"b": 0, "o": 0, "matches": 0},
        ]
        assert walk_diff_oracle(ch, index, after, body) == 4
        gone = walk_diff(ch, index, after, "a", 2, 3)
        assert gone["delta"] == {"b": -12, "o": -3, "matches": -2}
        assert [gone[side]["present"] for side in ("before", "after")] == [True, False]
        assert walk_diff_oracle(ch, index, after, gone) == 1
    finally:
        ch.close()


def test_multi_level_http_contract_and_depth_guard(index, ch_db, ch_url) -> None:
    from json import loads
    from types import SimpleNamespace

    from dt_cloud.box.server import ChBox, HttpError, ch_coarse

    box = ChBox(SimpleNamespace(url=ch_url, threads=1), narrow_target=ch_db,
                narrow_manifest={"prefix": "", "dates": ["2026-10-04", "2026-10-05"]})
    qs = {"date": ["2026-10-05"], "date0": ["2026-10-04"], "name": ["zarr.json"], "budget": ["3"], "levels": ["3"]}
    body = loads("".join(ch_coarse(box, qs)))
    assert (body["schema"], body["levels"], body["tree_nodes"], body["delta"]) == (
        "coarse-tree-diff-v1", 3, 6, {"b": 0, "o": -4, "matches": -2},
    )
    assert [body[side]["schema"] for side in ("before", "after")] == ["coarse-tree-v1", "coarse-tree-v1"]
    assert [body[side]["levels"] for side in ("before", "after")] == [3, 3]
    for value in ("0", "5", "abc"):
        with pytest.raises(HttpError) as caught:
            list(ch_coarse(box, {**qs, "levels": [value]}))
        assert (caught.value.status, caught.value.msg) == (400, "levels must be an integer from 1 to 4")


def test_directory_coverage_deduplicates_roots_and_uses_covered_rollups(index, ch_db, ch_url) -> None:
    from dt_cloud.chstore.coverage import Coverage, oracle

    ch = Ch(ch_url, db=ch_db, max_bytes_before_external_sort=256 << 20)
    try:
        coverage = Coverage.build(ch, ch_db, "2026-10-04", "a", min_free_bytes=0)
        assert [coverage.starts.tolist(), coverage.ends.tolist()] == [[1, 8, 9, 12], [5, 8, 9, 12]]
        root = coverage.view(ch, "", 3)
        assert root["tree"] == {
            "pre": 0, "path": "", "label": "all buckets", "b": 41, "o": 9, "leaf": False,
            "children": [
                {"pre": 6, "path": "b", "label": "b", "b": 18, "o": 4, "leaf": False},
                {"pre": 1, "path": "a", "label": "a", "b": 17, "o": 4, "leaf": False},
            ], "other": {"b": 6, "o": 1},
        }
        assert root["covered_parent"] is False
        assert oracle(ch, coverage, root) is True
        covered = coverage.view(ch, "a", 3)
        assert covered["covered_parent"] is True
        assert covered["tree"] == {
            "pre": 1, "path": "a", "label": "a", "b": 17, "o": 4, "leaf": False,
            "children": [{"pre": 2, "path": "a/zarr.json", "label": "zarr.json", "b": 9, "o": 1, "leaf": True}],
            "other": {"b": 8, "o": 3},
        }
        assert oracle(ch, coverage, covered) is True
        inside = coverage.view(ch, "a/nest", 2)
        assert inside["covered_parent"] is True
        assert inside["tree"] == {
            "pre": 3, "path": "a/nest", "label": "nest", "b": 3, "o": 2, "leaf": False,
            "children": [{"pre": 4, "path": "a/nest/zarr.json", "label": "zarr.json", "b": 3, "o": 2, "leaf": True}],
            "other": {"b": 0, "o": 0},
        }
        assert oracle(ch, coverage, inside) is True
        assert coverage.totals(ch, [(2, 2), (6, 11), (9, 9)]) == [(9, 1), (18, 4), (0, 1)]
    finally:
        ch.close()


def test_coverage_scan_bindings_remain_independent_and_deleted_paths_refuse(index, ch_db, ch_url) -> None:
    from dt_cloud.chstore.coarse import CoarseRequest
    from dt_cloud.chstore.coverage import Coverage, oracle

    ch = Ch(ch_url, db=ch_db)
    try:
        before = Coverage.build(ch, ch_db, "2026-10-04", "a", min_free_bytes=0)
        after = Coverage.build(ch, ch_db, "2026-10-05", "a", min_free_bytes=0)
        assert [before.starts.tolist(), after.starts.tolist()] == [[1, 8, 9, 12], [8, 9, 12]]
        bodies = [coverage.view(ch, "", 3) for coverage in (before, after)]
        assert [(body["tree"]["b"], body["tree"]["o"]) for body in bodies] == [(41, 9), (36, 4)]
        assert [oracle(ch, coverage, body) for coverage, body in zip((before, after), bodies)] == [True, True]
        with pytest.raises(CoarseRequest) as caught:
            after.view(ch, "a", 3)
        assert str(caught.value) == "path not present at the selected scan"
        empty = Coverage.build(ch, ch_db, "2026-10-04", "missing", min_free_bytes=0)
        assert empty.view(ch, "", 3)["tree"] == {
            "pre": 0, "path": "", "label": "all buckets", "b": 0, "o": 0, "leaf": False, "children": [], "other": {"b": 0, "o": 0},
        }
        assert before.totals(ch, [(1, 5), (6, 11)]) == [(17, 4), (18, 4)]
    finally:
        ch.close()


def test_covered_directory_keeps_own_object_in_remainder(index, ch_db, ch_url) -> None:
    from dt_cloud.chstore.coverage import Coverage, oracle

    own_db = ch_db + "_own"
    ch = Ch(ch_url, db=ch_db)
    ch.exec(f"CREATE DATABASE {own_db}")
    try:
        ch.exec(f"CREATE TABLE {own_db}.nodes AS {ch_db}.nodes ENGINE = Memory")
        ch.exec(f"INSERT INTO {own_db}.nodes SELECT pre, post, path, nid, b + if(pre IN (0,1), 2, 0), o + if(pre IN (0,1), 1, 0) FROM {ch_db}.nodes")
        ch.exec(f"CREATE VIEW {own_db}.nodes_by_name AS SELECT * FROM {own_db}.nodes")
        ch.exec(f"CREATE VIEW {own_db}.dictionary AS SELECT * FROM {ch_db}.dictionary")
        ch.exec(f"CREATE VIEW {own_db}.names AS SELECT * FROM {ch_db}.names")
        ch.exec(f"""CREATE VIEW {own_db}.metadata_by_parent AS SELECT n.pre AS pre, n.path AS path, n.b AS b, n.o AS o, d.pre AS parent_pre
            FROM {own_db}.nodes n LEFT JOIN {ch_db}.dictionary d ON d.path = if(position(n.path, '/') = 0, '', substring(n.path, 1, length(n.path) - position(reverse(n.path), '/')))""")
        ch.exec(f"CREATE TABLE {own_db}.history_manifest (doc String) ENGINE = Memory")
        ch.exec(f"INSERT INTO {own_db}.history_manifest VALUES ({lit(dumps({'dates': ['2026-10-04'], 'dbs': [own_db], 'prefix': ''}))})")
        coverage = Coverage.build(ch, own_db, "2026-10-04", "a", min_free_bytes=0)
        body = coverage.view(ch, "a", 256)
        assert body["tree"] == {
            "pre": 1, "path": "a", "label": "a", "b": 19, "o": 5, "leaf": False,
            "children": [
                {"pre": 2, "path": "a/zarr.json", "label": "zarr.json", "b": 9, "o": 1, "leaf": True},
                {"pre": 5, "path": "a/extra.zarr.json", "label": "extra.zarr.json", "b": 5, "o": 1, "leaf": True},
                {"pre": 3, "path": "a/nest", "label": "nest", "b": 3, "o": 2, "leaf": False},
            ], "other": {"b": 2, "o": 1},
        }
        assert oracle(ch, coverage, body) is True
    finally:
        ch.close()
        Ch(ch_url, session=False).exec(f"DROP DATABASE {own_db} SYNC")


def test_coverage_diff_aligns_deleted_and_canceling_children(index, ch_db, ch_url) -> None:
    from dt_cloud.chstore.coverage import Coverage, diff, diff_oracle

    ch = Ch(ch_url, db=ch_db)
    try:
        before = Coverage.build(ch, ch_db, "2026-10-04", "a", min_free_bytes=0)
        after = Coverage.build(ch, ch_db, "2026-10-05", "a", min_free_bytes=0)
        body = diff(ch, before, after, "", 3)
        assert (body["threshold_bytes"], body["delta"]) == (14, {"b": -5, "o": -5})
        assert [[(c["path"], c["b"], c["o"]) for c in body[side]["tree"]["children"]] for side in ("before", "after")] == [
            [("b", 18, 4), ("a", 17, 4)], [("b", 30, 3), ("a", 0, 0)],
        ]
        assert [body[side]["tree"]["other"] for side in ("before", "after")] == [{"b": 6, "o": 1}, {"b": 6, "o": 1}]
        assert diff_oracle(ch, before, after, body) is True
        deleted = diff(ch, before, after, "a", 3)
        assert deleted["delta"] == {"b": -17, "o": -4}
        assert [deleted[side]["present"] for side in ("before", "after")] == [True, False]
        assert [deleted[side]["tree"]["other"] for side in ("before", "after")] == [{"b": 8, "o": 3}, {"b": 0, "o": 0}]
        assert diff_oracle(ch, before, after, deleted) is True
        changed = diff(ch, before, after, "b", 3)
        assert [[(c["path"], c["b"], c["o"]) for c in changed[side]["tree"]["children"]] for side in ("before", "after")] == [
            [("b/zarr.json", 0, 1), ("b/deeper", 18, 3)], [("b/zarr.json", 20, 1), ("b/deeper", 10, 2)],
        ]
        assert diff_oracle(ch, before, after, changed) is True
    finally:
        ch.close()


def test_coverage_oracles_reject_inexact_remainders(index, ch_db, ch_url) -> None:
    from dt_cloud.chstore.coverage import Coverage, diff, diff_oracle, oracle

    ch = Ch(ch_url, db=ch_db)
    try:
        before = Coverage.build(ch, ch_db, "2026-10-04", "a", min_free_bytes=0)
        after = Coverage.build(ch, ch_db, "2026-10-05", "a", min_free_bytes=0)
        body = before.view(ch, "", 3)
        body["tree"]["other"]["o"] += 1
        with pytest.raises(ValueError) as caught:
            oracle(ch, before, body)
        assert str(caught.value) == "coverage folded remainder disagrees with its exact partition"
        paired = diff(ch, before, after, "", 3)
        paired["after"]["tree"]["other"]["b"] += 1
        with pytest.raises(ValueError) as caught:
            diff_oracle(ch, before, after, paired)
        assert str(caught.value) == "coverage diff folded remainder disagrees with its exact partition"
    finally:
        ch.close()


def test_coverage_refuses_locator_overflow_without_returning_partial_roots(index, ch_db, ch_url) -> None:
    from dt_cloud.chstore.coarse import CoarseRequest
    from dt_cloud.chstore.coverage import Coverage

    ch = Ch(ch_url, db=ch_db)
    try:
        with pytest.raises(CoarseRequest) as caught:
            Coverage.build(ch, ch_db, "2026-10-04", "a", max_roots=3, min_free_bytes=0)
        assert str(caught.value) == "coverage outer-root locator exceeds its 3-root work budget"
        for pattern in ("%", "_"):
            coverage = Coverage.build(ch, ch_db, "2026-10-04", pattern, min_free_bytes=0)
            assert coverage.starts.tolist() == []
            assert coverage.view(ch, "", 3)["tree"]["b"] == 0
    finally:
        ch.close()


def test_resident_coverage_survives_closed_build_session(index, ch_db, ch_url) -> None:
    from dt_cloud.chstore.coverage import Coverage, diff, diff_oracle, oracle

    builder = Ch(ch_url, db=ch_db)
    try:
        cached = [Coverage.build(builder, ch_db, date, "a", min_free_bytes=0, resident_roots=True)
                  for date in ("2026-10-04", "2026-10-05")]
    finally:
        builder.close()
    ch = Ch(ch_url, db=ch_db)
    try:
        intervals = [(lo, hi) for lo, hi in ch.json(f"SELECT pre, post FROM {ch_db}.dictionary ORDER BY pre")]
        assert [coverage.totals(ch, intervals) for coverage in cached] == [
            [(41, 9), (17, 4), (9, 1), (3, 2), (3, 2), (5, 1), (18, 4), (18, 3), (18, 3), (0, 1), (0, 0), (0, 0), (6, 1)],
            [(36, 4), (0, 0), (0, 0), (0, 0), (0, 0), (0, 0), (30, 3), (10, 2), (10, 2), (20, 1), (0, 0), (0, 0), (6, 1)],
        ]
        controls = [Coverage.build(ch, ch_db, date, "a", min_free_bytes=0) for date in ("2026-10-04", "2026-10-05")]
        for path, budget in (("", 3), ("a", 256), ("b", 3), ("b/zarr.json", 3)):
            for resident, control in zip(cached, controls):
                body = resident.view(ch, path, budget, allow_absent=True)
                assert body["tree"] == control.view(ch, path, budget, allow_absent=True)["tree"]
                assert oracle(ch, control, body) is True
            body = diff(ch, *cached, path, budget if budget <= 128 else 128)
            control_body = diff(ch, *controls, path, budget if budget <= 128 else 128)
            assert [body[side]["tree"] for side in ("before", "after")] == [
                control_body[side]["tree"] for side in ("before", "after")
            ]
            assert diff_oracle(ch, *controls, body) is True
    finally:
        ch.close()


def test_coverage_http_contract_caches_dirs_without_path_count_claims(index, ch_db, ch_url, monkeypatch) -> None:
    from json import loads
    from types import SimpleNamespace

    from dt_cloud.box.server import ChBox, HttpError, ch_coarse

    monkeypatch.setattr("dt_cloud.chstore.coverage.disk_reserve", lambda *args: None)
    box = ChBox(SimpleNamespace(url=ch_url, threads=1), narrow_target=ch_db,
                narrow_manifest={"prefix": "", "dates": ["2026-10-04", "2026-10-05"]})
    qs = {"date": ["2026-10-04"], "name": ["nest"], "mode": ["coverage"], "path": ["a/nest"], "budget": ["3"]}
    first, second = [loads("".join(ch_coarse(box, qs))) for _ in range(2)]
    assert [first["cache_hit"], second["cache_hit"]] == [False, True]
    assert first["tree"] == second["tree"] == {
        "pre": 3, "path": "a/nest", "label": "nest", "b": 3, "o": 2, "leaf": False,
        "children": [{"pre": 4, "path": "a/nest/zarr.json", "label": "zarr.json", "b": 3, "o": 2, "leaf": True}],
        "other": {"b": 0, "o": 0},
    }
    paired = loads("".join(ch_coarse(box, {**qs, "date": ["2026-10-05"], "date0": ["2026-10-04"]})))
    assert (paired["schema"], paired["delta"], paired["cache_hit"]) == ("coverage-diff-v1", {"b": -3, "o": -2}, False)
    assert [paired[side]["cache_hit"] for side in ("before", "after")] == [True, False]
    assert len(box.coarse_indexes) == 2
    with pytest.raises(HttpError) as caught:
        list(ch_coarse(box, {**qs, "levels": ["2"]}))
    assert (caught.value.status, caught.value.msg) == (400, "directory coverage currently serves one level per drill")


def test_coverage_oracle_budget_refuses_instead_of_sampled_parity(index, ch_db, ch_url) -> None:
    from dt_cloud.chstore.coverage import Coverage, oracle

    ch = Ch(ch_url, db=ch_db)
    try:
        coverage = Coverage.build(ch, ch_db, "2026-10-04", "a", min_free_bytes=0, resident_roots=True)
        body = coverage.view(ch, "", 3)
        with pytest.raises(ValueError) as caught:
            oracle(ch, coverage, body, max_rows=2)
        assert str(caught.value) == "coverage oracle matching-node set exceeds its 2-row work budget"
        assert oracle(ch, coverage, body, max_rows=10) is True
        covered = coverage.view(ch, "a", 256)
        with pytest.raises(ValueError) as caught:
            oracle(ch, coverage, covered, max_rows=2)
        assert str(caught.value) == "coverage oracle child map exceeds its 2-row work budget"
    finally:
        ch.close()


def test_coverage_refuses_oversized_leaf_sets_before_disk_preparation(index, ch_db, ch_url, monkeypatch) -> None:
    from dt_cloud.chstore.coarse import CoarseRequest
    from dt_cloud.chstore.coverage import Coverage

    def forbidden_disk_preparation(*args) -> None:
        raise AssertionError("oversized leaf set must refuse before disk preparation")

    monkeypatch.setattr("dt_cloud.chstore.coverage.disk_reserve", forbidden_disk_preparation)
    ch = Ch(ch_url, db=ch_db)
    try:
        with pytest.raises(CoarseRequest) as caught:
            Coverage.build(ch, ch_db, "2026-10-04", "zarr", max_roots=2, min_free_bytes=0)
        assert str(caught.value) == "coverage leaf-only set exceeds its 2-root work budget; use a leaf mode"
        assert len(ch._tmp) == 1
    finally:
        ch.close()


@pytest.mark.parametrize("date,expected", [
    ("2026-10-04", [[1, 8, 9, 12], [5, 8, 9, 12]]),
    ("2026-10-05", [[8, 9, 12], [8, 9, 12]]),
])
def test_coverage_ancestry_plan_matches_intervals_with_temporal_presence(index, ch_db, ch_url, date, expected) -> None:
    from dt_cloud.chstore.coverage import Coverage, oracle

    ch = Ch(ch_url, db=ch_db)
    try:
        ancestry = Coverage.build(ch, ch_db, date, "a", min_free_bytes=0, resident_roots=True, root_plan="ancestry")
        intervals = Coverage.build(ch, ch_db, date, "a", min_free_bytes=0, resident_roots=True)
        assert [ancestry.starts.tolist(), ancestry.ends.tolist()] == expected
        assert ancestry.fingerprint() == intervals.fingerprint()
        for path in ("", "b", "b/deeper", "b/zarr.json"):
            body = ancestry.view(ch, path, 3)
            assert body["tree"] == intervals.view(ch, path, 3)["tree"]
            assert oracle(ch, ancestry, body) is True
    finally:
        ch.close()


def test_lineage_geometry_sample_is_explicit_and_matches_frozen_ranges(index, ch_db, ch_url) -> None:
    from dt_cloud.chstore.order_keys import sample_bench

    body = sample_bench(ch_url, ch_db, "2026-10-04", "", sample_rows=5)
    assert (body["sample_limit"], body["sampled_rows"], body["cold"]) == (5, 5, False)
    assert [(v["parent_id"], v["exact_sample_scalar_equal"]) for v in body["views"]] == [(1, True), (3, True), (6, True)]


def test_lineage_posting_history_verifies_complete_scoped_dates(index, ch_db, ch_url) -> None:
    from dt_cloud.chstore.key_history import posting_bench

    body = posting_bench(ch_url, ch_db, "2026-10-04", "2026-10-05", "zarr.json", "")
    assert (body["posting_rows"], body["union_leaves"], body["dates"]) == ([5, 3], 5, ["2026-10-04", "2026-10-05"])
    assert [(v["parent_id"], v["date"], v["exact_scalar_equal"]) for v in body["views"]] == [
        (None, "2026-10-04", True), (None, "2026-10-05", True),
        (0, "2026-10-04", True), (0, "2026-10-05", True),
        (1, "2026-10-04", True), (1, "2026-10-05", True),
        (3, "2026-10-04", True), (3, "2026-10-05", True),
    ]


@pytest.mark.parametrize("mode", ["leaf", "coverage"])
def test_overbudget_vocabulary_is_bounded_before_posting_work(index, ch_db, ch_url, mode) -> None:
    from dt_cloud.chstore.coarse import CoarseRequest
    from dt_cloud.chstore.coverage import Coverage

    ch = Ch(ch_url, db=ch_db)
    target = ch_db + "_vocabulary"
    ch.exec(f"CREATE DATABASE {target}")
    try:
        ch.exec(f"CREATE TABLE {target}.history_manifest ENGINE=Memory AS SELECT * FROM {ch_db}.history_manifest")
        ch.exec(f"CREATE TABLE {target}.names (nid UInt32,l String) ENGINE=Memory")
        ch.exec(f"INSERT INTO {target}.names VALUES (1,'alpha.json'),(2,'beta.json'),(3,'gamma.json')")
        with pytest.raises(CoarseRequest) as caught:
            if mode == "leaf":
                NameIndex.build_pattern(ch, target, "2026-10-04", ".json", max_names=1)
            else:
                Coverage.build(ch, target, "2026-10-04", ".json", max_names=1, min_free_bytes=0)
        expected = "pattern vocabulary exceeds the 1-name work budget" if mode == "leaf" else "coverage vocabulary exceeds its 1-name work budget"
        assert str(caught.value) == expected
        assert len(ch._tmp) == 1
        assert ch.scalar(f"SELECT count() FROM {ch._tmp[0]}") == "2"
    finally:
        ch.close()
        ch.exec(f"DROP DATABASE {target}")


def test_vocabulary_at_the_budget_remains_complete(index, ch_db, ch_url) -> None:
    from dt_cloud.chstore.coverage import Coverage

    ch = Ch(ch_url, db=ch_db)
    try:
        leaf = NameIndex.build_pattern(ch, ch_db, "2026-10-04", ".json", max_names=2)
        assert (leaf.vocabulary_names, leaf.prefix.totals[0][-1], leaf.prefix.totals[1][-1], leaf.prefix.totals[2][-1]) == (2, 6, 41, 9)
        coverage = Coverage.build(ch, ch_db, "2026-10-04", ".json", max_names=2, min_free_bytes=0)
        assert (coverage.weights[0][-1], coverage.weights[1][-1]) == (41, 9)
    finally:
        ch.close()
