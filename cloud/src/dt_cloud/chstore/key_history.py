"""Bounded additive lineage history, with complete prefix-scoped oracles."""

from json import loads
from time import monotonic

from .client import Ch, lit
from .narrow import identifier
from .order_keys import bounds
from .serve import depth_of


def build_deltas(
    ch: Ch,
    keys: str,
    states: str,
    ticks: int,
    *,
    prefix: str = "key_history",
) -> str:
    """Keys `(id,key)`, own-leaf states `(id,tick,b,o)`; absent means zero."""
    for name in (keys, states, prefix):
        identifier(name)
    grid, output = f"{prefix}_grid", f"{prefix}_deltas"
    if {keys, states} & {grid, output}:
        raise ValueError("history staging prefix overlaps an input table")
    count = int(ch.scalar(f"SELECT count() FROM {keys}"))
    if not 1 <= ticks <= 32 or not 1 <= count or count * ticks > 1_000_000:
        raise ValueError("history staging exceeds its 1M-state grid budget")
    ids, lineages = ch.json(f"SELECT uniqExact(id),uniqExact(key) FROM {keys}")[0]
    if count != ids or count != lineages:
        raise ValueError("history identities and lineage keys must be unique")
    if int(ch.scalar(f"""SELECT count() FROM {keys} WHERE id < 0 OR id >= {1 << 63}
        OR empty(key) OR length(key) > 256 OR length(arrayDistinct(key)) != length(key)
        OR key[-1] != id OR arrayExists(x -> x < 0 OR x >= {1 << 63},key)""")):
        raise ValueError("history lineage keys violate the identity/depth domain")
    rows = int(ch.scalar(f"SELECT count() FROM {states}"))
    if rows > count * ticks:
        raise ValueError("history snapshot states have duplicate keys or invalid identities/ticks/weights")
    unique, invalid = ch.json(f"""SELECT uniqExact(tuple(id,tick)),
        countIf(tick < 0 OR tick >= {ticks} OR b < 0 OR o < 0 OR b > {(1 << 64) - 1}
            OR o > {(1 << 64) - 1} OR id NOT IN (SELECT id FROM {keys})) FROM {states}""")[0]
    if rows != unique or invalid:
        raise ValueError("history snapshot states have duplicate keys or invalid identities/ticks/weights")
    ch.tmp(grid, f"""SELECT k.id id,k.key key,t.tick tick,
        coalesce(s.b,0) b,coalesce(s.o,0) o,coalesce(s.present,0) n
        FROM {keys} k CROSS JOIN (SELECT toUInt32(number) tick FROM numbers({ticks})) t
        LEFT JOIN (SELECT *,toUInt8(1) present FROM {states}) s ON k.id=s.id AND t.tick=s.tick""")
    ch.tmp(output, f"""SELECT key,tick,db,do,dn FROM (
        SELECT key,tick,
            toInt128(b)-lagInFrame(toInt128(b),1,toInt128(0)) OVER w db,
            toInt128(o)-lagInFrame(toInt128(o),1,toInt128(0)) OVER w do,
            toInt128(n)-lagInFrame(toInt128(n),1,toInt128(0)) OVER w dn
        FROM {grid} WINDOW w AS (PARTITION BY id ORDER BY tick ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
    ) WHERE db != 0 OR do != 0 OR dn != 0""", disk=True, order_by=("key", "tick"))
    return output


def posting_bench(
    url: str,
    target: str,
    date0: str,
    date: str,
    name: str,
    path: str,
    *,
    cold: bool = False,
) -> dict:
    """Complete exact-basename postings in one bounded subtree, not a TM."""
    from .bench import drop_caches

    identifier(target)
    ch = Ch(url, max_threads=8, max_memory_usage=8 << 30, max_execution_time=120,
            max_bytes_before_external_sort=256 << 20, timeout_before_checking_execution_speed=0,
            timeout_overflow_mode="throw")
    try:
        start = monotonic()
        manifest = loads(ch.scalar(f"SELECT doc FROM {target}.history_manifest"))
        if not date0 < date or any(d not in manifest["dates"] for d in (date0, date)):
            raise ValueError("posting history requires two increasing frozen scan dates")
        dbs = [identifier(manifest["dbs"][manifest["dates"].index(d)]) for d in (date0, date)]
        selected = ch.json(f"SELECT pre,post FROM {target}.dictionary WHERE depth={depth_of(path)} AND path={lit(path)}")
        if len(selected) != 1:
            raise ValueError("posting history scope is outside the dictionary")
        lo, hi = selected[0]
        nid = ch.scalar(f"SELECT nid FROM {target}.names WHERE l={lit(name.lower())}")
        if nid is None:
            raise ValueError("posting history exact basename is unknown")
        sources = [f"SELECT pre,post,b,o FROM {db}.nodes_by_name WHERE nid={int(nid)} AND pre >= {lo} AND pre <= {hi}" for db in dbs]
        counts = []
        for source in sources:
            count, nonleaf = ch.json(f"SELECT count(),countIf(post != pre) FROM ({source})")[0]
            if nonleaf or count > 300_000:
                raise ValueError("posting history requires <=300K complete structural-leaf matches per date")
            counts.append(count)
        ch.tmp("posting_ids", "SELECT DISTINCT pre id FROM (" + " UNION ALL ".join(sources) + ")")
        union = int(ch.scalar("SELECT count() FROM posting_ids"))
        if not 1 <= union <= 300_000:
            raise ValueError("posting history requires 1..300K complete union leaves")
        ch.tmp("posting_parents", f"SELECT pre id,parent_pre FROM {target}.numeric_parents WHERE pre IN (SELECT id FROM posting_ids)")
        if int(ch.scalar("SELECT count() FROM posting_parents")) != union:
            raise ValueError("posting history lost identity parent bindings")
        ch.tmp("posting_hierarchy", f"SELECT pre,ancestors,toUInt8(1) present FROM {target}.hierarchy WHERE pre IN (SELECT parent_pre FROM posting_parents)")
        ch.tmp("posting_keys", """SELECT p.id id,p.parent_pre parent_pre,
            if(p.parent_pre < 0,[toUInt64(p.id)],arrayConcat(arrayMap(x -> toUInt64(x),h.ancestors),[toUInt64(p.id)])) key,
            p.parent_pre < 0 OR h.present=1 resolved
            FROM posting_parents p LEFT JOIN posting_hierarchy h ON p.parent_pre=h.pre""")
        if int(ch.scalar("SELECT count() FROM posting_keys WHERE NOT resolved")) or int(ch.scalar("SELECT count() FROM posting_keys")) != union:
            raise ValueError("posting history has unresolved or duplicate lineage")
        ch.tmp("posting_states", " UNION ALL ".join(f"SELECT pre id,toUInt32({tick}) tick,b,o FROM ({source})" for tick, source in enumerate(sources)))
        prepare_s = round(monotonic() - start, 4)
        start = monotonic()
        output = build_deltas(ch, "posting_keys", "posting_states", 2)
        build_s = round(monotonic() - start, 4)
        stat = ch.json(f"SELECT total_rows,total_bytes FROM system.tables WHERE is_temporary=1 AND name={lit(output)}")[0]
        # Global lineage [0] isn't assumed: derive every tested prefix from a
        # selected leaf's verified ancestor sequence.
        roots = ch.json("SELECT parent_pre,any(arrayPopBack(key)) FROM posting_keys WHERE parent_pre >= 0 GROUP BY parent_pre ORDER BY count() DESC,parent_pre LIMIT 3")
        views = []
        for parent, key in [(None, None), *roots]:
            where, snapshot_where = "1", "1"
            if parent is not None:
                points = ch.json(f"SELECT pre,post FROM {target}.dictionary WHERE pre={parent}")
                if len(points) != 1 or not key or key[-1] != parent:
                    raise ValueError("posting history subtree bounds are inconsistent")
                a, z = points[0]
                lower, upper = bounds(tuple(key))
                where = f"key >= CAST({list(lower)} AS Array(UInt64)) AND key < CAST({list(upper)} AS Array(UInt64))"
                snapshot_where = f"pre >= {a} AND pre <= {z}"
            for tick, source in enumerate(sources):
                answers, seconds = [], {}
                for label, sql in (
                    ("delta", f"SELECT sum(dn),sum(db),sum(do) FROM {output} WHERE {where} AND tick <= {tick}"),
                    ("snapshot", f"SELECT count(),sum(b),sum(o) FROM ({source}) WHERE {snapshot_where}"),
                ):
                    if cold:
                        drop_caches(url)
                    start = monotonic()
                    answers.append(ch.json(sql)[0])
                    seconds[label] = round(monotonic() - start, 4)
                if answers[0] != answers[1]:
                    raise ValueError("lineage posting history differs from the complete scoped snapshot oracle")
                views.append({"parent_id": parent, "date": (date0, date)[tick], "seconds": seconds, "exact_scalar_equal": True})
        return {"scope": "complete prefix-scoped exact-basename leaf scalar history; frozen IDs, not a full TM or incremental publisher",
                "target": target, "dates": [date0, date], "path": path, "name": name.lower(), "posting_rows": counts,
                "union_leaves": union, "threads": 8, "cold": cold, "prepare_s": prepare_s, "build_s": build_s,
                "delta_rows": stat[0], "delta_table_bytes": stat[1], "views": views}
    finally:
        ch.close()
