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

MIN_FRAC = 0.0002  # fold dirs below this fraction of total bytes into "(other)"


def write_webdata(
    listings: tuple[str, ...],
    out_dir: Path,
    asof: str,
    attributions: tuple[str, ...] = (),
    identities_path: Path | None = None,
    access: tuple[str, ...] = (),
) -> dict:
    """Write tree.json / age.json / meta.json under ``out_dir``; returns meta.

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
    if attr:
        import pandas as pd

        from .identity import DEFAULT_IDENTITIES, load_identities
        from .prefixes import load_prefix_map

        identities = load_identities(identities_path or DEFAULT_IDENTITIES)
        _rss("start")
        by_prefix = load_prefix_map(con, attributions, identities, src)
        _rss("prefix-map")
        pfx_df = pd.DataFrame(
            [
                {"key": k.removeprefix("gs://").rstrip("/"), "user": u, "team": t}
                for k, (u, t, _source) in by_prefix.items()
            ]
        )
        pfx_df["depth"] = pfx_df["key"].str.count("/") + 1
        maxd = int(pfx_df["depth"].max()) if len(pfx_df) else 1
        con.register("pfx", pfx_df)
        # deepest-prefix-wins for every distinct dir, entirely in SQL: explode
        # each dir key into its ancestors (up to the deepest attribution
        # prefix), equi-join, keep the deepest match. Replaces the
        # single-threaded python walk that OOMed the 32GiB Cloud Run job and
        # dominated webdata wall clock.
        con.execute(
            f"""
            CREATE TEMP TABLE dir_attr AS
            WITH dirs AS (
              SELECT DISTINCT bucket,
                CASE WHEN name LIKE '%/%' THEN regexp_replace(name, '/[^/]*$', '') ELSE '' END AS dir
              FROM {src}
            ),
            keyed AS (
              SELECT bucket, dir,
                CASE WHEN dir = '' THEN bucket ELSE bucket || '/' || dir END AS dk
              FROM dirs
            ),
            cand AS (
              SELECT k.bucket, k.dir, k.dk, r.k AS depth,
                array_to_string((str_split(k.dk, '/'))[1:r.k], '/') AS anc
              FROM keyed k, range(1, {maxd} + 1) r(k)
              WHERE len(str_split(k.dk, '/')) >= r.k
            )
            -- arg_max instead of a row_number window: a hash aggregate streams,
            -- the window would sort the full exploded candidate set. The
            -- struct keeps the winning row's (user, team) together — separate
            -- arg_max calls would skip a deeper row's NULL user and mix rows.
            , won AS (
              SELECT c.bucket, c.dir,
                arg_max(struct_pack(u := p."user", t := p.team), c.depth) AS win
              FROM cand c JOIN pfx p ON p.key = c.anc
              GROUP BY c.bucket, c.dir
            )
            SELECT bucket, dir, win.u AS "user", win.t AS team FROM won
            """
        )
        _rss("dir_attr")
        # per-(team|user) storage-class byte mixes (site prices group roll-ups
        # with class-aware rates) + the per-(user,team) leaderboard meta.users
        # needs — both derived from `dir_agg` below, so no separate object scan.
        team_class: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        user_class: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        user_bytes: dict[tuple, int] = defaultdict(int)

    # --- arbitrary-depth tree (specs/tree-builder-unification.md) ---
    # Roll every object up to *all* its ancestor prefixes (descendant-inclusive
    # totals at every depth), attribute per dir, keep only prefixes clearing the
    # fold floor so the Python side stays small regardless of object count, and
    # link them parent->child. No d1..d4 cap — the tree is as deep as the data.
    from disk_tree.tree_build import DirRow, build_tree

    fp_dir = "CASE WHEN name LIKE '%/%' THEN regexp_replace(name, '/[^/]*$', '') ELSE '' END"
    fp = f"CASE WHEN ({fp_dir}) = '' THEN bucket ELSE bucket || '/' || ({fp_dir}) END"
    attr_join_s = "LEFT JOIN dir_attr t ON t.bucket = s.bucket AND t.dir = s.dir" if attr else ""
    team_sel = "coalesce(t.team, 'unattributed')" if attr else "'unattributed'"
    user_sel = 't."user"' if attr else "CAST(NULL AS VARCHAR)"
    # Aggregate objects to their *immediate* dir once (attributed, with class /
    # weighted-mtime sums and the split path segments), then explode these dir
    # rows — a few million — to every ancestor rather than exploding all 595M
    # objects. ~50-100x fewer rows through the explode. dir_agg also feeds the
    # class-mix and leaderboard, so those need no separate object scan.
    con.execute(
        f"""
        CREATE TEMP TABLE dir_agg AS
        WITH obj AS (
          SELECT bucket, {fp_dir} AS dir, size_bytes, created, storage_class_id, {fp} AS fp
          FROM {src}
        ),
        dir_stats AS (
          -- collapse 595M objects to a few million dirs FIRST; attribution is
          -- then a join over dirs, not over every object.
          SELECT bucket, dir, fp,
            sum(size_bytes)::BIGINT AS b, count(*)::BIGINT AS o,
            sum(CASE WHEN created IS NOT NULL THEN size_bytes * epoch(created) END)::DOUBLE AS wts,
            sum(CASE WHEN created IS NOT NULL THEN size_bytes END)::BIGINT AS wb,
            sum(CASE WHEN storage_class_id = 2 THEN size_bytes END)::BIGINT AS c2,
            sum(CASE WHEN storage_class_id = 3 THEN size_bytes END)::BIGINT AS c3,
            sum(CASE WHEN storage_class_id = 4 THEN size_bytes END)::BIGINT AS c4
          FROM obj GROUP BY bucket, dir, fp
        )
        SELECT s.fp, {team_sel} AS team, {user_sel} AS usr,
          s.b, s.o, s.wts, s.wb, s.c2, s.c3, s.c4, string_split(s.fp, '/') AS segs
        FROM dir_stats s {attr_join_s}
        """
    )
    total_b, total_o = con.execute("SELECT coalesce(sum(b), 0)::BIGINT, coalesce(sum(o), 0)::BIGINT FROM dir_agg").fetchone()
    total_b, total_o = int(total_b), int(total_o)
    floor = int(total_b * MIN_FRAC)
    maxseg = int(con.execute("SELECT coalesce(max(len(segs)), 1) FROM dir_agg").fetchone()[0])
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

    ptu_rows = con.execute(
        f"""
        WITH exploded AS (
          SELECT array_to_string(segs[1:r.k], '/') AS path, b, o, wts, wb, c2, c3, c4, team, usr
          FROM dir_agg, range(1, {maxseg} + 1) r(k)
          WHERE len(segs) >= r.k
        ),
        ptu AS (
          SELECT path, team, usr,
            sum(b)::BIGINT AS b, sum(o)::BIGINT AS o,
            sum(wts)::DOUBLE AS wts, sum(wb)::BIGINT AS wb,
            sum(c2)::BIGINT AS c2, sum(c3)::BIGINT AS c3, sum(c4)::BIGINT AS c4
          FROM exploded GROUP BY path, team, usr
        )
        SELECT p.path, p.team, p.usr, p.b, p.o, p.wts, p.wb, p.c2, p.c3, p.c4
        FROM ptu p
        WHERE p.path IN (SELECT path FROM ptu GROUP BY path HAVING sum(b) >= {floor})
        """
    ).fetchall()
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
    tree = build_tree(dir_rows, total_b, MIN_FRAC)
    tree["n"] = "marin GCS"
    roots = tree.get("c", [])

    # Access join: per-(bucket, path) read-recency + read volume from the
    # access-log layer-2a shards. The agg's own rollups mean a prefix's values
    # already cover everything under it — deeper than the tree's depth cap
    # included. Two op filters, because they answer different questions:
    #   `a`      MAX(last_ts) over GET/HEAD/LIST — "did anyone touch this at
    #            all", the deletion veto.
    #   `ro`/`rb` counts/bytes over GET/HEAD only — a bucket listing is not a
    #            read of the data. `rb / ro` is mean read size, which separates
    #            few-huge-sequential from millions-of-small-random (the
    #            ops-cost-heavy pattern).
    access_window: tuple[int, int] | None = None
    if access:
        globs = "[" + ", ".join(f"'{g}'" for g in access) + "]"
        amap: dict[str, tuple[int, int, int]] = {}
        for bucket, path, aday, ro, rb in con.execute(
            f"""
            SELECT bucket, CASE WHEN path = '.' THEN '' ELSE path END AS path,
              CAST(floor(epoch(MAX(last_ts)) / 86400) AS INTEGER) AS aday,
              COALESCE(SUM(n_ops) FILTER (WHERE op IN ('GET', 'HEAD')), 0) AS ro,
              COALESCE(SUM(bytes_out) FILTER (WHERE op IN ('GET', 'HEAD')), 0) AS rb
            FROM read_parquet({globs})
            WHERE op IN ('GET', 'HEAD', 'LIST')
            GROUP BY 1, 2
            """
        ).fetchall():
            amap[f"{bucket}/{path}" if path else bucket] = (aday, int(ro), int(rb))
        lo, hi = con.execute(
            f"SELECT CAST(floor(epoch(MIN(last_ts)) / 86400) AS INTEGER), "
            f"CAST(floor(epoch(MAX(last_ts)) / 86400) AS INTEGER) FROM read_parquet({globs})"
        ).fetchone()
        if lo is not None:
            access_window = (int(lo), int(hi))
        _rss("access")

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

    if attr:
        # (day, d1, team, user) strata via the same SQL attribution join.
        # Day keys are epoch days; the site aggregates to day/week/month.
        age = con.execute(
            f"""
            WITH d AS (
              SELECT CAST(floor(epoch(created) / 86400) AS INTEGER) AS day, bucket,
                CASE WHEN name LIKE '%/%' THEN regexp_replace(name, '/[^/]*$', '') ELSE '' END AS dir,
                size_bytes
              FROM {src}
              WHERE created IS NOT NULL
            )
            SELECT d.day,
              CASE WHEN d.dir = '' THEN '(files)' ELSE regexp_extract(d.dir, '^([^/]+)', 1) END AS d1,
              coalesce(t.team, 'unattributed') AS team, t."user" AS user,
              sum(d.size_bytes)::BIGINT AS bytes, count(*)::BIGINT AS objects
            FROM d LEFT JOIN dir_attr t ON t.bucket = d.bucket AND t.dir = d.dir
            GROUP BY ALL ORDER BY ALL  -- fully deterministic output order (byte-identical reruns)
            """
        ).fetchall()
        age_rows = [
            {"d": day, "d1": d1, "t": t, **({"u": u} if u else {}), "b": b, "o": o}
            for day, d1, t, u, b, o in age
        ]
        _rss("age")
    else:
        age = con.execute(
            f"""
            SELECT CAST(floor(epoch(created) / 86400) AS INTEGER) AS day,
              regexp_extract(name, '^([^/]+)/', 1) AS d1,
              sum(size_bytes)::BIGINT AS bytes, count(*)::BIGINT AS objects
            FROM {src}
            WHERE created IS NOT NULL
            GROUP BY ALL ORDER BY day
            """
        ).fetchall()
        age_rows = [
            {"d": day, "d1": d1 or "(files)", "b": b, "o": o} for day, d1, b, o in age
        ]

    classes = con.execute(
        f"""
        SELECT storage_class_id, sum(size_bytes)::BIGINT AS bytes
        FROM {src} GROUP BY ALL ORDER BY storage_class_id
        """
    ).fetchall()

    meta = {
        "asof": asof,
        "generated": dt.date.today().isoformat(),
        "total_bytes": total_b,
        "total_objects": total_o,
        "class_bytes": {int(c): int(b) for c, b in classes},
        # Fold floor as a fraction of total bytes — lets the UI say "showing
        # prefixes ≥ X"; dirs below it live under an expandable (other) node.
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
