"""Data extracts for the web viz (``gcs-usage webdata``).

Everything here is laptop-scale DuckDB over the deduped listing parquet:
a nested prefix tree for the treemap, created-day age strata, and a
small meta blob. Output is plain JSON consumed by ``site/``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import resource
import sys
from collections import defaultdict
from functools import partial
from pathlib import Path

import duckdb

err = partial(print, file=sys.stderr)


def _rss(tag: str) -> None:
    """Log peak RSS so OOM autopsies can name the phase (linux: KB, mac: B)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    err(f"[rss] {tag}: peak {peak / (1024**2 if sys.platform == 'linux' else 1024**3):.1f} GB")

MIN_FRAC = 0.0002  # fold children below this fraction of their PARENT into "(other)"
# Aggregate bounds on the kept set — parent-relative alone is unbounded (the
# fleet has >100M dirs; 0.02%-of-parent keeps every child of an evenly-split
# parent, recursively — OOM-killed the 8/26 attempt-4 REPROC). Values chosen
# from the 8/26 full-listing estimate (see specs/dir-agg-cache.md).
ABS_FLOOR = int(float(os.environ.get("GCS_USAGE_TREE_ABS_FLOOR", "5e9")))  # bytes
TOP_K = int(os.environ.get("GCS_USAGE_TREE_TOP_K", "500"))  # kept children per parent


def write_webdata(
    listings: tuple[str, ...],
    out_dir: Path,
    asof: str,
    attributions: tuple[str, ...] = (),
    identities_path: Path | None = None,
    access: tuple[str, ...] = (),
    dir_cache: Path | None = None,
    path_index: Path | None = None,
) -> dict:
    """Write tree.json / age.json / meta.json under ``out_dir``; returns meta.

    ``path_index`` writes the complete floor-free rolled-up path index
    (every ancestor path × attribution, sorted ``(depth, path)``) — the
    artifact the pixel-budget subtree API serves
    (specs/path-index-lazy-drill.md).

    ``dir_cache`` names a directory for the layer-2 rollups (``dir-stats`` /
    ``age-days`` parquet) — attribution-independent per-dir aggregates, cached
    write-through so re-attribution runs skip the 595M-row object scans
    entirely (specs/dir-agg-cache.md). Immutable per scan date, like the
    listing they derive from.

    With ``attributions``, every dir is attributed (deepest-prefix-wins, same
    join as ``report``) and each tree node carries ``tm`` (team-bytes map) and
    ``us`` (per-user bytes) for ownership overlays.

    With ``access`` (layer-2a access-log agg parquet globs), every tree node
    that has been read since logging began carries ``a`` — the epoch day of
    its most recent read (GET/HEAD/LIST anywhere under the prefix) — and meta
    gains the observation window (``meta.access = {from, to}``).
    """
    from .listing import prepare_listing

    attr = bool(attributions)
    con = duckdb.connect()
    # attribution mode is node-scale (34M-dir python-side walk); plain mode
    # stays laptop-safe
    con.execute(f"SET memory_limit='{os.environ.get('DUCKDB_MEM', '24GB' if attr else '6GB')}'")
    # large hash aggregations stream instead of materializing ordered output;
    # matters in Cloud Run where DuckDB's disk spill is actually RAM-backed
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"SET threads={os.environ.get('DUCKDB_THREADS', '4')}")
    if tmp := os.environ.get("DUCKDB_TMP"):
        con.execute(f"SET temp_directory='{tmp}'")
    src = prepare_listing(con, listings)

    # --- layer-2 dir rollups (attribution-independent; cached when dir_cache) ---
    # Everything downstream needs objects only via these two aggregates:
    # `dir_stats` (per-dir sizes/objects/classes/weighted-mtimes) and
    # `age_days` (per-(day, dir) bytes/objects). Cache them as parquet next to
    # the listing (immutable per date) so re-attribution runs — REPROC, ledger
    # refreshes — do zero 595M-row object scans; cold runs also drop from four
    # object scans to two (attr dirs + storage classes now derive from these).
    fp_dir = "CASE WHEN name LIKE '%/%' THEN regexp_replace(name, '/[^/]*$', '') ELSE '' END"
    fp = f"CASE WHEN ({fp_dir}) = '' THEN bucket ELSE bucket || '/' || ({fp_dir}) END"
    stats_pq = dir_cache / "dir-stats.parquet" if dir_cache else None
    age_pq = dir_cache / "age-days.parquet" if dir_cache else None
    if stats_pq is not None and stats_pq.exists():
        con.execute(f"CREATE TEMP VIEW dir_stats AS SELECT * FROM read_parquet('{stats_pq}')")
        err(f"dir-stats: cache hit ({stats_pq})")
    else:
        con.execute(
            f"""
            CREATE TEMP TABLE dir_stats AS
            WITH obj AS (
              SELECT bucket, {fp_dir} AS dir, size_bytes, created, storage_class_id, {fp} AS fp
              FROM {src}
            )
            SELECT bucket, dir, fp,
              sum(size_bytes)::BIGINT AS b, count(*)::BIGINT AS o,
              sum(CASE WHEN created IS NOT NULL THEN size_bytes * epoch(created) END)::DOUBLE AS wts,
              sum(CASE WHEN created IS NOT NULL THEN size_bytes END)::BIGINT AS wb,
              sum(CASE WHEN storage_class_id = 2 THEN size_bytes END)::BIGINT AS c2,
              sum(CASE WHEN storage_class_id = 3 THEN size_bytes END)::BIGINT AS c3,
              sum(CASE WHEN storage_class_id = 4 THEN size_bytes END)::BIGINT AS c4
            FROM obj GROUP BY bucket, dir, fp
            """
        )
        if stats_pq is not None:
            stats_pq.parent.mkdir(parents=True, exist_ok=True)
            con.execute(f"COPY dir_stats TO '{stats_pq}' (FORMAT parquet)")
            err(f"dir-stats: wrote cache ({stats_pq})")
    _rss("dir-stats")
    if age_pq is not None and age_pq.exists():
        con.execute(f"CREATE TEMP VIEW age_days AS SELECT * FROM read_parquet('{age_pq}')")
        err(f"age-days: cache hit ({age_pq})")
    else:
        con.execute(
            f"""
            CREATE TEMP TABLE age_days AS
            SELECT CAST(floor(epoch(created) / 86400) AS INTEGER) AS day, bucket, {fp_dir} AS dir,
              sum(size_bytes)::BIGINT AS bytes, count(*)::BIGINT AS objects
            FROM {src}
            WHERE created IS NOT NULL
            GROUP BY ALL
            """
        )
        if age_pq is not None:
            con.execute(f"COPY age_days TO '{age_pq}' (FORMAT parquet)")
            err(f"age-days: wrote cache ({age_pq})")
    _rss("age-days")
    # Dir-level stand-in for the raw listing where only dir paths matter
    # (bucket enumeration + path-glob prefix_owners expansion).
    con.execute("CREATE TEMP VIEW listing_dirs AS SELECT bucket, dir AS name FROM dir_stats")

    if attr:
        import pandas as pd

        from .identity import DEFAULT_IDENTITIES, load_identities
        from .prefixes import load_prefix_map

        identities = load_identities(identities_path or DEFAULT_IDENTITIES)
        _rss("start")
        by_prefix = load_prefix_map(con, attributions, identities, "listing_dirs")
        _rss("prefix-map")
        pfx_df = pd.DataFrame(
            [
                {"key": k.removeprefix("gs://").rstrip("/"), "user": u, "team": t}
                for k, (u, t, _source) in by_prefix.items()
            ]
        )
        pfx_df["depth"] = pfx_df["key"].str.count("/") + 1
        # Cap attribution depth: the ancestor explosion below is dirs × maxd, so
        # a handful of ultra-deep prefixes inflate memory for *everything* (the
        # 2026-08-26 wandb re-mine's 231 depth-14+ config paths pushed maxd
        # 13 → 16 and OOMed the 100GB REPROC). Deeper-than-cap rows are dropped
        # (a truncated prefix would over-attribute whole parent dirs).
        attr_max_depth = int(os.environ.get("GCS_USAGE_ATTR_MAX_DEPTH", "12"))
        deep = pfx_df["depth"] > attr_max_depth
        if deep.any():
            err(f"dropping {int(deep.sum())} attribution prefixes deeper than {attr_max_depth}")
            pfx_df = pfx_df[~deep]
        con.register("pfx", pfx_df)
        # Deepest-prefix-wins, one INSERT per prefix depth (deepest first):
        # inner hash join (build side = that depth's prefixes — thousands) plus
        # an anti-join against already-resolved dirs (build side ≤ resolved
        # set, a few GB at fleet scale). Keeps the proven-fast split+equi-join
        # machinery of the original explosion but drops its un-spillable
        # dirs×maxd arg_max aggregate (OOM-killed the daily's 128GB node when
        # the 2026-08-26 wandb re-mine grew the prefix map). A chained-LEFT-
        # JOIN single-pass variant planned pathologically (~100× slower);
        # per-depth INSERTs give the planner 12 trivial queries instead.
        depths = sorted({int(d) for d in pfx_df["depth"]}, reverse=True)
        con.execute('CREATE TEMP TABLE dir_attr (bucket VARCHAR, dir VARCHAR, "user" VARCHAR, team VARCHAR)')
        dk = "CASE WHEN s.dir = '' THEN s.bucket ELSE s.bucket || '/' || s.dir END"
        for k in depths:
            con.execute(
                f"""
                INSERT INTO dir_attr
                SELECT s.bucket, s.dir, p."user", p.team
                FROM dir_stats s
                JOIN pfx p ON p.depth = {k}
                  AND p.key = array_to_string(str_split({dk}, '/')[1:{k}], '/')
                WHERE len(str_split({dk}, '/')) >= {k}
                  AND NOT EXISTS (
                    SELECT 1 FROM dir_attr d WHERE d.bucket = s.bucket AND d.dir = s.dir
                  )
                """
            )
        _rss("dir_attr")
        # per-(team|user) storage-class byte mixes (site prices group roll-ups
        # with class-aware rates) + the per-(user,team) leaderboard meta.users
        # needs — both derived from `dir_agg` below, so no separate object scan.
        team_class: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        user_class: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        user_bytes: dict[tuple, int] = defaultdict(int)

    # Access agg first (small): its per-dir last-read day joins into
    # `dir_agg` below so the path index carries a subtree-MAX `a`, and it
    # also decorates the built tree + age strata. Empty without logs.
    access_window: tuple[int, int] | None = None
    con.execute("CREATE TEMP TABLE access_agg (bucket VARCHAR, dir VARCHAR, aday INTEGER, ro BIGINT, rb BIGINT)")
    if access:
        globs = "[" + ", ".join(f"'{g}'" for g in access) + "]"
        con.execute(
            f"""
            INSERT INTO access_agg
            SELECT bucket, CASE WHEN path = '.' THEN '' ELSE path END AS dir,
              CAST(floor(epoch(MAX(last_ts)) / 86400) AS INTEGER) AS aday,
              COALESCE(SUM(n_ops) FILTER (WHERE op IN ('GET', 'HEAD')), 0) AS ro,
              COALESCE(SUM(bytes_out) FILTER (WHERE op IN ('GET', 'HEAD')), 0) AS rb
            FROM read_parquet({globs})
            WHERE op IN ('GET', 'HEAD', 'LIST')
            GROUP BY 1, 2
            """
        )
        amap: dict[str, tuple[int, int, int]] = {}
        for bucket, path, aday, ro, rb in con.execute(
            "SELECT bucket, dir, aday, ro, rb FROM access_agg"
        ).fetchall():
            amap[f"{bucket}/{path}" if path else bucket] = (aday, int(ro), int(rb))
        lo, hi = con.execute(
            f"SELECT CAST(floor(epoch(MIN(last_ts)) / 86400) AS INTEGER), "
            f"CAST(floor(epoch(MAX(last_ts)) / 86400) AS INTEGER) FROM read_parquet({globs})"
        ).fetchone()
        if lo is not None:
            access_window = (int(lo), int(hi))
        _rss("access")

    # --- arbitrary-depth tree (specs/tree-builder-unification.md) ---
    # Roll every object up to *all* its ancestor prefixes (descendant-inclusive
    # totals at every depth), attribute per dir, keep only prefixes clearing the
    # fold floor so the Python side stays small regardless of object count, and
    # link them parent->child. No d1..d4 cap — the tree is as deep as the data.
    from disk_tree.tree_build import DirRow, build_tree

    attr_join_s = "LEFT JOIN dir_attr t ON t.bucket = s.bucket AND t.dir = s.dir" if attr else ""
    team_sel = "coalesce(t.team, 'unattributed')" if attr else "'unattributed'"
    user_sel = 't."user"' if attr else "CAST(NULL AS VARCHAR)"
    # Attribution is a join over the cached per-dir rollups — a few million
    # rows — never over objects. dir_agg also feeds the class-mix and
    # leaderboard, so those need no separate scan either.
    con.execute(
        f"""
        CREATE TEMP TABLE dir_agg AS
        SELECT s.fp, {team_sel} AS team, {user_sel} AS usr,
          s.b, s.o, s.wts, s.wb, s.c2, s.c3, s.c4, xa.aday AS a
        FROM dir_stats s {attr_join_s}
        LEFT JOIN access_agg xa ON xa.bucket = s.bucket AND xa.dir = s.dir
        """
    )
    total_b, total_o = con.execute("SELECT coalesce(sum(b), 0)::BIGINT, coalesce(sum(o), 0)::BIGINT FROM dir_agg").fetchone()
    total_b, total_o = int(total_b), int(total_o)
    maxseg = int(con.execute("SELECT coalesce(max(len(string_split(fp, '/'))), 1) FROM dir_agg").fetchone()[0])
    _rss("dir-agg")

    if attr:
        # class-mix + leaderboard straight off dir_agg (class 1/STANDARD =
        # total minus the non-standard classes we track explicitly).
        for team, usr, b, c2, c3, c4 in con.execute(
            "SELECT team, usr, sum(b), sum(c2), sum(c3), sum(c4) FROM dir_agg GROUP BY team, usr"
        ).fetchall():
            c1 = int(b) - int(c2 or 0) - int(c3 or 0) - int(c4 or 0)
            for cid, cv in ((1, c1), (2, c2), (3, c3), (4, c4)):
                if cv:
                    team_class[team][cid] += int(cv)
                    if usr:
                        user_class[usr][cid] += int(cv)
            if usr:
                user_bytes[(usr, team)] += int(b)

    # The full rolled-up (path, team, user) relation — every ancestor path,
    # descendant-inclusive, attributed, NO floor. Materialized because three
    # consumers share it: the floored tree query below, the (optional)
    # path-index artifact, and — via that artifact — the pixel-budget subtree
    # API (specs/path-index-lazy-drill.md).
    con.execute(
        f"""
        CREATE TEMP TABLE ptu AS
        WITH da AS (
          -- split computed here (streamed), not materialized into dir_agg —
          -- a LIST column on 150M rows is a ~40GB temp table by itself
          SELECT *, string_split(fp, '/') AS segs FROM dir_agg
        ),
        exploded AS (
          SELECT array_to_string(segs[1:r.k], '/') AS path, r.k AS depth,
            b, o, wts, wb, c2, c3, c4, a, team, usr
          FROM da, range(1, {maxseg} + 1) r(k)
          WHERE len(segs) >= r.k
        )
        SELECT path, depth, team, usr,
          sum(b)::BIGINT AS b, sum(o)::BIGINT AS o,
          sum(wts)::DOUBLE AS wts, sum(wb)::BIGINT AS wb,
          sum(c2)::BIGINT AS c2, sum(c3)::BIGINT AS c3, sum(c4)::BIGINT AS c4,
          max(a) AS a  -- subtree-max last-read epoch day (NULL = never read)
        FROM exploded GROUP BY path, depth, team, usr
        """
    )
    _rss("ptu")
    if path_index is not None:
        # The complete, floor-free index the subtree API serves: one row per
        # (path, team, usr), sorted (depth, path) — the engine's canonical
        # order for prefix-range + row-group pruning. Immutable per date.
        path_index.parent.mkdir(parents=True, exist_ok=True)
        con.execute(
            f"COPY (SELECT path, depth, team, usr, b, o, wts, wb, c2, c3, c4, a "
            f"FROM ptu ORDER BY depth, path) TO '{path_index}' "
            # ~8k rows/group (~1 MB): a deep, narrow subtree drill reads ~1 MB
            # per level instead of a 7 MB 64k-row group (specs/path-agnostic-serving.md §2.1).
            "(FORMAT parquet, ROW_GROUP_SIZE 8192)"
        )
        err(f"path-index: wrote {path_index}")
        _rss("path-index")

    # Pre-floor is **parent-relative** (matches build_tree): keep a path iff its
    # rolled-up bytes clear MIN_FRAC of its parent's — so drilling stays useful
    # at every depth (a fleet-relative cut deletes every small-but-drillable
    # child everywhere; see build_tree's docstring for the grug regression).
    # Staged (not one statement): each big operator — the per-path totals agg,
    # then the ranking window — runs alone, so peak memory is one operator's
    # working set instead of a stacked pipeline. The one-statement version put
    # the whole stack on top of DuckDB's cap and got the container kernel-
    # OOM-killed (exit 137) on the daily's 128GB node, 2026-08-27.
    con.execute("CREATE TEMP TABLE tot AS SELECT path, sum(b) AS pb FROM ptu GROUP BY path")
    _rss("tot")
    con.execute(
        f"""
        CREATE TEMP TABLE keep AS
        WITH ranked AS (
          SELECT t.path, t.pb, par.pb AS parent_pb,
            row_number() OVER (
              PARTITION BY CASE WHEN t.path LIKE '%/%' THEN regexp_replace(t.path, '/[^/]*$', '') END
              ORDER BY t.pb DESC
            ) AS rk
          FROM tot t
          LEFT JOIN tot par
            ON par.path = CASE WHEN t.path LIKE '%/%' THEN regexp_replace(t.path, '/[^/]*$', '') END
        )
        SELECT path FROM ranked
        WHERE parent_pb IS NULL
           OR (pb >= greatest({MIN_FRAC} * parent_pb, {ABS_FLOOR}) AND rk <= {TOP_K})
        """
    )
    con.execute("DROP TABLE tot")
    _rss("keep")
    ptu_rows = con.execute(
        """
        SELECT p.path, p.team, p.usr, p.b, p.o, p.wts, p.wb, p.c2, p.c3, p.c4
        FROM ptu p JOIN keep k USING (path)
        """
    ).fetchall()
    con.execute("DROP TABLE keep")
    con.execute("DROP TABLE ptu")
    # A path can clear its own parent while an ancestor failed (thin chains) —
    # prune anything whose ancestry isn't fully kept, else build_tree would
    # silently orphan it.
    kept_paths = {p for p, *_ in ptu_rows}
    def _rooted(path: str) -> bool:
        while "/" in path:
            path = path.rsplit("/", 1)[0]
            if path not in kept_paths:
                return False
        return True
    rooted = {p for p in kept_paths if _rooted(p)}
    if len(rooted) < len(kept_paths):
        err(f"pruned {len(kept_paths) - len(rooted)} orphaned sub-floor-ancestry paths")
        ptu_rows = [r for r in ptu_rows if r[0] in rooted]
    _rss("dir-rows")

    def _new_add() -> dict:
        return {"b": 0, "o": 0, "wts": 0.0, "wb": 0,
                "cb": defaultdict(int), "tm": defaultdict(int),
                "ub": defaultdict(int), "sh": defaultdict(int)}

    def _merge(a: dict, b: int, o: int, wts, wb, c2, c3, c4, team: str, usr) -> None:
        a["b"] += int(b); a["o"] += int(o)
        a["wts"] += float(wts or 0); a["wb"] += int(wb or 0)
        for cid, cv in (("2", c2), ("3", c3), ("4", c4)):
            if cv:
                a["cb"][cid] += int(cv)
        a["tm"][team] += int(b)
        if usr:
            a["ub"][usr] += int(b)
        elif team != "unattributed":
            a["sh"][team] += int(b)

    def _add(a: dict) -> dict:
        out: dict = {}
        if a["wb"]:
            out["wts"], out["wb"] = a["wts"], a["wb"]
        for k in ("cb", "tm", "ub", "sh"):
            if a[k]:
                out[k] = dict(a[k])
        return out

    agg_by_path: dict[str, dict] = {}
    root = _new_add()
    for path, team, usr, b, o, wts, wb, c2, c3, c4 in ptu_rows:
        a = agg_by_path.get(path)
        if a is None:
            a = agg_by_path[path] = _new_add()
        _merge(a, b, o, wts, wb, c2, c3, c4, team, usr)
        if "/" not in path:  # bucket-level rows partition the fleet → the root
            _merge(root, b, o, wts, wb, c2, c3, c4, team, usr)

    dir_rows = [DirRow(p, a["b"], a["o"], _add(a)) for p, a in agg_by_path.items()]
    dir_rows.append(DirRow(".", total_b, total_o, _add(root)))
    tree = build_tree(dir_rows, total_b, MIN_FRAC, abs_floor=ABS_FLOOR, max_children=TOP_K)
    tree["n"] = "marin GCS"
    roots = tree.get("c", [])

    # Decorate the built tree with per-node read-recency/volume from `amap`
    # (built above with access_agg). `a` = MAX(last_ts) over GET/HEAD/LIST
    # (the deletion veto); `ro`/`rb` = GET/HEAD counts/bytes.
    if access:
        def _attach_access(node: dict, key: str) -> None:
            hit = amap.get(key)
            if hit is not None:
                a, ro, rb = hit
                node["a"] = a
                if ro:  # omit zero-read nodes entirely (LIST-only prefixes)
                    node["ro"], node["rb"] = ro, rb
            for c in node.get("c") or []:
                if not c["n"].startswith("("):
                    _attach_access(c, f"{key}/{c['n']}" if key else c["n"])

        for root_node in roots:
            _attach_access(root_node, root_node["n"])
        root_as = [n["a"] for n in roots if "a" in n]
        if root_as:
            tree["a"] = max(root_as)
        root_ro = sum(n.get("ro", 0) for n in roots)
        if root_ro:
            tree["ro"] = root_ro
            tree["rb"] = sum(n.get("rb", 0) for n in roots)

    # Age strata also carry `a` — the dir's last-read epoch day from the access
    # agg (subtree MAX, the same semantics as the tree's `a`) — so the site can
    # color a vintage by whether anyone has touched it since logging began.
    # Absent = no read observed. Multiplies rows by at most the number of
    # distinct read days (a few weeks of logs), not by dirs.
    if attr:
        # (day, d1, team, user, a) strata: the cached per-(day, dir) rollup
        # joined to the same dir attribution. Day keys are epoch days; the site
        # aggregates to day/week/month.
        age = con.execute(
            """
            SELECT d.day,
              CASE WHEN d.dir = '' THEN '(files)' ELSE regexp_extract(d.dir, '^([^/]+)', 1) END AS d1,
              coalesce(t.team, 'unattributed') AS team, t."user" AS user, x.aday AS a,
              sum(d.bytes)::BIGINT AS bytes, sum(d.objects)::BIGINT AS objects
            FROM age_days d
            LEFT JOIN dir_attr t ON t.bucket = d.bucket AND t.dir = d.dir
            LEFT JOIN access_agg x ON x.bucket = d.bucket AND x.dir = d.dir
            GROUP BY ALL ORDER BY ALL  -- fully deterministic output order (byte-identical reruns)
            """
        ).fetchall()
        age_rows = [
            {"d": day, "d1": d1, "t": t, **({"u": u} if u else {}), **({"a": a} if a is not None else {}), "b": b, "o": o}
            for day, d1, t, u, a, b, o in age
        ]
        _rss("age")
    else:
        age = con.execute(
            """
            SELECT d.day,
              CASE WHEN d.dir = '' THEN NULL ELSE regexp_extract(d.dir, '^([^/]+)', 1) END AS d1,
              x.aday AS a,
              sum(d.bytes)::BIGINT AS bytes, sum(d.objects)::BIGINT AS objects
            FROM age_days d LEFT JOIN access_agg x ON x.bucket = d.bucket AND x.dir = d.dir
            GROUP BY ALL ORDER BY ALL
            """
        ).fetchall()
        age_rows = [
            {"d": day, "d1": d1 or "(files)", **({"a": a} if a is not None else {}), "b": b, "o": o}
            for day, d1, a, b, o in age
        ]

    # Storage-class mix from the dir rollups (class 1/STANDARD = total minus
    # the explicitly-tracked classes — same derivation the team mix uses).
    s_b, s_c2, s_c3, s_c4 = con.execute(
        "SELECT coalesce(sum(b), 0)::BIGINT, coalesce(sum(c2), 0)::BIGINT,"
        " coalesce(sum(c3), 0)::BIGINT, coalesce(sum(c4), 0)::BIGINT FROM dir_stats"
    ).fetchone()
    classes = [(cid, cb) for cid, cb in ((1, int(s_b) - int(s_c2) - int(s_c3) - int(s_c4)), (2, int(s_c2)), (3, int(s_c3)), (4, int(s_c4))) if cb]

    meta = {
        "asof": asof,
        "generated": dt.date.today().isoformat(),
        "total_bytes": total_b,
        "total_objects": total_o,
        "class_bytes": {int(c): int(b) for c, b in classes},
        # Fold floor as a fraction of each PARENT's bytes — children below it
        # live under that parent's expandable (other) node.
        "fold_min_frac": MIN_FRAC,
    }
    if access_window:
        # Epoch days the access logs cover — the UI's "no reads since <from>"
        # is only meaningful relative to when logging began.
        meta["access"] = {"from": access_window[0], "to": access_window[1]}
    if attr:
        meta["users"] = [
            {"u": u, "t": t, "b": b}
            for (u, t), b in sorted(user_bytes.items(), key=lambda kv: -kv[1])
        ]
        meta["team_class_bytes"] = {t: dict(sorted(c.items())) for t, c in sorted(team_class.items())}
        meta["user_class_bytes"] = {u: dict(sorted(c.items())) for u, c in sorted(user_class.items())}

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "tree.json").write_text(json.dumps(tree, separators=(",", ":")) + "\n")
    (out_dir / "age.json").write_text(json.dumps(age_rows, separators=(",", ":")) + "\n")
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return meta
