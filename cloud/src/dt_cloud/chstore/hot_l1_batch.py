"""Fixture-scale streaming construction of many exact L1 predicates.

Names and predicates must already share the producer's lowercase convention.
The engine compares literal Unicode strings, never regex/glob syntax. An
Aho-Corasick trie discovers distinct name hits; only first activation on a
DFS ancestor chain contributes recursive weights. This is a Python algorithm
prototype, not a fleet-throughput claim or a persistent publication protocol.

For a positive slash-free literal, path truth is monotone: after an ancestor
name matches, every descendant full path matches. Newly activated queries
therefore identify disjoint first-hit frontiers. Summing those nodes' recursive
bytes/objects counts each matching own-object contribution once, including
objects stored at directory paths. Activation ends at the frozen inclusive
`post` boundary, so sibling hits can contribute independently.

Memory is O(depth + predicates * buckets + trie characters). Active flags and
generation stamps are predicate-sized; frames collectively retain at most one
activation per active predicate, never a full inherited set per node. Terminal
suffix links avoid copied automaton output lists. Work is stream rows plus name
characters and emitted automaton hits, not one scalar scan per predicate.
"""

from collections import deque
from dataclasses import dataclass, field
from time import monotonic
from typing import Iterable, Iterator, Sequence


@dataclass(frozen=True)
class Predicate:
    id: int
    text: str


@dataclass(frozen=True)
class Bucket:
    path: str
    pre: int
    post: int


@dataclass(frozen=True)
class Node:
    pre: int
    post: int
    name: str
    b: int
    o: int


@dataclass
class _State:
    children: dict[str, int] = field(default_factory=dict)
    fail: int = 0
    terminal: list[int] = field(default_factory=list)
    output: int = -1


class _Automaton:
    def __init__(self, patterns: Sequence[Predicate]) -> None:
        self.states = [_State()]
        for index, pattern in enumerate(patterns):
            state = 0
            for character in pattern.text:
                children = self.states[state].children
                if character not in children:
                    children[character] = len(self.states)
                    self.states.append(_State())
                state = children[character]
            self.states[state].terminal.append(index)
        queue = deque(self.states[0].children.values())
        while queue:
            state = queue.popleft()
            for character, child in self.states[state].children.items():
                fallback = self.states[state].fail
                while fallback and character not in self.states[fallback].children:
                    fallback = self.states[fallback].fail
                failed = self.states[fallback].children.get(character, 0)
                self.states[child].fail = failed
                self.states[child].output = failed if self.states[failed].terminal else self.states[failed].output
                queue.append(child)

    def matches(self, text: str) -> Iterator[int]:
        state = 0
        for character in text:
            while state and character not in self.states[state].children:
                state = self.states[state].fail
            state = self.states[state].children.get(character, 0)
            output = state
            while output != -1:
                yield from self.states[output].terminal
                output = self.states[output].output


@dataclass
class _Frame:
    post: int
    b: int
    o: int
    activated: list[int]
    child_b: int = 0
    child_o: int = 0


def _integer(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"batch {field_name} must be a nonnegative integer")
    return value


def _literal(value: object, field_name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value) or "/" in value:
        raise ValueError(f"batch {field_name} must be a {'possibly empty' if empty else 'nonempty'} slash-free string")
    return value


def build(
    nodes: Iterable[Node],
    buckets: Sequence[Bucket],
    predicates: Sequence[Predicate],
    *,
    expected_nodes: int,
) -> dict:
    """Consume one complete scalar DFS stream and return all registered L1s.

    Preorder gaps are allowed for absent snapshot paths. Intervals still use
    the audited frozen union bounds. The stream starts with virtual root 0;
    any present bucket's own row must precede its descendants.
    """
    start = monotonic()
    _integer(expected_nodes, "expected_nodes")
    if expected_nodes == 0:
        raise ValueError("batch expected_nodes must include the global root")
    buckets, predicates = tuple(buckets), tuple(predicates)
    if not 1 <= len(buckets) <= 6:
        raise ValueError("batch requires one to six global bucket intervals")
    for bucket in buckets:
        _literal(bucket.path, "bucket.path")
        _integer(bucket.pre, "bucket.pre")
        _integer(bucket.post, "bucket.post")
    if (len({bucket.path for bucket in buckets}) != len(buckets) or buckets[0].pre != 1 or
            any(bucket.pre > bucket.post for bucket in buckets) or
            any(a.post + 1 != b.pre for a, b in zip(buckets, buckets[1:]))):
        raise ValueError("batch bucket intervals must partition the ordered global domain")
    for predicate in predicates:
        _integer(predicate.id, "predicate.id")
        _literal(predicate.text, "predicate.text")
    if len({predicate.id for predicate in predicates}) != len(predicates) or len({predicate.text for predicate in predicates}) != len(predicates):
        raise ValueError("batch predicate IDs and normalized literals must be unique")
    automaton = _Automaton(predicates)
    automaton_s = monotonic() - start
    b_totals = [[0] * len(buckets) for _ in predicates]
    o_totals = [[0] * len(buckets) for _ in predicates]
    active, seen = [False] * len(predicates), [0] * len(predicates)
    stack, count, previous, bucket_index = [], 0, -1, 0
    ordinary = [None] * len(buckets)
    root = None
    peak_depth = peak_active = active_count = 0

    def pop() -> None:
        nonlocal active_count
        frame = stack.pop()
        if frame.child_b > frame.b or frame.child_o > frame.o:
            raise ValueError("batch child rollups exceed their parent")
        for index in frame.activated:
            active[index] = False
        active_count -= len(frame.activated)

    for node in nodes:
        for name, value in (("node.pre", node.pre), ("node.post", node.post), ("node.b", node.b), ("node.o", node.o)):
            _integer(value, name)
        _literal(node.name, "node.name", empty=True)
        if node.pre <= previous or node.pre > node.post:
            raise ValueError("batch node order or interval bounds are invalid")
        if node.post > buckets[-1].post:
            raise ValueError("batch node lies outside the global domain")
        previous, count = node.pre, count + 1
        if count == 1:
            if node.pre != 0 or node.post != buckets[-1].post or node.name != "":
                raise ValueError("batch stream must start with the empty-name complete global root")
            root = node.b, node.o
            stack.append(_Frame(node.post, node.b, node.o, []))
            peak_depth = 1
            continue
        while stack and stack[-1].post < node.pre:
            pop()
        if not stack or node.post > stack[-1].post:
            raise ValueError("batch node intervals cross rather than nest")
        parent = stack[-1]
        parent.child_b += node.b
        parent.child_o += node.o
        while bucket_index < len(buckets) and node.pre > buckets[bucket_index].post:
            bucket_index += 1
        if bucket_index == len(buckets) or node.post > buckets[bucket_index].post:
            raise ValueError("batch node interval crosses bucket boundaries")
        bucket = buckets[bucket_index]
        if node.pre == bucket.pre:
            if node.post != bucket.post:
                raise ValueError("batch bucket node differs from its frozen bounds")
            ordinary[bucket_index] = node.b, node.o
        elif ordinary[bucket_index] is None:
            raise ValueError("batch descendant has no present bucket root")
        activated = []
        for index in automaton.matches(node.name):
            if seen[index] == count:
                continue
            seen[index] = count
            if not active[index]:
                b_totals[index][bucket_index] += node.b
                o_totals[index][bucket_index] += node.o
                active[index] = True
                activated.append(index)
        active_count += len(activated)
        peak_active = max(peak_active, active_count)
        if node.pre == node.post:
            for index in activated:
                active[index] = False
            active_count -= len(activated)
        else:
            stack.append(_Frame(node.post, node.b, node.o, activated))
            peak_depth = max(peak_depth, len(stack))
    while stack:
        pop()
    if count != expected_nodes:
        raise ValueError("batch node count differs from the expected complete snapshot")
    if root != tuple(sum(value[column] for value in ordinary if value is not None) for column in (0, 1)):
        raise ValueError("batch bucket rollups disagree with the global root")
    results = []
    for index, predicate in enumerate(predicates):
        rows = [{"path": bucket.path, "pre": bucket.pre, "post": bucket.post, "b": b_totals[index][j], "o": o_totals[index][j]}
                for j, bucket in enumerate(buckets)]
        results.append({"predicate_id": predicate.id, "pattern": predicate.text, "root": {"b": sum(b_totals[index]), "o": sum(o_totals[index])}, "buckets": rows})
    return {"schema": "hot-l1-batch-v1", "exact": True, "incremental": False, "levels": 1,
            "nodes_read": count, "registered_predicates": len(predicates), "results": results,
            "peak_stack_depth": peak_depth, "peak_active_predicates": peak_active,
            "timings": {"automaton_build_s": round(automaton_s, 6), "total_s": round(monotonic() - start, 6)}}
