"""Streaming predicate activation agrees with disjoint own-object queries."""

from dataclasses import replace
from itertools import product
from typing import Iterator

import pytest

from dt_cloud.chstore.hot_l1_batch import Bucket, Node, Predicate, _Automaton, build


OWN = {
    "a/hit": (0, 1),
    "a/hit/hit.json": (5, 1),
    "a/hit/plain": (7, 1),
    "a/aaaa": (3, 1),
    "a/ÅRO/Blå.json": (11, 1),
    "b/nest/hit.json": (0, 1),
    "b/nest/%_": (13, 1),
    "b/other": (17, 1),
}
PATTERNS = [Predicate(8, "hit"), Predicate(20, ".json"), Predicate(3, "a"), Predicate(41, "aa"),
            Predicate(2, "aaa"), Predicate(9, "å"), Predicate(12, "%_"), Predicate(1, "absent"), Predicate(50, "b")]


def fixture(own: dict[str, tuple[int, int]]) -> tuple[list[Node], list[Bucket]]:
    paths = {""}
    for path in OWN:
        parts = path.split("/")
        paths.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    paths = sorted(paths)
    bounds = {path: max(i for i, child in enumerate(paths) if not path or child == path or child.startswith(path + "/")) for path in paths}
    buckets = [Bucket(path, i, bounds[path]) for i, path in enumerate(paths) if path and "/" not in path]
    nodes = []
    for pre, path in enumerate(paths):
        values = [value for child, value in own.items() if not path or child == path or child.startswith(path + "/")]
        if path and not values:
            continue
        nodes.append(Node(pre, bounds[path], path.rsplit("/", 1)[-1].lower(), sum(v[0] for v in values), sum(v[1] for v in values)))
    return nodes, buckets


def expected(own: dict[str, tuple[int, int]], buckets: list[Bucket]) -> list[dict]:
    results = []
    for predicate in PATTERNS:
        rows = []
        for bucket in buckets:
            matches = [value for path, value in own.items() if path.split("/")[0] == bucket.path and predicate.text in path.lower()]
            rows.append({"path": bucket.path, "pre": bucket.pre, "post": bucket.post,
                         "b": sum(v[0] for v in matches), "o": sum(v[1] for v in matches)})
        results.append({"predicate_id": predicate.id, "pattern": predicate.text,
                        "root": {"b": sum(row["b"] for row in rows), "o": sum(row["o"] for row in rows)}, "buckets": rows})
    return results


@pytest.mark.parametrize("own", [OWN, {path: value for path, value in OWN.items() if path.startswith("b/")}, {}])
def test_complete_stream_matches_independent_own_object_oracle(own: dict[str, tuple[int, int]]) -> None:
    nodes, buckets = fixture(own)
    body = build(iter(nodes), buckets, PATTERNS, expected_nodes=len(nodes))
    assert body["results"] == expected(own, buckets)
    assert (body["schema"], body["exact"], body["incremental"], body["levels"], body["nodes_read"], body["registered_predicates"]) == (
        "hot-l1-batch-v1", True, False, 1, len(nodes), len(PATTERNS),
    )
    assert sorted(body["timings"]) == ["automaton_build_s", "total_s"]
    assert [type(value) is float and value >= 0 for value in body["timings"].values()] == [True, True]
    if not own:
        assert (body["peak_stack_depth"], body["peak_active_predicates"]) == (1, 0)


def test_repeated_occurrences_suffix_patterns_and_siblings_contribute_once() -> None:
    nodes = [Node(0, 3, "", 10, 3), Node(1, 3, "bucket", 10, 3), Node(2, 2, "aaaa", 3, 1), Node(3, 3, "aaaa", 7, 2)]
    predicates = [Predicate(7, "a"), Predicate(8, "aa"), Predicate(9, "aaa"), Predicate(10, "aaaa")]
    body = build(nodes, [Bucket("bucket", 1, 3)], predicates, expected_nodes=4)
    assert body["results"] == [{"predicate_id": predicate.id, "pattern": predicate.text, "root": {"b": 10, "o": 3},
                                "buckets": [{"path": "bucket", "pre": 1, "post": 3, "b": 10, "o": 3}]} for predicate in predicates]
    assert (body["peak_stack_depth"], body["peak_active_predicates"]) == (2, 4)


def test_automaton_matches_every_small_literal_without_copied_suffix_outputs() -> None:
    patterns = [Predicate(i, "".join(chars)) for i, chars in enumerate(chars for n in range(1, 4) for chars in product("ab", repeat=n))]
    machine = _Automaton(patterns)
    assert sum(len(state.terminal) for state in machine.states) == len(patterns)
    for n in range(6):
        for characters in product("ab", repeat=n):
            text = "".join(characters)
            assert sorted(set(machine.matches(text))) == [i for i, pattern in enumerate(patterns) if pattern.text in text]


@pytest.mark.parametrize("change,message", [
    ("order", "batch node order or interval bounds are invalid"),
    ("cross", "batch node intervals cross rather than nest"),
    ("bucket", "batch bucket node differs from its frozen bounds"),
    ("root-name", "batch stream must start with the empty-name complete global root"),
    ("root-bound", "batch stream must start with the empty-name complete global root"),
    ("outside", "batch node lies outside the global domain"),
    ("missing-bucket", "batch descendant has no present bucket root"),
    ("too-big-child", "batch child rollups exceed their parent"),
    ("root-sum", "batch bucket rollups disagree with the global root"),
    ("null", "batch node.o must be a nonnegative integer"),
    ("bool", "batch node.b must be a nonnegative integer"),
    ("negative", "batch node.b must be a nonnegative integer"),
    ("slash", "batch node.name must be a possibly empty slash-free string"),
])
def test_broken_source_refuses(change: str, message: str) -> None:
    nodes = [Node(0, 5, "", 10, 2), Node(1, 5, "bucket", 10, 2), Node(2, 3, "nested", 3, 1),
             Node(3, 3, "hit", 3, 1), Node(4, 5, "another", 7, 1), Node(5, 5, "hit", 7, 1)]
    if change == "order":
        nodes[3] = replace(nodes[3], pre=2)
    elif change == "cross":
        nodes[3] = replace(nodes[3], post=4)
    elif change == "bucket":
        nodes[1] = replace(nodes[1], post=4)
    elif change == "root-name":
        nodes[0] = replace(nodes[0], name="root")
    elif change == "root-bound":
        nodes[0] = replace(nodes[0], post=4)
    elif change == "outside":
        nodes[2] = replace(nodes[2], post=6)
    elif change == "missing-bucket":
        del nodes[1]
    elif change == "too-big-child":
        nodes[3] = replace(nodes[3], b=4)
    elif change == "root-sum":
        nodes[0] = replace(nodes[0], b=11)
    elif change == "null":
        nodes[3] = replace(nodes[3], o=None)
    elif change == "bool":
        nodes[3] = replace(nodes[3], b=True)
    elif change == "negative":
        nodes[3] = replace(nodes[3], b=-1)
    else:
        nodes[3] = replace(nodes[3], name="a/b")
    with pytest.raises(ValueError) as caught:
        build(nodes, [Bucket("bucket", 1, 5)], [Predicate(1, "hit")], expected_nodes=len(nodes))
    assert str(caught.value) == message


def test_truncated_stream_and_iteration_failure_never_return_partial_totals() -> None:
    nodes, buckets = fixture(OWN)
    with pytest.raises(ValueError) as caught:
        build(nodes[:-1], buckets, PATTERNS, expected_nodes=len(nodes))
    assert str(caught.value) == "batch node count differs from the expected complete snapshot"

    def broken() -> Iterator[Node]:
        yield nodes[0]
        raise OSError("source failed")

    with pytest.raises(OSError) as caught:
        build(broken(), buckets, PATTERNS, expected_nodes=len(nodes))
    assert str(caught.value) == "source failed"


@pytest.mark.parametrize("predicates,message", [
    ([Predicate(1, "hit"), Predicate(1, "other")], "batch predicate IDs and normalized literals must be unique"),
    ([Predicate(1, "hit"), Predicate(2, "hit")], "batch predicate IDs and normalized literals must be unique"),
    ([Predicate(True, "hit")], "batch predicate.id must be a nonnegative integer"),
    ([Predicate(1, "")], "batch predicate.text must be a nonempty slash-free string"),
    ([Predicate(1, "a/b")], "batch predicate.text must be a nonempty slash-free string"),
])
def test_predicate_registry_refuses_invalid_identities(predicates: list[Predicate], message: str) -> None:
    nodes, buckets = fixture(OWN)
    with pytest.raises(ValueError) as caught:
        build(nodes, buckets, predicates, expected_nodes=len(nodes))
    assert str(caught.value) == message


@pytest.mark.parametrize("buckets,message", [
    ([], "batch requires one to six global bucket intervals"),
    ([Bucket("bucket", 2, 5)], "batch bucket intervals must partition the ordered global domain"),
    ([Bucket("a", 1, 3), Bucket("b", 3, 5)], "batch bucket intervals must partition the ordered global domain"),
    ([Bucket("a", 1, 3), Bucket("a", 4, 5)], "batch bucket intervals must partition the ordered global domain"),
    ([Bucket("a", 1, True)], "batch bucket.post must be a nonnegative integer"),
])
def test_bucket_registry_refuses_incomplete_or_ambiguous_bounds(buckets: list[Bucket], message: str) -> None:
    with pytest.raises(ValueError) as caught:
        build([], buckets, PATTERNS, expected_nodes=1)
    assert str(caught.value) == message
