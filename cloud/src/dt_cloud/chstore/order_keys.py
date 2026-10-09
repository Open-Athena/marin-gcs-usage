"""Stable hierarchy order from opaque ID lineage, not dense DFS ranks.

Sibling order need not be alphabetical for subtree range sums or byte-rank
discovery. A node's key is its root-to-node ID sequence. New descendants can
insert into that lexicographic order without changing existing keys.
The reader's index/summaries and their incremental upkeep remain separate.
"""

from time import monotonic
from json import loads

from .client import Ch, lit
from .narrow import identifier
from .serve import depth_of


def append_key(parent: tuple[int, ...], identity: int) -> tuple[int, ...]:
    if not 0 <= identity < 1 << 63 or any(not 0 <= value < 1 << 63 for value in parent):
        raise ValueError("lineage identities must fit the nonnegative signed parent-ID domain")
    if len(parent) >= 256 or len(set(parent)) != len(parent) or identity in parent:
        raise ValueError("lineage exceeds the depth budget or repeats an identity")
    return (*parent, identity)


def bounds(key: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Inclusive node/subtree lower bound, exclusive following-prefix bound."""
    if not key:
        raise ValueError("a subtree key must contain its root identity")
    append_key(key[:-1], key[-1])
    return key, (*key[:-1], key[-1] + 1)


def bench(
    url: str,
    groups: int,
    leaves: int,
    depth: int,
    *,
    cold: bool = False,
) -> dict:
    """Synthetic single-name posting geometry, not historical serving."""
    from .bench import drop_caches

    rows = groups * leaves
    if not 2 <= groups <= 10_000 or not 1 <= rows <= 1_000_000 or not 2 <= depth <= 32:
        raise ValueError("synthetic key index requires 2..10K groups, <=1M leaves and depth 2..32")
    ch = Ch(url, max_threads=8, max_memory_usage=8 << 30, max_execution_time=120,
            max_bytes_before_external_sort=256 << 20, timeout_before_checking_execution_speed=0,
            timeout_overflow_mode="throw")
    prefix, base = tuple(range(1, depth - 1)), 1 << 40
    try:
        ch.tmp("key_source", f"""SELECT toUInt64(number) pre,
            arrayConcat(CAST({list(prefix)} AS Array(UInt64)),[toUInt64(100 + intDiv(number,{leaves})),toUInt64({base}) + number]) key,
            toUInt64(1 + number % 17) b, toUInt64(1) o FROM numbers({rows})""")
        builds = {}
        for table, key in (("dense_postings", "pre"), ("lineage_postings", "key")):
            start = monotonic()
            projection = "pre,b,o" if key == "pre" else "pre,key,b,o"
            ch.tmp(table, f"SELECT {projection} FROM key_source", disk=True, order_by=key)
            builds[key] = round(monotonic() - start, 4)
        table_stats = {row[0]: {"rows": row[1], "bytes": row[2]} for row in ch.json(
            "SELECT name,total_rows,total_bytes FROM system.tables WHERE is_temporary = 1 AND name IN ('dense_postings','lineage_postings')"
        )}
        views = []
        for group in (0, groups // 2, groups - 1):
            lo, hi = bounds((*prefix, 100 + group))
            answers, seconds = [], {}
            for table, key, where in (
                ("dense_postings", "pre", f"pre >= {group * leaves} AND pre < {(group + 1) * leaves}"),
                ("lineage_postings", "key", f"key >= CAST({list(lo)} AS Array(UInt64)) AND key < CAST({list(hi)} AS Array(UInt64))"),
            ):
                if cold:
                    drop_caches(url)
                start = monotonic()
                answers.append(ch.json(f"SELECT count(),sum(b),sum(o) FROM {table} WHERE {where}")[0])
                seconds[key] = round(monotonic() - start, 4)
            if answers[0] != answers[1]:
                raise ValueError("lineage range differs from the initial dense-range scalar oracle")
            views.append({"group": group, "seconds": seconds, "exact_scalar_equal": True})
        key = (*prefix, 100, base + rows)
        ch.exec(f"INSERT INTO lineage_postings VALUES ({rows},{list(key)},23,1)")
        lo, hi = bounds((*prefix, 100))
        before = ch.json(f"SELECT count(),sum(b),sum(o) FROM dense_postings WHERE pre < {leaves}")[0]
        after = ch.json(f"SELECT count(),sum(b),sum(o) FROM lineage_postings WHERE key >= CAST({list(lo)} AS Array(UInt64)) AND key < CAST({list(hi)} AS Array(UInt64))")[0]
        if after != [before[0] + 1, before[1] + 23, before[2] + 1]:
            raise ValueError("stable lineage range failed to include the newly appended descendant")
        return {"scope": "synthetic single-name index geometry only; no historical data, reservation or publication",
                "rows": rows, "depth": depth, "threads": 8, "cold": cold, "build_s": builds,
                "logical_key_value_bytes": rows * depth * 8, "logical_pre_value_bytes": rows * 8,
                "temporary_table_stats": table_stats, "views": views, "appended_descendant_in_unchanged_range": True}
    finally:
        ch.close()


def sample_bench(
    url: str,
    target: str,
    date: str,
    path: str,
    *,
    sample_rows: int = 100_000,
    cold: bool = False,
) -> dict:
    """Explicit contiguous leaf sample, never full-query/FTS acceptance."""
    identifier(target)
    if not 1 <= sample_rows <= 1_000_000:
        raise ValueError("geometry sample must be bounded to 1..1M leaf rows")
    from .bench import drop_caches

    ch = Ch(url, max_threads=8, max_memory_usage=8 << 30, max_execution_time=120,
            max_bytes_before_external_sort=256 << 20, timeout_before_checking_execution_speed=0,
            timeout_overflow_mode="throw")
    try:
        manifest = loads(ch.scalar(f"SELECT doc FROM {target}.history_manifest"))
        if date not in manifest["dates"]:
            raise ValueError("geometry sample scan is outside the frozen index")
        db = identifier(manifest["dbs"][manifest["dates"].index(date)])
        selected = ch.json(f"SELECT pre,post FROM {target}.dictionary WHERE depth={depth_of(path)} AND path={lit(path)}")
        if len(selected) != 1:
            raise ValueError("geometry sample path is outside the dictionary")
        lo, hi = selected[0]
        if int(ch.scalar(f"SELECT count() FROM {db}.nodes WHERE pre={lo}")) != 1:
            raise ValueError("geometry sample path is absent at the selected scan")
        ch.tmp("sample_leaves", f"SELECT pre,b,o FROM {db}.nodes WHERE pre >= {lo} AND pre <= {hi} AND post=pre ORDER BY pre LIMIT {sample_rows}")
        count = int(ch.scalar("SELECT count() FROM sample_leaves"))
        if not count:
            raise ValueError("geometry sample contains no structural leaves")
        ch.tmp("sample_parents", f"SELECT pre,parent_pre FROM {target}.numeric_parents WHERE pre IN (SELECT pre FROM sample_leaves)")
        if int(ch.scalar("SELECT count() FROM sample_parents")) != count:
            raise ValueError("geometry sample lost leaf parent bindings")
        ch.tmp("sample_hierarchy", f"SELECT pre,ancestors,toUInt8(1) present FROM {target}.hierarchy WHERE pre IN (SELECT parent_pre FROM sample_parents)")
        ch.tmp("sample_keys", """SELECT n.pre pre,p.parent_pre parent_pre,n.b b,n.o o,
            if(p.parent_pre < 0,[toUInt64(n.pre)],arrayConcat(arrayMap(x -> toUInt64(x),h.ancestors),[toUInt64(n.pre)])) key,
            p.parent_pre < 0 OR h.present=1 resolved
            FROM sample_leaves n INNER JOIN sample_parents p ON n.pre=p.pre LEFT JOIN sample_hierarchy h ON p.parent_pre=h.pre""")
        if int(ch.scalar("SELECT count() FROM sample_keys WHERE NOT resolved")) or int(ch.scalar("SELECT count() FROM sample_keys")) != count:
            raise ValueError("geometry sample has incomplete or duplicate lineage")
        builds = {}
        for table, order, projection in (("sample_dense", "pre", "pre,b,o"), ("sample_lineage", "key", "pre,key,b,o")):
            start = monotonic()
            ch.tmp(table, f"SELECT {projection} FROM sample_keys", disk=True, order_by=order)
            builds[order] = round(monotonic() - start, 4)
        stats = {r[0]: {"rows": r[1], "bytes": r[2]} for r in ch.json(
            "SELECT name,total_rows,total_bytes FROM system.tables WHERE is_temporary=1 AND name IN ('sample_dense','sample_lineage')"
        )}
        roots = ch.json("SELECT parent_pre,any(arrayPopBack(key)) FROM sample_keys WHERE parent_pre >= 0 GROUP BY parent_pre ORDER BY count() DESC,parent_pre LIMIT 3")
        views = []
        for parent, key in roots:
            points = ch.json(f"SELECT pre,post FROM {db}.nodes WHERE pre={parent}")
            if len(points) != 1 or not key or key[-1] != parent:
                raise ValueError("geometry sample parent bounds/presence are inconsistent")
            a, z = points[0]
            lower, upper = bounds(tuple(key))
            answers, seconds = [], {}
            for table, label, where in (
                ("sample_dense", "pre", f"pre >= {a} AND pre <= {z}"),
                ("sample_lineage", "key", f"key >= CAST({list(lower)} AS Array(UInt64)) AND key < CAST({list(upper)} AS Array(UInt64))"),
            ):
                if cold:
                    drop_caches(url)
                start = monotonic()
                answers.append(ch.json(f"SELECT count(),sum(b),sum(o) FROM {table} WHERE {where}")[0])
                seconds[label] = round(monotonic() - start, 4)
            if answers[0] != answers[1]:
                raise ValueError("sample lineage range differs from the frozen geometry oracle")
            views.append({"parent_id": parent, "seconds": seconds, "exact_sample_scalar_equal": True})
        return {"scope": "explicit contiguous structural-leaf geometry sample; not a complete FTS query, history or incremental reader",
                "target": target, "date": date, "path": path, "sample_limit": sample_rows, "sampled_rows": count,
                "threads": 8, "cold": cold, "build_s": builds, "temporary_table_stats": stats, "views": views}
    finally:
        ch.close()


def history_bench(
    url: str,
    groups: int,
    leaves: int,
    *,
    cold: bool = False,
) -> dict:
    """Additive own-leaf history under stable bounds; no publication protocol.

    Three synthetic snapshots include size changes, deletion, resurrection
    and new descendants. Independent snapshot sums validate signed deltas.
    Recursive directory weights and non-additive rich metrics are not inputs.
    """
    from .bench import drop_caches

    rows = groups * leaves
    if not 2 <= groups <= 10_000 or not 1 <= leaves or not rows + groups <= 300_000:
        raise ValueError("history geometry requires 2..10K groups and <=300K union leaves")
    ch = Ch(url, max_threads=8, max_memory_usage=8 << 30, max_execution_time=120,
            max_bytes_before_external_sort=256 << 20, timeout_before_checking_execution_speed=0,
            timeout_overflow_mode="throw")
    base = 1 << 40
    try:
        ch.tmp("history_keys", f"""SELECT toUInt64(number) id,
            [toUInt64(0),toUInt64(100 + intDiv(number,{leaves})),toUInt64({base}) + number] key
            FROM numbers({rows}) UNION ALL SELECT toUInt64({rows}) + number,
            [toUInt64(0),toUInt64(100) + number,toUInt64({base + rows}) + number]
            FROM numbers({groups})""")
        ch.tmp("history_states", f"""SELECT id,key,toUInt32(0) tick,toUInt64(1 + id % 17) b,toUInt64(1) o
            FROM history_keys WHERE id < {rows} UNION ALL
            SELECT id,key,toUInt32(1),toUInt64(1 + id % 17 + if(id % 10 = 0,23,0)),toUInt64(1)
            FROM history_keys WHERE id < {rows} AND id % 100 != 1 UNION ALL
            SELECT id,key,toUInt32(2),toUInt64(if(id < {rows},1 + id % 17,31)),toUInt64(1)
            FROM history_keys""", disk=True, order_by=("tick", "key"))
        start = monotonic()
        ch.tmp("history_grid", """SELECT k.id id,k.key key,t.tick tick,
            coalesce(s.b,0) b,coalesce(s.o,0) o,coalesce(s.present,0) n
            FROM history_keys k CROSS JOIN (SELECT toUInt32(number) tick FROM numbers(3)) t
            LEFT JOIN (SELECT *,toUInt8(1) present FROM history_states) s ON k.id=s.id AND t.tick=s.tick""")
        ch.tmp("history_deltas", """SELECT key,tick,db,do,dn FROM (
            SELECT key,tick,
                toInt128(b)-lagInFrame(toInt128(b),1,toInt128(0)) OVER w db,
                toInt128(o)-lagInFrame(toInt128(o),1,toInt128(0)) OVER w do,
                toInt128(n)-lagInFrame(toInt128(n),1,toInt128(0)) OVER w dn
            FROM history_grid WINDOW w AS (PARTITION BY id ORDER BY tick ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
        ) WHERE db != 0 OR do != 0 OR dn != 0""", disk=True, order_by=("key", "tick"))
        build_s = round(monotonic() - start, 4)
        stats = {r[0]: {"rows": r[1], "bytes": r[2]} for r in ch.json(
            "SELECT name,total_rows,total_bytes FROM system.tables WHERE is_temporary=1 AND name IN ('history_states','history_deltas')"
        )}
        views = []
        for group in (0, groups // 2, groups - 1):
            lower, upper = bounds((0, 100 + group))
            where = f"key >= CAST({list(lower)} AS Array(UInt64)) AND key < CAST({list(upper)} AS Array(UInt64))"
            totals = []
            for tick in range(3):
                answers, seconds = [], {}
                for label, sql in (
                    ("delta", f"SELECT sum(dn),sum(db),sum(do) FROM history_deltas WHERE {where} AND tick <= {tick}"),
                    ("snapshot", f"SELECT count(),sum(b),sum(o) FROM history_states WHERE {where} AND tick = {tick}"),
                ):
                    if cold:
                        drop_caches(url)
                    start = monotonic()
                    answers.append(ch.json(sql)[0])
                    seconds[label] = round(monotonic() - start, 4)
                if answers[0] != answers[1]:
                    raise ValueError("stable-key historical deltas differ from the complete snapshot oracle")
                totals.append(answers[0])
                views.append({"group": group, "tick": tick, "seconds": seconds, "exact_scalar_equal": True})
            # Tick 2 resurrects all original leaves and appends one new leaf.
            if totals[2] != [totals[0][0] + 1, totals[0][1] + 31, totals[0][2] + 1]:
                raise ValueError("unchanged subtree bounds lost a resurrected or new descendant")
        return {"scope": "synthetic additive own-leaf history only; no durable allocator, publication, query summaries or rich metrics",
                "union_leaves": rows + groups, "scans": 3, "threads": 8, "cold": cold,
                "build_s": build_s, "temporary_table_stats": stats, "views": views,
                "updates_deletions_resurrection_and_appends_verified": True}
    finally:
        ch.close()
