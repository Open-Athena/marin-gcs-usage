"""One arbitrary-depth tree builder for the site's layer-3 JSON.

Both the GCS (`marin/gcs_usage/viz.py`) and CoreWeave (`job/cw-webdata.py`)
paths produce the same `{n, b, o, c?, …}` `TreeNode`; they differed only in
that CW linked rolled-up dir rows parent→child (any depth) while GCS flattened
to four fixed path components. That flattening was a cardinality hack for
aggregating a 595M-row object listing, never a design choice — see
`specs/tree-builder-unification.md`. This module is CW's linker, generalized to
carry GCS's per-node fields and to mark folded nodes so the client can expand
them.

Input: **rolled-up dir rows**, one per directory, whose `b`/`o` (and any
`extra` fields) are already **descendant-inclusive**. A node's children are the
dir rows one path segment deeper; bytes the kept children don't account for
(direct files + sub-floor dirs) become one `(other)` node carrying a fold
count. The floor is a fraction of the total, so it scales with the fleet
instead of a constant that a 10× growth silently turns into "mostly (other)".
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

#: Root sentinel — the path of the top dir row (CW uses "."; either works).
ROOT_KEYS = ("", ".")


@dataclass
class DirRow:
    """One rolled-up directory. ``extra`` holds already-summarized, additive
    per-node fields (class bytes ``cb``, weighted-mtime ``wts``/``wb``, and
    attribution ``tm``/``sh``/``us``) that :func:`build_tree` carries onto the
    node and re-combines for ``(other)`` fold nodes."""

    path: str
    b: int
    o: int
    extra: dict = field(default_factory=dict)


def _seg(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else "."


def _combine(nodes: list[dict]) -> dict:
    """Merge the additive optional fields of several nodes into one — the math
    the ``(other)`` fold and any re-summary needs. Bytes/objects are summed by
    the caller; this handles the maps and the byte-weighted mean date."""
    out: dict = {}
    # Byte-weighted mean created day (`d`): re-weight children's means by bytes.
    dated = [(n["d"], n["b"]) for n in nodes if "d" in n and n["b"]]
    if dated:
        out["d"] = int(sum(d * b for d, b in dated) / sum(b for _, b in dated))
    for key in ("cb", "tm", "sh"):
        acc: dict[str, int] = defaultdict(int)
        for n in nodes:
            for k, v in (n.get(key) or {}).items():
                acc[k] += v
        if acc:
            out[key] = dict(sorted(acc.items(), key=lambda kv: -kv[1]))
    us: dict[str, int] = defaultdict(int)
    for n in nodes:
        for u, v in n.get("us", []):
            us[u] += v
    if us:
        out["us"] = [[u, v] for u, v in sorted(us.items(), key=lambda kv: -kv[1])]
    return out


def build_tree(rows: list[DirRow], total_bytes: int, min_frac: float) -> dict:
    """Link ``rows`` into a nested tree; return the root node (the row whose
    path is a :data:`ROOT_KEYS` sentinel).

    ``min_frac`` × ``total_bytes`` is the fold floor: dirs below it are dropped
    and their bytes roll into a per-parent ``(other)`` node marked ``f`` (the
    count folded), which the client renders as an expandable placeholder rather
    than a dead leaf. A lone sub-floor child is kept as-is (an ``(other)`` of
    one is just noise).
    """
    floor = int(total_bytes * min_frac)
    nodes: dict[str, dict] = {}
    for r in rows:
        n: dict = {"n": _seg(r.path) if r.path not in ROOT_KEYS else r.path, "b": int(r.b), "o": int(r.o), **r.extra}
        nodes[r.path] = n

    kids: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    root_path = None
    for path, n in nodes.items():
        if path in ROOT_KEYS:
            root_path = path
            continue
        kids[_parent(path)].append((path, n))

    for parent_path, children in kids.items():
        parent = nodes.get(parent_path)
        if not parent:
            continue
        kept = sorted((n for _, n in children if n["b"] >= floor), key=lambda n: -n["b"])
        folded = [n for _, n in children if n["b"] < floor]
        parent["c"] = kept
        # Bytes/objects not in kept children = direct files + folded dirs. Fold
        # into one marked (other) when that remainder is itself above the floor;
        # otherwise it's dust and drops (its bytes still count in the parent).
        rest_b = parent["b"] - sum(n["b"] for n in kept)
        rest_o = parent["o"] - sum(n["o"] for n in kept)
        if rest_b > floor:
            other = {"n": "(other)", "b": int(rest_b), "o": int(max(rest_o, 0))}
            if folded:
                other["f"] = len(folded)
                other.update(_combine(folded))
            parent["c"].append(other)
        if not parent["c"]:
            del parent["c"]

    if root_path is None:
        raise ValueError("no root row (path in {'', '.'}) among dir rows")
    return nodes[root_path]
