from dataclasses import replace

import pytest

from dt_cloud.chstore.hot_names import HotNames, Node, hash_fixture


NODES = [
    Node(100, 114, "bucket", 90, 19),
    Node(101, 106, "bucket/aaaa", 35, 7),
    Node(102, 104, "bucket/aaaa/nest", 22, 4),
    Node(103, 103, "bucket/aaaa/nest/aaaa.json", 20, 2),
    Node(104, 104, "bucket/aaaa/nest/zero.txt", 0, 1),
    Node(105, 105, "bucket/aaaa/plain.txt", 10, 2),
    Node(106, 106, "bucket/aaaa/empty.dat", 0, 0),
    Node(107, 109, "bucket/cold", 45, 5),
    Node(108, 108, "bucket/cold/bbbb.json", 40, 2),
    Node(109, 109, "bucket/cold/zero.txt", 0, 2),
    Node(110, 111, "bucket/ab", 0, 1),
    Node(111, 111, "bucket/ab/cd", 0, 1),
    Node(113, 113, "bucket/dust.npy", 0, 3),
    Node(114, 114, "bucket/baaa.json", 7, 1),
]


def test_repeated_fragments_count_paths_once_and_matching_ancestors_cover_descendants() -> None:
    index = HotNames(NODES, 3)
    assert {pattern: index.counts[pattern] for pattern in ("aa", "aaa", ".json")} == {
        "aa": 3, "aaa": 3, ".json": 3,
    }
    payload = index.payloads["aa"]
    assert (list(payload.starts), list(payload.ends), list(payload.bytes_prefix), list(payload.objects_prefix)) == (
        [101, 114], [106, 114], [0, 35, 42], [0, 7, 8],
    )
    assert [(node.path, index.total("aa", node)) for node in NODES] == [
        ("bucket", (42, 8)), ("bucket/aaaa", (35, 7)),
        ("bucket/aaaa/nest", (22, 4)), ("bucket/aaaa/nest/aaaa.json", (20, 2)),
        ("bucket/aaaa/nest/zero.txt", (0, 1)), ("bucket/aaaa/plain.txt", (10, 2)),
        ("bucket/aaaa/empty.dat", (0, 0)), ("bucket/cold", (0, 0)),
        ("bucket/cold/bbbb.json", (0, 0)), ("bucket/cold/zero.txt", (0, 0)),
        ("bucket/ab", (0, 0)), ("bucket/ab/cd", (0, 0)),
        ("bucket/dust.npy", (0, 0)), ("bucket/baaa.json", (7, 1)),
    ]


def test_name_matching_never_crosses_a_slash() -> None:
    body = HotNames(NODES, 1).view("bc", "bucket", budget=4)
    assert body == {
        "pattern": "bc", "route": "cold-postings", "threshold_bytes": 1,
        "tree": {"path": "bucket", "b": 0, "o": 0, "children": [], "remainder": [0, 0]},
    }


@pytest.mark.parametrize("threshold,route", [(3, "hot-prefix"), (4, "cold-postings")])
def test_exact_frequency_boundary_switches_only_the_query_route(threshold: int, route: str) -> None:
    assert HotNames(NODES, threshold).view("aa", "bucket", levels=1, budget=1) == {
        "pattern": "aa", "route": route, "threshold_bytes": 42,
        "tree": {"path": "bucket", "b": 42, "o": 8, "children": [], "remainder": [42, 8]},
    }


def test_matching_root_includes_its_own_object_contribution() -> None:
    assert HotNames(NODES, 1).view("bucket", "bucket", levels=3, budget=1) == {
        "pattern": "bucket", "route": "hot-prefix", "threshold_bytes": 90,
        "tree": {"path": "bucket", "b": 90, "o": 19, "children": [], "remainder": [90, 19]},
    }


@pytest.mark.parametrize("pattern,total", [("aa", (42, 8)), (".json", (67, 5)), ("nest", (22, 4)), ("zero", (0, 3)), ("absent", (0, 0))])
def test_hot_and_cold_thresholds_preserve_complete_totals_and_every_partition(pattern: str, total: tuple[int, int]) -> None:
    hot = HotNames(NODES, 1)
    cold = HotNames(NODES, 100)
    hot_body = hot.view(pattern, "bucket", levels=3, budget=6)
    cold_body = cold.view(pattern, "bucket", levels=3, budget=6)
    assert (hot_body["tree"]["b"], hot_body["tree"]["o"]) == total
    assert (cold_body["tree"]["b"], cold_body["tree"]["o"]) == total
    assert {key: value for key, value in hot_body.items() if key != "route"} == {
        key: value for key, value in cold_body.items() if key != "route"
    }
    assert cold_body["route"] == "cold-postings"
    assert hot.check(hot_body) == cold.check(cold_body)


def test_three_level_refinement_preserves_directory_own_bytes_and_zero_byte_objects() -> None:
    index = HotNames(NODES, 3)
    body = index.view("AA", "bucket", levels=3, budget=6)
    assert body == {
        "pattern": "aa", "route": "hot-prefix", "threshold_bytes": 7,
        "tree": {
            "path": "bucket", "b": 42, "o": 8, "remainder": [0, 0],
            "children": [
                {
                    "path": "bucket/aaaa", "b": 35, "o": 7, "remainder": [3, 1],
                    "children": [
                        {
                            "path": "bucket/aaaa/nest", "b": 22, "o": 4, "remainder": [2, 2],
                            "children": [{"path": "bucket/aaaa/nest/aaaa.json", "b": 20, "o": 2,
                                          "children": [], "remainder": [20, 2]}],
                        },
                        {"path": "bucket/aaaa/plain.txt", "b": 10, "o": 2, "children": [], "remainder": [10, 2]},
                    ],
                },
                {"path": "bucket/baaa.json", "b": 7, "o": 1, "children": [], "remainder": [7, 1]},
            ],
        },
    }
    assert index.check(body) == 6
    assert index.view("aa", "bucket/aaaa/nest", levels=3, budget=2) == {
        "pattern": "aa", "route": "hot-prefix", "threshold_bytes": 11,
        "tree": {"path": "bucket/aaaa/nest", "b": 22, "o": 4, "remainder": [2, 2],
                 "children": [{"path": "bucket/aaaa/nest/aaaa.json", "b": 20, "o": 2,
                               "children": [], "remainder": [20, 2]}]},
    }


def test_all_zero_byte_matches_remain_in_exact_count_remainder() -> None:
    index = HotNames(NODES, 2)
    assert index.view("zero", "bucket", levels=3, budget=256) == {
        "pattern": "zero", "route": "hot-prefix", "threshold_bytes": 1,
        "tree": {"path": "bucket", "b": 0, "o": 3, "children": [], "remainder": [0, 3]},
    }
    assert index.view("zero", "bucket/aaaa/nest/zero.txt") == {
        "pattern": "zero", "route": "hot-prefix", "threshold_bytes": 1,
        "tree": {"path": "bucket/aaaa/nest/zero.txt", "b": 0, "o": 1, "children": [], "remainder": [0, 1]},
    }


def test_matching_external_ancestor_covers_even_a_cold_query() -> None:
    nodes = [replace(node, path="aa/" + node.path) for node in NODES]
    index = HotNames(nodes, 100)
    assert index.view("aa", "aa/bucket/cold", levels=1, budget=2) == {
        "pattern": "aa", "route": "ancestor-rollup", "threshold_bytes": 23,
        "tree": {"path": "aa/bucket/cold", "b": 45, "o": 5, "remainder": [5, 3],
                 "children": [{"path": "aa/bucket/cold/bbbb.json", "b": 40, "o": 2,
                               "children": [], "remainder": [40, 2]}]},
    }


def test_preorder_offsets_and_gaps_do_not_change_answers() -> None:
    original = HotNames(NODES, 3)
    relocated = HotNames([replace(node, pre=node.pre * 17 + 9000, post=node.post * 17 + 9000) for node in NODES], 3)
    assert relocated.view("aa", "bucket", budget=6) == original.view("aa", "bucket", budget=6)
    assert relocated.stats() == original.stats()


def test_rare_prefix_prunes_all_longer_hex_branches() -> None:
    index = HotNames([Node(10, 11, "r", 1, 1), Node(11, 11, "r/a0f9z", 1, 1)], 2)
    assert dict(index.counts) == {}
    assert index.stats() == {
        "threshold_paths": 2, "hot_patterns": 0, "unique_payloads": 0,
        "root_records_before_sharing": 0, "root_records_after_sharing": 0,
        "packed_payload_bytes": 0, "hot_query_utf8_bytes": 0,
        "candidate_patterns_counted": 6, "candidate_name_pattern_pairs": 6,
    }
    assert index.view("a0f9z", "r") == {
        "pattern": "a0f9z", "route": "cold-postings", "threshold_bytes": 1,
        "tree": {"path": "r", "b": 1, "o": 1, "remainder": [0, 0],
                 "children": [{"path": "r/a0f9z", "b": 1, "o": 1, "children": [], "remainder": [1, 1]}]},
    }


def test_hot_prefix_chain_stops_before_rare_extensions_and_shares_identical_coverage() -> None:
    index = HotNames([Node(10, 12, "r", 3, 2), Node(11, 11, "r/aaaa", 1, 1), Node(12, 12, "r/aaab", 2, 1)], 2)
    assert dict(index.counts) == {"a": 2, "aa": 2, "aaa": 2}
    assert index.stats() == {
        "threshold_paths": 2, "hot_patterns": 3, "unique_payloads": 1,
        "root_records_before_sharing": 6, "root_records_after_sharing": 2,
        "packed_payload_bytes": 80, "hot_query_utf8_bytes": 6,
        "candidate_patterns_counted": 9, "candidate_name_pattern_pairs": 12,
    }
    assert (index.total("aa", index.nodes[0]), index.total("aaaa", index.nodes[0])) == ((3, 2), (1, 1))


def test_pair_budget_is_exact_and_refuses_instead_of_returning_partial_grams() -> None:
    nodes = [Node(10, 11, "r", 1, 1), Node(11, 11, "r/aaaa", 1, 1)]
    assert HotNames(nodes, 1, pair_budget=5).pairs == 5
    with pytest.raises(ValueError) as caught:
        HotNames(nodes, 1, pair_budget=4)
    assert str(caught.value) == "name-pattern pair budget exceeded; no partial index returned"


def test_empty_global_root_accepts_direct_children() -> None:
    index = HotNames([Node(0, 1, "", 7, 2), Node(1, 1, "aa", 5, 1)], 1)
    assert index.view("aa", "", levels=1) == {
        "pattern": "aa", "route": "hot-prefix", "threshold_bytes": 1,
        "tree": {"path": "", "b": 5, "o": 1, "remainder": [0, 0],
                 "children": [{"path": "aa", "b": 5, "o": 1, "children": [], "remainder": [5, 1]}]},
    }


def test_hash_fixture_has_exact_three_level_topology_and_leaf_weights() -> None:
    assert hash_fixture(2) == [
        Node(17, 23, "fleet", 3, 2),
        Node(18, 20, "fleet/region-0", 1, 1),
        Node(19, 20, "fleet/region-0/group-0", 1, 1),
        Node(20, 20, "fleet/region-0/group-0/5feceb66ffc86f38d952786c6d696c79.npy", 1, 1),
        Node(21, 23, "fleet/region-1", 2, 1),
        Node(22, 23, "fleet/region-1/group-1", 2, 1),
        Node(23, 23, "fleet/region-1/group-1/6b86b273ff34fce19d6b804eff5a3f57.npy", 2, 1),
    ]
    assert hash_fixture(18)[0] == Node(17, 61, "fleet", 154, 18)


@pytest.mark.parametrize("threshold,route", [(2, "hot-prefix"), (3, "cold-postings")])
def test_hash_suffix_hot_and_cold_views_refine_exactly_without_persisting_cold_payloads(threshold: int, route: str) -> None:
    index = HotNames(hash_fixture(2), threshold)
    before = index.stats()
    body = index.view(".npy", "fleet", levels=3)
    assert body == {
        "pattern": ".npy", "route": route, "threshold_bytes": 1,
        "tree": {
            "path": "fleet", "b": 3, "o": 2, "remainder": [0, 0],
            "children": [
                {
                    "path": "fleet/region-0", "b": 1, "o": 1, "remainder": [0, 0],
                    "children": [{
                        "path": "fleet/region-0/group-0", "b": 1, "o": 1, "remainder": [0, 0],
                        "children": [{"path": "fleet/region-0/group-0/5feceb66ffc86f38d952786c6d696c79.npy",
                                      "b": 1, "o": 1, "children": [], "remainder": [1, 1]}],
                    }],
                },
                {
                    "path": "fleet/region-1", "b": 2, "o": 1, "remainder": [0, 0],
                    "children": [{
                        "path": "fleet/region-1/group-1", "b": 2, "o": 1, "remainder": [0, 0],
                        "children": [{"path": "fleet/region-1/group-1/6b86b273ff34fce19d6b804eff5a3f57.npy",
                                      "b": 2, "o": 1, "children": [], "remainder": [2, 1]}],
                    }],
                },
            ],
        },
    }
    assert index.check(body) == 7
    assert index.stats() == before


@pytest.mark.parametrize("nodes,error", [
    ([], "complete subtree must contain 1..20K nodes"),
    ([Node(0, 0, "r", 0, 0)] * 20_001, "complete subtree must contain 1..20K nodes"),
    ([Node(-1, 0, "r", 0, 0)], "invalid preorder or negative rollup"),
    ([Node(10, 9, "r", 0, 0)], "invalid preorder or negative rollup"),
    ([Node(10, 10, "r", -1, 0)], "invalid preorder or negative rollup"),
    ([Node(10, 10, "r", 0, -1)], "invalid preorder or negative rollup"),
    ([Node(10, 12, "r", 1, 1), Node(9, 9, "r/a", 1, 1)], "invalid preorder or negative rollup"),
    ([Node(10, 12, "r", 1, 1), Node(11, 11, "r/missing/a", 1, 1)], "subtree is incomplete or intervals are not laminar"),
    ([Node(10, 11, "r", 1, 1), Node(11, 12, "r/a", 1, 1)], "subtree is incomplete or intervals are not laminar"),
    ([Node(10, 10, "r", 1, 1), Node(11, 11, "s", 1, 1)], "subtree has multiple roots"),
    ([Node(10, 11, "r", 1, 1), Node(11, 11, "r/a", 2, 1)], "duplicate paths or child rollups exceed parent"),
    ([Node(10, 11, "r", 1, 1), Node(11, 11, "r/a", 1, 2)], "duplicate paths or child rollups exceed parent"),
    ([Node(10, 12, "r", 2, 2), Node(11, 11, "r/a", 1, 1), Node(12, 12, "r/a", 1, 1)], "duplicate paths or child rollups exceed parent"),
    ([Node(10, 10, "x" * 2049, 0, 0)], "invalid own contribution or name longer than 2048 characters"),
])
def test_invalid_or_incomplete_inputs_refuse(nodes: list[Node], error: str) -> None:
    with pytest.raises(ValueError) as caught:
        HotNames(nodes, 1)
    assert str(caught.value) == error


@pytest.mark.parametrize("kwargs", [{"threshold": 0}, {"max_chars": 0}, {"max_chars": 8}, {"pair_budget": 0}, {"pair_budget": 2_000_001}])
def test_invalid_construction_budgets_refuse(kwargs: dict) -> None:
    with pytest.raises(ValueError) as caught:
        HotNames([Node(10, 10, "r", 0, 0)], **({"threshold": 1} | kwargs))
    assert str(caught.value) == "positive threshold, 1..7 characters and <=2M name-pattern pairs required"


@pytest.mark.parametrize("pattern,kwargs", [("", {}), ("a/b", {}), ("toolong8", {}), ("aa", {"levels": 0}), ("aa", {"levels": 4}), ("aa", {"budget": 0}), ("aa", {"budget": 257})])
def test_invalid_view_patterns_and_limits_refuse(pattern: str, kwargs: dict) -> None:
    with pytest.raises(ValueError) as caught:
        HotNames(NODES, 1).view(pattern, "bucket", **kwargs)
    assert str(caught.value) == "one name literal <=max_chars, 1..3 levels and 1..256 children required"
