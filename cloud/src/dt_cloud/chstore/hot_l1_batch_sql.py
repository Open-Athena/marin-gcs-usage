"""Native multi-literal first-hit frontiers over one frozen scalar snapshot.

The immutable source is audited for UTF-8 before Hyperscan is invoked. Native
unsupported-pattern/resource errors propagate; no fallback or partial answer
is accepted. Caller-owned query memory/time/spill limits remain in effect.
"""

from json import loads
from time import monotonic

from .client import Ch, lit
from .coarse import CoarseRequest
from .hot_l1 import _bounds_sql, _buckets, _complete
from .narrow import identifier

SCOPE = "case-insensitive substring within names; directory hits cover descendants; bytes/objects only"
PARENT = "if(position(path, '/') = 0, '', substring(path, 1, length(path) - position(reverse(path), '/')))"
NAME = "if(position(path, '/') = 0, path, substring(path, length(path) - position(reverse(path), '/') + 2))"


def _number(value: object) -> int:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    raise RuntimeError("native batch returned an invalid unsigned scalar")


def _query(
    db: str,
    patterns: tuple[str, ...],
    buckets: list[list],
) -> str:
    literals = "[" + ",".join(map(lit, patterns)) + "]"
    return f"""WITH arrayMap(p -> regexpQuoteMeta(p), {literals}) AS regexes
        SELECT s.q, c.pre, sum(toUInt128(s.b)), sum(toUInt128(s.o)) FROM (
            SELECT toUInt64(pre) AS pre, b, o, toUInt8(0) AS shard,
                arrayJoin(arrayExcept(name_hits, parent_hits)) AS q
            FROM (
                SELECT pre, b, o,
                    multiMatchAllIndices(lowerUTF8({NAME}), regexes) AS name_hits,
                    multiMatchAllIndices(lowerUTF8({PARENT}), regexes) AS parent_hits
                FROM {db}.nodes
            )
        ) s ASOF INNER JOIN ({_bounds_sql(buckets)}) c ON s.shard = c.shard AND s.pre >= c.pre
        WHERE s.pre <= c.post GROUP BY s.q, c.pre"""


def build(
    ch: Ch,
    target: str,
    date: str,
    patterns: tuple[str, ...],
    *,
    max_sql_bytes: int = 64 << 20,
) -> dict:
    """All registered L1 predicates; native IDs are one-based and unordered.

    Basename hits not present in the parent's full path identify disjoint
    first-hit frontiers. Recursive scalar sums include objects at directories
    without expanding every covered object. This is offline construction.
    """
    identifier(target)
    if type(max_sql_bytes) is not int or max_sql_bytes <= 0:
        raise CoarseRequest("native batch SQL byte budget must be positive")
    if not patterns or any(not isinstance(pattern, str) or not pattern or "/" in pattern or "\x00" in pattern or len(pattern) > 512 for pattern in patterns):
        raise CoarseRequest("native batch requires nonempty slash-free, NUL-free literals of at most 512 characters")
    patterns = tuple(pattern.lower() for pattern in patterns)
    if len(set(patterns)) != len(patterns):
        raise CoarseRequest("native batch normalized literals must be unique")
    start = monotonic()
    manifest = loads(ch.scalar(f"SELECT doc FROM {target}.history_manifest"))
    if manifest["prefix"] != "":
        raise CoarseRequest("native batch requires a global frozen target")
    if date not in manifest["dates"]:
        raise CoarseRequest("scan outside the frozen index")
    db = identifier(manifest["dbs"][manifest["dates"].index(date)])
    buckets = _buckets(ch, target)
    query = _query(db, patterns, buckets)
    sql_bytes = len((query + " FORMAT JSONCompactEachRow").encode("utf-8"))
    if sql_bytes > max_sql_bytes:
        raise CoarseRequest("native batch SQL exceeds its explicit byte budget")
    stage = monotonic()
    audit = ch.json(f"""SELECT count(), countIf(isNull(path) OR NOT isValidUTF8(path)), sum(length(path)),
        countIf(isNull(pre) OR isNull(post) OR isNull(b) OR isNull(o) OR pre < 0 OR post < pre OR b < 0 OR o < 0)
        FROM {db}.nodes""")
    if len(audit) != 1 or len(audit[0]) != 4:
        raise RuntimeError("native batch source audit returned an invalid shape")
    rows, invalid_utf8, path_bytes, invalid_scalars = map(_number, audit[0])
    if invalid_utf8:
        raise CoarseRequest("native batch source contains null or invalid UTF-8 paths")
    if invalid_scalars:
        raise CoarseRequest("native batch source contains invalid or null scalar rows")
    if not rows:
        raise CoarseRequest("native batch source has no complete global root")
    audit_s = monotonic() - stage
    stage = monotonic()
    settings = {"join_algorithm": "hash", "join_use_nulls": 0, "max_query_size": max_sql_bytes,
                "max_ast_elements": max(50_000, len(patterns) * 8 + 1000),
                "max_expanded_ast_elements": max(500_000, len(patterns) * 32 + 10_000)}
    totals = [[] for _ in patterns]
    for row in ch.json(query, settings=settings):
        if len(row) != 4:
            raise RuntimeError("native batch aggregation returned an invalid shape")
        q, pre, b, o = map(_number, row)
        if not 1 <= q <= len(patterns):
            raise RuntimeError("native batch returned an unregistered predicate ID")
        totals[q - 1].append([pre, b, o])
    aggregate_s = monotonic() - stage
    results = []
    for q, (pattern, values) in enumerate(zip(patterns, totals, strict=True), start=1):
        completed = _complete(buckets, values)
        results.append({"predicate_id": q, "pattern": pattern,
                        "root": {"b": sum(row["b"] for row in completed), "o": sum(row["o"] for row in completed)}, "buckets": completed})
    return {"schema": "hot-l1-batch-sql-v1", "target": target, "snapshot_db": db, "date": date,
            "exact": True, "incremental": False, "levels": 1, "scope": SCOPE, "results": results,
            "compiled_patterns": len(patterns), "sql_bytes": sql_bytes,
            "source_validation": {"rows": rows, "invalid_utf8_paths": invalid_utf8, "path_bytes": path_bytes, "invalid_scalar_rows": invalid_scalars},
            "stages": {"source_validation_s": round(audit_s, 6), "aggregate_s": round(aggregate_s, 6)}, "build_s": round(monotonic() - start, 6)}
