"""Batched aggregate-first traversal with a fixed global byte threshold.

Each frontier is disjoint. Across that frontier there are at most K byte
quantile points and K heavy children, regardless of its directory count.
Statements batch directory boundaries, selected blocks and child totals.
"""

from bisect import bisect_left
from time import monotonic

from .client import Ch, lit
from .coarse import CoarseRequest, NameIndex, byte_ranks, diff_oracle, oracle, select_rows
from .range_bench import aggregate, cumulative
from .serve import depth_of


def keys(paths: list[str]) -> str:
    return ",".join(f"({depth_of(path)},{lit(path)})" for path in paths)


def batch(
    ch: Ch,
    index: NameIndex,
    paths: list[str],
    budget: int,
    *,
    threshold: int | None = None,
    allow_absent: bool = False,
) -> list[dict]:
    """One disjoint frontier. A multi-parent batch needs a shared threshold."""
    start = monotonic()
    if not 1 <= budget <= 256 or not paths or len(paths) > 2 * budget or len(set(paths)) != len(paths):
        raise CoarseRequest("frontier requires 1..2*budget unique paths and budget <=256")
    if threshold is None and len(paths) != 1:
        raise CoarseRequest("multiple parents require one global byte threshold")
    if threshold is not None and threshold < 1:
        raise CoarseRequest("global byte threshold must be positive")
    if index.root and any(path != index.root and not path.startswith(index.root + "/") for path in paths):
        raise CoarseRequest("path outside the frozen index")
    bounds = ch.json(f"SELECT pre, post, path FROM {index.target}.dictionary WHERE (depth, path) IN ({keys(paths)}) ORDER BY pre")
    if len(bounds) != len(paths):
        raise CoarseRequest("frontier path not in the frozen dictionary")
    if any(a[1] >= b[0] for a, b in zip(bounds, bounds[1:])):
        raise CoarseRequest("frontier directory intervals overlap")
    present = {row[0] for row in ch.json(f"SELECT pre FROM {index.db}.nodes WHERE pre IN ({','.join(str(row[0]) for row in bounds)})")}
    if present != {row[0] for row in bounds} and not allow_absent:
        raise CoarseRequest("frontier path not present at the selected scan")
    limits = cumulative(ch, index.source, [point for lo, hi, _ in bounds if lo in present for point in (lo, hi + 1)], index.prefix, index.block_rows)
    totals = {lo: tuple(b - a for a, b in zip(limits[lo], limits[hi + 1])) if lo in present else (0, 0, 0) for lo, hi, _ in bounds}
    if threshold is None:
        threshold, _ = byte_ranks(totals[bounds[0][0]][1], budget)
    ranks, owners = [], []
    for lo, hi, path in bounds:
        if lo == hi or lo not in present:
            continue
        local = range(threshold, totals[lo][1] + 1, threshold)
        ranks.extend(limits[lo][1] + rank for rank in local)
        owners.extend([lo] * len(local))
    if len(ranks) > budget:
        raise CoarseRequest("global threshold exceeds the frontier quantile work budget")
    candidates, child_owners, selected = [], {}, []
    if ranks:
        blocks = sorted({index.prefix.blocks[bisect_left(index.prefix.totals[1], rank) - 1] for rank in ranks})
        postings = ch.json(f"SELECT pre, b FROM ({index.source}) WHERE intDiv(pre, {index.block_rows}) IN ({','.join(map(str, blocks))}) ORDER BY pre")
        selected = select_rows(index.prefix, ranks, postings, index.block_rows)
        selected_paths = dict(ch.json(f"SELECT pre, path FROM {index.db}.nodes WHERE pre IN ({','.join(map(str, sorted(set(selected))))})"))
        parent_paths = {lo: path for lo, _, path in bounds}
        for owner, leaf in zip(owners, selected):
            parent = parent_paths[owner]
            prefix = parent + "/" if parent else ""
            path = selected_paths[leaf]
            if not path.startswith(prefix) or path == parent:
                raise RuntimeError("selected posting is outside its frontier parent")
            child_owners[prefix + path[len(prefix):].split("/")[0]] = owner
        candidates = ch.json(f"SELECT pre, post, path FROM {index.target}.dictionary WHERE (depth, path) IN ({keys(list(child_owners))}) ORDER BY pre")
        if len(candidates) != len(child_owners):
            raise RuntimeError("frontier child dictionary intervals are inconsistent")
    values = aggregate(ch, index.source, [(lo, hi + 1) for lo, hi, _ in candidates], index.prefix, index.block_rows)
    children = {lo: [] for lo, _, _ in bounds}
    parent_ends = {lo: hi for lo, hi, _ in bounds}
    for (lo, hi, path), (n, b, o) in zip(candidates, values):
        owner = child_owners[path]
        if not owner < lo <= hi <= parent_ends[owner]:
            raise RuntimeError("frontier child interval is outside its parent")
        if b >= threshold:
            children[owner].append({"pre": lo, "path": path, "label": path.rsplit("/", 1)[-1], "b": b, "o": o,
                                    "matches": n, "leaf": lo == hi})
    bodies = {}
    for lo, hi, path in bounds:
        n, b, o = totals[lo]
        kept = sorted(children[lo], key=lambda child: (-child["b"], child["path"]))
        other = {"b": b - sum(c["b"] for c in kept), "o": o - sum(c["o"] for c in kept), "matches": n - sum(c["matches"] for c in kept)}
        if lo == hi:
            other = {"b": 0, "o": 0, "matches": 0}
        if any(value < 0 for value in other.values()):
            raise RuntimeError("frontier children exceed their exact parent totals")
        bodies[path] = {"schema": "coarse-v1", "date": index.date, "name": index.name, "mode": index.predicate_mode,
                        "path": path, "present": lo in present, "exact": True, "incremental": False,
                        "scope": "leaf basename byte/count frontier", "threshold_bytes": threshold, "child_budget": budget,
                        "tree": {"pre": lo, "path": path, "label": path.rsplit("/", 1)[-1] or "all buckets", "b": b, "o": o,
                                 "matches": n, "leaf": lo == hi, "children": kept, "other": other},
                        "response_s": round(monotonic() - start, 4)}
    return [bodies[path] for path in paths]


def paired_batch(
    ch: Ch,
    before: NameIndex,
    after: NameIndex,
    paths: list[str],
    budget: int,
    *,
    threshold: int | None = None,
) -> list[dict]:
    """Discover both frontiers, then batch exact cross-side union lookups."""
    if (before.target, before.name, before.root, before.predicate_mode) != (after.target, after.name, after.root, after.predicate_mode):
        raise CoarseRequest("paired frontier requires one frozen dictionary and predicate")
    if not 1 <= budget <= 128:
        raise CoarseRequest("diff budget must be from 1 to 128 (at most twice that many children)")
    start = monotonic()
    sides = [batch(ch, index, paths, budget, threshold=threshold, allow_absent=True) for index in (before, after)]
    if threshold is None:
        threshold, _ = byte_ranks(max(side[0]["tree"]["b"] for side in sides), budget)
    owners = {c["path"]: i for side in sides for i, body in enumerate(side) for c in body["tree"]["children"]}
    known = [{c["path"]: c for body in side for c in body["tree"]["children"]} for side in sides]
    missing = [path for path in owners if any(path not in values for values in known)]
    candidates = ch.json(f"SELECT pre, post, path FROM {before.target}.dictionary WHERE (depth, path) IN ({keys(missing)}) ORDER BY pre") if missing else []
    if len(candidates) != len(missing):
        raise RuntimeError("paired frontier child dictionary intervals are inconsistent")
    templates = {**known[0], **known[1]}
    for index, values in zip((before, after), known):
        needed = [(lo, hi, path) for lo, hi, path in candidates if path not in values]
        totals = aggregate(ch, index.source, [(lo, hi + 1) for lo, hi, _ in needed], index.prefix, index.block_rows)
        for (lo, hi, path), (n, b, o) in zip(needed, totals):
            if (templates[path]["pre"], templates[path]["leaf"]) != (lo, lo == hi):
                raise RuntimeError("paired frontier metadata differs across the frozen dictionary")
            values[path] = {"pre": lo, "path": path, "label": templates[path]["label"], "leaf": lo == hi, "matches": n, "b": b, "o": o}
    kept = [path for path in owners if max(values[path]["b"] for values in known) >= threshold]
    kept.sort(key=lambda path: (-max(values[path]["b"] for values in known), path))
    if len(kept) > 2 * budget:
        raise RuntimeError("paired frontier exceeded its heavy-child budget")
    grouped = {i: [] for i in range(len(paths))}
    for path in kept:
        grouped[owners[path]].append(path)
    result = []
    for parent, path in enumerate(paths):
        pair = [side[parent] for side in sides]
        if not any(body["present"] for body in pair):
            raise CoarseRequest("path not present at either selected scan")
        for body, values in zip(pair, known):
            tree = body["tree"]
            tree["children"] = [values[path] for path in grouped[parent]]
            tree["other"] = {key: tree[key] - sum(c[key] for c in tree["children"]) for key in ("b", "o", "matches")}
            if tree["leaf"]:
                tree["other"] = {"b": 0, "o": 0, "matches": 0}
            if any(value < 0 for value in tree["other"].values()):
                raise RuntimeError("paired frontier children exceed their parent totals")
            body["threshold_bytes"] = threshold
        result.append({"schema": "coarse-diff-v1", "exact": True, "incremental": False, "path": path, "name": after.name,
                       "before": pair[0], "after": pair[1], "child_budget": budget, "max_children": 2 * budget,
                       "threshold_bytes": threshold, "delta": {key: pair[1]["tree"][key] - pair[0]["tree"][key] for key in ("b", "o", "matches")},
                       "response_s": round(monotonic() - start, 4)})
    return result


def walk(
    ch: Ch,
    index: NameIndex,
    path: str,
    budget: int,
    levels: int,
) -> dict:
    """At most K heavy cells per level; no repeated K-per-branch expansion."""
    if not 1 <= levels <= 4:
        raise CoarseRequest("levels must be from 1 to 4")
    start = monotonic()
    root = batch(ch, index, [path], budget)[0]
    root["schema"] = "coarse-tree-v1"
    frontier = [c for c in root["tree"]["children"] if not c["leaf"]]
    batches, nodes = 1, 1 + len(root["tree"]["children"])
    for _ in range(1, levels):
        if not frontier:
            break
        bodies = batch(ch, index, [c["path"] for c in frontier], budget, threshold=root["threshold_bytes"])
        next_frontier = []
        for node, body in zip(frontier, bodies):
            tree = body["tree"]
            if tuple(node[k] for k in ("pre", "b", "o", "matches")) != tuple(tree[k] for k in ("pre", "b", "o", "matches")):
                raise RuntimeError("refined node totals changed within the frozen index")
            node.update(children=tree["children"], other=tree["other"])
            nodes += len(tree["children"])
            next_frontier.extend(c for c in tree["children"] if not c["leaf"])
        frontier = next_frontier
        batches += 1
    root.update(levels=levels, frontier_batches=batches, tree_nodes=nodes, response_s=round(monotonic() - start, 4))
    return root


def walk_oracle(ch: Ch, index: NameIndex, body: dict) -> int:
    """Validate every expanded partition against complete independent rows."""
    todo, checked = [body["tree"]], 0
    while todo:
        tree = todo.pop()
        if "children" not in tree:
            continue
        oracle(ch, index, {"path": tree["path"], "threshold_bytes": body["threshold_bytes"], "tree": tree})
        checked += 1
        todo.extend(tree["children"])
    return checked


def walk_diff(
    ch: Ch,
    before: NameIndex,
    after: NameIndex,
    path: str,
    budget: int,
    levels: int,
) -> dict:
    if not 1 <= levels <= 4:
        raise CoarseRequest("levels must be from 1 to 4")
    start = monotonic()
    root = paired_batch(ch, before, after, [path], budget)[0]
    root["schema"] = "coarse-tree-diff-v1"
    for side in ("before", "after"):
        root[side].update(schema="coarse-tree-v1", levels=levels)
    trees = [root[side]["tree"] for side in ("before", "after")]
    frontier = [(a, b) for a, b in zip(trees[0]["children"], trees[1]["children"]) if not a["leaf"]]
    batches, nodes = 1, 1 + len(trees[0]["children"])
    for _ in range(1, levels):
        if not frontier:
            break
        bodies = paired_batch(ch, before, after, [a["path"] for a, _ in frontier], budget, threshold=root["threshold_bytes"])
        next_frontier = []
        for pair, body in zip(frontier, bodies):
            for node, side in zip(pair, ("before", "after")):
                tree = body[side]["tree"]
                if tuple(node[k] for k in ("pre", "b", "o", "matches")) != tuple(tree[k] for k in ("pre", "b", "o", "matches")):
                    raise RuntimeError("refined diff totals changed within the frozen index")
                node.update(children=tree["children"], other=tree["other"])
            nodes += len(pair[0]["children"])
            next_frontier.extend((a, b) for a, b in zip(pair[0]["children"], pair[1]["children"]) if not a["leaf"])
        frontier = next_frontier
        batches += 1
    root.update(levels=levels, frontier_batches=batches, tree_nodes=nodes, response_s=round(monotonic() - start, 4))
    return root


def walk_diff_oracle(
    ch: Ch,
    before: NameIndex,
    after: NameIndex,
    body: dict,
) -> int:
    todo, checked = [(body["before"]["tree"], body["after"]["tree"])], 0
    while todo:
        a, b = todo.pop()
        if "children" not in a:
            continue
        diff_oracle(ch, before, after, {"before": {"path": a["path"], "tree": a}, "after": {"path": b["path"], "tree": b},
                                      "threshold_bytes": body["threshold_bytes"]})
        if [c["path"] for c in a["children"]] != [c["path"] for c in b["children"]]:
            raise ValueError("refined diff partitions do not align")
        checked += 1
        todo.extend(zip(a["children"], b["children"]))
    return checked
