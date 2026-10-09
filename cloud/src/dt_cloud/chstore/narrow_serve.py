"""Complete filtered responses over the bounded numeric historical experiment.

Uses the normal serializers and diff walker, but preaggregated rich versions,
numeric root containment/ancestors, and a parent-ID ordered child access path.
Not a production backend: the hierarchy/scan coverage remain frozen.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace

from ..bench.ch import ChIndex, ChResult
from ..bench.query import Ast, compile_query, parse
from . import serve as cs
from .client import Ch, lit, statement_timeout_settings
from .narrow import identifier, rich_name_variant_identity, root_metadata_source
from .schema import scan_dt


ROOT_JOINS = ("default", "hash", "grace_hash", "full_sorting_merge")


def serving_options(plan: str) -> dict[str, object]:
    """Serving presets keep benchmark switches out of deployment config."""
    if plan == "legacy":
        return {}
    if plan == "visible":
        return {"bounded_joins": True, "root_join": "hash", "leaf_intervals": True,
                "visible_intervals": True, "fold_parent_pruning": True}
    raise ValueError(f"unknown numeric serving plan: {plan}")


def root_join_settings(plan: str) -> dict[str, object]:
    """Explicit benchmark plans; leave live/default query planning unchanged."""
    if plan not in ROOT_JOINS:
        raise ValueError(f"unknown root join plan: {plan}")
    if plan == "default":
        return {}
    # Explicit root overrides must not inherit the whole-response GraceHash
    # threshold: a hash join cannot spill when it reaches that threshold.
    settings = {"join_algorithm": plan, "query_plan_join_swap_table": "false", "max_block_size": 8192,
                "max_bytes_in_join": 0, "max_bytes_before_external_join": 0, "max_bytes_ratio_before_external_join": 0}
    if plan == "grace_hash":
        settings.update(grace_hash_join_initial_buckets=16, max_bytes_in_join=512 << 20,
                        max_bytes_before_external_join=512 << 20, max_bytes_ratio_before_external_join=0,
                        join_overflow_mode="throw")
    return settings


def materialize_root_rows(
    ch: Ch,
    db: str,
    ast: Ast,
    sfx: str,
    *,
    name_index: bool,
    hit: bool,
    large_roots: bool,
    name_index_variant: str | None = None,
    root_join: str = "default",
    leaf_intervals: bool = False,
) -> str:
    """Positive descendant roots need one rich read/write, not a net copy.

    Preserve the net table's nonnegative object/mtime-weight normalization.
    View hits keep their original row separately; exclusions need it for cuts.
    """
    net = not ast.neg and not hit
    table = f"{'rn' if net else 'rt'}_{sfx}"
    cols = ", ".join(
        f"{f'greatest(0, m.{c})' if net and c in ('o', 'wb') else f'm.{c}'} AS {c}"
        for c in cs.AGG_COLS.split(", ")
    )
    source = root_metadata_source(db, ast, name_index=name_index, hit=hit, name_index_variant=name_index_variant)
    # The source already semijoins every row to roots. Leaves need no payload
    # from that table: their interval ends at their own preorder position.
    post = "greatest(m.pre, coalesce(r.post, m.pre))" if leaf_intervals else "r.post"
    join = "LEFT JOIN (SELECT pre, post FROM roots WHERE post > pre)" if leaf_intervals else "INNER JOIN roots"
    ch.tmp(table, f"""SELECT m.pre AS pre, {post} AS post, m.parent_pre AS parent_pre,
        m.path AS path, m.depth AS depth, {cols} FROM ({source}) m
        {join} r ON m.pre = r.pre""", disk=large_roots, ordered=False,
        **({"settings": root_join_settings(root_join)} if root_join != "default" else {}))
    return table


def prepare(
    ch: Ch,
    db: str,
    scan: cs.Scan,
    path: str,
    ast: Ast,
    sfx: str,
    *,
    dictionary: str,
    path_free: bool = True,
    name_index: bool = False,
    name_index_variant: str | None = None,
    root_join: str = "default",
    leaf_intervals: bool = False,
) -> cs.Prep | None:
    """Numeric discovery, covered exclusions, net rich roots and numeric cuts."""
    identifier(db)
    start = time.monotonic()
    ix = ChIndex(ch.url, db=db, threads=int(ch.settings["max_threads"]), bounded_view=path, path_free=path_free, trigram_names=True)
    # Share temporary-table lifetime with the response and the other diff side.
    ix.settings.update(session_id=ch.settings["session_id"])
    ix.settings.update(max_memory_usage=str(8 << 30), max_bytes_before_external_sort=str(256 << 20),
                       max_bytes_ratio_before_external_sort="0", max_bytes_before_external_group_by=str(256 << 20),
                       max_bytes_ratio_before_external_group_by="0")
    rows = ch.json(f"""SELECT pre, post, depth, b, o FROM {db}.nodes WHERE pre IN (
        SELECT pre FROM {dictionary} WHERE (depth, path) = ({cs.depth_of(path)}, {lit(path)}))""")
    if not rows:
        raise cs.NotFound(path)
    if len(rows) != 1:
        raise ValueError(f"duplicate numeric view root: {path}")
    ix._views[path] = tuple(int(value) for value in rows[0])
    hit = bool(compile_query(ast)(path))
    if hit and not ast.neg:
        pre, post, _, b, o = ix.view(path)
        ix.tmp("roots", f"SELECT toUInt32({pre}) AS pre, toUInt32({post}) AS post, toInt64({b}) AS b, toInt64({o}) AS o")
        result = ChResult(True, 1, b, o, 0, {"s": round(time.monotonic() - start, 4), "view_shortcut": True})
    else:
        # A hit already selects the entire view. Only exclusions need candidates;
        # searching positive names would rediscover data that cannot change roots.
        result = ix.evaluate(replace(ast, alts=((),)) if hit else ast, path)
    stats = {**result.stats, "init_and_discovery_s": round(time.monotonic() - start, 4), "view_pre": rows[0][0]}
    if result.b <= 0:
        return None
    large_roots = result.roots > cs.HARD_CAP
    cols = ", ".join(f"m.{c} AS {c}" for c in cs.AGG_COLS.split(", "))
    root_table = materialize_root_rows(ch, db, ast, sfx, name_index=name_index, hit=result.hit, large_roots=large_roots,
                                       name_index_variant=name_index_variant, root_join=root_join, leaf_intervals=leaf_intervals)
    if ast.neg:
        ch.tmp(f"covered_{sfx}", """SELECT pre, rr.1 AS root_pre FROM (
            SELECT pre, r, max(if(r, tuple(toInt64(pre), toInt64(post)), tuple(toInt64(-1), toInt64(-1))))
                OVER (ORDER BY pre, r DESC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS rr
            FROM (SELECT pre, post, 1 AS r FROM roots UNION ALL SELECT pre, post, 0 AS r FROM ex))
            WHERE NOT r AND rr.1 < pre AND rr.2 >= pre""")
        ch.tmp(f"ex_{sfx}", f"""SELECT m.pre AS pre, m.parent_pre AS parent_pre, m.path AS path, m.depth AS depth,
            r.pre AS root_pre, r.path AS rp, r.depth AS rd, {cols}
            FROM (SELECT * FROM {db}.metadata WHERE pre IN (SELECT pre FROM covered_{sfx})) m
            INNER JOIN covered_{sfx} c ON m.pre = c.pre INNER JOIN rt_{sfx} r ON c.root_pre = r.pre""", disk=result.excluded > cs.HARD_CAP, ordered=False)
    else:
        ch.tmp(f"ex_{sfx}", f"SELECT *, pre AS root_pre, path AS rp, depth AS rd FROM {root_table} WHERE 0")
    ch.tmp(f"cut_{sfx}", f"""SELECT ancestor AS pre, count() AS n, {cs.SUM_AGG} FROM (
        SELECT e.*, arrayJoin(h.ancestors) AS ancestor FROM ex_{sfx} e INNER JOIN (
            SELECT pre, ancestors FROM {db}.hierarchy WHERE pre IN (SELECT parent_pre FROM ex_{sfx})) h ON e.parent_pre = h.pre)
        WHERE ancestor >= root_pre GROUP BY ancestor""")
    ch.tmp(f"lost_{sfx}", f"SELECT parent_pre AS pre, count() AS lost FROM ex_{sfx} GROUP BY parent_pre")
    if root_table != f"rn_{sfx}":
        ch.tmp(f"rn_{sfx}", f"""SELECT r.pre AS pre, r.post AS post, r.parent_pre AS parent_pre, r.path AS path, r.depth AS depth,
            r.b - c.b AS b, greatest(0, r.o - c.o) AS o, r.wts - c.wts AS wts, greatest(0, r.wb - c.wb) AS wb,
            r.a AS a, r.c2 - c.c2 AS c2, r.c3 - c.c3 AS c3, r.c4 - c.c4 AS c4,
            if(c.n > 0, mapFilter((u, x) -> x > 0, mapSubtract(r.ub, c.ub)), r.ub) AS ub,
            r.kind AS kind, if(r.nc < 0, r.nc, greatest(0, r.nc - l.lost)) AS nc
            FROM rt_{sfx} r LEFT JOIN cut_{sfx} c ON r.pre = c.pre LEFT JOIN lost_{sfx} l ON r.pre = l.pre""", disk=large_roots, ordered=False)
    n, *row = ch.json(f"SELECT count(), {cs.SUM_AGG} FROM rn_{sfx}")[0]
    total = cs.agg_row(row, synth=True)
    root = cs.agg_row(ch.json(f"SELECT {cs.AGG_COLS} FROM rt_{sfx}")[0]) if result.hit else None
    if result.hit:
        total.kind, total.nc = root.kind, int(ch.scalar(f"SELECT nc FROM rn_{sfx}"))
    return cs.Prep(sfx, scan, path, cs.depth_of(path), result.hit, n,
                   int(ch.scalar(f"SELECT count() FROM ex_{sfx}")), total, root, stats)


def scalar_ancestors_bottom_up(
    ch: Ch,
    db: str,
    sfx: str,
    view_depth: int,
) -> list[dict[str, int]]:
    """Roll bytes up one directory level at a time, preserving zero-byte nodes.

    Root contributions can start at different depths. At each level combine
    those direct contributions with the previous level's aggregated children.
    This is an experimental alternative to expanding every ancestor array.
    """
    total = f"anc0_{sfx}"
    ch.tmp(total, "SELECT toUInt32(0) AS pre, toInt64(0) AS b WHERE 0", disk=True, ordered=False)
    deepest = int(ch.scalar(f"SELECT max(depth) FROM rn_{sfx}")) - 1
    previous, levels = None, []
    for depth in range(deepest, view_depth, -1):
        source = f"SELECT toUInt32(parent_pre) AS pre, b FROM rn_{sfx} WHERE depth = {depth + 1}"
        if previous is not None:
            source += f""" UNION ALL SELECT toUInt32(m.parent_pre) AS pre, a.b AS b FROM {previous} a
                INNER JOIN (SELECT pre, parent_pre FROM {db}.metadata WHERE pre IN (SELECT pre FROM {previous})) m ON a.pre = m.pre"""
        table = f"ancestor_level_{sfx}_{depth}"
        ch.tmp(table, f"SELECT pre, sum(b) AS b FROM ({source}) GROUP BY pre",
               settings=root_join_settings("full_sorting_merge"), disk=True, ordered=False)
        rows = int(ch.scalar(f"SELECT count() FROM {table}"))
        ch.exec(f"INSERT INTO {total} SELECT pre, b FROM {table}")
        levels.append({"depth": depth, "rows": rows})
        previous = table
    return levels


def ancestor_source(
    ch: Ch,
    db: str,
    sfx: str,
    *,
    preaggregate: bool = False,
) -> str:
    """Optionally combine sibling roots before expanding shared ancestors.

    This changes floating-point summation order. Complete response parity is
    required for adoption; the original plan remains the default.
    """
    table = f"rn_{sfx}"
    if preaggregate:
        table = f"ancestor_parents_{sfx}"
        ch.tmp(table, f"SELECT parent_pre, {cs.SUM_AGG} FROM rn_{sfx} GROUP BY parent_pre", disk=True, ordered=False)
    return f"""(SELECT r.*, arrayJoin(h.ancestors) AS ancestor FROM {table} r INNER JOIN (
        SELECT pre, ancestors FROM {db}.hierarchy WHERE pre IN (SELECT parent_pre FROM {table})) h ON r.parent_pre = h.pre)"""


def view(
    ch: Ch,
    db: str,
    pr: cs.Prep,
    *,
    w: int,
    h: int,
    min_area: float,
    atten: float,
    parent_index: bool = False,
    ancestor_preaggregate: bool = False,
    bounded_joins: bool = False,
    visible_intervals: bool = False,
    fold_parent_pruning: bool = False,
    ancestor_bottom_up: bool = False,
    max_depth: int | None = None,
    threshold: float | None = None,
) -> cs.View:
    """Numeric synthesized ancestors and one indexed child read per frontier."""
    sf, path, dp = pr.sfx, pr.path, pr.dP
    t = threshold if threshold is not None else pr.total.b * min_area / (w * h)
    deepest = dp if pr.hit else int(ch.scalar(f"SELECT max(depth) FROM (SELECT depth FROM rn_{sf} ORDER BY b DESC, path LIMIT {cs.REGION_READS})"))
    folded = not pr.hit and pr.n_roots > cs.HARD_CAP
    out = cs.View(path, dp, pr.total, {}, {}, {}, t, deepest, atten, prep=pr)
    frontier = []
    if pr.hit:
        root_pre = int(ch.scalar(f"SELECT pre FROM rn_{sf}"))
        out.root_paths.add(path)
        frontier = [(root_pre, dp, dp, path)]
    else:
        for pre, p, d, *r in ch.json(f"SELECT pre, path, depth, {cs.AGG_COLS} FROM rn_{sf}" + (f" WHERE b >= {t!r}" if folded else "")):
            out.kept[p], out.depth[p] = cs.agg_row(r), d
            out.root_paths.add(p)
            frontier.append((pre, d, d, p))
        root_pre = int(pr.stats["view_pre"])
        src = ancestor_source(ch, db, sf, preaggregate=ancestor_preaggregate)
        if ancestor_preaggregate:
            pr.stats["ancestor_parent_groups"] = int(ch.scalar(f"SELECT count() FROM ancestor_parents_{sf}"))
        if ancestor_bottom_up:
            pr.stats["ancestor_levels"] = scalar_ancestors_bottom_up(ch, db, sf, dp)
        else:
            ch.tmp(f"anc0_{sf}", f"SELECT ancestor AS pre, sum(b) AS b FROM {src} WHERE ancestor > {root_pre} GROUP BY ancestor",
                   **({"disk": True, "ordered": False} if bounded_joins and folded else {}))
        condition = f"ancestor IN (SELECT pre FROM anc0_{sf} WHERE b >= {t!r})" if folded else "1"
        rich_source, rich_settings = src, {}
        if visible_intervals and folded:
            from .visible_ancestors import source as visible_source

            selected = visible_source(ch, db, sf, t)
            if selected is not None:
                rich_source, rich_settings = selected, {"settings": root_join_settings("hash")}
        ch.tmp(f"anc_{sf}", f"SELECT ancestor AS pre, {cs.SUM_AGG} FROM {rich_source} WHERE ancestor > {root_pre} AND {condition} GROUP BY ancestor", **rich_settings)
        for p, d, *r in ch.json(f"""SELECT m.path, m.depth, {', '.join('a.' + c for c in cs.SUM_COLS.split(', '))}
            FROM anc_{sf} a INNER JOIN (SELECT pre, path, depth FROM {db}.metadata WHERE pre IN (SELECT pre FROM anc_{sf})) m ON a.pre = m.pre"""):
            out.kept[p], out.depth[p] = cs.agg_row(r, synth=True), d
        if folded:
            out.folded = pr.n_roots - len(frontier)
            # Only drawn parents can consume folded counts. Keep parent IDs
            # numeric until that bounded lookup, rather than decoding every
            # path in the metadata table or retaining millions of invisible
            # dictionary entries in Python.
            parent_source = f"{db}.directory_parents" if parent_index else f"{db}.metadata"
            kept_parents = f"SELECT pre FROM rn_{sf} WHERE b >= {t!r} UNION ALL SELECT pre FROM anc_{sf} UNION ALL SELECT toUInt32({root_pre})"
            parent_filter = f"pre IN (SELECT pre FROM anc0_{sf} WHERE b < {t!r})"
            if fold_parent_pruning:
                parent_source = f"{db}.directory_parents" if parent_index else f"{db}.metadata_by_parent"
                parent_filter = f"parent_pre IN ({kept_parents})"
            ch.tmp(f"fold_{sf}", f"""SELECT parent_pre AS pre, count() AS n FROM (
                SELECT parent_pre FROM rn_{sf} WHERE b < {t!r} UNION ALL
                SELECT m.parent_pre FROM anc0_{sf} a INNER JOIN (SELECT pre, parent_pre FROM {parent_source}
                    WHERE {parent_filter}) m ON a.pre = m.pre WHERE a.b < {t!r})
                WHERE parent_pre IN ({kept_parents}) GROUP BY parent_pre""")
            for par, n in ch.json(f"""SELECT m.path, f.n FROM fold_{sf} f INNER JOIN (
                    SELECT pre, path FROM {db}.metadata WHERE pre IN (SELECT pre FROM fold_{sf})) m ON f.pre = m.pre
                """):
                out.folded_of[par] = n
    while frontier and (max_depth is None or max_depth > 0):
        want = [(pre, d, rd, p) for pre, d, rd, p in frontier if max_depth is None or d + 1 <= rd + max_depth]
        if not want:
            break
        thresholds = {pre: t * atten ** max(0, d - rd) for pre, d, rd, _ in want}
        depths = {pre: rd for pre, _, rd, _ in want}
        ids = ",".join(str(pre) for pre in thresholds)
        rows = ch.json(f"""SELECT m.pre, m.parent_pre, m.path, m.depth, {', '.join('m.' + c for c in cs.AGG_COLS.split(', '))},
            c.n, {', '.join('c.' + c for c in cs.SUM_COLS.split(', '))}, l.lost, e.pre + 1
            FROM (SELECT * FROM {db}.metadata_by_parent WHERE parent_pre IN ({ids}) AND b >= {min(thresholds.values())!r}) m
            LEFT JOIN cut_{sf} c ON m.pre = c.pre LEFT JOIN lost_{sf} l ON m.pre = l.pre LEFT JOIN ex_{sf} e ON m.pre = e.pre
            ORDER BY m.path""", settings={"join_use_nulls": 1})
        frontier = []
        for pre, par, p, d, *r in rows:
            if r[22] is not None or r[0] < thresholds[par]:
                continue
            cut = cs.agg_row(r[12:21], synth=True) if r[11] else None
            a = cs.minus(cs.agg_row(r[:11]), cut, r[21] or 0)
            if a.b <= 0 or a.b < thresholds[par]:
                continue
            out.kept[p], out.depth[p] = a, d
            frontier.append((pre, d, depths[par], p))
    out.kept = {p: out.kept[p] for p in sorted(out.kept)}
    return out


def lookup(
    ch: Ch,
    db: str,
    v: cs.View,
    asks: list[tuple[str, int]],
    *,
    dictionary: str,
) -> tuple[dict, int]:
    """Diff point reads from preaggregated rich metadata, preserving query membership."""
    sf = v.prep.sfx
    prefixes = sorted({"/".join(p.split("/")[:i]) for p, _ in asks for i in range(1, p.count("/") + 2)})
    keys = ",".join(lit(p) for p in prefixes)
    roots = {r[0] for r in ch.json(f"SELECT path FROM rn_{sf} WHERE path IN ({keys})")}
    excluded = {r[0] for r in ch.json(f"SELECT path FROM ex_{sf} WHERE path IN ({keys})")}
    todo = [p for p, _ in asks if (v.prep.hit or any("/".join(p.split("/")[:i]) in roots for i in range(1, p.count("/") + 2)))
            and not any("/".join(p.split("/")[:i]) in excluded for i in range(1, p.count("/") + 2))]
    out = {p: None for p, _ in asks}
    if todo:
        keys = ",".join(f"({cs.depth_of(p)}, {lit(p)})" for p in todo)
        for p, *r in ch.json(f"""SELECT m.path, {', '.join('m.' + c for c in cs.AGG_COLS.split(', '))}, c.n,
            {', '.join('c.' + c for c in cs.SUM_COLS.split(', '))}, l.lost FROM {db}.metadata m
            LEFT JOIN cut_{sf} c ON m.pre = c.pre LEFT JOIN lost_{sf} l ON m.pre = l.pre
            WHERE m.pre IN (SELECT pre FROM {dictionary} WHERE (depth, path) IN ({keys}))"""):
            a = cs.minus(cs.agg_row(r[:11]), cs.agg_row(r[12:21], synth=True) if r[11] else None, r[21])
            out[p] = a if a.b > 0 else None
    return out, len(todo)


def response(
    url: str,
    target: str,
    date: str,
    query: str,
    *,
    previous: str | None = None,
    path: str | None = None,
    syntax: str = "simple",
    threads: int = 8,
    w: int = 1408,
    h: int = 896,
    min_area: float = 12,
    atten: float = 2,
    match_limit: int | None = 1000,
    max_depth: int | None = None,
    top: int = 500,
    summary: bool = False,
    root_label: str = "marin GCS",
    path_free: bool = True,
    name_index: bool = False,
    parent_index: bool = False,
    name_index_variant: str | None = None,
    ancestor_preaggregate: bool = False,
    root_join: str = "default",
    bounded_joins: bool = False,
    leaf_intervals: bool = False,
    visible_intervals: bool = False,
    fold_parent_pruning: bool = False,
    ancestor_bottom_up: bool = False,
) -> dict:
    """Build an actual subtree/diff JSON body, including initialization/serialization."""
    identifier(target)
    root_join_settings(root_join)
    if name_index_variant is not None:
        identifier(name_index_variant)
        if not name_index:
            raise ValueError("a rich name-index variant requires name_index")
    start = time.monotonic()
    ch = cs.Store(url, db=target, threads=threads).session()
    ch.settings.update(max_memory_usage=str(8 << 30), max_bytes_before_external_group_by=str(256 << 20),
                       max_bytes_before_external_sort=str(256 << 20), max_bytes_ratio_before_external_sort="0",
                       max_bytes_ratio_before_external_group_by="0")
    if bounded_joins:
        ch.settings.update({key: str(value) for key, value in root_join_settings("grace_hash").items()})
    scans, prepared, dbs = {}, {}, {}
    present = set()
    try:
        manifest = json.loads(ch.scalar("SELECT doc FROM history_manifest"))
        if name_index_variant is not None:
            checkpoint = json.loads(ch.scalar(f"SELECT doc FROM rich_name_manifest_{name_index_variant}"))
            expected = rich_name_variant_identity(target, name_index_variant)
            if {key: checkpoint.get(key) for key in expected} != expected:
                raise ValueError("rich name-index variant checkpoint differs from the response target")
        if parent_index and json.loads(ch.scalar("SELECT doc FROM parent_index_manifest"))["target"] != target:
            raise ValueError("directory parent index checkpoint differs from the response target")
        ast = parse(query, syntax)
        if ast is None:
            raise ValueError("a nonempty query is required")
        prefix = manifest["prefix"]
        path = prefix if path is None else path
        if prefix and path != prefix and not path.startswith(prefix + "/"):
            raise ValueError("the requested view is outside the experimental prefix")
        kw = dict(w=w, h=h, min_area=min_area, atten=atten)
        for sf, day in (("a", previous), ("b", date)):
            if day is None:
                continue
            db = manifest["dbs"][manifest["dates"].index(day)]
            scan = cs.Scan(day, scan_dt(day), 2, scan_dt(manifest["dates"][0]))
            scans[sf], dbs[sf] = scan, db
            try:
                prepared[sf] = prepare(ch, db, scan, path, ast, sf, dictionary=f"{target}.dictionary", path_free=path_free,
                                       name_index=name_index, name_index_variant=name_index_variant, root_join=root_join,
                                       leaf_intervals=leaf_intervals)
            except cs.NotFound:
                if previous is None:
                    raise
                prepared[sf] = None
            else:
                present.add(sf)
        if not present:
            raise cs.NotFound(path)
        discovery_s = time.monotonic() - start
        threshold = max((pr.total.b for pr in prepared.values() if pr), default=0) * min_area / (w * h)
        views = {sf: view(ch, dbs[sf], pr, threshold=threshold, max_depth=0 if summary else max_depth, parent_index=parent_index,
                          ancestor_preaggregate=ancestor_preaggregate, bounded_joins=bounded_joins,
                          visible_intervals=visible_intervals, fold_parent_pruning=fold_parent_pruning,
                          ancestor_bottom_up=ancestor_bottom_up, **kw) if pr else None for sf, pr in prepared.items()}
        walk_s = time.monotonic() - start - discovery_s
        if previous is None:
            body = "".join(cs.subtree_body(ch, views["b"], date=date, path=path, q=query, root_label=root_label, match_limit=match_limit, **kw))
        else:
            def points(client: Ch, scan: cs.Scan, v: cs.View, asks: list[tuple[str, int]]) -> tuple[dict, int]:
                return lookup(client, dbs[v.prep.sfx], v, asks, dictionary=f"{target}.dictionary")

            body = "".join(cs.views_diff_body(ch, scans["a"], scans["b"], views["a"], views["b"], path=path, top=top, ast=ast,
                                             q=query, summary=summary, depth=max_depth, match_limit=match_limit, lookup=points, matched_key="pre"))
        result = {"body": json.loads(body), "discovery_s": round(discovery_s, 4),
                  "walk_s": round(walk_s, 4), "bytes": len(body.encode()), "renders_tree": True, "incremental": False}
        if ancestor_preaggregate:
            result["ancestor_grouping"] = {
                sf: {"roots": pr.n_roots, "parents": pr.stats["ancestor_parent_groups"]}
                for sf, pr in prepared.items() if pr and not pr.hit
            }
        if ancestor_bottom_up:
            result["ancestor_levels"] = {sf: pr.stats["ancestor_levels"] for sf, pr in prepared.items() if pr and not pr.hit}
    finally:
        ch.close()
        for name in ("roots", "ex", "cr", "cn"):
            ch.exec(f"DROP TEMPORARY TABLE IF EXISTS {name}")
    result["response_s"] = round(time.monotonic() - start, 4)
    return result


def compare_response(
    store: cs.Store,
    date: str,
    path: str,
    query: str,
    *,
    previous: str | None = None,
    syntax: str = "simple",
    match_limit: int | None = 1000,
    reference_timeout: int | None = None,
) -> dict:
    """Complete canonical response at the experiment's fixed canvas/settings."""
    limits = statement_timeout_settings(reference_timeout) if reference_timeout is not None else {}
    start = time.monotonic()
    ch = store.session()
    if reference_timeout is not None:
        ch.settings.update(limits)
        ch.timeout = reference_timeout + 60
    ast = parse(query, syntax)
    kw = dict(path=path, w=1408, h=896, min_area=12, atten=2, q=query, match_limit=match_limit)
    scan = store.scan(date)
    try:
        if previous:
            body = "".join(cs.diff_body(ch, store.scan(previous), scan, ast=ast, top=500, **kw))
        else:
            pr = cs.filter_prepare(ch, scan, path, ast)
            v = cs.filter_view(ch, pr, w=1408, h=896, min_area=12, atten=2) if pr else None
            body = "".join(cs.subtree_body(ch, v, date=date, root_label=store.root_label, **kw))
        result = {"body": json.loads(body), "bytes": len(body.encode())}
    finally:
        ch.close()
    result["response_s"] = round(time.monotonic() - start, 4)
    return result


def summarize(records: list[dict]) -> dict:
    from ..bench.local import pct

    groups = {}
    for row in records:
        key = f"{row['previous'] or 'subtree'}->{row['date']}/t{row['trial']}"
        group = groups.setdefault(key, [])
        group.append(row)
    result = {}
    for key, rows in sorted(groups.items()):
        timings = {k: [r[k] for r in rows] for k in ("response_s", "discovery_s", "walk_s")}
        if all("baseline" in r for r in rows):
            timings["baseline_response_s"] = [r["baseline"]["response_s"] for r in rows]
        result[key] = {"n": len(rows), "exact": sum(r.get("exact") is True for r in rows),
                       "timings": {field: {"p50": pct(values, .5), "p90": pct(values, .9), "max": max(values)}
                                   for field, values in timings.items()}}
    return result


def compare_runs(
    before: list[dict],
    after: list[dict],
) -> list[dict]:
    """Pair complete-response records; never compare different workloads silently.

    Missing legacy thread metadata stays unknown rather than inferring a limit.
    This verifies body comparisons, not uncapped historical root identities.
    """
    fields = ("prefix", "date", "previous", "query", "trial", "cold")

    def index(rows: list[dict]) -> dict[tuple, dict]:
        if not rows:
            raise ValueError("an empty response run cannot be compared")
        result = {}
        for row in rows:
            key = tuple(row[field] for field in fields)
            if key in result:
                raise ValueError(f"duplicate response case: {key}")
            result[key] = row
        return result

    a, b = index(before), index(after)
    if a.keys() != b.keys():
        raise ValueError("response workloads differ; dates, queries, trials and cache modes must match")
    result = []
    for key, old in a.items():
        new = b[key]
        if old.get("comparison_phase", "interleaved") != new.get("comparison_phase", "interleaved"):
            raise ValueError(f"response comparison_phase differs: {key}")
        for field in ("threads", "query_text", "syntax"):
            if old.get(field) is not None and new.get(field) is not None and old[field] != new[field]:
                raise ValueError(f"response {field} differs: {key}")
        if old.get("exact") is not True or new.get("exact") is not True or not old.get("sha") or old["sha"] != new.get("sha"):
            raise ValueError(f"both response bodies must match canonical serving and each other: {key}")
        if old["response_s"] <= 0 or new["response_s"] <= 0:
            raise ValueError(f"response durations must be positive: {key}")
        result.append({
            **dict(zip(fields, key, strict=True)),
            "same_body": True,
            "threads_verified": old.get("threads") is not None and new.get("threads") is not None,
            "comparison_phase": old.get("comparison_phase", "interleaved"),
            "before_mode": {field: old.get(field) for field in ("path_free", "name_index", "name_index_variant", "parent_index", "ancestor_preaggregate", "root_join", "bounded_joins", "leaf_intervals", "visible_intervals", "fold_parent_pruning", "ancestor_bottom_up", "threads")},
            "after_mode": {field: new.get(field) for field in ("path_free", "name_index", "name_index_variant", "parent_index", "ancestor_preaggregate", "root_join", "bounded_joins", "leaf_intervals", "visible_intervals", "fold_parent_pruning", "ancestor_bottom_up", "threads")},
            "timings": {field: {"before": old[field], "after": new[field], "speedup": round(old[field] / new[field], 4) if new[field] else None}
                        for field in ("response_s", "discovery_s", "walk_s")},
        })
    return result
