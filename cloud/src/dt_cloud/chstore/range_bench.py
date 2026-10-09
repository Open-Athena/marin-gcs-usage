"""Bounded exact-name weighted range aggregation, not a treemap renderer."""

from bisect import bisect_left
from array import array
from itertools import accumulate
from json import loads
from time import monotonic

from .bench import drop_caches
from .client import Ch, lit
from .narrow import identifier


class NonLeafMatches(ValueError):
    """This scalar index cannot account for matching non-leaf subtrees."""

    def __init__(
        self,
        message: str,
        *,
        posting_rows: int,
        nonleaf_rows: int,
    ) -> None:
        super().__init__(message)
        self.posting_rows = posting_rows
        self.nonleaf_rows = nonleaf_rows


class Prefix:
    """One sparse summary per occupied preorder block; integer arithmetic."""

    def __init__(self, rows: list[list[int]]):
        blocks = [row[0] for row in rows]
        if blocks != sorted(set(blocks)):
            raise ValueError("summary blocks must be strictly increasing")
        self.blocks = array("Q", blocks)
        self.totals = [array("Q", accumulate((row[i] for row in rows), initial=0)) for i in (1, 2, 3)]

    def before(self, block: int) -> tuple[int, int, int]:
        offset = bisect_left(self.blocks, block)
        return tuple(column[offset] for column in self.totals)


def cumulative(
    ch: Ch,
    source: str,
    points: list[int],
    prefix: Prefix,
    block_rows: int,
) -> dict[int, tuple[int, int, int]]:
    """Exact totals strictly before each preorder position."""
    if block_rows < 1 or any(x < 0 for x in points):
        raise ValueError("invalid block size or position")
    points = sorted(set(points))
    if not points:
        return {}
    ch.tmp("range_points", " UNION ALL ".join(
        f"SELECT toUInt64({x}) AS x, toUInt64({x // block_rows}) AS block" for x in points
    ))
    residual = {row[0]: tuple(row[1:]) for row in ch.json(f"""
        SELECT p.x, count(), sum(s.b), sum(s.o)
        FROM (SELECT pre, b, o, intDiv(pre, {block_rows}) AS block FROM ({source})
              WHERE intDiv(pre, {block_rows}) IN (SELECT block FROM range_points)) s
        INNER JOIN range_points p ON s.block = p.block
        WHERE s.pre < p.x GROUP BY p.x
    """)}
    return {x: tuple(a + b for a, b in zip(prefix.before(x // block_rows), residual.get(x, (0, 0, 0)))) for x in points}


def aggregate(
    ch: Ch,
    source: str,
    ranges: list[tuple[int, int]],
    prefix: Prefix,
    block_rows: int,
) -> list[tuple[int, int, int]]:
    """Exact (posting count, bytes, objects) for half-open preorder ranges."""
    if any(lo < 0 or hi < lo for lo, hi in ranges):
        raise ValueError("invalid block size or range")
    values = cumulative(ch, source, [x for bounds in ranges for x in bounds], prefix, block_rows)
    return [tuple(b - a for a, b in zip(values[lo], values[hi])) for lo, hi in ranges]


def build_prefix(
    ch: Ch,
    source: str,
    block_rows: int,
) -> Prefix:
    ch.tmp("range_blocks", f"""
        SELECT intDiv(pre, {block_rows}) AS block, count() AS n, sum(b) AS b, sum(o) AS o,
               countIf(post != pre) AS nonleaf FROM ({source}) GROUP BY block
    """)
    rows = ch.json("SELECT block, n, b, o, nonleaf FROM range_blocks ORDER BY block")
    nonleaf_rows = sum(row[4] for row in rows)
    if nonleaf_rows:
        raise NonLeafMatches("exact-name prototype requires only leaf matches", posting_rows=sum(row[1] for row in rows), nonleaf_rows=nonleaf_rows)
    return Prefix([row[:4] for row in rows])


def direct(
    ch: Ch,
    source: str,
    ranges: list[tuple[int, int]],
) -> list[tuple[int, int, int]]:
    """One direct posting scan as an independent scalar-aggregation oracle."""
    ch.tmp("range_bounds", " UNION ALL ".join(
        f"SELECT toUInt64({i}) AS rid, toUInt64({lo}) AS lo, toUInt64({hi}) AS hi"
        for i, (lo, hi) in enumerate(ranges)
    ))
    rows = {row[0]: tuple(row[1:]) for row in ch.json(f"""
        SELECT r.rid, count(), sum(s.b), sum(s.o) FROM ({source}) s CROSS JOIN range_bounds r
        WHERE s.pre >= r.lo AND s.pre < r.hi GROUP BY r.rid
    """)}
    return [rows.get(i, (0, 0, 0)) for i in range(len(ranges))]


def bench(
    url: str,
    target: str,
    date: str,
    name: str,
    *,
    block_rows: int = 4096,
    cold: bool = False,
    threads: int = 8,
) -> dict:
    """Build only session summaries, compare small ranges, then remove them.

    Exact basename and leaf-only, one frozen scan. No full-path substring,
    Boolean, rich metrics, rendering or incremental-publication claim.
    """
    identifier(target)
    if block_rows < 1024:
        raise ValueError("real-data block size must be at least 1024")
    ch = Ch(url, db=target, max_threads=threads, max_memory_usage=8 << 30,
            max_bytes_before_external_group_by=256 << 20, max_execution_time=120,
            timeout_before_checking_execution_speed=0, timeout_overflow_mode="throw")
    try:
        manifest = loads(ch.scalar("SELECT doc FROM history_manifest"))
        tick = manifest["dates"].index(date)
        db = identifier(manifest["dbs"][tick])
        nid = ch.scalar(f"SELECT nid FROM names WHERE l = {lit(name.lower())}")
        if nid is None:
            raise ValueError(f"unknown exact name: {name}")
        source = f"SELECT pre, post, b, o FROM {db}.nodes_by_name WHERE nid = {int(nid)}"
        start = monotonic()
        prefix = build_prefix(ch, source, block_rows)
        build_s = monotonic() - start
        ranges = [(int(lo), int(hi) + 1) for lo, hi in ch.json("SELECT pre, post FROM dictionary WHERE depth <= 1 ORDER BY pre")]
        ranges.extend([(0, 0), (1, block_rows - 1), (block_rows - 1, block_rows + 1),
                       (block_rows + 1, 2 * block_rows + 3)])
        if cold:
            drop_caches(url)
        start = monotonic()
        actual = aggregate(ch, source, ranges, prefix, block_rows)
        aggregate_s = monotonic() - start
        if cold:
            drop_caches(url)
        start = monotonic()
        expected = direct(ch, source, ranges)
        direct_s = monotonic() - start
        if actual != expected:
            raise ValueError("weighted range totals differ from direct posting scan")
        return {
            "target": target, "date": date, "exact_name": name.lower(), "scope": "leaf-only scalar range aggregation",
            "cold": cold, "threads": threads, "block_rows": block_rows, "posting_rows": prefix.totals[0][-1],
            "summary_rows": len(prefix.blocks), "summary_numeric_bytes": len(prefix.blocks) * 32,
            "ranges": len(ranges), "build_s": round(build_s, 4), "aggregate_s": round(aggregate_s, 4),
            "direct_s": round(direct_s, 4), "exact": True, "renders_tree": False, "incremental": False,
            "prefix_resident": True,
        }
    finally:
        ch.close()
