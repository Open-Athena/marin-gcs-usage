"""Anchored name search over the static index (specs/anchored-search.md; options in specs/search-extensions.md §2):
`^q` (a name starts with `q`), `q$` (a name ends with `q`), `^q$` (a name is `q`), exact at any view path and scan.

A path matches when some segment (lowercased) matches, so the match roots under P are the paths under P whose name
matches and none of whose ancestor segments match. Each root is one version of one owner slice (the contains
literal's first hits).

Index, per tier (the base generation, and each run under `deltas/<id>/`), new keys beside the suffix shards:

- `names/` — the **name index**: one row per version (depth ≥ 1), `s` = `/` + the lowercase name, in the suffix
  shards' layout (`SX_SCHEMA`, sorted `(s, path, usr, vf)`, shards cut at three-character prefixes; `shards.json`,
  `sx/`, `sidecar/`, `sidecar.parquet`). `/` occurs in no segment: `^q` is the range `/q…`, `^q$` the key `/q`. A
  run's rows are its `cdelta`'s versions (opens, and close records with their final `vt`), so the tiers combine as
  the suffix rows do (equal `(s, path, usr, vf)`: the smallest `vt`).
- `anchors/` — rollups for the heavy `(k, dir)`: `k` an exact suffix (`end`, from `sx/`: `q$`'s rows are `s == q`)
  or `/`+name (`exact`, from `names/`), `dir` a directory (or `''`, the fleet root: children = buckets) with more
  than R rows of `k` at or under it (rows, not roots: the reader's dispatch bounds rows). Per child of `dir` the
  running Σ size, n_files of the first hits under it (`static_roots.ROLLUP_SCHEMA`; K kept children + an exact
  remainder; header `b` = rows under `dir`, `o` = children holding first hits), in the drill's two-level index
  (`{end,exact}-rollups-index[.top].parquet`). `meta.json` (written last) holds R, K, the shards' `rg` and, per
  rollup set, its row groups.

Rows are never copied out as roots files: `k`'s rows under P are the key range `[(k, P/), (k, P0))` of the shards
(sorted by path within `s`), bounded by whole row groups through the `s` and `path` column statistics.

A run's rollups (`run_rollups`): for each `(k, dir)` heavy before the run and touched by it, a delta header (`kind`
−1) and the cells dated at the run's scan (kept children frozen; a new child is kept while fewer than K are, else
summed into the remainder); for each that becomes heavy with the run (its rows over every tier pass R), a full
restatement (`kind` 0) from every tier's first hits under it. Heaviness is sticky and counted by stored rows (close
records included), as the reader's bound. Merged runs stack per `(k, dir)` (`merge_rollups`).
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from time import monotonic

import pyarrow as pa
import pyarrow.parquet as pq
from click import Choice, argument, group, option

from .static_catalog import PARENT
from .static_names import (
    CODEC, NAME, OPEN, PREFIX, SX_RG, _batches, _sx_cast, _task, connect, q, read_json,
)
from .static_profile import data_bucket, scratch_bucket

err = partial(print, file=sys.stderr, flush=True)

NAMES = "names"
ANCHORS = "anchors"
KINDS = ("end", "exact")
#: The read bound (the drill's R) and kept children per heavy directory.
R_DEFAULT = 100_000
K_DEFAULT = 256
#: The whole-range read bound of the light path: V plus two row groups (`staticFilter.ts` `MAX_ROWS`).
MAX_ROWS = 100_000 + 2 * 8192
#: Target rows per name-index shard (a three-character prefix is never split).
NAME_SHARD_ROWS = 50_000_000
#: Rows per rollup row group (the decode unit).
ROLLUP_RG = 2048


# ── Predicates ─────────────────────────────────────────────────────────────


def segment_matches(seg: str, text: str, mode: str) -> bool:
    return seg.startswith(text) if mode == "start" else seg.endswith(text) if mode == "end" else seg == text if mode == "exact" else text in seg


def parse_key(key: str) -> tuple[str, str | None]:
    """`termKey`'s inverse: `/q` → (q, start), `q/` → (q, end), `/q/` → (q, exact), else (key, None)."""
    start, end = key.startswith("/"), len(key) > 1 and key.endswith("/")
    text = key[1 if start else 0:len(key) - 1 if end else len(key)]
    return text, "exact" if start and end else "start" if start else "end" if end else None


def term_key(text: str, mode: str | None) -> str:
    return {"start": f"/{text}", "end": f"{text}/", "exact": f"/{text}/", None: text}[mode]


def first_hit_sql(mode: str, key: str = "s", par: str = "par") -> str:
    """The per-segment first-hit test of a row whose `key` column is the matched key (`q` for `end`, `/q` for
    `start`/`exact`) and whose lowercase parent is `par`: no ancestor segment matches."""
    if mode == "end":
        return f"NOT contains({par} || '/', {key} || '/')"
    if mode == "exact":
        return f"NOT contains('/' || {par} || '/', {key} || '/')"
    raise ValueError(mode)


# ── The name index ─────────────────────────────────────────────────────────


def names_sql(versions_sql: str) -> str:
    """Name-index rows `(s, depth, path, usr, vf, vt, size, n_files)` (epoch seconds) of versions (depth ≥ 1)."""
    return f"""SELECT '/' || {NAME} AS s, depth, path, usr, vf, vt, size, n_files FROM ({versions_sql}) WHERE depth >= 1"""


def write_names(con, versions_sql: str, out: Path, target_rows: int = NAME_SHARD_ROWS) -> dict:
    """The name index of `versions_sql`'s versions (coalesced intervals, or a run's `cdelta`) under `out` (`names/`):
    sorted `(s, path, usr, vf)` by DuckDB, cut into shards (`static_append.write_run_shards`)."""
    from .static_append import write_run_shards

    sql = f"SELECT * FROM ({names_sql(versions_sql)}) ORDER BY s, path, usr, vf"
    return write_run_shards((_sx_cast(b) for b in _batches(con, sql)), out, target_rows)


def files_sql(files: list[str], cols: str = "*") -> str:
    return f"SELECT {cols} FROM read_parquet([{', '.join(q(f) for f in files)}])"


# ── Rollups ────────────────────────────────────────────────────────────────


#: Rows `(k, depth, path, usr, vf, vt, size, n_files, par)` (epoch seconds; `par` the lowercase parent).
AR_COLS = "k VARCHAR, depth UTINYINT, path VARCHAR, usr VARCHAR, vf BIGINT, vt BIGINT, size BIGINT, n_files BIGINT, par VARCHAR"


def shard_rows_sql(sx_files: list[str], where: str = "true") -> str:
    """A shard set's rows as `AR_COLS` (`k` = `s`; timestamps → epoch seconds)."""
    return f"""SELECT s AS k, depth, path, usr, epoch(vf)::BIGINT AS vf, epoch(vt)::BIGINT AS vt, size, n_files, {PARENT} AS par
        FROM ({files_sql(sx_files)}) WHERE depth >= 1 AND ({where})"""


def heavy_dirs(con, rows: str, R: int) -> None:
    """`hv(k, lvl, dir, rows)`: every `(k, dir)` with more than `R` rows of `rows` (a table of `AR_COLS`) at or under
    it — `dir` `''` (lvl 0, the fleet root) or a directory at depth `lvl` — bottom-up over the depths."""
    con.execute("CREATE OR REPLACE TABLE hv (k VARCHAR, lvl INTEGER, dir VARCHAR, rows BIGINT)")
    con.execute(f"CREATE OR REPLACE TABLE hcnt AS SELECT k, depth, path, count(*)::BIGINT AS n FROM {rows} GROUP BY ALL")
    maxd = con.execute("SELECT coalesce(max(depth), 0) FROM hcnt").fetchone()[0]
    con.execute("CREATE OR REPLACE TABLE hcur (k VARCHAR, dir VARCHAR, n BIGINT)")
    for lvl in range(maxd - 1, 0, -1):
        con.execute(f"""CREATE OR REPLACE TABLE hnext AS SELECT k, left(x, length(x) - length(string_split(x, '/')[-1]) - 1) AS dir, sum(n)::BIGINT AS n
            FROM (SELECT k, path AS x, n FROM hcnt WHERE depth = {lvl + 1} UNION ALL SELECT k, dir, n FROM hcur) GROUP BY ALL""")
        con.execute(f"INSERT INTO hv SELECT k, {lvl}, dir, n FROM hnext WHERE n > {R}")
        con.execute("CREATE OR REPLACE TABLE hcur AS SELECT * FROM hnext")
    con.execute(f"INSERT INTO hv SELECT k, 0, '', n FROM (SELECT k, sum(n)::BIGINT AS n FROM hcnt GROUP BY k) WHERE n > {R}")
    for t in ("hcnt", "hcur", "hnext"):
        con.execute(f"DROP TABLE IF EXISTS {t}")


def rollup_cells(con, roots: str, heads: str, K: int, into: str, closed: bool = False) -> None:
    """Full rollups (`static_roots.ROLLUP_SCHEMA` rows, header first) into table `into` for `heads(k, lvl, dir, rows)`
    from `roots` (`AR_COLS`, every first hit under them): per child of `dir` (its next segment; a bucket for `''`)
    the running Σ size, n_files of the roots at or under it, `+` at `vf` and `−` at `vt`, one cell per change; the K
    children with the largest peak bytes (ties by name) kept, the rest summed into the remainder (`kind` 2). Header
    (`kind` 0): `vf` = kept, `b` = `rows`, `o` = children holding roots. `closed`: the heads are closed upwards (a
    head's parent directory is one), so each level's roots are a subset of the last's."""
    con.execute(f"CREATE TABLE IF NOT EXISTS {into} (q VARCHAR, dir VARCHAR, kind TINYINT, child VARCHAR, vf BIGINT, b BIGINT, o BIGINT)")
    con.execute("CREATE OR REPLACE TABLE rev (k VARCHAR, dir VARCHAR, child VARCHAR, t BIGINT, db HUGEINT, dn HUGEINT)")
    top = con.execute(f"SELECT coalesce(max(lvl), -1) FROM {heads}").fetchone()[0]
    con.execute(f"CREATE OR REPLACE TABLE hc AS SELECT k, string_split(path, '/') AS segs, vf, vt, size, n_files FROM {roots} SEMI JOIN (SELECT DISTINCT k FROM {heads}) USING (k)")
    for lvl in range(0, top + 1):
        con.execute(f"""CREATE OR REPLACE TABLE hl AS SELECT hc.* FROM hc SEMI JOIN (SELECT k, dir FROM {heads} WHERE lvl = {lvl}) AS h
            ON hc.k = h.k AND array_to_string(hc.segs[1:{lvl}], '/') = h.dir AND len(hc.segs) > {lvl}""")
        if closed:
            con.execute("CREATE OR REPLACE TABLE hc AS SELECT * FROM hl")
        hit = f"SELECT k, array_to_string(segs[1:{lvl}], '/') AS dir, segs[{lvl + 1}] AS child, vf, vt, size, n_files FROM hl"
        con.execute(f"""INSERT INTO rev SELECT k, dir, child, t, sum(db), sum(dn) FROM (
                SELECT k, dir, child, vf AS t, size AS db, n_files AS dn FROM ({hit})
                UNION ALL SELECT k, dir, child, vt AS t, -size, -n_files FROM ({hit}) WHERE vt <> {OPEN}
            ) GROUP BY k, dir, child, t""")
    con.execute("DROP TABLE IF EXISTS hc; DROP TABLE IF EXISTS hl")
    run = """sum(db) OVER (PARTITION BY k, dir, child ORDER BY t ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS b"""
    con.execute(f"""CREATE OR REPLACE TABLE rkeep AS SELECT k, dir, child FROM (
            SELECT k, dir, child, row_number() OVER (PARTITION BY k, dir ORDER BY peak DESC, child) AS r FROM (
                SELECT k, dir, child, max(b) AS peak FROM (SELECT k, dir, child, {run} FROM rev) GROUP BY k, dir, child))
        WHERE r <= {K}""")
    con.execute("""CREATE OR REPLACE TABLE rev2 AS
            SELECT k, dir, 1::TINYINT AS kind, child, t, db, dn FROM rev SEMI JOIN rkeep USING (k, dir, child)
            UNION ALL SELECT k, dir, 2::TINYINT, '', t, sum(db), sum(dn) FROM rev ANTI JOIN rkeep USING (k, dir, child) GROUP BY k, dir, t""")
    con.execute(f"""INSERT INTO {into}
        SELECT h.k, h.dir, 0, '', (SELECT count(*) FROM rkeep AS r WHERE r.k = h.k AND r.dir = h.dir),
            h.rows, (SELECT count(DISTINCT child) FROM rev AS e WHERE e.k = h.k AND e.dir = h.dir) FROM {heads} AS h""")
    con.execute(f"""INSERT INTO {into} SELECT k, dir, kind, child, t,
            sum(db) OVER (PARTITION BY k, dir, kind, child ORDER BY t ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)::BIGINT,
            sum(dn) OVER (PARTITION BY k, dir, kind, child ORDER BY t ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)::BIGINT
        FROM (SELECT k, dir, kind, child, t, sum(db) AS db, sum(dn) AS dn FROM rev2 GROUP BY ALL HAVING sum(db) <> 0 OR sum(dn) <> 0)""")
    for t in ("rev", "rev2", "rkeep"):
        con.execute(f"DROP TABLE IF EXISTS {t}")


def base_rollups(con, files: list[str], mode: str, R: int, K: int, into: str) -> dict:
    """A base shard set's rollups (`mode` `end` over suffix shards, `exact` over name shards) into table `into`: the
    keys holding more than R rows, their heavy directories (`heavy_dirs`) and the rollups of their first hits
    (`rollup_cells`)."""
    con.execute(f"CREATE OR REPLACE TABLE hk AS SELECT s AS k FROM ({files_sql(files, 's')}) GROUP BY s HAVING count(*) > {R}")
    con.execute(f"CREATE OR REPLACE TABLE ar AS SELECT x.* FROM ({shard_rows_sql(files)}) AS x SEMI JOIN hk USING (k)")
    heavy_dirs(con, "ar", R)
    con.execute(f"CREATE OR REPLACE TABLE rt AS SELECT * FROM ar WHERE {first_hit_sql(mode, 'k')}")
    con.execute("DROP TABLE ar; DROP TABLE hk")
    rollup_cells(con, "rt", "hv", K, into, closed=True)
    keys, dirs = con.execute("SELECT count(DISTINCT k), count(*) FROM hv").fetchone()
    for t in ("ar", "rt", "hv"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    return {"keys": keys, "heavy_dirs": dirs}


def write_rollups(con, table: str, out: Path, name: str) -> tuple[int, pa.Table]:
    """Table `table`'s rollup rows sorted `(q, dir, kind, child, vf)` → `out/<name>` with its group index (`file` =
    `name`)."""
    from .static_roots import ROLLUP_SCHEMA, write_indexed
    from . import static_roots as sr

    rg, sr.ROOT_RG = sr.ROOT_RG, ROLLUP_RG
    try:
        return write_indexed(_batches(con, f"SELECT q, dir, kind, child, vf, b, o FROM {table} ORDER BY q, dir, kind, child, vf"),
                             out / name, ROLLUP_SCHEMA, "dir", name, ["q", "dir", "child"])
    finally:
        sr.ROOT_RG = rg


def write_index(parts: list[pa.Table], out: Path, kind: str) -> dict:
    """A rollup set's per-file group indexes concatenated (sorted by first key) → `out/<kind>-rollups-index.parquet`
    and its top."""
    from .static_roots import GROUP_INDEX_SCHEMA, write_index_levels

    t = pa.concat_tables(parts) if parts else GROUP_INDEX_SCHEMA.empty_table()
    t = t.sort_by([("q_min", "ascending"), ("k_min", "ascending")])
    top = write_index_levels(t, out / f"{kind}-rollups-index.parquet")
    pq.write_table(top, out / f"{kind}-rollups-index.top.parquet", compression=CODEC)
    return {"row_groups": t.num_rows, "rows": int(pa.compute.sum(t.column("rows")).as_py() or 0) if t.num_rows else 0,
            "files": len(set(t.column("file").to_pylist())) if t.num_rows else 0}


def anchors_meta(R: int, K: int, sets: dict, scans: list[str] | None = None, **extra) -> dict:
    from .static_roots import IDX_RG

    return {"R": R, "K": K, "rg": SX_RG, "idx_rg": IDX_RG, "dispatch_slack_groups": 4, **{f"{k}_rollups": v for k, v in sets.items()},
            **({"scans": scans} if scans is not None else {}), **extra}


def build_tier_local(con, sx_files: list[str], names_files: list[str], out: Path, R: int, K: int, scans: list[str] | None = None) -> dict:
    """A whole tier's `anchors/` locally from its shard files (the base path in tests; on Batch per shard)."""
    out.mkdir(parents=True, exist_ok=True)
    sets, docs = {}, {}
    for kind, files in (("end", sx_files), ("exact", names_files)):
        con.execute("DROP TABLE IF EXISTS cells")
        parts = []
        docs[kind] = base_rollups(con, files, kind, R, K, "cells") if files else {}
        con.execute("CREATE TABLE IF NOT EXISTS cells (q VARCHAR, dir VARCHAR, kind TINYINT, child VARCHAR, vf BIGINT, b BIGINT, o BIGINT)")
        _, idx = write_rollups(con, "cells", out, f"rollups/{kind}-s0000.parquet")
        parts.append(idx)
        sets[kind] = write_index(parts, out, kind)
    (out / "meta.json").write_text(json.dumps(anchors_meta(R, K, sets, scans), indent=1) + "\n")
    return {"sets": sets, "docs": docs}


# ── Runs ───────────────────────────────────────────────────────────────────


@dataclass
class Tier:
    """A tier's local (or mounted) directory: `sx/` + `sidecar.parquet`, `names/`, `anchors/`; `keys`: its shards'
    per-group key bounds by kind (`keys_table`), when precomputed (the base's `anchors/keys-<kind>.parquet`)."""
    root: Path
    keys: dict[str, str] = field(default_factory=dict)

    def shard_dir(self, kind: str) -> Path:
        return self.root if kind == "end" else self.root / NAMES

    def files(self, kind: str) -> list[str]:
        return [str(f) for f in sorted((self.shard_dir(kind) / "sx").glob("*.parquet"))]

    def rollup_files(self, kind: str) -> list[str]:
        return [str(f) for f in sorted((self.root / ANCHORS / "rollups").glob(f"{kind}-*.parquet"))]

    def shard_file(self, kind: str, k: str) -> str | None:
        """The shard file holding key `k` (its first three characters in a shard's `[lo, hi)`), else None."""
        plan = self.shard_dir(kind) / "shards.json"
        if not plan.exists():
            return None
        p3 = k[:3]
        for sh in json.loads(plan.read_text())["shards"]:
            if sh["lo"] <= p3 and (sh["hi"] is None or p3 < sh["hi"]):
                return str(self.shard_dir(kind) / "sx" / f"s{sh['i']:04d}.parquet")
        return None


KEYS_SCHEMA = pa.schema([
    pa.field("file", pa.string(), nullable=False),
    pa.field("rg", pa.int32(), nullable=False),
    pa.field("s_min", pa.string(), nullable=False),
    pa.field("s_max", pa.string(), nullable=False),
    pa.field("p_min", pa.string(), nullable=True),
    pa.field("p_max", pa.string(), nullable=True),
    pa.field("rows", pa.int64(), nullable=False),
])


def keys_table(files: list[str], names: list[str] | None = None) -> pa.Table:
    """Per row group of shard `files`: its `s` and `path` bounds (column statistics, untruncated as written) and rows
    — the reader's `PathKeys` plus the group index, for the run builder's bounds."""
    cols = {k: [] for k in KEYS_SCHEMA.names}
    for f, name in zip(files, names or files):
        md = pq.ParquetFile(f).metadata
        si, pi = md.schema.names.index("s"), md.schema.names.index("path")
        for g in range(md.num_row_groups):
            rg = md.row_group(g)
            ss, ps = rg.column(si).statistics, rg.column(pi).statistics
            if ss is None or not ss.has_min_max:
                raise RuntimeError(f"{f} rg {g}: no `s` statistics")
            p_ok = ps is not None and ps.has_min_max
            for k, v in zip(KEYS_SCHEMA.names, (name, g, ss.min, ss.max, ps.min if p_ok else None, ps.max if p_ok else None, rg.num_rows)):
                cols[k].append(v)
    return pa.table(cols, schema=KEYS_SCHEMA)


def _level_dirs(src: str) -> str:
    """`(k, lvl, dir, path, …)` of every row of `src` at every level above it: `''` (lvl 0) and each proper ancestor."""
    return f"""SELECT *, CASE WHEN lvl = 0 THEN '' ELSE array_to_string(string_split(path, '/')[1:lvl], '/') END AS dir
        FROM (SELECT *, unnest(range(0, depth)) AS lvl FROM {src})"""


def _child(path: str, dir_: str) -> str:
    return f"CASE WHEN {dir_} = '' THEN string_split({path}, '/')[1] ELSE string_split(substring({path}, length({dir_}) + 2), '/')[1] END"


def run_rollups(con, prior: list[Tier], run: Tier, D: int, mode: str, R: int, K: int, into: str) -> dict:
    """One level-0 run's rollups of `mode` at scan `D` (epoch seconds) into table `into` (see the module doc).
    `prior`: the tiers before the run, base first, each with its `anchors/`; `run`: the run's shards."""
    con.execute(f"CREATE TABLE IF NOT EXISTS {into} (q VARCHAR, dir VARCHAR, kind TINYINT, child VARCHAR, vf BIGINT, b BIGINT, o BIGINT)")
    run_files = run.files(mode)
    doc = {"keys": 0, "delta": 0, "new_heavy": 0, "probes": 0, "probe_rows": 0}
    if not run_files:
        return doc
    con.execute(f"CREATE OR REPLACE TABLE rr AS {shard_rows_sql(run_files)}")
    # 1. Keys whose rows over every tier can pass R: the run's count plus each prior tier's bound — its groups from the
    # first with `s_max ≥ k` to the last with `s_min ≤ k` (cumulative rows; ties collapsed to their last group).
    con.execute("CREATE OR REPLACE TABLE rk AS SELECT k, count(*)::BIGINT AS n FROM rr GROUP BY k")
    con.execute("CREATE OR REPLACE TABLE pb (k VARCHAR, n BIGINT)")
    for t in prior:
        side = t.shard_dir(mode) / "sidecar.parquet"
        if not side.exists():
            continue
        con.execute(f"""CREATE OR REPLACE TABLE sc AS SELECT s_min, s_max,
                sum(rows) OVER (ORDER BY s_min, s_max, file, rg ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cum
            FROM read_parquet({q(str(side))})""")
        con.execute("CREATE OR REPLACE TABLE sca AS SELECT s_min AS x, max(cum) AS cum FROM sc GROUP BY s_min")
        con.execute("CREATE OR REPLACE TABLE scb AS SELECT s_max AS x, max(cum) AS cum FROM sc GROUP BY s_max")
        con.execute("""INSERT INTO pb SELECT rk.k, coalesce(a.cum, 0) - coalesce(b.cum, 0)
            FROM rk ASOF LEFT JOIN sca AS a ON rk.k >= a.x ASOF LEFT JOIN scb AS b ON rk.k > b.x""")
    con.execute(f"""CREATE OR REPLACE TABLE ck AS SELECT k FROM (SELECT k, sum(n) AS n FROM (SELECT k, n FROM rk UNION ALL SELECT k, n FROM pb) GROUP BY k)
        WHERE n > {R}""")
    if not con.execute("SELECT count(*) FROM ck").fetchone()[0]:
        return doc
    con.execute("CREATE OR REPLACE TABLE rr AS SELECT * FROM rr SEMI JOIN ck USING (k)")
    con.execute(f"CREATE OR REPLACE TABLE rd AS SELECT k, lvl, dir, count(*)::BIGINT AS n FROM ({_level_dirs('rr')}) GROUP BY ALL")
    # 2. The prior stacks of the touched (k, dir): every tier from the newest down to the newest full header.
    con.execute("CREATE OR REPLACE TABLE pr (t INTEGER, q VARCHAR, dir VARCHAR, kind TINYINT, child VARCHAR, vf BIGINT, b BIGINT, o BIGINT)")
    for i, t in enumerate(prior):
        fs = t.rollup_files(mode)
        if fs:
            con.execute(f"""INSERT INTO pr SELECT {i}, x.q, x.dir, x.kind, x.child, x.vf, x.b, x.o FROM ({files_sql(fs)}) AS x
                SEMI JOIN rd ON x.q = rd.k AND x.dir = rd.dir""")
    stack_rows(con, "pr", "ps")
    # 3. Newly heavy: (k, dir) not heavy before whose prior rows plus the run's pass R. The prior rows are bounded by the
    # tiers' groups meeting `[(k, dir/), (k, dir0))` (path keys); only where that bound can pass R are they counted.
    con.execute("""CREATE OR REPLACE TABLE cand AS SELECT rd.* FROM rd ANTI JOIN (SELECT DISTINCT q, dir FROM ps) AS h ON rd.k = h.q AND rd.dir = h.dir""")
    con.execute("CREATE OR REPLACE TABLE kt (t INTEGER, file VARCHAR, rg INTEGER, s_min VARCHAR, s_max VARCHAR, p_min VARCHAR, p_max VARCHAR, rows BIGINT)")
    for i, t in enumerate(prior):
        src = t.keys.get(mode)
        if src:
            con.execute(f"INSERT INTO kt SELECT {i}, file, rg, s_min, s_max, p_min, p_max, rows FROM read_parquet({q(src)})")
        elif t.files(mode):
            con.register("kt_in", keys_table(t.files(mode)))
            con.execute(f"INSERT INTO kt SELECT {i}, * FROM kt_in")
            con.unregister("kt_in")
    con.execute("""CREATE OR REPLACE TABLE ktk AS SELECT ck.k, kt.* FROM ck JOIN kt ON kt.s_min <= ck.k AND ck.k <= kt.s_max""")
    con.execute(f"""CREATE OR REPLACE TABLE near AS SELECT c.k, c.lvl, c.dir, c.n, coalesce(sum(g.rows), 0)::BIGINT AS bound
        FROM cand AS c LEFT JOIN ktk AS g ON g.k = c.k AND (c.dir = '' OR ((g.p_max IS NULL OR g.p_max >= c.dir || '/') AND (g.p_min IS NULL OR g.p_min < c.dir || '0')))
        GROUP BY c.k, c.lvl, c.dir, c.n HAVING c.n + coalesce(sum(g.rows), 0) > {R}""")
    # The prior rows of each near (k, dir), exactly: per key one pushdown read of the tiers' files holding it.
    con.execute("CREATE OR REPLACE TABLE prows AS SELECT * FROM rr WHERE false")
    for (k,) in con.execute("SELECT DISTINCT k FROM near ORDER BY k").fetchall():
        dirs = [d for (d,) in con.execute("SELECT dir FROM near WHERE k = ?", [k]).fetchall()]
        rng = "true" if "" in dirs else " OR ".join(f"(path >= {q(d + '/')} AND path < {q(d + '0')})" for d in dirs)
        files = [f for f in (t.shard_file(mode, k) for t in prior) if f]
        if files:
            con.execute(f"INSERT INTO prows {shard_rows_sql(files, f's = {q(k)} AND ({rng})')}")
        doc["probes"] += 1
    doc["probe_rows"] = con.execute("SELECT count(*) FROM prows").fetchone()[0]
    con.execute(f"""CREATE OR REPLACE TABLE pv AS SELECT k, any_value(depth) AS depth, path, usr, vf, min(vt) AS vt, any_value(size) AS size,
            any_value(n_files) AS n_files, any_value(par) AS par FROM prows GROUP BY k, path, usr, vf""")
    con.execute(f"""CREATE OR REPLACE TABLE pd AS SELECT x.k, x.lvl, x.dir, count(*)::BIGINT AS n FROM ({_level_dirs('pv')}) AS x
        SEMI JOIN near USING (k, lvl, dir) GROUP BY ALL""")
    con.execute(f"""CREATE OR REPLACE TABLE nh AS SELECT near.k, near.lvl, near.dir, near.n + coalesce(pd.n, 0) AS rows FROM near
        LEFT JOIN pd USING (k, lvl, dir) WHERE near.n + coalesce(pd.n, 0) > {R}""")
    doc["new_heavy"] = con.execute("SELECT count(*) FROM nh").fetchone()[0]
    if doc["new_heavy"]:
        con.execute(f"""CREATE OR REPLACE TABLE nroots AS SELECT * FROM (
                SELECT k, any_value(depth) AS depth, path, usr, vf, min(vt) AS vt, any_value(size) AS size, any_value(n_files) AS n_files, any_value(par) AS par
                FROM (SELECT * FROM pv UNION ALL SELECT * FROM rr) GROUP BY k, path, usr, vf)
            WHERE {first_hit_sql(mode, 'k')}""")
        rollup_cells(con, "nroots", "nh", K, into)
    # 4. Delta headers and cells at D for the heavy (k, dir) the run touches.
    doc["delta"] = delta_cells(con, mode, D, K, into)
    doc["keys"] = con.execute(f"SELECT count(DISTINCT q) FROM {into}").fetchone()[0]
    for t in ("rr", "rk", "pb", "sc", "sca", "scb", "ck", "rd", "pr", "ps", "cand", "kt", "ktk", "near", "prows", "pv", "pd", "nh", "nroots"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    return doc


def stack_rows(con, tiers: str, out: str) -> None:
    """`DRILL_RULES.stack` in SQL: per `(q, dir)` of `tiers` (`t` the tier, `ROLLUP_SCHEMA` rows), the tiers from the
    newest down to the newest one whose header is full (`kind` 0), as table `out` (with `t`)."""
    con.execute(f"""CREATE OR REPLACE TABLE {out} AS SELECT x.* FROM {tiers} AS x JOIN (
            SELECT q, dir, coalesce(max(t) FILTER (WHERE kind = 0), -1) AS floor FROM {tiers} WHERE kind IN (0, -1) GROUP BY q, dir) AS f
        USING (q, dir) WHERE x.t >= f.floor""")


def delta_cells(con, mode: str, D: int, K: int, into: str) -> int:
    """Delta headers (`kind` −1) and the cells dated `D` into `into`, for every `(k, dir)` of the prior stacks `ps`
    the run's rows `rr` touch (`rd`): each series' last value (a kept child's, or the remainder's) moved by the run's
    first-hit events under it (an open adds at `D`, a close record subtracts); a child no tier has kept is kept while
    fewer than K are (then every child holding roots is a kept one, so it is new), else it is the remainder's.
    Header: `vf` = kept, `b` = rows under `dir` (prior + the run's), `o` = children holding roots (prior + the new kept
    ones: exact while fewer than K are kept, then a lower bound)."""
    con.execute("""CREATE OR REPLACE TABLE ph AS SELECT q, dir, arg_max(vf, t) AS kept, arg_max(b, t) AS b, arg_max(o, t) AS o
        FROM ps WHERE kind IN (0, -1) GROUP BY q, dir""")
    con.execute("CREATE OR REPLACE TABLE dt AS SELECT ph.*, rd.n FROM ph JOIN rd ON rd.k = ph.q AND rd.dir = ph.dir")
    n = con.execute("SELECT count(*) FROM dt").fetchone()[0]
    if not n:
        return 0
    con.execute("CREATE OR REPLACE TABLE pkid AS SELECT DISTINCT q, dir, child FROM ps WHERE kind = 1")
    con.execute("CREATE OR REPLACE TABLE plast AS SELECT q, dir, kind, child, arg_max(b, vf) AS b, arg_max(o, vf) AS o FROM ps WHERE kind IN (1, 2) GROUP BY ALL")
    con.execute(f"""CREATE OR REPLACE TABLE ev AS SELECT dt.q, dt.dir, {_child('x.path', 'dt.dir')} AS child,
            sum(CASE WHEN x.vf = {D} THEN x.size ELSE 0 END - CASE WHEN x.vt = {D} THEN x.size ELSE 0 END)::BIGINT AS db,
            sum(CASE WHEN x.vf = {D} THEN x.n_files ELSE 0 END - CASE WHEN x.vt = {D} THEN x.n_files ELSE 0 END)::BIGINT AS dn
        FROM dt JOIN (SELECT * FROM rr WHERE {first_hit_sql(mode, 'k')}) AS x
            ON x.k = dt.q AND (dt.dir = '' OR starts_with(x.path, dt.dir || '/'))
        GROUP BY ALL""")
    # New kept children: not kept in any tier, while the kept count is under K, by name.
    con.execute(f"""CREATE OR REPLACE TABLE nk AS SELECT q, dir, child FROM (
            SELECT e.q, e.dir, e.child, row_number() OVER (PARTITION BY e.q, e.dir ORDER BY e.child) AS r, dt.kept
            FROM (SELECT DISTINCT q, dir, child FROM ev) AS e ANTI JOIN pkid USING (q, dir, child) JOIN dt USING (q, dir))
        WHERE r <= {K} - kept""")
    con.execute("""CREATE OR REPLACE TABLE ser AS
            SELECT e.q, e.dir, 1::TINYINT AS kind, e.child, e.db, e.dn FROM ev AS e SEMI JOIN (SELECT * FROM pkid UNION ALL SELECT * FROM nk) AS k USING (q, dir, child)
            UNION ALL SELECT e.q, e.dir, 2::TINYINT, '', sum(e.db), sum(e.dn) FROM ev AS e
                ANTI JOIN (SELECT * FROM pkid UNION ALL SELECT * FROM nk) AS k USING (q, dir, child) GROUP BY e.q, e.dir""")
    con.execute(f"""INSERT INTO {into} SELECT dt.q, dt.dir, -1, '', dt.kept + coalesce(c.n, 0), dt.b + dt.n, dt.o + coalesce(c.n, 0)
        FROM dt LEFT JOIN (SELECT q, dir, count(*) AS n FROM nk GROUP BY q, dir) AS c USING (q, dir)""")
    con.execute(f"""INSERT INTO {into} SELECT s.q, s.dir, s.kind, s.child, {D}, (coalesce(l.b, 0) + s.db)::BIGINT, (coalesce(l.o, 0) + s.dn)::BIGINT
        FROM (SELECT q, dir, kind, child, sum(db) AS db, sum(dn) AS dn FROM ser GROUP BY ALL HAVING sum(db) <> 0 OR sum(dn) <> 0) AS s
        LEFT JOIN plast AS l USING (q, dir, kind, child)""")
    for t in ("ph", "dt", "pkid", "plast", "ev", "nk", "ser"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    return n


def merge_rollups(con, files_by_tier: list[list[str]], into: str) -> None:
    """Runs' rollup rows (oldest first) stacked per `(q, dir)` into one tier's (`into`): the tiers from the newest down to
    the newest full header, the newest header kept (full if the stack reached one), every cell of the stacked tiers."""
    con.execute("CREATE OR REPLACE TABLE mr (t INTEGER, q VARCHAR, dir VARCHAR, kind TINYINT, child VARCHAR, vf BIGINT, b BIGINT, o BIGINT)")
    for i, fs in enumerate(files_by_tier):
        if fs:
            con.execute(f"INSERT INTO mr SELECT {i}, q, dir, kind, child, vf, b, o FROM ({files_sql(fs)})")
    stack_rows(con, "mr", "ms")
    con.execute(f"CREATE TABLE IF NOT EXISTS {into} (q VARCHAR, dir VARCHAR, kind TINYINT, child VARCHAR, vf BIGINT, b BIGINT, o BIGINT)")
    con.execute(f"""INSERT INTO {into} SELECT q, dir, CASE WHEN bool_or(kind = 0) THEN 0 ELSE -1 END::TINYINT, '', arg_max(vf, t), arg_max(b, t), arg_max(o, t)
        FROM ms WHERE kind IN (0, -1) GROUP BY q, dir""")
    con.execute(f"INSERT INTO {into} SELECT q, dir, kind, child, vf, b, o FROM ms WHERE kind IN (1, 2)")
    con.execute("DROP TABLE mr; DROP TABLE ms")


def build_run_local(con, prior: list[Tier], run: Tier, D: int, R: int, K: int, scans: list[str], versions_sql: str | None = None,
                    names_rows: int = NAME_SHARD_ROWS) -> dict:
    """A level-0 run's `names/` (from `versions_sql`, its `cdelta`, unless already there) and `anchors/` (both kinds,
    `meta.json` last), locally."""
    if versions_sql is not None:
        write_names(con, versions_sql, run.root / NAMES, names_rows)
    out = run.root / ANCHORS
    out.mkdir(parents=True, exist_ok=True)
    sets, docs = {}, {}
    for kind in KINDS:
        con.execute("DROP TABLE IF EXISTS cells")
        docs[kind] = run_rollups(con, prior, run, D, kind, R, K, "cells")
        _, idx = write_rollups(con, "cells", out, f"rollups/{kind}-s0000.parquet")
        sets[kind] = write_index([idx], out, kind)
    (out / "meta.json").write_text(json.dumps(anchors_meta(R, K, sets, scans), indent=1) + "\n")
    return {"sets": sets, "docs": docs}


def merge_run_local(con, runs: list[Tier], out: Tier, R: int, K: int, scans: list[str], names_rows: int = NAME_SHARD_ROWS) -> dict:
    """Runs (oldest first) merged into one tier's `names/` (`static_append.merge_shards`) and `anchors/` (`merge_rollups`)."""
    from .static_append import merge_shards

    merge_shards([r.root / NAMES for r in runs], out.root / NAMES, names_rows)
    dst = out.root / ANCHORS
    dst.mkdir(parents=True, exist_ok=True)
    sets = {}
    for kind in KINDS:
        con.execute("DROP TABLE IF EXISTS cells")
        merge_rollups(con, [r.rollup_files(kind) for r in runs], "cells")
        _, idx = write_rollups(con, "cells", dst, f"rollups/{kind}-s0000.parquet")
        sets[kind] = write_index([idx], dst, kind)
    (dst / "meta.json").write_text(json.dumps(anchors_meta(R, K, sets, scans), indent=1) + "\n")
    return {"sets": sets}


# ── Reading (the Worker's logic, `staticAnchors.ts`) ───────────────────────


def term_in_path(key: str, path: str) -> bool:
    """Whether `path` holds the keyed term in some segment (its view is then the plain one)."""
    text, mode = parse_key(key)
    if mode is None:
        return text in path.lower()
    return path != "" and any(segment_matches(seg, text, mode) for seg in path.lower().split("/"))


def under_range(P: str) -> tuple[str, str]:
    return ("", "\U0010ffff") if P == "" else (P + "/", P + "0")


class ShardSet:
    """A tier's suffix shards (`sx/`) or name index (`names/`), local or over fsspec: `select` bounds a key's rows by
    whole row groups (`s` and `path` statistics), `read` decodes groups."""

    def __init__(self, fs, root: str):
        self.fs, self.root = fs, root.rstrip("/")
        with fs.open(f"{self.root}/shards.json") as f:
            self.plan = json.load(f)["shards"]
        self._md: dict[int, pq.FileMetaData] = {}

    def shard(self, k: str) -> int | None:
        p3 = k[:3]
        for sh in self.plan:
            if sh["lo"] <= p3 and (sh["hi"] is None or p3 < sh["hi"]):
                return sh["i"]
        return None

    def path(self, i: int) -> str:
        return f"{self.root}/sx/s{i:04d}.parquet"

    def md(self, i: int) -> pq.FileMetaData:
        if i not in self._md:
            with self.fs.open(self.path(i)) as f:
                self._md[i] = pq.ParquetFile(f).metadata
        return self._md[i]

    def select(self, k: str, exact: bool, under: str | None = None) -> tuple[int | None, list[int], int]:
        i = self.shard(k)
        if i is None:
            return None, [], 0
        md = self.md(i)
        si, pi = md.schema.names.index("s"), md.schema.names.index("path")
        lo, hi = under_range(under) if under else ("", "\U0010ffff")
        out, rows = [], 0
        for g in range(md.num_row_groups):
            rg = md.row_group(g)
            st = rg.column(si).statistics
            if st.max < k or (st.min > k if exact else st.min >= k + "\U0010ffff"):
                continue
            ps = rg.column(pi).statistics if under else None
            if ps is not None and ps.has_min_max and (ps.max < lo or ps.min >= hi):
                continue
            out.append(g)
            rows += rg.num_rows
        return i, out, rows

    def read(self, i: int, groups: list[int]) -> list[dict]:
        if not groups:
            return []
        with self.fs.open(self.path(i)) as f:
            t = pq.ParquetFile(f).read_row_groups(groups)
        t = t.set_column(t.schema.get_field_index("vf"), "vf", t.column("vf").cast(pa.int64()))
        t = t.set_column(t.schema.get_field_index("vt"), "vt", t.column("vt").cast(pa.int64()))
        return t.to_pylist()


def fold(rows: list[dict], key: str, under: str | None = None) -> list[dict]:
    """`AnchoredHits`: the rows matching the key (`s == q` for `q$`, `/q…` / `/q` for `^q` / `^q$`) under `under`,
    depth ≥ 1, with no ancestor segment matching."""
    text, mode = parse_key(key)
    k = text if mode == "end" else "/" + text
    lo, hi = under_range(under) if under else (None, None)
    out = []
    for r in rows:
        s = r["s"]
        if (s != k) if mode != "start" else not s.startswith(k):
            continue
        p = r["path"]
        if lo is not None and not (lo <= p < hi):
            continue
        if r["depth"] < 1:
            continue
        par = p.rsplit("/", 1)[0].lower() if "/" in p else ""
        if par and any(segment_matches(seg, text, mode) for seg in par.split("/")):
            continue
        out.append(r)
    return out


def combine(parts: list[list[dict]]) -> list[dict]:
    """Tiers' hits as one set: equal `(path, usr, vf)` is one, the smallest `vt` wins."""
    best: dict[tuple, dict] = {}
    for p in parts:
        for h in p:
            k = (h["path"], h["usr"], h["vf"])
            if k not in best or h["vt"] < best[k]["vt"]:
                best[k] = h
    return list(best.values())


def child_sums(hits: list[dict], P: str, D_ms: int) -> dict[str, list[int]]:
    acc: dict[str, list[int]] = {}
    for h in hits:
        if not (h["vf"] <= D_ms < h["vt"]):
            continue
        rest = h["path"] if P == "" else h["path"][len(P) + 1:]
        c = rest.split("/", 1)[0]
        e = acc.setdefault(c, [0, 0])
        e[0] += h["size"]
        e[1] += h["n_files"]
    return {c: v for c, v in sorted(acc.items()) if v != [0, 0]}


class RollupSet:
    """A tier's `anchors/<kind>-rollups-index.parquet` and data files, read whole per key (verification scale)."""

    def __init__(self, fs, root: str, kind: str):
        self.fs, self.root, self.kind = fs, root.rstrip("/"), kind
        self._idx: pa.Table | None = None

    def rows(self, k: str, P: str) -> list[dict]:
        if self._idx is None:
            with self.fs.open(f"{self.root}/{self.kind}-rollups-index.parquet") as f:
                self._idx = pq.read_table(f)
        idx = self._idx.to_pylist()
        sel = [e for e in idx if (e["q_max"], e["k_max"]) >= (k, P) and (e["q_min"], e["k_min"]) < (k, P + "\0")]
        out = []
        for e in sel:
            with self.fs.open(f"{self.root}/{e['file']}") as f:
                t = pq.ParquetFile(f).read_row_group(e["rg"]).to_pylist()
            out += [r for r in t if r["q"] == k and r["dir"] == P]
        return out


def stack(parts: list[list[dict]]) -> tuple[dict, list[dict]] | None:
    """`DRILL_RULES.stack`: newest tier first, each tier's cells, down to a full header; the newest header."""
    head, cells = None, []
    for rows in reversed(parts):
        if not rows:
            continue
        if rows[0]["kind"] not in (0, -1):
            raise RuntimeError("rollup rows without a header")
        head = head or rows[0]
        cells += rows[1:]
        if rows[0]["kind"] == 0:
            break
    return (head, cells) if head else None


@dataclass
class ReaderTier:
    """One tier for `AnchoredReader`: its suffix shards, its name index and rollups (None: no `anchors/` there)."""
    sx: ShardSet
    names: ShardSet | None
    rollups: dict[str, RollupSet] | None
    meta: dict | None


def reader_tier(fs, root: str) -> ReaderTier:
    root = root.rstrip("/")
    meta = None
    if fs.exists(f"{root}/{ANCHORS}/meta.json"):
        with fs.open(f"{root}/{ANCHORS}/meta.json") as f:
            meta = json.load(f)
    return ReaderTier(ShardSet(fs, root), ShardSet(fs, f"{root}/{NAMES}") if meta else None,
                      {k: RollupSet(fs, f"{root}/{ANCHORS}", k) for k in KINDS} if meta else None, meta)


class AnchoredReader:
    """`staticAnchors.ts` `AnchoredSource` over tiers (base first): `view(key, P, dates)` → the per-child answers on each
    date (`answers`; a rollup's `rest`), the source (`plain`, `light`, `roots`, `rollup`, `declined`) and the scans'
    tiers it read. The anchored tiers are the prefix with `anchors/meta.json`."""

    def __init__(self, tiers: list[ReaderTier], max_rows: int = MAX_ROWS):
        self.light = tiers
        n = next((i for i, t in enumerate(tiers) if t.meta is None), len(tiers))
        self.anch = tiers[:n]
        self.max_rows = max_rows

    def view(self, key: str, P: str, dates: list[str]) -> dict:
        from .scan_id import scan_epoch

        text, mode = parse_key(key)
        out: dict = {"key": key, "P": P}
        if term_in_path(key, P):
            return {**out, "source": "plain"}
        Ds = {d: scan_epoch(d) * 1000 for d in dates}
        k = text if mode == "end" else "/" + text
        exact = mode != "start"
        sets = [t.sx for t in self.light] if mode == "end" else [t.names for t in self.anch]
        sels = [s.select(k, exact) for s in sets]
        if sum(x[2] for x in sels) <= self.max_rows:
            hits = combine([fold(s.read(i, g), key) for s, (i, g, _) in zip(sets, sels) if i is not None])
            hits = [h for h in hits if P == "" or h["path"].startswith(P + "/")]
            return {**out, "source": "light", "tiers": len(sets), "answers": {d: child_sums(hits, P, D) for d, D in Ds.items()}}
        if mode == "start":
            return {**out, "source": "declined"}
        if not self.anch:
            return {**out, "source": "declined"}
        sets = [t.sx for t in self.anch] if mode == "end" else [t.names for t in self.anch]
        bound = self.anch[0].meta["R"] + 4 * sum(t.meta["rg"] for t in self.anch)
        sels = [s.select(k, True, P) for s in sets]
        upper = sum(x[2] for x in sels)
        out.update(upper=upper, tiers=len(self.anch))
        if upper <= bound:
            hits = combine([fold(s.read(i, g), key, P) for s, (i, g, _) in zip(sets, sels) if i is not None])
            return {**out, "source": "roots", "answers": {d: child_sums(hits, P, D) for d, D in Ds.items()}}
        got = stack([t.rollups[mode].rows(k, P) for t in self.anch])
        if got is None:
            raise RuntimeError(f"({key!r}, {P!r}): {upper:,} rows bound, but no rollup")
        head, cells = got
        out.update(source="rollup", header={"kept": head["vf"], "rows": head["b"], "children": head["o"]}, answers={}, rest={})
        for d, D in Ds.items():
            cur: dict[tuple[int, str], tuple[int, int, int]] = {}
            for c in cells:
                if c["vf"] * 1000 <= D:
                    key_ = (c["kind"], c["child"])
                    if key_ not in cur or c["vf"] > cur[key_][0]:
                        cur[key_] = (c["vf"], c["b"], c["o"])
            out["answers"][d] = {c: [b, o] for (kd, c), (_, b, o) in sorted(cur.items()) if kd == 1 and (b, o) != (0, 0)}
            out["rest"][d] = list(cur.get((2, ""), (0, 0, 0))[1:])
        return out


def brute_view(versions: list[tuple], key: str, P: str, D_ms: int) -> dict[str, list[int]]:
    """The definition, over versions `(path, usr, vf_ms, vt_ms, size, n_files)`: per child of P the live first hits."""
    text, mode = parse_key(key)
    hits = []
    for path, usr, vf, vt, size, n in versions:
        if not (vf <= D_ms < vt) or (P != "" and not path.startswith(P + "/")):
            continue
        segs = path.lower().split("/")
        if not segment_matches(segs[-1], text, mode) or any(segment_matches(s, text, mode) for s in segs[:-1]):
            continue
        hits.append({"path": path, "usr": usr, "vf": vf, "vt": vt, "size": size, "n_files": n})
    return child_sums(hits, P, D_ms)


def brute_sql(src: str, version: int, cases: str) -> str:
    """Per (case, child of its P): Σ size, n_files over one scan's rows (`src`, its `path` sort; v1 `b`/`o`) strictly
    under P (`''`: everything), depth ≥ 1, whose lowercase name matches the case's anchored key (`k`: the literal,
    `m`: start | end | exact) and none of whose ancestor segments does. `cases`: a table of `(key, k, m, P)`."""
    size, n = ("size", "n_files") if version == 2 else ("b", "o")
    match = """CASE c.m WHEN 'start' THEN starts_with(x.l, c.k) AND NOT contains('/' || x.par, '/' || c.k)
        WHEN 'end' THEN ends_with(x.l, c.k) AND NOT contains(x.par || '/', c.k || '/')
        ELSE x.l = c.k AND NOT contains('/' || x.par || '/', '/' || c.k || '/') END"""
    return f"""WITH x AS (SELECT path, {NAME} AS l, {PARENT} AS par, {size} AS sz, {n} AS nf FROM read_parquet({q(src)}) WHERE depth >= 1)
        SELECT c.key, c.P, CASE WHEN c.P = '' THEN split_part(x.path, '/', 1) ELSE split_part(substring(x.path, length(c.P) + 2), '/', 1) END AS child,
            sum(x.sz)::BIGINT AS b, sum(x.nf)::BIGINT AS o
        FROM x JOIN {cases} AS c ON (c.P = '' OR starts_with(x.path, c.P || '/')) AND {match}
        GROUP BY ALL"""


# ── CLI (Batch stages; `dt-cloud static-names anchors …`) ───────────────────


@group("anchors")
def cli() -> None:
    """Anchored name search's index (`^q`, `q$`, `^q$`; specs/anchored-search.md): the name index, the heavy keys'
    rollups, per tier."""


def _gcs(bucket: str):
    from google.cloud import storage

    return storage.Client().bucket(bucket)


def _upload_dir(b, local: Path, prefix: str, last: tuple[str, ...] = ()) -> int:
    """Every file under `local` → `prefix/<rel>`, the `last` names after all others."""
    files = sorted(p for p in local.rglob("*") if p.is_file())
    files = [p for p in files if p.name not in last] + [p for p in files if p.name in last]
    for p in files:
        b.blob(f"{prefix}/{p.relative_to(local)}").upload_from_filename(str(p))
    return len(files)


def _tier_root(mount: str, gen: str, run: str | None) -> Path:
    return Path(mount) / PREFIX / gen / ("" if run is None else f"deltas/{run}")


def _manifest_runs(mount: str, gen: str, through: str | None = None) -> list[dict]:
    """The newest manifest's runs (oldest first), cut after run `through` (its scan id) when given."""
    d = Path(mount) / PREFIX / gen / "manifests"
    keys = sorted(d.glob("*.json"))
    if not keys:
        return []
    runs = json.loads(keys[-1].read_text())["runs"]
    if through is not None:
        k = next((i for i, r in enumerate(runs) if r["key"] == f"deltas/{through}"), None)
        if k is None:
            raise SystemExit(f"run deltas/{through} is not in {keys[-1].name}")
        runs = runs[:k + 1]
    return runs


@cli.command("names")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-d", "--run", help="A run's scan id (its `cdelta`); default: the base (its `cintervals`)")
@option("-g", "--gen", required=True, help="Generation")
@option("-m", "--mount", required=True, help="Local mount of the bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-r", "--target-rows", default=NAME_SHARD_ROWS, type=int, help="Rows per shard (cut at three-character prefixes)")
@option("-T", "--tmp", default="/stage/tmp", help="Local scratch")
def names_cmd(bucket, run, gen, mount, mem, threads, target_rows, tmp) -> None:
    """The name index of the base (or a run) → `names/` (`sidecar.parquet` last); skipped when it is there."""
    b = _gcs(bucket)
    prefix = f"{PREFIX}/{gen}" + ("" if run is None else f"/deltas/{run}")
    if b.blob(f"{prefix}/{NAMES}/sidecar.parquet").exists():
        err(f"names {prefix}: done")
        return
    src = _tier_root(mount, gen, run) / ("cintervals" if run is None else "cdelta")
    files = sorted(str(f) for f in src.glob("*.parquet"))
    if not files:
        raise SystemExit(f"no versions under {src}")
    t0 = monotonic()
    con = connect(threads, mem, tmp)
    out = Path(tmp) / "names-out"
    doc = write_names(con, files_sql(files, "depth, path, usr, vf, vt, size, n_files"), out, target_rows)
    n = _upload_dir(b, out, f"{prefix}/{NAMES}", last=("sidecar.parquet",))
    err(f"names {prefix}: {doc['rows']:,} rows, {doc['shards']} shards, {doc['bytes']:,} B, {n} files in {monotonic() - t0:.0f}s")
    print(json.dumps(doc))


@cli.command("census")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-f", "--floor-rows", default=50_000, type=int, help="Keep prefixes with at least this many rows")
@option("-g", "--gen", required=True, help="Generation")
@option("-m", "--mount", required=True, help="Local mount of the bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-T", "--tmp", default="/stage/tmp", help="Local scratch")
@option("-V", "--max-rows", default=100_000, type=int, help="The light bound (V) the report counts against")
def census_cmd(bucket, floor_rows, gen, mount, mem, threads, tmp, max_rows) -> None:
    """The heavy-`^q` census: every name-index prefix `/q` (`q` ≥ 2 characters) with at least `-f` rows →
    `anchors/census-start.parquet` (GCS only), and a report of those over V (the prefixes `^q` would need a drill for)."""
    import shutil

    from .static_catalog import census

    b = _gcs(bucket)
    prefix = f"{PREFIX}/{gen}"
    root = Path(mount) / prefix / NAMES / "sx"
    con = connect(threads, mem, tmp)
    parts = []
    t0 = monotonic()
    for f in sorted(root.glob("*.parquet")):
        local = Path(tmp) / f.name
        shutil.copy(f, local)
        parts.append(census(con, f"read_parquet({q(str(local))})", floor_rows))
        local.unlink()
        err(f"census {f.name}: {parts[-1].num_rows:,} prefixes ≥ {floor_rows:,} ({monotonic() - t0:.0f}s)")
    t = pa.concat_tables(parts).sort_by("q")
    sink = pa.BufferOutputStream()
    pq.write_table(t, sink, compression=CODEC)
    b.blob(f"{prefix}/{ANCHORS}/census-start.parquet").upload_from_string(sink.getvalue().to_pybytes())
    heavy = [r for r in t.to_pylist() if r["rows"] > max_rows]
    by_len: dict[int, int] = {}
    for r in heavy:
        by_len[len(r["q"]) - 1] = by_len.get(len(r["q"]) - 1, 0) + 1
    doc = {"floor_rows": floor_rows, "V": max_rows, "prefixes": t.num_rows, "heavy": len(heavy), "heavy_rows": sum(r["rows"] for r in heavy),
           "heavy_by_len": dict(sorted(by_len.items())), "top": sorted(((r["q"][1:], r["rows"]) for r in heavy), key=lambda x: -x[1])[:40]}
    b.blob(f"{prefix}/{ANCHORS}/census-start.json").upload_from_string(json.dumps(doc, indent=1) + "\n")
    print(json.dumps(doc, indent=1))


@cli.command("rollups")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Generation (its base)")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-k", "--kind", type=Choice(KINDS), required=True, help="end (the suffix shards) | exact (the name index)")
@option("-K", "--keep", "K", default=K_DEFAULT, type=int, help="Kept children per heavy directory")
@option("-l", "--lease", default=5400, type=int, help="Seconds before another task takes over a claimed shard")
@option("-m", "--mount", required=True, help="Local mount of the bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-R", "--read-rows", "R", default=R_DEFAULT, type=int, help="Rows a directory holds before it is heavy (R)")
@option("-s", "--shard", "shards", multiple=True, type=int, help="Only these shards (repeat)")
@option("-t", "--trial", is_flag=True, help="Write under the scratch bucket's `<gen>/anchors-trial/` instead (a smoke run)")
@option("-T", "--tmp", default="/stage/tmp", help="Local scratch")
def rollups_cmd(bucket, gen, index, kind, K, lease, mount, mem, threads, R, shards, trial, tmp) -> None:
    """The base's rollups of one kind, shard by shard from a shared queue (biggest first): per shard
    `anchors/rollups/<kind>-s####.parquet`, its group index `anchors/rollups-index/…` and its key bounds
    `anchors/keys/<kind>-s####.parquet` (for the runs' builder)."""
    import shutil

    from .static_roots import Queue, _put

    if kind not in KINDS:
        raise SystemExit(f"kind {kind!r}: want one of {KINDS}")
    sb = _gcs(scratch_bucket())
    b = sb if trial else _gcs(bucket)
    prefix = f"{PREFIX}/{gen}"
    out_anchors = f"{prefix}/{ANCHORS}-trial" if trial else f"{prefix}/{ANCHORS}"
    sub = "" if kind == "end" else f"{NAMES}/"
    plan = json.loads((Path(mount) / prefix / sub / "shards.json").read_text())
    queue = Queue(sb, f"{out_anchors}", kind, _task(index), lease)
    con = connect(threads, mem, tmp)
    for sh in sorted(plan["shards"], key=lambda s: -s["rows"]):
        if shards and sh["i"] not in shards:
            continue
        name = f"s{sh['i']:04d}"
        key = f"{out_anchors}/rollups-index/{kind}-{name}.parquet"
        if b.blob(key).exists() or not queue.claim(f"{kind}-{name}"):
            continue
        t0 = monotonic()
        local = Path(tmp) / f"{kind}-{name}.parquet"
        shutil.copy(Path(mount) / prefix / sub / "sx" / f"{name}.parquet", local)
        con.execute("DROP TABLE IF EXISTS cells")
        doc = base_rollups(con, [str(local)], kind, R, K, "cells")
        out = Path(tmp) / "anchors-out"
        rel = f"rollups/{kind}-{name}.parquet"
        cells, idx = write_rollups(con, "cells", out, rel)
        b.blob(f"{out_anchors}/{rel}").upload_from_filename(str(out / rel))
        _put(b, f"{out_anchors}/keys/{kind}-{name}.parquet", keys_table([str(local)], [f"{sub}sx/{name}.parquet"]))
        _put(b, key, idx)
        local.unlink()
        (out / rel).unlink()
        err(f"rollups {kind} {name}: {sh['rows']:,} rows, {doc['keys']:,} heavy keys, {doc['heavy_dirs']:,} heavy dirs, {cells:,} cells in {monotonic() - t0:.0f}s")


@cli.command("index")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-K", "--keep", "K", default=K_DEFAULT, type=int, help="Kept children per heavy directory (meta.json)")
@option("-m", "--mount", required=True, help="Local mount of the bucket")
@option("-R", "--read-rows", "R", default=R_DEFAULT, type=int, help="R (meta.json)")
@option("-T", "--tmp", default="/stage/tmp", help="Local scratch")
def index_cmd(bucket, gen, K, mount, R, tmp) -> None:
    """The base's rollup indexes (every shard's, per kind, with their tops) and key tables, then `anchors/meta.json`
    (last: the readers' liveness marker). Refuses unless every shard of both kinds is done."""
    b = _gcs(bucket)
    prefix = f"{PREFIX}/{gen}"
    root = Path(mount) / prefix
    out = Path(tmp) / "anchors-index"
    out.mkdir(parents=True, exist_ok=True)
    sets = {}
    for kind in KINDS:
        sub = "" if kind == "end" else f"{NAMES}/"
        plan = json.loads((root / sub / "shards.json").read_text())
        names = [f"s{sh['i']:04d}" for sh in plan["shards"]]
        missing = [n for n in names if not (root / ANCHORS / "rollups-index" / f"{kind}-{n}.parquet").exists()]
        if missing:
            raise SystemExit(f"{kind}: {len(missing)} shards without rollups ({missing[:5]}…)")
        sets[kind] = write_index([pq.read_table(root / ANCHORS / "rollups-index" / f"{kind}-{n}.parquet") for n in names], out, kind)
        keys = pa.concat_tables([pq.read_table(root / ANCHORS / "keys" / f"{kind}-{n}.parquet") for n in names])
        pq.write_table(keys, out / f"keys-{kind}.parquet", compression=CODEC)
        for f in (f"{kind}-rollups-index.parquet", f"{kind}-rollups-index.top.parquet", f"keys-{kind}.parquet"):
            b.blob(f"{prefix}/{ANCHORS}/{f}").upload_from_filename(str(out / f))
    names_doc = json.loads((root / NAMES / "shards.json").read_text())
    meta = anchors_meta(R, K, sets, gen=gen, names={"rows": names_doc["total_rows"], "shards": len(names_doc["shards"])})
    b.blob(f"{prefix}/{ANCHORS}/meta.json").upload_from_string(json.dumps(meta, indent=1) + "\n")
    print(json.dumps(meta, indent=1))


@cli.command("run")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-d", "--run", required=True, help="The run's scan id (a level-0 run in the newest manifest)")
@option("-g", "--gen", required=True, help="Generation")
@option("-K", "--keep", "K", default=K_DEFAULT, type=int, help="Kept children per heavy directory")
@option("-m", "--mount", required=True, help="Local mount of the bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-R", "--read-rows", "R", default=R_DEFAULT, type=int, help="R")
@option("-T", "--tmp", default="/stage/tmp", help="Local scratch")
def run_cmd(bucket, run, gen, K, mount, mem, threads, R, tmp) -> None:
    """A level-0 run's `names/` and `anchors/` (both kinds; `meta.json` last), over the tiers before it in the newest
    manifest (the base and its earlier runs, each with `anchors/meta.json`). Skipped when its `anchors/meta.json` is there."""
    from .scan_id import scan_epoch

    b = _gcs(bucket)
    prefix = f"{PREFIX}/{gen}/deltas/{run}"
    if b.blob(f"{prefix}/{ANCHORS}/meta.json").exists():
        err(f"anchors {prefix}: done")
        return
    t0 = monotonic()
    runs = _manifest_runs(mount, gen, run)
    if runs[-1].get("level", 0) != 0:
        raise SystemExit(f"{runs[-1]['key']} is a merged run: build its scans' runs and merge them")
    base = _tier_root(mount, gen, None)
    keys = {k: str(base / ANCHORS / f"keys-{k}.parquet") for k in KINDS}
    prior = [Tier(base, keys)] + [Tier(Path(mount) / PREFIX / gen / r["key"]) for r in runs[:-1]]
    for t in prior:
        if not (t.root / ANCHORS / "meta.json").exists():
            raise SystemExit(f"{t.root}: no {ANCHORS}/meta.json (build the tiers in order)")
    con = connect(threads, mem, tmp)
    local = Path(tmp) / f"run-{run}"
    local.mkdir(parents=True, exist_ok=True)
    src = _tier_root(mount, gen, run)
    (local / "sx").unlink(missing_ok=True)
    (local / "sx").symlink_to(src / "sx")
    cdelta = sorted(str(f) for f in (src / "cdelta").glob("*.parquet"))
    if (src / NAMES / "sidecar.parquet").exists():
        (local / NAMES).unlink(missing_ok=True)
        (local / NAMES).symlink_to(src / NAMES)
        versions = None
    else:
        versions = files_sql(cdelta, "depth, path, usr, vf, vt, size, n_files")
    doc = build_run_local(con, prior, Tier(local), scan_epoch(run), R, K, runs[-1]["scans"], versions)
    if versions is not None:
        _upload_dir(b, local / NAMES, f"{prefix}/{NAMES}", last=("sidecar.parquet",))
    _upload_dir(b, local / ANCHORS, f"{prefix}/{ANCHORS}", last=("meta.json",))
    err(f"anchors {prefix}: {json.dumps(doc['docs'])} in {monotonic() - t0:.0f}s")
    print(json.dumps(doc))


def _cases(path: str) -> list[tuple[str, str]]:
    from .static_names import read_text

    return [(d["key"], d["P"]) for d in map(json.loads, read_text(path).splitlines()) if d]


@cli.command("brute")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-c", "--cases", "cases_file", required=True, help="JSON lines `{key, P}` (a path or gs:// URL)")
@option("-d", "--date", "dates", multiple=True, required=True, help="Scan id; repeat (task i answers the i-th)")
@option("-g", "--gen", required=True, help="Generation (its scans, and the newest manifest's runs')")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", required=True, help="Local mount of the bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def brute_cmd(bucket, cases_file, dates, gen, index, mount, mem, threads, tmp) -> None:
    """Reference anchored views by brute force over one scan's file → `anchors/verify/brute/<date>.jsonl` (`{date, key,
    P, children: {child: [bytes, objects]}}`, nonzero children; views P itself matches left out)."""
    date = dates[_task(index)]
    root = Path(mount) / PREFIX / gen
    scans = json.loads((root / "scans.json").read_text())["scans"]
    for r in _manifest_runs(mount, gen):
        scans += json.loads((root / r["key"] / "scans.json").read_text())["scans"] if (root / r["key"] / "scans.json").exists() else []
    scan = next(s for s in scans if s["id"] == date)
    cases = [(key, *parse_key(key), P) for key, P in sorted(set(_cases(cases_file))) if not term_in_path(key, P)]
    con = connect(threads, mem, tmp)
    con.execute("CREATE TABLE cases (key VARCHAR, k VARCHAR, m VARCHAR, P VARCHAR)")
    con.executemany("INSERT INTO cases VALUES (?, ?, ?, ?)", cases)
    t0 = monotonic()
    got: dict[tuple[str, str], dict] = {(key, P): {} for key, _, _, P in cases}
    for key, P, child, b_, o_ in con.execute(brute_sql(f"{mount}/{scan['src']}", scan["version"], "cases") + " ORDER BY ALL").fetchall():
        if b_ or o_:
            got[(key, P)][child] = [int(b_), int(o_)]
    body = "".join(json.dumps({"date": date, "key": k, "P": P, "children": got[(k, P)]}) + "\n" for k, _, _, P in cases)
    _gcs(bucket).blob(f"{PREFIX}/{gen}/{ANCHORS}/verify/brute/{date}.jsonl").upload_from_string(body)
    err(f"anchors brute {date}: {len(cases)} cases in {monotonic() - t0:.1f}s")


@cli.command("query")
@option("-c", "--cases", "cases_file", required=True, help="JSON lines `{key, P}`")
@option("-d", "--date", "dates", multiple=True, required=True, help="Scan id; repeat")
@option("-g", "--gen", required=True, help="Generation")
@option("-r", "--root", required=True, help="The bucket's root: a mount (`/gcs/<bucket>`) or `gs://<bucket>`")
def query_cmd(cases_file, dates, gen, root) -> None:
    """Answer cases with the Python reader (`AnchoredReader`, the Worker's dispatch) over the base and the newest
    manifest's runs: one JSON line per case (`source`, `answers`, a rollup's `rest`, the time taken)."""
    import fsspec

    fs, base = fsspec.core.url_to_fs(f"{root.rstrip('/')}/{PREFIX}/{gen}")
    mf = sorted(fs.glob(f"{base}/manifests/*.json"))
    runs = json.loads(fs.cat(mf[-1]))["runs"] if mf else []
    reader = AnchoredReader([reader_tier(fs, base), *(reader_tier(fs, f"{base}/{r['key']}") for r in runs)])
    for key, P in _cases(cases_file):
        t0 = monotonic()
        out = reader.view(key, P, list(dates))
        out["s"] = round(monotonic() - t0, 3)
        print(json.dumps(out), flush=True)


@cli.command("verify")
@argument("brute_jsonl", nargs=-1, required=True)
@option("-a", "--answers", required=True, help="`query` output (JSON lines)")
def verify_cmd(brute_jsonl, answers) -> None:
    """Compare `query` answers with `brute` references, per (key, P, date): equal children (a rollup: its kept children
    equal, its remainder = the other children's sum). Prints the counts; exits 1 on any difference."""
    from .static_names import read_text

    ref = {}
    for path in brute_jsonl:
        for line in read_text(path).splitlines():
            if line:
                d = json.loads(line)
                ref[(d["key"], d["P"], d["date"])] = d["children"]
    n, bad, by = 0, [], {}
    for line in read_text(answers).splitlines():
        if not line.startswith("{"):
            continue
        a = json.loads(line)
        by[a["source"]] = by.get(a["source"], 0) + 1
        if a["source"] in ("plain", "declined"):
            continue
        for d, kids in a["answers"].items():
            k = (a["key"], a["P"], d)
            if k not in ref:
                continue
            want = ref[k]
            n += 1
            if a["source"] == "rollup":
                rest = [sum(v[0] for c, v in want.items() if c not in kids), sum(v[1] for c, v in want.items() if c not in kids)]
                ok = all(want.get(c) == v for c, v in kids.items()) and rest == a["rest"][d]
            else:
                ok = kids == want
            if not ok:
                bad.append(k)
    doc = {"compared": n, "equal": n - len(bad), "sources": by, "differ": bad[:20]}
    print(json.dumps(doc, indent=1))
    if bad:
        raise SystemExit(1)


if __name__ == "__main__":
    cli()
