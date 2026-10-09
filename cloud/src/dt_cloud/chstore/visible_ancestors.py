"""Bounded visible-ancestor interval maps for frozen preorder experiments."""

from collections.abc import Iterator, Sequence

from .client import Ch

MAX_INTERVALS = 25_000
MAX_MEMBERSHIPS = 500_000


def regions(intervals: Sequence[tuple[int, int]]) -> Iterator[tuple[int, tuple[int, ...]]]:
    """Emit unique ASOF breakpoints, including gaps and inclusive endpoints.

    Valid subtree intervals form a nested/disjoint forest. Never approximate a
    crossing interval or conflate the exclusive end event with its last member.
    """
    events = {0: []}
    stack = []
    previous = -1
    for pre, post in sorted(intervals):
        if not 0 <= pre <= post <= 0xFFFFFFFF or pre == previous:
            raise ValueError("invalid or duplicate visible preorder interval")
        while stack and stack[-1] < pre:
            stack.pop()
        if stack and post > stack[-1]:
            raise ValueError("crossing visible preorder intervals")
        stack.append(post)
        previous = pre
        events.setdefault(pre, []).append((True, pre))
        events.setdefault(post + 1, []).append((False, pre))
    active = set()
    for position, changes in sorted(events.items()):
        for add, pre in changes:
            if add:
                active.add(pre)
            else:
                active.remove(pre)
        yield position, tuple(sorted(active))


def source(
    ch: Ch,
    db: str,
    sfx: str,
    threshold: float,
) -> str | None:
    """Small visible-only RHS, or explicit legacy fallback for oversized maps.

    The scalar ancestor pass already determines the exact visible set. Query
    each root's *parent* position to preserve ancestor/self distinctions, and
    use an equality shard plus the ASOF position required by a hash ASOF join.
    """
    intervals = ch.json(f"""SELECT pre, post FROM {db}.nodes
        WHERE pre IN (SELECT pre FROM anc0_{sfx} WHERE b >= {threshold!r}) LIMIT {MAX_INTERVALS + 1}""")
    if len(intervals) > MAX_INTERVALS:
        return None
    states, memberships = [], 0
    for position, ancestors in regions(intervals):
        memberships += len(ancestors)
        if memberships > MAX_MEMBERSHIPS:
            return None
        states.append(f"(0,{position},[{','.join(map(str, ancestors))}])")
    table = f"visible_ranges_{sfx}"
    ch.tmp(table, "SELECT * FROM values('shard UInt8, start Int64, ancestors Array(UInt32)', " + ",".join(states) + ") ORDER BY start")
    return f"""(SELECT r.*, arrayJoin(v.ancestors) AS ancestor
        FROM (SELECT *, toUInt8(0) AS shard FROM rn_{sfx}) r
        ASOF INNER JOIN {table} v ON r.shard = v.shard AND r.parent_pre >= v.start)"""
