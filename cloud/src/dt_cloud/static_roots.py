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
import pyarrow.parquet as pq
from click import IntRange, group, option

from .static_catalog import CHUNK_ROWS, PARENT
from .static_names import (
    CODEC, DATA_BUCKET, NAME, OPEN, PREFIX, SCRATCH_BUCKET, _task, connect, err, q, read_json,
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


if __name__ == "__main__":
    cli()
