"""Match roots of the heavy name terms (specs/architecture/static-name-search.md, "Drilldown"): for every
catalog member — the literals whose suffix range exceeds V rows, and every one- and two-character literal —
its first-hit rows themselves, so a filtered treemap / table / diff at any path P is computed from the
roots under P.

A **match root** of `q` is a first-hit row: a version (one owner slice of one path, live on `[vf, vt)`) at
depth ≥ 1 whose lowercase name contains `q` and whose lowercase parent path does not. The filtered view
at P on date D is the roots under P live on D, summed by child of P. (If P's own lowercase path contains
`q`, the whole subtree is covered by a root at or above P: the filtered view is the plain one.)

Long members' roots come from the suffix shards, level by level as the catalog's answers
(`static_catalog.member_events`), keeping the rows instead of summing them; short literals' from the
coalesced versions (`static_catalog.short_events`' rule). `measure` / `measure-short` count them:
per member its root rows and distinct root paths, and per directory the root rows under it
(bottom-up, one level at a time: a directory's totals are its children's).
"""
from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic, time

import pyarrow as pa
import pyarrow.compute  # noqa: F401  (pa.compute)
import pyarrow.parquet as pq
from click import IntRange, group, option

from .static_catalog import CHUNK_ROWS, PARENT
from .static_names import (
    _batches, CODEC, DATA_BUCKET, NAME, OPEN, PREFIX, SCRATCH_BUCKET, _task, connect, err, q, read_json,
)

#: The parent of a path `x` (raw case), by string cut: every directory level, newline or not.
def raw_parent(x: str) -> str:
    return f"left({x}, length({x}) - length(string_split({x}, '/')[-1]) - 1)"


ROOT_COLS = "q VARCHAR, depth UTINYINT, path VARCHAR, usr VARCHAR, vf BIGINT, vt BIGINT, size BIGINT, n_files BIGINT"
#: Per (q, root path): its rows (versions × owner slices) and how many are open (live on the newest scan).
RP_COLS = "q VARCHAR, depth UTINYINT, path VARCHAR, n BIGINT, n_open BIGINT"


def sx_rows_sql(src: str) -> str:
    """Suffix-shard rows with the owner slice, epoch seconds."""
    return f"SELECT s, depth, path, usr, epoch(vf)::BIGINT AS vf, epoch(vt)::BIGINT AS vt, size, n_files FROM {src}"


def _sink(con, into: str, hit: str, agg: bool) -> None:
    if agg:
        con.execute(f"""INSERT INTO {into} SELECT q, depth, path, count(*), count(*) FILTER (WHERE vt = {OPEN})
            FROM ({hit}) GROUP BY q, depth, path""")
    else:
        con.execute(f"INSERT INTO {into} SELECT q, depth, path, usr, vf, vt, size, n_files FROM ({hit})")


def member_roots(con, rows_sql: str, members: str, into: str, agg: bool = False) -> None:
    """Add `members`' (a table with `q`, ≥ 3 characters) first-hit rows among `rows_sql`'s suffix rows
    `(s, depth, path, usr, vf, vt, size, n_files)` to table `into` (created if absent): `ROOT_COLS`, or with
    `agg` per `(q, depth, path)` `RP_COLS`. The rule and level loop are `static_catalog.member_events`': at
    length L the rows whose prefix of length L is a prefix of a member are carried; a row is a hit of
    `q = left(s, L)` when `q` is a member, `s` starts at `q`'s first occurrence in the name, and the parent
    does not contain `q`. A `(q, path)`'s rows all share one suffix (its first occurrence), so feeding a
    shard in `(s, path)`-cut chunks never splits one: the aggregate is exact per chunk."""
    con.execute(f"CREATE TABLE IF NOT EXISTS {into} ({RP_COLS if agg else ROOT_COLS})")
    top = con.execute(f"SELECT max(length(q)) FROM {members}").fetchone()[0]
    if not top:
        return
    con.execute("DROP TABLE IF EXISTS rx; DROP TABLE IF EXISTS rpre")
    con.execute(f"""CREATE TABLE rpre AS SELECT DISTINCT left(q, L) AS p, L, max(length(q) = L) OVER (PARTITION BY left(q, L)) AS member
        FROM (SELECT q, unnest(range(3, length(q) + 1)) AS L FROM {members})""")
    con.execute(f"""CREATE TABLE rx AS SELECT s, depth, path, usr, vf, vt, size, n_files, {NAME} AS l, {PARENT} AS par
        FROM ({rows_sql}) WHERE depth >= 1""")
    for L in range(3, top + 1):
        con.execute("DROP TABLE IF EXISTS rl")
        con.execute(f"CREATE TABLE rl AS SELECT p, bool_or(member) AS member FROM rpre WHERE L = {L} GROUP BY p")
        con.execute(f"CREATE OR REPLACE TABLE rx AS SELECT rx.* FROM rx SEMI JOIN rl ON left(rx.s, {L}) = rl.p")
        if con.execute("SELECT count(*) FROM rx").fetchone()[0] == 0:
            break
        hit = f"""SELECT left(s, {L}) AS q, depth, path, usr, vf, vt, size, n_files FROM rx
            SEMI JOIN (SELECT p FROM rl WHERE member) AS mm ON left(rx.s, {L}) = mm.p
            WHERE instr(l, left(s, {L})) = length(l) - length(s) + 1 AND NOT contains(par, left(s, {L}))"""
        _sink(con, into, hit, agg)
        con.execute(f"DELETE FROM rx WHERE length(s) <= {L}")
    con.execute("DROP TABLE IF EXISTS rx; DROP TABLE IF EXISTS rl; DROP TABLE IF EXISTS rpre")


def short_roots(con, versions_sql: str, into: str, agg: bool = False) -> None:
    """Add every one- and two-character literal's first-hit rows among `versions_sql`'s rows `(depth, path,
    usr, vf, vt, size, n_files)` to `into` (as `member_roots`): each distinct character and character pair of
    the lowercase name that the lowercase parent does not contain (`static_catalog.short_events`' rule)."""
    con.execute(f"CREATE TABLE IF NOT EXISTS {into} ({RP_COLS if agg else ROOT_COLS})")
    hit = f"""SELECT g AS q, depth, path, usr, vf, vt, size, n_files FROM (
            SELECT unnest(list_distinct(list_transform(range(1, length(l) + 1), lambda p: substring(l, p, 1))
                || list_transform(range(1, length(l)), lambda p: substring(l, p, 2)))) AS g, par, depth, path, usr, vf, vt, size, n_files
            FROM (SELECT {NAME} AS l, {PARENT} AS par, depth, path, usr, vf, vt, size, n_files FROM ({versions_sql}) WHERE depth >= 1)
        ) WHERE NOT contains(par, g)"""
    _sink(con, into, hit, agg)


# ── Measuring ──────────────────────────────────────────────────────────────


def q_stats(con, rp: str) -> tuple[pa.Table, pa.Table]:
    """Per member: root rows, distinct root paths, open rows and paths, bucket (depth-1) roots, depth range;
    and per (member, depth) the root paths and rows there."""
    qs = con.execute(f"""SELECT q, sum(n)::BIGINT AS rows, count(*)::BIGINT AS paths, sum(n_open)::BIGINT AS open_rows,
            count(*) FILTER (WHERE n_open > 0)::BIGINT AS open_paths, count(*) FILTER (WHERE depth = 1)::BIGINT AS d1_paths,
            min(depth)::INTEGER AS min_depth, max(depth)::INTEGER AS max_depth
        FROM {rp} GROUP BY q ORDER BY q""").to_arrow_table()
    qd = con.execute(f"""SELECT q, depth::INTEGER AS depth, count(*)::BIGINT AS paths, sum(n)::BIGINT AS rows
        FROM {rp} GROUP BY q, depth ORDER BY q, depth""").to_arrow_table()
    return qs, qd


def dir_stats(con, rp: str, floor: int, partial_top: bool = False) -> tuple[pa.Table, pa.Table, pa.Table | None]:
    """Per (member, directory) the roots strictly under it, bottom-up: a directory at depth k sums its
    children at k + 1 — roots there and directories with roots under them (disjoint: nothing under a root
    is a root). Returns the directories with at least `floor` root rows `(q, k, dir, rows, paths, open_rows,
    children, direct, max_child)` (`children`: its children holding or being roots; `direct`: those that are
    roots; `max_child`: the largest child's rows), the histogram of every directory's rows `(q, k, lg, dirs,
    rows)` (`lg` = ⌊log2 rows⌋), and with `partial_top` the depth-1 directories unfloored and out of the
    histogram (when `rp` is one subtree partition, they are partial sums)."""
    maxd = con.execute(f"SELECT max(depth) FROM {rp}").fetchone()[0] or 0
    con.execute("""CREATE OR REPLACE TABLE dstat (q VARCHAR, k INTEGER, dir VARCHAR, rows BIGINT, paths BIGINT, open_rows BIGINT,
        children BIGINT, direct BIGINT, max_child BIGINT)""")
    con.execute("CREATE OR REPLACE TABLE dhist (q VARCHAR, k INTEGER, lg INTEGER, dirs BIGINT, rows BIGINT)")
    con.execute("CREATE OR REPLACE TABLE dtop (q VARCHAR, k INTEGER, dir VARCHAR, rows BIGINT, paths BIGINT, open_rows BIGINT, children BIGINT, direct BIGINT, max_child BIGINT)")
    con.execute("CREATE OR REPLACE TABLE dcur (q VARCHAR, dir VARCHAR, rows BIGINT, paths BIGINT, open_rows BIGINT)")
    for k in range(maxd - 1, 0, -1):
        con.execute(f"""CREATE OR REPLACE TABLE dnext AS SELECT q, {raw_parent('x')} AS dir, sum(rows)::BIGINT AS rows, sum(paths)::BIGINT AS paths,
                sum(open_rows)::BIGINT AS open_rows, count(*)::BIGINT AS children, sum(is_root)::BIGINT AS direct, max(rows)::BIGINT AS max_child
            FROM (SELECT q, path AS x, n AS rows, 1 AS paths, n_open AS open_rows, 1 AS is_root FROM {rp} WHERE depth = {k + 1}
                  UNION ALL SELECT q, dir, rows, paths, open_rows, 0 FROM dcur)
            GROUP BY ALL""")
        if k == 1 and partial_top:
            con.execute("INSERT INTO dtop SELECT q, 1, dir, rows, paths, open_rows, children, direct, max_child FROM dnext")
        else:
            con.execute(f"INSERT INTO dstat SELECT q, {k}, dir, rows, paths, open_rows, children, direct, max_child FROM dnext WHERE rows >= {floor}")
            con.execute(f"""INSERT INTO dhist SELECT q, {k}, floor(log2(rows))::INTEGER AS lg, count(*), sum(rows)
                FROM dnext GROUP BY q, lg""")
        con.execute("CREATE OR REPLACE TABLE dcur AS SELECT q, dir, rows, paths, open_rows FROM dnext")
    dirs = con.execute("SELECT * FROM dstat ORDER BY q, k, dir").to_arrow_table()
    hist = con.execute("SELECT * FROM dhist ORDER BY q, k, lg").to_arrow_table()
    top = con.execute("SELECT * FROM dtop ORDER BY q, dir").to_arrow_table() if partial_top else None
    for t in ("dstat", "dhist", "dtop", "dcur", "dnext"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    return dirs, hist, top


# ── Shard plumbing ─────────────────────────────────────────────────────────


def chunk_wheres(sx: str, chunk_rows: int = CHUNK_ROWS) -> list[str]:
    """`sx`'s rows in chunks of about `chunk_rows`, cut at row-group starts as `(s, path)` ranges (as
    `static_catalog.shard_cells`): each a DuckDB predicate."""
    pf = pq.ParquetFile(sx)
    cuts, n = [], 0
    for g in range(pf.metadata.num_row_groups):
        if n >= chunk_rows:
            first = pf.read_row_group(g, columns=["s", "path"]).slice(0, 1).to_pylist()[0]
            cuts.append((first["s"], first["path"]))
            n = 0
        n += pf.metadata.row_group(g).num_rows
    bounds = [None, *cuts, None]
    out = []
    for lo, hi in zip(bounds, bounds[1:]):
        conds = []
        if lo is not None:
            conds.append(f"s >= {q(lo[0])} AND (s > {q(lo[0])} OR path >= {q(lo[1])})")
        if hi is not None:
            conds.append(f"s <= {q(hi[0])} AND (s < {q(hi[0])} OR path < {q(hi[1])})")
        out.append(" AND ".join(conds) or "true")
    return out


class Queue:
    """Shards from a shared queue: a task claims one by creating `claims/<kind>/<name>` in the scratch bucket;
    a claim older than `lease` seconds is taken over (a preempted task's)."""

    def __init__(self, scratch_bucket, prefix: str, kind: str, task: int, lease: int):
        self.b, self.prefix, self.kind, self.t, self.lease = scratch_bucket, prefix, kind, task, lease

    def claim(self, name: str) -> bool:
        from google.api_core.exceptions import NotFound, PreconditionFailed

        blob = self.b.blob(f"{self.prefix}/claims/{self.kind}/{name}")
        try:
            blob.upload_from_string(str(self.t), if_generation_match=0)
            return True
        except PreconditionFailed:
            pass
        try:
            blob.reload()
        except NotFound:
            return self.claim(name)
        if time() - blob.updated.timestamp() < self.lease:
            return False
        try:
            blob.upload_from_string(str(self.t), if_generation_match=blob.generation)
            err(f"{self.kind} {name}: taking over a claim {time() - blob.updated.timestamp():.0f}s old")
            return True
        except PreconditionFailed:
            return False


def _put(b, key: str, t: pa.Table) -> None:
    sink = pa.BufferOutputStream()
    pq.write_table(t, sink, compression=CODEC)
    b.blob(key).upload_from_string(sink.getvalue().to_pybytes())


def _queue_order(plan: dict) -> list[dict]:
    """Every shard, biggest first (so the stragglers start early)."""
    return sorted(plan["shards"], key=lambda s: -s["rows"])


# ── Building: the roots files and the rollups ──────────────────────────────

#: Rows per row group of the roots and rollup files (the decode unit; the Worker reads whole groups).
ROOT_RG = 8192
ROOT_SCHEMA = pa.schema([
    pa.field("q", pa.string(), nullable=False),
    pa.field("path", pa.string(), nullable=False),
    pa.field("usr", pa.string(), nullable=False),
    pa.field("vf", pa.int64(), nullable=False),
    pa.field("vt", pa.int64(), nullable=False),
    pa.field("size", pa.int64(), nullable=False),
    pa.field("n_files", pa.int64(), nullable=False),
])
#: `kind` 0: the (q, dir) header — `child` '', `vf` = the children kept, `b` = root rows under dir, `o` =
#: its children holding or being roots; 1: a kept child's cells; 2: the remainder's cells (every other
#: child, summed; `child` '').
ROLLUP_SCHEMA = pa.schema([
    pa.field("q", pa.string(), nullable=False),
    pa.field("dir", pa.string(), nullable=False),
    pa.field("kind", pa.int8(), nullable=False),
    pa.field("child", pa.string(), nullable=False),
    pa.field("vf", pa.int64(), nullable=False),
    pa.field("b", pa.int64(), nullable=False),
    pa.field("o", pa.int64(), nullable=False),
])
#: Per row group: its exact first and last `(q, key)` (`key` = `path` for roots, `dir` for rollups), byte span,
#: rows, and per column `(data_page_offset, total_compressed_size, dictionary_page_offset or 0)`.
GROUP_INDEX_SCHEMA = pa.schema([
    pa.field("file", pa.string(), nullable=False),
    pa.field("rg", pa.int32(), nullable=False),
    pa.field("q_min", pa.string(), nullable=False),
    pa.field("k_min", pa.string(), nullable=False),
    pa.field("q_max", pa.string(), nullable=False),
    pa.field("k_max", pa.string(), nullable=False),
    pa.field("offset", pa.int64(), nullable=False),
    pa.field("length", pa.int64(), nullable=False),
    pa.field("rows", pa.int32(), nullable=False),
    pa.field("chunks", pa.list_(pa.int64()), nullable=False),
])


def write_indexed(batches, out: Path, schema: pa.Schema, key: str, name: str, dictionary: list[str]) -> tuple[int, pa.Table]:
    """Write sorted batches as `ROOT_RG`-row groups and cut their `GROUP_INDEX_SCHEMA` rows (`file` = `name`)."""
    from .static_names import write_sorted

    stats: list[tuple] = []

    def on_group(g: pa.Table) -> None:
        qc, kc = g.column("q"), g.column(key)
        stats.append((qc[0].as_py(), kc[0].as_py(), qc[g.num_rows - 1].as_py(), kc[g.num_rows - 1].as_py()))

    rows = write_sorted(batches, out, schema, ROOT_RG, on_group=on_group, dictionary=dictionary)
    md = pq.ParquetFile(out).metadata
    if md.num_row_groups != len(stats):
        raise RuntimeError(f"{out}: {md.num_row_groups} row groups, {len(stats)} recorded")
    cols = {k: [] for k in GROUP_INDEX_SCHEMA.names}
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        chunks, starts, ends = [], [], []
        for c in range(rg.num_columns):
            cc = rg.column(c)
            dict_off = cc.dictionary_page_offset or 0
            start = min(dict_off, cc.data_page_offset) if dict_off else cc.data_page_offset
            chunks += [cc.data_page_offset, cc.total_compressed_size, dict_off]
            starts.append(start)
            ends.append(start + cc.total_compressed_size)
        for k, v in zip(GROUP_INDEX_SCHEMA.names, (name, g, *stats[g], min(starts), max(ends) - min(starts), rg.num_rows, chunks)):
            cols[k].append(v)
    return rows, pa.table(cols, schema=GROUP_INDEX_SCHEMA)


def rollup_sql(con, rt: str, R: int, K: int) -> str:
    """Rollup cells (`ROLLUP_SCHEMA`, a query) of `rt`'s (`ROOT_COLS`, every root of its members) heavy
    directories: `(q, dir)` with more than `R` root rows strictly under it. Per heavy `(q, dir)`, per child
    of `dir` (its next path segment), the running Σ size, n_files of the roots at or under the child, one
    cell per change (`+` at `vf`, `−` at `vt`); the `K` children with the largest peak bytes (ties by name)
    are kept by name, the rest summed into the remainder. Heavy directories are closed upwards (their
    ancestors are heavy), so the roots are exploded one depth at a time over a shrinking candidate set."""
    con.execute("DROP TABLE IF EXISTS hrp")
    con.execute(f"CREATE TABLE hrp AS SELECT q, depth, path, count(*)::BIGINT AS n, count(*) FILTER (WHERE vt = {OPEN})::BIGINT AS n_open FROM {rt} GROUP BY q, depth, path")
    dirs, _, _ = dir_stats(con, "hrp", R + 1)
    con.execute("DROP TABLE hrp")
    con.execute("CREATE OR REPLACE TABLE hv (q VARCHAR, k INTEGER, dir VARCHAR, rows BIGINT, children BIGINT)")
    if dirs.num_rows:
        con.register("hv_in", dirs.select(["q", "k", "dir", "rows", "children"]))
        con.execute("INSERT INTO hv SELECT * FROM hv_in")
        con.unregister("hv_in")
    con.execute("CREATE OR REPLACE TABLE rev (q VARCHAR, dir VARCHAR, child VARCHAR, t BIGINT, db HUGEINT, dn HUGEINT)")
    con.execute(f"CREATE OR REPLACE TABLE hc AS SELECT q, string_split(path, '/') AS segs, vf, vt, size, n_files FROM {rt} SEMI JOIN (SELECT DISTINCT q FROM hv) USING (q)")
    top = con.execute("SELECT coalesce(max(k), 0) FROM hv").fetchone()[0]
    for k in range(1, top + 1):
        con.execute(f"""CREATE OR REPLACE TABLE hc AS SELECT hc.* FROM hc SEMI JOIN (SELECT q, dir FROM hv WHERE k = {k}) AS h
            ON hc.q = h.q AND array_to_string(hc.segs[1:{k}], '/') = h.dir""")
        hit = f"SELECT q, array_to_string(segs[1:{k}], '/') AS dir, segs[{k + 1}] AS child, vf, vt, size, n_files FROM hc"
        con.execute(f"""INSERT INTO rev SELECT q, dir, child, t, sum(db), sum(dn) FROM (
                SELECT q, dir, child, vf AS t, size AS db, n_files AS dn FROM ({hit})
                UNION ALL SELECT q, dir, child, vt AS t, -size, -n_files FROM ({hit}) WHERE vt <> {OPEN}
            ) GROUP BY q, dir, child, t""")
    con.execute("DROP TABLE hc")
    run = """sum(db) OVER (PARTITION BY q, dir, child ORDER BY t ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)::BIGINT AS b,
             sum(dn) OVER (PARTITION BY q, dir, child ORDER BY t ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)::BIGINT AS o"""
    con.execute(f"""CREATE OR REPLACE TABLE rkeep AS SELECT q, dir, child FROM (
            SELECT q, dir, child, row_number() OVER (PARTITION BY q, dir ORDER BY peak DESC, child) AS r FROM (
                SELECT q, dir, child, max(b) AS peak FROM (SELECT q, dir, child, {run} FROM rev) GROUP BY q, dir, child))
        WHERE r <= {K}""")
    con.execute("""CREATE OR REPLACE TABLE rev2 AS
            SELECT q, dir, 1::TINYINT AS kind, child, t, db, dn FROM rev SEMI JOIN rkeep USING (q, dir, child)
            UNION ALL SELECT q, dir, 2::TINYINT, '', t, sum(db), sum(dn) FROM rev ANTI JOIN rkeep USING (q, dir, child) GROUP BY q, dir, t""")
    con.execute("DROP TABLE rev")
    cells = f"""SELECT q, dir, kind, child, t AS vf,
            sum(db) OVER (PARTITION BY q, dir, kind, child ORDER BY t ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)::BIGINT AS b,
            sum(dn) OVER (PARTITION BY q, dir, kind, child ORDER BY t ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)::BIGINT AS o
        FROM (SELECT q, dir, kind, child, t, sum(db) AS db, sum(dn) AS dn FROM rev2 GROUP BY ALL HAVING sum(db) <> 0 OR sum(dn) <> 0)"""
    return f"""SELECT h.q, h.dir, 0::TINYINT AS kind, '' AS child, (SELECT count(*) FROM rkeep AS r WHERE r.q = h.q AND r.dir = h.dir)::BIGINT AS vf,
            h.rows AS b, h.children AS o FROM hv AS h
        UNION ALL {cells}"""


def build_roots(con, rt: str, R: int, K: int, out: Path, name: str) -> dict:
    """`rt`'s roots (every root of its members) → `out/roots/<name>.parquet` (sorted `(q, path, usr, vf)`) and
    `out/rollups/<name>.parquet` (sorted `(q, dir, kind, child, vf)`), with their group indexes
    `out/{roots,rollups}-index/<name>.parquet`; returns the counts."""
    t0 = monotonic()
    rows, idx = write_indexed(_batches(con, f"SELECT q, path, usr, vf, vt, size, n_files FROM {rt} ORDER BY q, path, usr, vf"),
                              out / "roots" / f"{name}.parquet", ROOT_SCHEMA, "path", f"roots/{name}.parquet", ["q", "usr"])
    t1 = monotonic()
    sql = rollup_sql(con, rt, R, K)
    con.execute(f"CREATE OR REPLACE TABLE rcells AS {sql}")
    cells, ridx = write_indexed(_batches(con, "SELECT * FROM rcells ORDER BY q, dir, kind, child, vf"), out / "rollups" / f"{name}.parquet",
                                ROLLUP_SCHEMA, "dir", f"rollups/{name}.parquet", ["q", "dir", "child"])
    heavy = con.execute("SELECT count(*) FROM hv").fetchone()[0]
    for sub, t in (("roots-index", idx), ("rollups-index", ridx)):
        (out / sub).mkdir(parents=True, exist_ok=True)
        pq.write_table(t, out / sub / f"{name}.parquet", compression=CODEC)
    for t in ("rcells", "rev2", "rkeep", "hv"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    return {"roots": rows, "roots_bytes": (out / "roots" / f"{name}.parquet").stat().st_size, "roots_groups": idx.num_rows,
            "heavy_dirs": heavy, "rollup_cells": cells, "rollup_bytes": (out / "rollups" / f"{name}.parquet").stat().st_size,
            "roots_s": round(t1 - t0, 1), "rollups_s": round(monotonic() - t1, 1)}



# ── Reading (the Worker's logic) ───────────────────────────────────────────


class GroupFile:
    """Row groups of one roots or rollups set, through its group index: the groups that can hold keys in
    `[lo, hi)` (contiguous: the index is sorted by `(q_min, k_min)` and the keys are totally ordered) and
    their rows from one ranged read per file."""

    def __init__(self, index: pa.Table, fetch, size_of):
        from .static_names import Spans

        self.groups = index.sort_by([("q_min", "ascending"), ("k_min", "ascending")]).to_pylist()
        self.lo = [(g["q_min"], g["k_min"]) for g in self.groups]
        self.hi = [(g["q_max"], g["k_max"]) for g in self.groups]
        self.fetch, self.size_of, self.Spans = fetch, size_of, Spans
        self.foot: dict[str, tuple[int, bytes]] = {}

    def span(self, lo: tuple[str, str], hi: tuple[str, str]) -> tuple[int, int]:
        """Groups `[a, b)` whose key range meets `[lo, hi)`."""
        from bisect import bisect_left

        a = bisect_left(self.hi, lo)
        b = bisect_left(self.lo, hi)
        return a, max(a, b)

    def upper(self, lo, hi) -> int:
        a, b = self.span(lo, hi)
        return sum(g["rows"] for g in self.groups[a:b])

    def footer(self, file: str) -> tuple[int, bytes]:
        if file not in self.foot:
            size = self.size_of(file)
            tail = self.fetch(file, size - 8, size)
            flen = int.from_bytes(tail[:4], "little")
            data = self.fetch(file, max(0, size - max(8 + flen, 1 << 16)), size)  # pyarrow reads up to 64 KB of tail
            self.foot[file] = (size - len(data), data)
        return self.foot[file]

    def read(self, lo, hi) -> tuple[list[dict], dict]:
        a, b = self.span(lo, hi)
        rows, io = [], {"groups": b - a, "bytes": 0, "rows_read": 0}
        by_file: dict[str, list[dict]] = {}
        for g in self.groups[a:b]:
            by_file.setdefault(g["file"], []).append(g)
        for file, gs in by_file.items():
            start, end = gs[0]["offset"], gs[-1]["offset"] + gs[-1]["length"]
            data = self.fetch(file, start, end)
            io["bytes"] += len(data)
            fstart, foot = self.footer(file)
            pf = pq.ParquetFile(self.Spans(self.size_of(file), [(fstart, foot), (start, data)]))
            for r in pf.read_row_groups([g["rg"] for g in gs]).to_pylist():
                io["rows_read"] += 1
                if "path" in r:
                    key = (r["q"], r["path"])
                else:
                    key = (r["q"], r["dir"])
                if lo <= key < hi:
                    rows.append(r)
        return rows, io


class Drill:
    """A heavy term's filtered view at a directory `P`: per child of `P`, Σ size, n_files of the roots under
    it live on a date. Dispatch from the indexes alone: if the roots index bounds `[(q, P/), (q, P0))` at
    ≤ `R + 2·rg` rows (`rg` = the files' row-group size), read them; else `(q, P)` is heavy (its true rows >
    R), so its rollup holds the kept children (`answers[date]`) and the remainder (`rest[date]`)."""

    def __init__(self, roots: GroupFile, rollups: GroupFile, R: int, rg: int = ROOT_RG):
        self.roots, self.rollups, self.R, self.rg = roots, rollups, R, rg

    def view(self, term: str, P: str, dates: list[str]) -> dict:
        from .static_names import scan_epoch

        t = term.lower()
        if t in P.lower():
            return {"q": t, "P": P, "source": "plain", "answers": None}
        lo, hi = (t, P + "/"), (t, P + "0")
        ub = self.roots.upper(lo, hi)
        out: dict = {"q": t, "P": P, "upper": ub}
        if ub <= self.R + 2 * self.rg:
            rows, io = self.roots.read(lo, hi)
            out.update(source="roots", io=io, rows=len(rows), answers={})
            for d in dates:
                D = scan_epoch(d)
                acc: dict[str, list[int]] = {}
                for r in rows:
                    if r["vf"] <= D < r["vt"]:
                        c = r["path"][len(P) + 1:].split("/", 1)[0]
                        e = acc.setdefault(c, [0, 0])
                        e[0] += r["size"]
                        e[1] += r["n_files"]
                out["answers"][d] = {k: v for k, v in sorted(acc.items()) if v != [0, 0]}
            return out
        rows, io = self.rollups.read((t, P), (t, P + "\x00"))
        out.update(source="rollup", io=io, rows=len(rows), answers={}, rest={})
        if not rows or rows[0]["kind"] != 0:
            raise RuntimeError(f"({t!r}, {P!r}): {ub:,} root rows bound, but no rollup")
        out["header"] = {"kept": rows[0]["vf"], "rows": rows[0]["b"], "children": rows[0]["o"]}
        for d in dates:
            D = scan_epoch(d)
            cur: dict[tuple[int, str], tuple[int, int]] = {}
            for r in rows[1:]:
                if r["vf"] <= D:
                    cur[(r["kind"], r["child"])] = (r["b"], r["o"])
            out["answers"][d] = {c: list(v) for (kd, c), v in sorted(cur.items()) if kd == 1 and v != (0, 0)}
            out["rest"][d] = list(cur.get((2, ""), (0, 0)))
        return out


# ── CLI ────────────────────────────────────────────────────────────────────


@group("roots")
def cli() -> None:
    """Match roots of the catalog members: measurement, the roots files, rollups (specs/architecture/static-name-search.md)."""


MEASURE = "roots-measure"


@cli.command("measure")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-f", "--floor", "floor_rows", default=10_000, type=int, help="Keep directories with at least this many root rows under them")
@option("-g", "--gen", required=True, help="Generation")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-l", "--lease", default=5400, type=int, help="Seconds after which another task may take over a claimed, unfinished shard")
@option("-m", "--mount", required=True, help="Local mount of the bucket (for `catalog/members.parquet`)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-o", "--only", help="Only these shards (comma-separated indices)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-S", "--scratch", default=SCRATCH_BUCKET, help="Bucket holding the queue's claims")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir, and where each shard is downloaded")
def measure_cmd(bucket, floor_rows, gen, index, lease, mount, mem, only, threads, scratch, tmp) -> None:
    """Long members' roots counted per shard (a shared queue, biggest shard first) → `roots-measure/{q,qdepth,dirs,hist}/s####.parquet`
    (`q_stats`, `dir_stats`); a shard whose `q/` file exists is skipped."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    plan = read_json(f"gs://{bucket}/{prefix}/shards.json")
    t = _task(index)
    client = storage.Client()
    b = client.bucket(bucket)
    queue = Queue(client.bucket(scratch), prefix, "roots-measure", t, lease)
    con = connect(threads, mem, tmp)
    con.execute(f"CREATE TABLE allm AS SELECT q, shard, rows FROM read_parquet({q(f'{mount}/{prefix}/catalog/members.parquet')})")
    t_start, n_done = monotonic(), 0
    keep = {int(x) for x in only.split(",")} if only else None
    for s in _queue_order(plan):
        name = f"s{s['i']:04d}"
        if keep is not None and s["i"] not in keep:
            continue
        if b.blob(f"{prefix}/{MEASURE}/q/{name}.parquet").exists() or not queue.claim(name):
            continue
        t0 = monotonic()
        con.execute(f"CREATE OR REPLACE TABLE mem AS SELECT q, rows FROM allm WHERE shard = {s['i']}")
        n_members = con.execute("SELECT count(*) FROM mem").fetchone()[0]
        con.execute("DROP TABLE IF EXISTS rp")
        if n_members:
            src = Path(tmp) / f"sx-{name}.parquet"
            b.blob(f"{prefix}/sx/{name}.parquet").download_to_filename(str(src))
            err(f"measure {name}: {s['rows']:,} rows, {n_members:,} members, downloaded in {monotonic() - t0:.1f}s")
            wheres = chunk_wheres(str(src))
            for k, where in enumerate(wheres):
                member_roots(con, sx_rows_sql(f"(SELECT * FROM read_parquet({q(str(src))}) WHERE {where})"), "mem", "rp", agg=True)
                err(f"measure {name}: chunk {k + 1}/{len(wheres)} in {monotonic() - t0:.1f}s")
            src.unlink()
        con.execute(f"CREATE TABLE IF NOT EXISTS rp ({RP_COLS})")
        t1 = monotonic()
        n_rp, n_rows = con.execute("SELECT count(*), coalesce(sum(n), 0) FROM rp").fetchone()
        qs, qd = q_stats(con, "rp")
        dirs, hist, _ = dir_stats(con, "rp", floor_rows)
        for sub, tab in (("qdepth", qd), ("dirs", dirs), ("hist", hist), ("q", qs)):
            _put(b, f"{prefix}/{MEASURE}/{sub}/{name}.parquet", tab)
        n_done += 1
        doc = {"shard": s["i"], "rows": s["rows"], "members": n_members, "root_rows": int(n_rows), "root_paths": n_rp, "dirs": dirs.num_rows,
               "s": round(monotonic() - t0, 1), "stats_s": round(monotonic() - t1, 1)}
        err(f"measure {name}: {n_rows:,} root rows, {n_rp:,} root paths, {dirs.num_rows:,} dirs ≥ {floor_rows:,} in {doc['s']}s "
            f"(stats {doc['stats_s']}s; task {t}: {n_done} shards in {monotonic() - t_start:.0f}s)")
        print(json.dumps(doc), flush=True)
        con.execute("DROP TABLE rp")


def _partition(parts: int) -> str:
    """A version's subtree partition: by its first two path segments, so every directory at depth ≥ 2 has
    all its descendants in one partition (depth-1 directories are summed across partitions)."""
    return f"(hash(split_part(path, '/', 1) || '/' || split_part(path, '/', 2)) % {parts})"


def download(bucket, keys: list[str], dst: Path, workers: int = 16) -> list[Path]:
    dst.mkdir(parents=True, exist_ok=True)

    def one(key: str) -> Path:
        p = dst / Path(key).name
        if not p.exists():
            bucket.blob(key).download_to_filename(str(p))
        return p

    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(one, keys))


@cli.command("measure-short")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-f", "--floor", "floor_rows", default=10_000, type=int, help="Keep directories with at least this many root rows under them")
@option("-g", "--gen", required=True, help="Generation (its `cintervals/`)")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX): the subtree partition")
@option("-m", "--mount", required=True, help="Local mount of the bucket (unused: versions are downloaded)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--parts", default=32, type=IntRange(min=1), help="Subtree partitions (= tasks)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir, and where the versions are downloaded")
def measure_short_cmd(bucket, floor_rows, gen, index, mount, mem, parts, threads, tmp) -> None:
    """One- and two-character literals' roots counted over one subtree partition of the coalesced versions →
    `roots-measure/short/{q,qdepth,dirs,hist,top}/p###.parquet` (`top`: the partial depth-1 directories)."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    t = _task(index)
    client = storage.Client()
    b = client.bucket(bucket)
    name = f"p{t:03d}"
    if b.blob(f"{prefix}/{MEASURE}/short/q/{name}.parquet").exists():
        err(f"measure-short {name}: done")
        return
    t0 = monotonic()
    keys = sorted(x.name for x in client.list_blobs(bucket, prefix=f"{prefix}/cintervals/") if x.name.endswith(".parquet"))
    local = download(b, keys, Path(tmp) / "cintervals")
    err(f"measure-short {name}: {len(local)} version files downloaded in {monotonic() - t0:.1f}s")
    con = connect(threads, mem, tmp)
    versions = f"SELECT * FROM read_parquet({q(str(Path(tmp) / 'cintervals' / '*.parquet'))}) WHERE {_partition(parts)} = {t}"
    short_roots(con, versions, "rp", agg=True)
    n_rp, n_rows = con.execute("SELECT count(*), coalesce(sum(n), 0) FROM rp").fetchone()
    err(f"measure-short {name}: {n_rows:,} root rows, {n_rp:,} root paths in {monotonic() - t0:.1f}s")
    qs, qd = q_stats(con, "rp")
    dirs, hist, top = dir_stats(con, "rp", floor_rows, partial_top=True)
    for sub, tab in (("qdepth", qd), ("dirs", dirs), ("hist", hist), ("top", top), ("q", qs)):
        _put(b, f"{prefix}/{MEASURE}/short/{sub}/{name}.parquet", tab)
    doc = {"part": t, "root_rows": int(n_rows), "root_paths": n_rp, "dirs": dirs.num_rows, "s": round(monotonic() - t0, 1)}
    err(f"measure-short {name}: {dirs.num_rows:,} dirs ≥ {floor_rows:,}, done in {doc['s']}s")
    print(json.dumps(doc), flush=True)
    shutil.rmtree(Path(tmp) / "cintervals")


@cli.command("measure-report")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-R", "--read-rows", "read_rows", default=100_000, type=int, help="A drill's read bound (root rows under the directory)")
@option("-t", "--top", default=50, type=int, help="Members listed by root rows")
def measure_report_cmd(bucket, gen, read_rows, top) -> None:
    """Summarize `roots-measure/` (long members per shard, short literals per subtree partition, depth-1
    partials summed): totals, quantiles, the top members, and the directories whose roots exceed the read
    bound (`-R`) by depth, with their children (rollup sizing). JSON on stdout."""
    import tempfile

    import duckdb
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}/{MEASURE}"
    client = storage.Client()
    with tempfile.TemporaryDirectory() as d:
        n = 0
        for blob in client.list_blobs(bucket, prefix=prefix + "/"):
            p = Path(d) / blob.name.removeprefix(prefix + "/")
            p.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(p))
            n += 1
        con = duckdb.connect()
        rp = lambda sub: f"read_parquet({q(d + '/' + sub + '/*.parquet')})"  # noqa: E731
        has_short = (Path(d) / "short" / "q").exists()
        con.execute(f"CREATE TABLE ql AS SELECT 'long' AS kind, * FROM {rp('q')}")
        con.execute(f"CREATE TABLE dl AS SELECT 'long' AS kind, * FROM {rp('dirs')}")
        con.execute(f"CREATE TABLE hl AS SELECT 'long' AS kind, * FROM {rp('hist')}")
        if has_short:
            con.execute(f"""INSERT INTO ql SELECT 'short', q, sum(rows), sum(paths), sum(open_rows), sum(open_paths), sum(d1_paths), min(min_depth), max(max_depth)
                FROM {rp('short/q')} GROUP BY q""")
            con.execute(f"""CREATE TABLE stop AS SELECT q, k, dir, sum(rows)::BIGINT AS rows, sum(paths)::BIGINT AS paths, sum(open_rows)::BIGINT AS open_rows,
                sum(children)::BIGINT AS children, sum(direct)::BIGINT AS direct, max(max_child)::BIGINT AS max_child FROM {rp('short/top')} GROUP BY q, k, dir""")
            con.execute(f"INSERT INTO dl SELECT 'short', * FROM {rp('short/dirs')}")
            con.execute("INSERT INTO dl SELECT 'short', * FROM stop WHERE rows >= 10000")
            con.execute(f"INSERT INTO hl SELECT 'short', * FROM {rp('short/hist')}")
            con.execute("INSERT INTO hl SELECT 'short', q, 1, floor(log2(rows))::INTEGER AS lg, count(*), sum(rows) FROM stop GROUP BY q, lg")
        out: dict = {"gen": gen, "files": n, "read_rows": read_rows}
        for kind in ("long", "short"):
            r = con.execute(f"""SELECT count(*), sum(rows), sum(paths), sum(open_rows), sum(open_paths),
                    quantile_disc(rows, [0.5, 0.9, 0.99, 0.999]), max(rows), count(*) FILTER (WHERE rows > {read_rows})
                FROM ql WHERE kind = '{kind}'""").fetchone()
            if not r[0]:
                continue
            out[kind] = {"members": r[0], "root_rows": int(r[1]), "root_paths": int(r[2]), "open_rows": int(r[3]), "open_paths": int(r[4]),
                         "rows_q50_q90_q99_q999": [int(x) for x in r[5]], "rows_max": int(r[6]), "members_over_read_rows": r[7],
                         "top": [dict(zip(("q", "rows", "paths", "open_rows", "d1_paths", "max_depth"), x)) for x in con.execute(
                             f"SELECT q, rows, paths, open_rows, d1_paths, max_depth FROM ql WHERE kind = '{kind}' ORDER BY rows DESC, q LIMIT {top}").fetchall()]}
            heavy = con.execute(f"""SELECT k, count(*), count(DISTINCT q), sum(children), sum(least(children, 256)), max(rows), max(children),
                    quantile_disc(children, 0.5), quantile_disc(children, 0.99), sum(rows)
                FROM dl WHERE kind = '{kind}' AND rows > {read_rows} GROUP BY k ORDER BY k""").fetchall()
            out[kind]["heavy_dirs_by_depth"] = [dict(zip(("k", "dirs", "members", "children", "children_capped_256", "max_rows", "max_children",
                                                          "children_q50", "children_q99", "rows"), [int(v) for v in x])) for x in heavy]
            tops = [t["q"] for t in out[kind]["top"][:20]]
            prof = {}
            for t in tops:
                prof[t] = {}
                for k, lg, dirs, rows in con.execute(f"SELECT k, lg, dirs, rows FROM hl WHERE kind = '{kind}' AND q = ? ORDER BY k, lg", [t]).fetchall():
                    prof[t].setdefault(str(k), {})[f"2^{lg}"] = int(dirs)
            out[kind]["dir_rows_histogram_top20"] = prof
        print(json.dumps(out, indent=1, default=str))


DRILL = "drill"


def _upload_dir(b, local: Path, prefix: str) -> None:
    for f in sorted(p for p in local.rglob("*") if p.is_file()):
        blob = b.blob(f"{prefix}/{f.relative_to(local).as_posix()}")
        blob.chunk_size = 64 << 20
        blob.upload_from_filename(str(f))


@cli.command("build")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-K", "--keep", "K", default=1000, type=int, help="Children kept by name per heavy directory (the rest: the remainder)")
@option("-l", "--lease", default=5400, type=int, help="Seconds after which another task may take over a claimed, unfinished shard")
@option("-m", "--mount", required=True, help="Local mount of the bucket (for `catalog/members.parquet`)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-o", "--only", help="Only these shards (comma-separated indices)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-R", "--read-rows", "R", default=100_000, type=int, help="Directories with more root rows under them get rollups")
@option("-S", "--scratch", default=SCRATCH_BUCKET, help="Bucket holding the queue's claims")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir, and where each shard is downloaded")
def build_cmd(bucket, gen, index, K, lease, mount, mem, only, threads, R, scratch, tmp) -> None:
    """Long members' roots and rollups per shard (a shared queue, biggest first) → `drill/long/{roots,rollups,roots-index,rollups-index}/s####.parquet`;
    a shard whose `rollups-index/` file exists is skipped."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    plan = read_json(f"gs://{bucket}/{prefix}/shards.json")
    t = _task(index)
    client = storage.Client()
    b = client.bucket(bucket)
    queue = Queue(client.bucket(scratch), prefix, "drill-build", t, lease)
    con = connect(threads, mem, tmp)
    con.execute(f"CREATE TABLE allm AS SELECT q, shard, rows FROM read_parquet({q(f'{mount}/{prefix}/catalog/members.parquet')})")
    keep = {int(x) for x in only.split(",")} if only else None
    t_start, n_done = monotonic(), 0
    for s in _queue_order(plan):
        name = f"s{s['i']:04d}"
        if keep is not None and s["i"] not in keep:
            continue
        if b.blob(f"{prefix}/{DRILL}/long/rollups-index/{name}.parquet").exists() or not queue.claim(name):
            continue
        t0 = monotonic()
        con.execute(f"CREATE OR REPLACE TABLE mem AS SELECT q, rows FROM allm WHERE shard = {s['i']}")
        n_members = con.execute("SELECT count(*) FROM mem").fetchone()[0]
        con.execute(f"DROP TABLE IF EXISTS rt; CREATE TABLE rt ({ROOT_COLS})")
        if n_members:
            src = Path(tmp) / f"sx-{name}.parquet"
            b.blob(f"{prefix}/sx/{name}.parquet").download_to_filename(str(src))
            err(f"build {name}: {s['rows']:,} rows, {n_members:,} members, downloaded in {monotonic() - t0:.1f}s")
            wheres = chunk_wheres(str(src))
            for k, where in enumerate(wheres):
                member_roots(con, sx_rows_sql(f"(SELECT * FROM read_parquet({q(str(src))}) WHERE {where})"), "mem", "rt")
                err(f"build {name}: chunk {k + 1}/{len(wheres)} in {monotonic() - t0:.1f}s")
            src.unlink()
        out = Path(tmp) / "drill"
        shutil.rmtree(out, ignore_errors=True)
        doc = {"shard": s["i"], "members": n_members, **build_roots(con, "rt", R, K, out, name)}
        _upload_dir(b, out / "roots", f"{prefix}/{DRILL}/long/roots")
        _upload_dir(b, out / "rollups", f"{prefix}/{DRILL}/long/rollups")
        _upload_dir(b, out / "roots-index", f"{prefix}/{DRILL}/long/roots-index")
        _upload_dir(b, out / "rollups-index", f"{prefix}/{DRILL}/long/rollups-index")
        shutil.rmtree(out)
        con.execute("DROP TABLE rt")
        n_done += 1
        doc["s"] = round(monotonic() - t0, 1)
        err(f"build {name}: {doc['roots']:,} roots ({doc['roots_bytes']:,} B), {doc['heavy_dirs']:,} heavy dirs, {doc['rollup_cells']:,} cells "
            f"in {doc['s']}s (task {t}: {n_done} shards in {monotonic() - t_start:.0f}s)")
        print(json.dumps(doc), flush=True)


@cli.command("short-plan")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-g", "--gen", required=True, help="Generation (its `roots-measure/short/q/`)")
@option("-r", "--target-rows", default=150_000_000, type=int, help="Root rows per q-group")
def short_plan_cmd(bucket, gen, target_rows) -> None:
    """Cut the short literals into q-groups of about `-r` root rows (contiguous in code-point order) from the
    measurement → `drill/short-plan.json` (each group's first literal; printed too)."""
    import tempfile

    import duckdb
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    client = storage.Client()
    with tempfile.TemporaryDirectory() as d:
        for blob in client.list_blobs(bucket, prefix=f"{prefix}/{MEASURE}/short/q/"):
            blob.download_to_filename(f"{d}/{Path(blob.name).name}")
        per_q = duckdb.connect().execute(f"SELECT q, sum(rows)::BIGINT FROM read_parquet({q(d + '/*.parquet')}) GROUP BY q ORDER BY q").fetchall()
    groups, acc = [], 0
    for lit, n in per_q:
        if not groups or acc + n > target_rows:
            groups.append({"lo": lit, "rows": 0})
            acc = 0
        groups[-1]["rows"] += n
        acc += n
    doc = {"gen": gen, "target_rows": target_rows, "literals": len(per_q), "rows": sum(n for _, n in per_q), "groups": groups}
    client.bucket(bucket).blob(f"{prefix}/{DRILL}/short-plan.json").upload_from_string(json.dumps(doc, indent=1) + "\n")
    print(json.dumps(doc, indent=1))


def _group_table(con, plan: dict) -> None:
    con.execute("CREATE OR REPLACE TABLE qg (lo VARCHAR, grp INTEGER)")
    con.executemany("INSERT INTO qg VALUES (?, ?)", [(g["lo"], k) for k, g in enumerate(plan["groups"])])


@cli.command("short-map")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-g", "--gen", required=True, help="Generation (its `cintervals/`, `drill/short-plan.json`)")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX): the subtree partition")
@option("-m", "--mount", required=True, help="Local mount of the bucket (unused: versions are downloaded)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--parts", default=32, type=IntRange(min=1), help="Subtree partitions (= tasks)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-S", "--scratch", default=SCRATCH_BUCKET, help="Bucket for the shuffle (an intermediate)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir, and where the versions are downloaded")
def short_map_cmd(bucket, gen, index, mount, mem, parts, threads, scratch, tmp) -> None:
    """One subtree partition's short-literal roots, split by q-group → the scratch bucket's
    `static-names/GEN/drill-short-map/g###/p###.parquet` (a partition's marker: `drill-short-map/done/p###`)."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    t = _task(index)
    client = storage.Client()
    b, sb = client.bucket(bucket), client.bucket(scratch)
    name = f"p{t:03d}"
    if sb.blob(f"{prefix}/drill-short-map/done/{name}").exists():
        err(f"short-map {name}: done")
        return
    plan = read_json(f"gs://{bucket}/{prefix}/{DRILL}/short-plan.json")
    t0 = monotonic()
    keys = sorted(x.name for x in client.list_blobs(bucket, prefix=f"{prefix}/cintervals/") if x.name.endswith(".parquet"))
    download(b, keys, Path(tmp) / "cintervals")
    con = connect(threads, mem, tmp)
    short_roots(con, f"SELECT * FROM read_parquet({q(str(Path(tmp) / 'cintervals' / '*.parquet'))}) WHERE {_partition(parts)} = {t}", "rt")
    _group_table(con, plan)
    part = Path(tmp) / "smap"
    shutil.rmtree(part, ignore_errors=True)
    con.execute(f"COPY (SELECT rt.*, qg.grp FROM rt ASOF JOIN qg ON rt.q >= qg.lo) TO {q(str(part))} (FORMAT parquet, PARTITION_BY (grp), COMPRESSION zstd)")
    n = con.execute("SELECT count(*) FROM rt").fetchone()[0]
    for d in sorted(part.glob("grp=*")):
        g = int(d.name.split("=")[1])
        for k, f in enumerate(sorted(d.glob("*.parquet"))):
            sb.blob(f"{prefix}/drill-short-map/g{g:03d}/{name}-{k}.parquet").upload_from_filename(str(f))
    sb.blob(f"{prefix}/drill-short-map/done/{name}").upload_from_string(json.dumps({"rows": n}))
    shutil.rmtree(part)
    shutil.rmtree(Path(tmp) / "cintervals")
    err(f"short-map {name}: {n:,} roots in {monotonic() - t0:.1f}s")


@cli.command("short-reduce")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX): the first q-group")
@option("-K", "--keep", "K", default=1000, type=int, help="Children kept by name per heavy directory")
@option("-m", "--mount", required=True, help="Local mount of the bucket (the scratch bucket mounted beside it)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--stride", default=1, type=IntRange(min=1), help="Task i builds q-groups i, i + n, i + 2n, …")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-R", "--read-rows", "R", default=100_000, type=int, help="Directories with more root rows under them get rollups")
@option("-S", "--scratch", default=SCRATCH_BUCKET, help="Bucket holding the shuffle")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def short_reduce_cmd(bucket, gen, index, K, mount, mem, stride, threads, R, scratch, tmp) -> None:
    """Each of the task's q-groups: every partition's roots of its literals → `drill/short/{roots,rollups,roots-index,rollups-index}/g###.parquet`."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    plan = read_json(f"gs://{bucket}/{prefix}/{DRILL}/short-plan.json")
    t = _task(index)
    client = storage.Client()
    b = client.bucket(bucket)
    smount = str(Path(mount).parent / scratch)
    con = connect(threads, mem, tmp)
    for g in range(t, len(plan["groups"]), stride):
        name = f"g{g:03d}"
        if b.blob(f"{prefix}/{DRILL}/short/rollups-index/{name}.parquet").exists():
            err(f"short-reduce {name}: done")
            continue
        t0 = monotonic()
        src = Path(tmp) / "sred"
        shutil.rmtree(src, ignore_errors=True)
        keys = [x.name for x in client.list_blobs(scratch, prefix=f"{prefix}/drill-short-map/{name}/")]
        download(client.bucket(scratch), keys, src)
        con.execute(f"DROP TABLE IF EXISTS rt; CREATE TABLE rt AS SELECT q, depth, path, usr, vf, vt, size, n_files FROM read_parquet({q(str(src / '*.parquet'))})")
        out = Path(tmp) / "drill"
        shutil.rmtree(out, ignore_errors=True)
        doc = {"group": g, "files": len(keys), **build_roots(con, "rt", R, K, out, name)}
        for sub in ("roots", "rollups", "roots-index", "rollups-index"):
            _upload_dir(b, out / sub, f"{prefix}/{DRILL}/short/{sub}")
        shutil.rmtree(out)
        shutil.rmtree(src)
        doc["s"] = round(monotonic() - t0, 1)
        err(f"short-reduce {name}: {doc['roots']:,} roots, {doc['heavy_dirs']:,} heavy dirs, {doc['rollup_cells']:,} cells in {doc['s']}s")
        print(json.dumps(doc), flush=True)


@cli.command("index")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-K", "--keep", "K", default=1000, type=int, help="The build's -K (recorded)")
@option("-R", "--read-rows", "R", default=100_000, type=int, help="The build's -R (recorded)")
def index_cmd(bucket, gen, K, R) -> None:
    """Concatenate the per-file group indexes into `drill/{long,short}-{roots,rollups}-index.parquet` (`file`
    relative to `drill/`, sorted `(q_min, k_min)`) and write `drill/meta.json`; prints it."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}/{DRILL}"
    client = storage.Client()
    b = client.bucket(bucket)
    meta: dict = {"gen": gen, "R": R, "K": K, "rg": ROOT_RG, "dispatch_rows": R + 2 * ROOT_RG}
    for kind in ("long", "short"):
        for sub in ("roots", "rollups"):
            tabs = []
            for blob in sorted(client.list_blobs(bucket, prefix=f"{prefix}/{kind}/{sub}-index/"), key=lambda x: x.name):
                t = pq.read_table(pa.BufferReader(blob.download_as_bytes()))
                tabs.append(t.set_column(0, "file", pa.array([f"{kind}/{f}" for f in t.column("file").to_pylist()], pa.string())))
            t = pa.concat_tables(tabs).sort_by([("q_min", "ascending"), ("k_min", "ascending")]) if tabs else None
            if t is None:
                continue
            his = list(zip(t.column("q_max").to_pylist(), t.column("k_max").to_pylist()))
            los = list(zip(t.column("q_min").to_pylist(), t.column("k_min").to_pylist()))
            if any(his[i] > los[i + 1] for i in range(len(los) - 1)):
                raise RuntimeError(f"{kind} {sub}: row groups overlap")
            sink = pa.BufferOutputStream()
            pq.write_table(t, sink, compression=CODEC)
            body = sink.getvalue().to_pybytes()
            b.blob(f"{prefix}/{kind}-{sub}-index.parquet").upload_from_string(body)
            files = {f for f in t.column("file").to_pylist()}
            nbytes = sum(int(x.size) for x in client.list_blobs(bucket, prefix=f"{prefix}/{kind}/{sub}/"))
            meta[f"{kind}_{sub}"] = {"files": len(files), "row_groups": t.num_rows, "rows": int(pa.compute.sum(t.column("rows")).as_py()),
                                     "bytes": nbytes, "index_bytes": len(body)}
    b.blob(f"{prefix}/meta.json").upload_from_string(json.dumps(meta, indent=1) + "\n")
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    cli()
