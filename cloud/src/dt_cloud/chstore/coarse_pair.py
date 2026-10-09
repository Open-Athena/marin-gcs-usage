"""Bounded leaf diff across independent snapshot dictionaries.

Discover heavy children with each snapshot's own byte ranks, align only those
child paths, and look up counterpart intervals in that side's dictionary. No
union dictionary or enumeration of all matching paths is required. This is a
research contract: numeric identities are side-local and absent geometry is
None, not an ID borrowed from the other snapshot.
"""

from time import monotonic

from .client import Ch, lit
from .coarse import CoarseRequest, NameIndex, byte_ranks
from .range_bench import aggregate
from .serve import depth_of


COUNTS = ("b", "o", "matches")


def _view(
    ch: Ch,
    index: NameIndex,
    path: str,
    budget: int,
) -> dict:
    if index.root and path != index.root and not path.startswith(index.root + "/"):
        raise CoarseRequest("path outside the snapshot index")
    geometry = ch.json(f"SELECT pre, post FROM {index.target}.dictionary WHERE depth = {depth_of(path)} AND path = {lit(path)}")
    if len(geometry) > 1:
        raise RuntimeError("snapshot parent dictionary identity is duplicated")
    if geometry:
        # Different snapshots may reuse a session table name while assigning
        # different name IDs. Rebind before every source-consuming phase.
        index.prepare(ch)
        return index.view(ch, path, budget, allow_absent=True)
    return {
        "schema": "coarse-pair-side-v1", "date": index.date, "name": index.name,
        "mode": index.predicate_mode, "path": path, "exact": True,
        "present": False, "incremental": False,
        "tree": {
            "pre": None, "path": path, "label": path.rsplit("/", 1)[-1] or "all buckets",
            "b": 0, "o": 0, "matches": 0, "leaf": None, "children": [],
            "other": dict.fromkeys(COUNTS, 0),
        },
    }


def _counterparts(
    ch: Ch,
    index: NameIndex,
    side: dict,
    paths: list[str],
) -> dict[str, dict]:
    if not paths or not side["present"]:
        return {}
    tree = side["tree"]
    rows = ch.json(f"""SELECT pre, post, path FROM {index.target}.dictionary
        WHERE depth = {depth_of(side['path']) + 1} AND path IN ({','.join(map(lit, paths))}) ORDER BY pre""")
    if len(rows) != len({row[2] for row in rows}) or len(rows) != len({row[0] for row in rows}):
        raise RuntimeError("snapshot child dictionary identity is duplicated")
    parent = ch.json(f"SELECT post FROM {index.target}.dictionary WHERE pre = {tree['pre']} AND path = {lit(side['path'])}")
    if len(parent) != 1 or any(not tree["pre"] < lo <= hi <= parent[0][0] for lo, hi, _ in rows):
        raise RuntimeError("snapshot child dictionary intervals are inconsistent")
    index.prepare(ch)
    totals = aggregate(ch, index.source, [(lo, hi + 1) for lo, hi, _ in rows], index.prefix, index.block_rows)
    present = {row[0] for row in ch.json(f"SELECT pre FROM {index.db}.nodes WHERE pre IN ({','.join(str(row[0]) for row in rows)})")} if rows else set()
    if len(present) > len(rows):
        raise RuntimeError("snapshot child presence exceeds its dictionary")
    result = {}
    for (lo, hi, path), (n, b, o) in zip(rows, totals):
        if lo not in present and (n, b, o) != (0, 0, 0):
            raise RuntimeError("absent snapshot child has matching descendants")
        result[path] = {"pre": lo, "path": path, "label": path.rsplit("/", 1)[-1],
                        "b": b, "o": o, "matches": n, "leaf": lo == hi, "present": lo in present}
    return result


def diff(
    ch: Ch,
    before: NameIndex,
    after: NameIndex,
    path: str,
    budget: int = 64,
) -> dict:
    """Exact one-level partitions with at most 2K path-aligned heavy children.

    T = ceil(max(parent bytes) / K), at least one byte. Each side's own
    threshold is <= T, so its existing quantile walk discovers every child
    that can survive the shared threshold. Only these <=2K paths are looked
    up on the opposite side. Zero-byte counts remain in exact remainders.
    """
    if before.name != after.name or before.root != after.root or before.predicate_mode != after.predicate_mode:
        raise ValueError("independent coarse diff requires one root scope and predicate")
    if not 1 <= budget <= 128:
        raise CoarseRequest("diff budget must be from 1 to 128 (at most twice that many children)")
    start = monotonic()
    indexes = before, after
    sides = [_view(ch, index, path, budget) for index in indexes]
    if not any(side["present"] for side in sides):
        raise CoarseRequest("path not present at either selected scan")
    threshold, _ = byte_ranks(max(side["tree"]["b"] for side in sides), budget)
    paths = sorted({child["path"] for side in sides for child in side["tree"]["children"]})
    if len(paths) > 2 * budget:
        raise RuntimeError("snapshot quantile candidates exceed the paired work budget")
    values = [_counterparts(ch, index, side, paths) for index, side in zip(indexes, sides)]
    kept = [path for path in paths if max(value.get(path, {}).get("b", 0) for value in values) >= threshold]
    kept.sort(key=lambda path: (-max(value.get(path, {}).get("b", 0) for value in values), path))
    for index, side, children in zip(indexes, sides, values):
        tree = side["tree"]
        tree["children"] = [children.get(path, {
            "pre": None, "path": path, "label": path.rsplit("/", 1)[-1],
            "b": 0, "o": 0, "matches": 0, "leaf": None, "present": False,
        }) for path in kept]
        tree["other"] = {key: tree[key] - sum(child[key] for child in tree["children"]) for key in COUNTS}
        if tree["leaf"]:
            tree["other"] = dict.fromkeys(COUNTS, 0)
        if any(value < 0 for value in tree["other"].values()):
            raise RuntimeError("paired children exceed exact snapshot parent totals")
        side.update(schema="coarse-pair-side-v1", target=index.target, threshold_bytes=threshold)
    return {
        "schema": "coarse-pair-v1", "exact": True, "incremental": False,
        "identity": "snapshot-local; align by path", "path": path, "name": after.name,
        "mode": after.predicate_mode, "before": sides[0], "after": sides[1],
        "child_budget": budget, "max_children": 2 * budget, "threshold_bytes": threshold,
        "delta": {key: sides[1]["tree"][key] - sides[0]["tree"][key] for key in COUNTS},
        "response_s": round(monotonic() - start, 4),
    }
