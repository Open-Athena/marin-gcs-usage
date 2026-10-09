"""Bounded subtree-first leaf predicates; research only, no serving fallback."""

from json import loads
from time import monotonic
from uuid import uuid4

from .bench import drop_caches
from .client import Ch, lit
from .coarse import CoarseRequest, NameIndex, oracle
from .narrow import identifier
from .range_bench import build_prefix
from .serve import depth_of


def build(
    ch: Ch,
    target: str,
    date: str,
    path: str,
    pattern: str,
    *,
    max_nodes: int = 1_000_000,
) -> NameIndex:
    """Scan every node in an accepted union interval, never a partial answer.

    Union interval width is a conservative scan budget, including absent
    nodes. Filtering basename strings directly avoids global vocabulary
    enumeration. A matching nonleaf still refuses this leaf-only contract.
    The returned index owns session tables and must not enter a process LRU.
    """
    identifier(target)
    if not pattern or "/" in pattern or len(pattern) > 512 or not 1 <= max_nodes <= 1_000_000:
        raise CoarseRequest("scoped pattern requires one basename literal and a 1..1M-node budget")
    start = monotonic()
    manifest = loads(ch.scalar(f"SELECT doc FROM {target}.history_manifest"))
    if date not in manifest["dates"]:
        raise CoarseRequest("scan outside the frozen index")
    root = manifest["prefix"]
    if root and path != root and not path.startswith(root + "/"):
        raise CoarseRequest("path outside the frozen index")
    bounds = ch.json(f"SELECT pre,post FROM {target}.dictionary WHERE depth={depth_of(path)} AND path={lit(path)}")
    if len(bounds) != 1:
        raise CoarseRequest("path not in the frozen dictionary")
    lo, hi = bounds[0]
    if hi - lo + 1 > max_nodes:
        raise CoarseRequest(f"scoped union interval exceeds its {max_nodes:,}-node work budget")
    db = identifier(manifest["dbs"][manifest["dates"].index(date)])
    pattern = pattern.lower()
    source = f"""SELECT pre,post,b,o FROM {db}.nodes WHERE pre >= {lo} AND pre <= {hi}
        AND position(lowerUTF8(arrayElement(splitByChar('/',path),-1)),{lit(pattern)}) > 0"""
    table = "scoped_pattern_" + uuid4().hex
    ch.tmp(table, source)
    selected = f"SELECT pre,post,b,o FROM {table}"
    prefix = build_prefix(ch, selected, 4096)
    return NameIndex(target, date, db, 0, pattern, path, prefix, 4096, monotonic() - start,
                     selected, "contains", 0, source, {"union_nodes": hi - lo + 1})


def bench(
    url: str,
    target: str,
    date: str,
    path: str,
    pattern: str,
    *,
    cold: bool = False,
) -> dict:
    """Complete scoped partition oracle; not global broad-query acceptance."""
    ch = Ch(url, db=target, max_threads=8, max_memory_usage=8 << 30,
            max_execution_time=30, timeout_before_checking_execution_speed=0,
            timeout_overflow_mode="throw")
    try:
        if cold:
            drop_caches(url)
        index = build(ch, target, date, path, pattern)
        if cold:
            drop_caches(url)
        start = monotonic()
        body = index.view(ch, path)
        view_s = monotonic() - start
        if cold:
            drop_caches(url)
        start = monotonic()
        oracle(ch, index, body)
        oracle_s = monotonic() - start
        return {"scope": "complete bounded subtree, leaf basename contains; no global/incremental claim",
                "path": path, "date": date, "pattern": pattern.lower(), "cold": cold, "threads": 8,
                "union_nodes": index.build_stages["union_nodes"], "global_vocabulary_enumerated": False,
                "build_s": round(index.build_s, 4), "view_s": round(view_s, 4), "oracle_s": round(oracle_s, 4),
                "summary_rows": len(index.prefix.blocks), "tree": body["tree"], "exact": True}
    finally:
        ch.close()
