"""Pure bounded correctness model, NOT a disk-backed or fleet serving index.

Construction scans N complete nodes and keeps shared own-object prefixes O(N).
Each query scans N names to construct F disjoint first-hit roots, then keeps
O(F) frontier prefixes. Neither construction cost has fleet acceptance.

For a scope with matching bytes M and integer threshold T, probe byte ranks
T, 2T, ..., floor(M/T)*T (zero-based byte offsets kT-1). Each child occupies
one contiguous interval in the
matching-byte order. An interval of length >=T contains a probe, including
exact ties. Refine a selected frontier rollup through the shared own-byte
prefix to a real contributing node BEFORE mapping it to an immediate child.
Query work: O(log F) aggregate; each probe O(log F + log N + depth) select /
parent lookup; each unique candidate O(log F) exact aggregate. No sibling
enumeration occurs after construction. Zero-byte counts and parent own
objects survive exact subtraction into Other. A paired local threshold
ceil(max(M0,M1)/K), clamped to 1, yields at most K probes per date and at most
2K path-aligned candidates. Counterpart totals use each date's own geometry.

Needed disk primitives: immutable audited dated scalar source; path/interval /
parent locator; shared own-byte rank-select; query-frontier range sums and
rank-select. Ordinary covered subtree totals reuse audited scalar rollups.
Neither physical blocks/read amplification, frontier storage/build cost,
lookup latency, cache behavior nor peak RAM has been measured here. Python
storage is illustrative; this is not archive, expiry or publication acceptance.
"""

from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from typing import Sequence

from .hot_names import Node, pack_roots


@dataclass
class Cost:
    probes: int = 0
    aggregate_calls: int = 0
    frontier_selects: int = 0
    shared_selects: int = 0
    parent_hops: int = 0
    candidate_children: int = 0


def _positive(value: int) -> bool:
    return type(value) is int and value > 0


class SharedWeights:
    """One complete dated tree; caller must not mutate retained source attributes."""

    def __init__(self, nodes: Sequence[Node]) -> None:
        if not 1 <= len(nodes) <= 20_000:
            raise ValueError('complete correctness fixture must have 1..20K nodes')
        self.nodes = tuple(nodes)
        self.positions, self.by_path, self.parents = [], {}, []
        own, stack, previous = [], [], -1
        for index, node in enumerate(self.nodes):
            if (any(type(value) is not int for value in (node.pre, node.post, node.b, node.o)) or
                    not previous < node.pre <= node.post < 1 << 64 or not 0 <= min(node.b, node.o) or max(node.b, node.o) >= 1 << 64 or
                    not isinstance(node.path, str) or node.path in self.by_path):
                raise ValueError('invalid unique paths, increasing intervals or UInt64 rollups')
            while stack and self.nodes[stack[-1]].post < node.pre:
                closed = self.nodes[stack.pop()]
                if closed.post != previous:
                    raise ValueError('interval endpoint is not the last complete descendant')
            parent = stack[-1] if stack else None
            if parent is None and index != 0:
                raise ValueError('complete source must have one root')
            if parent is not None:
                ancestor = self.nodes[parent]
                if node.post > ancestor.post or node.path.rpartition('/')[0] != ancestor.path:
                    raise ValueError('source is not prefix-closed with laminar intervals')
                own[parent][0] -= node.b
                own[parent][1] -= node.o
            self.parents.append(parent)
            self.positions.append(node.pre)
            self.by_path[node.path] = index
            own.append([node.b, node.o])
            stack.append(index)
            previous = node.pre
        if any(self.nodes[index].post != previous for index in stack):
            raise ValueError('incomplete interval endpoints')
        if any(min(row) < 0 for row in own):
            raise ValueError('child rollups exceed their parent')
        if any(b > 0 and o == 0 for b, o in own):
            raise ValueError('positive own bytes require a positive own object count')
        bytes_prefix, objects_prefix = [0], [0]
        for b, o in own:
            bytes_prefix.append(bytes_prefix[-1] + b)
            objects_prefix.append(objects_prefix[-1] + o)
        if (bytes_prefix[-1], objects_prefix[-1]) != (self.nodes[0].b, self.nodes[0].o):
            raise ValueError('own-object prefixes do not conserve complete root weights')
        self.bytes_prefix, self.objects_prefix = tuple(bytes_prefix), tuple(objects_prefix)

    def locate(self, path: str) -> int | None:
        root = self.nodes[0].path
        if not isinstance(path, str) or (root and path != root and not path.startswith(root + '/')):
            raise ValueError('path is outside this complete source scope')
        return self.by_path.get(path)

    def select(self, index: int, rank: int, cost: Cost) -> int:
        """1-based byte rank within an ordinary subtree, select OWN contribution."""
        node = self.nodes[index]
        if not _positive(rank) or rank > node.b:
            raise ValueError('byte rank is outside the subtree')
        end = bisect_right(self.positions, node.post)
        cost.shared_selects += 1
        return bisect_left(self.bytes_prefix, self.bytes_prefix[index] + rank, index + 1, end + 1) - 1

    def child(self, scope: int, contribution: int, cost: Cost) -> str | None:
        if scope == contribution:
            return None  # Scope's own objects belong to Other, not a made-up child.
        while self.parents[contribution] != scope:
            parent = self.parents[contribution]
            if parent is None:
                raise ValueError('selected contribution is outside the requested subtree')
            contribution = parent
            cost.parent_hops += 1
        cost.parent_hops += 1
        return self.nodes[contribution].path


class Frontier:
    def __init__(
        self,
        source: SharedWeights,
        pattern: str,
    ) -> None:
        if not isinstance(pattern, str) or not pattern or '/' in pattern or '\0' in pattern or len(pattern.lower()) > 512:
            raise ValueError('one nonempty name literal of at most 512 characters is required')
        self.source, self.pattern = source, pattern.lower()
        external = source.nodes[0].path.split('/')[:-1]
        roots = [source.nodes[0]] if any(self.pattern in component.lower() for component in external) else []
        last_end = roots[0].post if roots else -1
        for node in source.nodes:
            if node.pre > last_end and self.pattern in node.path.rsplit('/', 1)[-1].lower():
                roots.append(node)
                last_end = node.post
        self.roots = tuple(roots)
        self.payload = pack_roots(roots)

    def total(self, path: str, cost: Cost | None = None) -> tuple[int, int]:
        if cost is not None:
            cost.aggregate_calls += 1
        index = self.source.locate(path)
        return self.payload.total(self.source.nodes[index]) if index is not None else (0, 0)

    def select(self, index: int, rank: int, cost: Cost) -> int:
        node, payload = self.source.nodes[index], self.payload
        covering = bisect_right(payload.starts, node.pre) - 1
        if covering >= 0 and payload.ends[covering] >= node.post:
            return self.source.select(index, rank, cost)
        lo, hi = bisect_left(payload.starts, node.pre), bisect_right(payload.starts, node.post)
        if not _positive(rank) or rank > payload.bytes_prefix[hi] - payload.bytes_prefix[lo]:
            raise ValueError('byte rank is outside the matching subtree')
        absolute = payload.bytes_prefix[lo] + rank
        cost.frontier_selects += 1
        selected = bisect_left(payload.bytes_prefix, absolute, lo + 1, hi + 1) - 1
        root = self.roots[selected]
        return self.source.select(self.source.by_path[root.path], absolute - payload.bytes_prefix[selected], cost)

    def candidates(
        self,
        path: str,
        threshold: int,
        max_probes: int = 256,
        *,
        cost: Cost,
    ) -> set[str]:
        if not _positive(threshold) or not _positive(max_probes) or max_probes > 256:
            raise ValueError('positive threshold and 1..256 probe cap are required')
        index, total = self.source.locate(path), self.total(path, cost)[0]
        if total // threshold > max_probes:
            raise ValueError('complete quantile discovery exceeds probe cap; no partial result')
        candidates = set()
        for rank in range(threshold, total + 1, threshold):
            cost.probes += 1
            own = self.select(index, rank, cost)
            child = self.source.child(index, own, cost)
            if child is not None:
                candidates.add(child)
        cost.candidate_children = len(candidates)
        return candidates

    def partition(self, path: str, threshold: int) -> dict:
        cost = Cost()
        candidates = self.candidates(path, threshold, cost=cost)
        b, o = self.total(path, cost)
        children = [{'path': child, 'b': cb, 'o': co} for child in sorted(candidates)
                    for cb, co in [self.total(child, cost)] if cb >= threshold]
        children.sort(key=lambda row: (-row['b'], row['path']))
        return {'path': path, 'present': self.source.locate(path) is not None, 'b': b, 'o': o, 'threshold_bytes': threshold,
                'children': children, 'other': {'b': b - sum(row['b'] for row in children), 'o': o - sum(row['o'] for row in children)}, 'cost': asdict(cost)}


def paired(
    before: Frontier,
    after: Frontier,
    path: str,
    budget: int = 64,
) -> dict:
    if before.pattern != after.pattern or before.source.nodes[0].path != after.source.nodes[0].path or not _positive(budget) or budget > 256:
        raise ValueError('same literal/source scope and a 1..256 child budget required')
    if before.source.locate(path) is None and after.source.locate(path) is None:
        raise ValueError('requested path is absent from both complete dated sources')
    costs = [Cost(), Cost()]
    totals = [side.total(path, cost) for side, cost in zip((before, after), costs)]
    threshold = max(1, (max(b for b, _ in totals) + budget - 1) // budget)
    candidates = before.candidates(path, threshold, budget, cost=costs[0]) | after.candidates(path, threshold, budget, cost=costs[1])

    def weights(a: tuple[int, int], b: tuple[int, int]) -> dict:
        return {'before': {'b': a[0], 'o': a[1]}, 'after': {'b': b[0], 'o': b[1]}, 'delta': {'b': b[0] - a[0], 'o': b[1] - a[1]}}

    children = []
    for child in sorted(candidates):
        values = [side.total(child, cost) for side, cost in zip((before, after), costs)]
        if max(b for b, _ in values) >= threshold:
            children.append({'path': child, **weights(*values)})
    children.sort(key=lambda row: (-max(row['before']['b'], row['after']['b']), row['path']))
    remainder = [tuple(value - sum(row[side][key] for row in children) for value, key in zip(total, ('b', 'o')))
                 for total, side in zip(totals, ('before', 'after'))]
    return {'path': path, 'threshold_bytes': threshold, **weights(*totals), 'children': children, 'other': weights(*remainder),
            'present': [side.source.locate(path) is not None for side in (before, after)], 'candidate_children': len(candidates),
            'cost': {'before': asdict(costs[0]), 'after': asdict(costs[1])}}
