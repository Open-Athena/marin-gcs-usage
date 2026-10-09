"""Bounded, complete-subtree hot-name experiment; not a fleet serving index."""

from array import array
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha256
from json import loads
from resource import RUSAGE_SELF, getrusage
from sys import platform
from time import perf_counter
from typing import Mapping, Protocol

from .client import Ch, lit
from .narrow import identifier
from .serve import depth_of


@dataclass(frozen=True)
class Node:
    pre: int
    post: int
    path: str
    b: int
    o: int


class Aggregate(Protocol):
    def total(self, node: Node) -> tuple[int, int]: ...


@dataclass
class Payload:
    starts: array
    ends: array
    bytes_prefix: array
    objects_prefix: array

    def total(self, node: Node) -> tuple[int, int]:
        i = bisect_right(self.starts, node.pre) - 1
        if i >= 0 and self.ends[i] >= node.post:
            return node.b, node.o
        lo = bisect_left(self.starts, node.pre)
        hi = bisect_right(self.starts, node.post)
        return self.bytes_prefix[hi] - self.bytes_prefix[lo], self.objects_prefix[hi] - self.objects_prefix[lo]

    @property
    def packed_bytes(self) -> int:
        return sum(len(a) * a.itemsize for a in (self.starts, self.ends, self.bytes_prefix, self.objects_prefix))


def pack_roots(selected: list[Node]) -> Payload:
    b_prefix, o_prefix = array("Q", [0]), array("Q", [0])
    for node in selected:
        b_prefix.append(b_prefix[-1] + node.b)
        o_prefix.append(o_prefix[-1] + node.o)
    return Payload(array("Q", (node.pre for node in selected)), array("Q", (node.post for node in selected)), b_prefix, o_prefix)


class HotNames:
    def __init__(
        self,
        nodes: list[Node],
        threshold: int,
        max_chars: int = 7,
        pair_budget: int = 2_000_000,
    ):
        if not 1 <= threshold or not 1 <= max_chars <= 7 or not 1 <= pair_budget <= 2_000_000:
            raise ValueError("positive threshold, 1..7 characters and <=2M name-pattern pairs required")
        if not nodes or len(nodes) > 20_000:
            raise ValueError("complete subtree must contain 1..20K nodes")
        self.nodes = nodes
        self.by_path = {node.path: node for node in nodes}
        self.by_name: dict[str, list[Node]] = defaultdict(list)
        self.children: dict[str, list[Node]] = defaultdict(list)
        self.own = {node.path: [node.b, node.o] for node in nodes}
        stack: list[Node] = []
        previous = -1
        for node in nodes:
            if node.pre <= previous or node.post < node.pre or min(node.b, node.o) < 0:
                raise ValueError("invalid preorder or negative rollup")
            previous = node.pre
            while stack and stack[-1].post < node.pre:
                stack.pop()
            if stack:
                parent = stack[-1]
                if node.path.rpartition("/")[0] != parent.path or node.post > parent.post:
                    raise ValueError("subtree is incomplete or intervals are not laminar")
                self.children[parent.path].append(node)
                self.own[parent.path][0] -= node.b
                self.own[parent.path][1] -= node.o
            elif node != nodes[0]:
                raise ValueError("subtree has multiple roots")
            if min(self.own[node.path]) < 0 or len(node.path.rsplit("/", 1)[-1]) > 2048:
                raise ValueError("invalid own contribution or name longer than 2048 characters")
            stack.append(node)
            self.by_name[node.path.rsplit("/", 1)[-1].lower()].append(node)
        if len(self.by_path) != len(nodes) or any(min(values) < 0 for values in self.own.values()):
            raise ValueError("duplicate paths or child rollups exceed parent")
        self.max_chars = max_chars
        frequencies = Counter(node.path.rsplit("/", 1)[-1].lower() for node in nodes)
        self.name_frequencies = frequencies
        name_grams: dict[str, set[str]] = {name: set() for name in frequencies}
        self.counts: Counter[str] = Counter()
        pairs = 0
        candidate_patterns = 0
        previous_hot: set[str] = set()
        for k in range(1, max_chars + 1):
            candidates: dict[str, set[str]] = {}
            counts: Counter[str] = Counter()
            for name, frequency in frequencies.items():
                grams = {name[i:i + k] for i in range(max(0, len(name) - k + 1))
                         if k == 1 or name[i:i + k - 1] in previous_hot}
                pairs += len(grams)
                if pairs > pair_budget:
                    raise ValueError("name-pattern pair budget exceeded; no partial index returned")
                candidates[name] = grams
                for gram in grams:
                    counts[gram] += frequency
            candidate_patterns += len(counts)
            previous_hot = {gram for gram, count in counts.items() if count >= threshold}
            self.counts.update({gram: counts[gram] for gram in previous_hot})
            for name, grams in candidates.items():
                name_grams[name].update(grams & previous_hot)
            if not previous_hot:
                break
        self.pairs = pairs
        self.candidate_patterns = candidate_patterns
        hot = set(self.counts)
        roots: dict[str, list[Node]] = {gram: [] for gram in hot}
        external = nodes[0].path.split("/")[:-1]
        external_hot = {gram for gram in hot if any(gram in name.lower() for name in external)}
        for gram in external_hot:
            roots[gram].append(nodes[0])
        active = Counter({gram: 1 for gram in external_hot})
        lineage: list[tuple[Node, set[str]]] = []
        root_records = len(external_hot)
        for node in nodes:
            while lineage and lineage[-1][0].post < node.pre:
                _, old = lineage.pop()
                active.subtract(old)
            matches = name_grams[node.path.rsplit("/", 1)[-1].lower()] & hot
            for gram in matches:
                if not active[gram]:
                    roots[gram].append(node)
                    root_records += 1
                    if root_records > 2_000_000:
                        raise ValueError("coverage-root budget exceeded; no partial index returned")
            active.update(matches)
            lineage.append((node, matches))
        shared: dict[tuple[int, ...], Payload] = {}
        self.payloads: dict[str, Payload] = {}
        for gram, selected in roots.items():
            key = tuple(node.pre for node in selected)
            if key not in shared:
                shared[key] = pack_roots(selected)
            self.payloads[gram] = shared[key]
        self.threshold = threshold
        self.shared = shared

    def oracle(self, pattern: str, node: Node) -> tuple[int, int]:
        """Own-contribution scan, independent of grams and coverage-root reduction."""
        b, o = 0, 0
        for candidate in self.nodes:
            if node.pre <= candidate.pre <= node.post and any(pattern in name.lower() for name in candidate.path.split("/")):
                own_b, own_o = self.own[candidate.path]
                b += own_b
                o += own_o
        return b, o

    def total(self, pattern: str, node: Node) -> tuple[int, int]:
        if any(pattern in name.lower() for name in self.nodes[0].path.split("/")[:-1]):
            return node.b, node.o
        payload = self.payloads.get(pattern)
        return payload.total(node) if payload is not None else self.oracle(pattern, node)

    def view(
        self,
        pattern: str,
        path: str,
        levels: int = 3,
        budget: int = 64,
        *,
        payloads: Mapping[str, Aggregate] | None = None,
    ) -> dict:
        pattern = pattern.lower()
        if not pattern or "/" in pattern or len(pattern) > self.max_chars or not 1 <= levels <= 3 or not 1 <= budget <= 256:
            raise ValueError("one name literal <=max_chars, 1..3 levels and 1..256 children required")
        node = self.by_path[path]
        ancestor_match = any(pattern in name.lower() for name in self.nodes[0].path.split("/")[:-1])
        payload = (self.payloads if payloads is None else payloads).get(pattern)
        if not ancestor_match and payload is None:
            selected = sorted((candidate for name, postings in self.by_name.items() if pattern in name for candidate in postings), key=lambda candidate: candidate.pre)
            roots = []
            last_end = -1
            for candidate in selected:
                if candidate.pre > last_end:
                    roots.append(candidate)
                    last_end = candidate.post
            payload = pack_roots(roots)

        def get_total(current: Node) -> tuple[int, int]:
            return (current.b, current.o) if ancestor_match else payload.total(current)

        total = get_total(node)
        byte_threshold = max(1, (total[0] + budget - 1) // budget)

        def expand(current: Node, remaining: int) -> dict:
            b, o = get_total(current)
            children = []
            if remaining:
                for child in self.children[current.path]:
                    if get_total(child)[0] >= byte_threshold:
                        children.append(expand(child, remaining - 1))
            remainder = [b - sum(child["b"] for child in children), o - sum(child["o"] for child in children)]
            return {"path": current.path, "b": b, "o": o, "children": children, "remainder": remainder}

        tree = expand(node, levels)
        route = "ancestor-rollup" if ancestor_match else "hot-prefix" if pattern in self.payloads else "cold-postings"
        return {"pattern": pattern, "route": route, "threshold_bytes": byte_threshold, "tree": tree}

    def check(self, body: dict) -> int:
        def check_node(tree: dict) -> int:
            expected = self.oracle(body["pattern"], self.by_path[tree["path"]])
            if (tree["b"], tree["o"]) != expected:
                raise AssertionError("prefix result differs from complete own-contribution scan")
            remainder = [expected[i] - sum(child[("b", "o")[i]] for child in tree["children"]) for i in range(2)]
            if tree["remainder"] != remainder or min(remainder) < 0:
                raise AssertionError("invalid exact remainder")
            return 1 + sum(check_node(child) for child in tree["children"])

        return check_node(body["tree"])

    def stats(self) -> dict:
        return {"threshold_paths": self.threshold, "hot_patterns": len(self.payloads), "unique_payloads": len(self.shared),
                "root_records_before_sharing": sum(len(p.starts) for p in self.payloads.values()),
                "root_records_after_sharing": sum(len(p.starts) for p in self.shared.values()),
                "packed_payload_bytes": sum(p.packed_bytes for p in self.shared.values()),
                "hot_query_utf8_bytes": sum(len(q.encode()) for q in self.payloads),
                "candidate_patterns_counted": self.candidate_patterns, "candidate_name_pattern_pairs": self.pairs}


def bench(
    url: str,
    target: str,
    date: str,
    path: str,
    thresholds: tuple[int, ...],
    patterns: tuple[str, ...],
) -> dict:
    identifier(target)
    ch = Ch(url, db=target, max_threads=4, max_memory_usage=1 << 30,
            max_execution_time=30, timeout_before_checking_execution_speed=0, timeout_overflow_mode="throw")
    try:
        start = perf_counter()
        manifest = loads(ch.scalar(f"SELECT doc FROM {target}.history_manifest"))
        db = identifier(manifest["dbs"][manifest["dates"].index(date)])
        bounds = ch.json(f"SELECT pre,post FROM {target}.dictionary WHERE depth={depth_of(path)} AND path={lit(path)}")
        if len(bounds) != 1 or bounds[0][1] - bounds[0][0] + 1 > 20_000:
            raise ValueError("one complete union interval <=20K nodes required")
        lo, hi = bounds[0]
        nodes = [Node(*row) for row in ch.json(f"SELECT pre,post,path,b,o FROM {db}.nodes WHERE pre >= {lo} AND pre <= {hi} ORDER BY pre LIMIT 20001")]
        if not nodes or nodes[0].path != path:
            raise ValueError("scope absent in requested snapshot")
        load_s = perf_counter() - start
        return {**bench_nodes(nodes, thresholds, patterns), "scope": "complete bounded single-snapshot subtree; local thresholds, not global frequency or fleet acceptance",
                "date": date, "load_s": load_s}
    finally:
        ch.close()


def bench_nodes(
    nodes: list[Node],
    thresholds: tuple[int, ...],
    patterns: tuple[str, ...],
) -> dict:
    from .hot_names_blocked import BlockedBuilder

    builder = BlockedBuilder(nodes)
    runs = []
    for threshold in thresholds:
        start = perf_counter()
        index = HotNames(nodes, threshold)
        build_s = perf_counter() - start
        start = perf_counter()
        blocked_unique = {id(payload): builder.pack(payload) for payload in index.shared.values()}
        blocked = {pattern: blocked_unique[id(payload)] for pattern, payload in index.payloads.items()}
        blocked_build_s = perf_counter() - start
        queries = []
        for pattern in patterns:
            timings = []
            for _ in range(5):
                start = perf_counter()
                body = index.view(pattern, nodes[0].path)
                timings.append(perf_counter() - start)
            start = perf_counter()
            checked = index.check(body)
            oracle_s = perf_counter() - start
            matching_paths = sum(count for name, count in index.name_frequencies.items() if pattern.lower() in name)
            blocked_timings = []
            for _ in range(5):
                start = perf_counter()
                blocked_body = index.view(pattern, nodes[0].path, payloads=blocked)
                blocked_timings.append(perf_counter() - start)
            if blocked_body != body:
                raise AssertionError("blocked payload changes the complete partition")
            queries.append({"pattern": pattern, "direct_matching_paths": matching_paths,
                            "route": body["route"], "view_median_s": sorted(timings)[2],
                            "blocked_view_median_s": sorted(blocked_timings)[2],
                            "oracle_s": oracle_s, "verified_nodes": checked, "tree": body["tree"]})
            if body["tree"]["children"]:
                drill_path = body["tree"]["children"][0]["path"]
                start = perf_counter()
                drill = index.view(pattern, drill_path)
                drill_s = perf_counter() - start
                drill_checked = index.check(drill)
                start = perf_counter()
                blocked_drill = index.view(pattern, drill_path, payloads=blocked)
                blocked_drill_s = perf_counter() - start
                if blocked_drill != drill:
                    raise AssertionError("blocked payload changes the complete drill partition")
                queries[-1]["drill"] = {"view_s": drill_s, "blocked_view_s": blocked_drill_s, "verified_nodes": drill_checked, "tree": drill["tree"]}
        runs.append({**index.stats(), "build_s": build_s, "blocked_conversion_s": blocked_build_s,
                     "blocked_payload_bytes": sum(payload.packed_bytes for payload in blocked_unique.values()), "queries": queries})
        del index, blocked, blocked_unique
    rss = getrusage(RUSAGE_SELF).ru_maxrss * (1 if platform == "darwin" else 1024)
    return {"path": nodes[0].path, "nodes": len(nodes), "max_chars": 7, "levels": 3, "budget": 64,
            "peak_process_rss_bytes": rss, "rss_includes_both_comparison_indexes": True,
            "payload_bytes_exclude_python_and_dictionary_overhead": True, "runs": runs}


def hash_fixture(leaves: int = 8192) -> list[Node]:
    """Complete three-level deterministic random-looking hash names with .npy tails."""
    if not 1 <= leaves <= 16_000:
        raise ValueError("hash fixture requires 1..16K leaves")
    names: dict[str, list[str]] = defaultdict(list)
    weights = {}
    for i in range(leaves):
        path = f"fleet/region-{i % 8}/group-{i % 128}/{sha256(str(i).encode()).hexdigest()[:32]}.npy"
        weights[path] = (1 + i % 17, 1)
        parts = path.split("/")
        for k in range(1, len(parts)):
            parent, child = "/".join(parts[:k]), "/".join(parts[:k + 1])
            if child not in names[parent]:
                names[parent].append(child)
    nodes: list[Node | None] = []

    def visit(path: str) -> tuple[int, int]:
        position = len(nodes)
        nodes.append(None)
        b, o = weights.get(path, (0, 0))
        for child in sorted(names[path]):
            child_b, child_o = visit(child)
            b += child_b
            o += child_o
        nodes[position] = Node(position + 17, len(nodes) + 16, path, b, o)
        return b, o

    visit("fleet")
    return [node for node in nodes if node is not None]


def find_scopes(
    url: str,
    target: str,
    date: str,
    name: str,
) -> dict:
    """Find complete <=20K-node parents from the first eight exact-name postings."""
    identifier(target)
    if not name or "/" in name or len(name) > 512:
        raise ValueError("one exact basename required")
    ch = Ch(url, db=target, max_threads=4, max_memory_usage=1 << 30,
            max_execution_time=30, timeout_before_checking_execution_speed=0, timeout_overflow_mode="throw")
    try:
        manifest = loads(ch.scalar(f"SELECT doc FROM {target}.history_manifest"))
        db = identifier(manifest["dbs"][manifest["dates"].index(date)])
        ids = ch.json(f"SELECT nid FROM {target}.names WHERE l={lit(name.lower())} LIMIT 2")
        if len(ids) != 1:
            raise ValueError("exact basename absent or ambiguous")
        paths = ch.json(f"SELECT path FROM {db}.nodes_by_name WHERE nid={ids[0][0]} LIMIT 8")
        scopes = {}
        for (path,) in paths:
            parent = path.rpartition("/")[0]
            bounds = ch.json(f"SELECT pre,post FROM {target}.dictionary WHERE depth={depth_of(parent)} AND path={lit(parent)}")
            if len(bounds) != 1:
                raise ValueError("posting parent missing from dictionary")
            lo, hi = bounds[0]
            if hi - lo + 1 <= 20_000:
                scopes[parent] = {"path": parent, "union_nodes": hi - lo + 1}
        return {"scope": "first eight exact-basename postings, not a representative sample", "name": name, "date": date, "scopes": list(scopes.values())}
    finally:
        ch.close()
