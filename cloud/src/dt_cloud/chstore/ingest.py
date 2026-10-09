"""`dt-cloud ch-ingest`: one scan into the store, append-only (specs/ch-store.md §3).

The scan's path store (`path-index.parquet`, the `path` sort: a v2 store
generation, or a v1 dirs-only index) is read where the server can read it
(its `user_files`; a local or `gs://` file is first streamed there) and
compared with the versions the previous scan sees, one key range at a time
(both sides read by `(depth, path)` range: the parquet by its row-group
statistics, `nodes` by its primary key), each range a `FULL JOIN` whose
differing keys alone flow on:

- a key new, or whose values changed: a version opens (`nodes`, `vf` = the
  scan);
- a key gone, or changed: its version closes (`closures`, `vt` = the scan);
- both, day to day, as `changes` events.

Nothing already written is rewritten: a day costs one read of the scan and
of the open versions, and writes of its churn. Then the day's new names go
into `names` and the scan into `scans`.

Idempotent and re-runnable: a scan already in `scans` is a no-op; anything
else first drops what an interrupted attempt wrote — the scan's own
partitions of `nodes`, `closures` and `changes` (partitioned by scan) — and
redoes it. Until its `scans` row lands a scan is invisible: no other scan's
reads see a version opened or closed after it. Scans are appended in order:
one older than the newest is refused (history is rebuilt, not back-filled).

Guards against a partial scan: a depth-1 root (bucket) the previous scan has
and this one lacks refuses the ingest (`allow_drop` overrides), as does a scan
of fewer than half the previous scan's rows (`force`)."""

from __future__ import annotations

import io
import json
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from .client import Ch, lit
from .schema import VALUE_COLS, VALUES_DDL, create, dt_lit, live, name_expr, scan_dt, scan_epochs

V2_INPUT = ("path String, usr Nullable(String), size Int64, depth Int32, kind String, n_files Int64, n_children Nullable(Int64), "
            "n_desc Nullable(Int64), mtime Nullable(Int64), mtime_mean Nullable(Float64), last_read Nullable(Int32), "
            "sum_storage_class_id_2 Nullable(Int64), sum_storage_class_id_3 Nullable(Int64), sum_storage_class_id_4 Nullable(Int64)")
V1_INPUT = "path String, depth Int64, usr Nullable(String), b Int64, o Int64, wts Nullable(Float64), wb Nullable(Int64), c2 Nullable(Int64), c3 Nullable(Int64), c4 Nullable(Int64), a Nullable(Int32)"

# Source columns → the store's.
V2_SELECT = """toUInt8(depth) AS depth, path, ifNull(usr, '') AS usr,
    if(kind = 'file', 'file', 'dir') AS kind, size, n_files, ifNull(n_children, -1) AS n_children, ifNull(n_desc, -1) AS n_desc,
    ifNull(mtime, -1) AS mtime, ifNull(mtime_mean, 0) AS mtime_mean, if(mtime_mean IS NULL, 0, size) AS mtime_w,
    ifNull(last_read, -1) AS last_read, ifNull(sum_storage_class_id_2, 0) AS c2, ifNull(sum_storage_class_id_3, 0) AS c3,
    ifNull(sum_storage_class_id_4, 0) AS c4"""
V1_SELECT = """toUInt8(depth) AS depth, path, ifNull(usr, '') AS usr,
    'dir' AS kind, b AS size, o AS n_files, -1 AS n_children, -1 AS n_desc, -1 AS mtime,
    if(ifNull(wb, 0) > 0, ifNull(wts, 0) / wb, 0) AS mtime_mean, ifNull(wb, 0) AS mtime_w,
    ifNull(a, -1) AS last_read, ifNull(c2, 0) AS c2, ifNull(c3, 0) AS c3, ifNull(c4, 0) AS c4"""
# The scan's rows for a key merged as `view.ts` `merge` would: a source can hold one slice twice (v1
# indexes have duplicate unattributed rows), and the Worker sums them.
MERGED = {"kind": "any(kind)", "size": "sum(size)", "n_files": "sum(n_files)", "n_children": "max(n_children)", "n_desc": "max(n_desc)",
          "mtime": "max(mtime)", "mtime_mean": "if(count() = 1, any(mtime_mean), sum(mtime_mean * mtime_w) / greatest(sum(mtime_w), 1))",
          "mtime_w": "sum(mtime_w)", "last_read": "max(last_read)", "c2": "sum(c2)", "c3": "sum(c3)", "c4": "sum(c4)"}
STAGE_FILE = "ch-ingest-stage.parquet"  # a pushed source's copy in the server's user_files (one at a time)
# A first scan's "old side": nothing.
NO_OLD = ", ".join(("'dir'" if c == "kind" else "0") + f" AS {c}0" for c in VALUE_COLS)


def changed_values() -> str:
    """Compare weighted mean timestamps at the ingest contract's second precision."""
    def values(side: str) -> str:
        return ", ".join(f"round({c}{side})" if c == "mtime_mean" else f"{c}{side}" for c in VALUE_COLS)

    return f"tuple({values('1')}) != tuple({values('0')})"


def range_settings(threads: int) -> dict:
    """The bounded range plan, shared with read-only construction benchmarks."""
    return {"max_threads": threads, "max_insert_threads": 1,
            "max_memory_usage": 8 << 30,
            "max_bytes_before_external_group_by": 256 << 20,
            "max_bytes_ratio_before_external_group_by": 0,
            "max_bytes_before_external_sort": 256 << 20,
            "max_bytes_ratio_before_external_sort": 0,
            "max_bytes_before_external_join": 1 << 30,
            "max_bytes_ratio_before_external_join": 0,
            "join_algorithm": "grace_hash", "grace_hash_join_initial_buckets": 16,
            "join_use_nulls": 0, "input_format_parquet_filter_push_down": 1}


class IngestError(RuntimeError):
    pass


def err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def parquet_version(path: str) -> int:
    """2 for a store generation's sort (`size`, `kind`), 1 for a v1 index (`b`)."""
    import pyarrow.parquet as pq

    names = set(pq.ParquetFile(path).schema_arrow.names)
    if {"size", "kind", "n_files"} <= names:
        return 2
    if {"b", "o", "wts", "wb"} <= names:
        return 1
    raise IngestError(f"{path}: neither a v2 store sort nor a v1 index ({sorted(names)})")


def arrow_batches(path: str, version: int, batch_rows: int = 1 << 20):
    """The parquet's columns as an Arrow IPC stream (bytes chunks), cast to
    the `input()` structure the insert declares."""
    import pyarrow as pa
    import pyarrow.ipc as ipc
    import pyarrow.parquet as pq

    types = {
        2: {"path": pa.string(), "usr": pa.string(), "size": pa.int64(), "depth": pa.int32(), "kind": pa.string(), "n_files": pa.int64(),
            "n_children": pa.int64(), "n_desc": pa.int64(), "mtime": pa.int64(), "mtime_mean": pa.float64(), "last_read": pa.int32(),
            "sum_storage_class_id_2": pa.int64(), "sum_storage_class_id_3": pa.int64(), "sum_storage_class_id_4": pa.int64()},
        1: {"path": pa.string(), "depth": pa.int64(), "usr": pa.string(), "b": pa.int64(), "o": pa.int64(), "wts": pa.float64(), "wb": pa.int64(),
            "c2": pa.int64(), "c3": pa.int64(), "c4": pa.int64(), "a": pa.int32()},
    }[version]
    schema = pa.schema([pa.field(k, t) for k, t in types.items()])
    pf = pq.ParquetFile(path)
    have = set(pf.schema_arrow.names)
    sink = io.BytesIO()
    w = ipc.new_stream(sink, schema)
    for b in pf.iter_batches(batch_size=batch_rows, columns=[c for c in types if c in have]):
        cols = [b.column(k).cast(t) if k in have else pa.nulls(b.num_rows, t) for k, t in types.items()]
        w.write_batch(pa.record_batch(cols, schema=schema))
        yield sink.getvalue()
        sink.seek(0)
        sink.truncate()
    w.close()
    yield sink.getvalue()


def _fan_ddl() -> str:
    types = dict(line.strip().rstrip(",").split(" ", 1) for line in VALUES_DDL.splitlines())
    # A FULL JOIN fills a missing side with the type's default. Enum's default
    # is an invalid empty string, so keep `kind[01]` untyped in this transient
    # fan-out table; rows that reach `nodes` / `changes` always hold dir/file
    # and are cast back to the persisted Enum8 there.
    types["kind"] = "String"
    cols = ["depth UInt8", "path String", "usr LowCardinality(String)", "hn UInt8", "ho UInt8", "vf0 DateTime('UTC')"]
    cols += [f"{c}1 {types[c]}" for c in VALUE_COLS] + [f"{c}0 {types[c]}" for c in VALUE_COLS]
    return "CREATE TABLE {t} (" + ", ".join(cols) + ") ENGINE = Null"


def _bounds(pts: list[tuple]) -> list[str]:
    """Key ranges between sorted `(depth, path)` cut points, as WHERE conditions (every slice of a path in one)."""

    def ge(p: tuple) -> str:
        return f"(depth > {p[0]} OR (depth = {p[0]} AND path >= {lit(p[1])}))"

    def lt(p: tuple) -> str:
        return f"(depth < {p[0]} OR (depth = {p[0]} AND path < {lit(p[1])}))"

    pts = sorted(set(pts))
    if not pts:
        return ["1"]
    return [lt(pts[0]), *(f"{ge(a)} AND {lt(b)}" for a, b in zip(pts, pts[1:])), ge(pts[-1])]


def sample_bounds(ch: Ch, n: int) -> list[str]:
    """Disjoint path/owner-safe ranges sampled from the store's primary marks."""
    if n <= 0:
        raise IngestError("range sample count must be positive")
    idx = "mergeTreeIndex(currentDatabase(), 'nodes')"
    marks = int(ch.scalar(f"SELECT count() FROM {idx}"))
    step = max(1, marks // n)
    pts = ch.json(f"""SELECT depth, path FROM (SELECT depth, path, row_number() OVER (ORDER BY depth, path) AS rn FROM {idx})
                WHERE rn % {step} = 0""")
    return _bounds([tuple(p) for p in pts])


@dataclass(frozen=True)
class Prev:
    """The newest ingested scan."""

    dt: str
    id: str
    version: int
    rows: int
    epoch: str

    def live(self, restrict: str = "1") -> str:
        return live(dt_lit(self.dt), restrict, since=dt_lit(self.epoch))


class Ingest:
    """One scan's ingest into the store behind `ch` (its database)."""

    def __init__(self, ch: Ch, scan_id: str, src: str, *, version: int | None = None, server_file: bool = False, force: bool = False,
                 allow_drop: bool = False, threads: int = 8, pairs: int | None = None, stage_dir: Path | None = None, log=err):
        if pairs is not None and pairs <= 0:
            raise IngestError("ingest pairs must be positive")
        self.ch, self.id, self.src = ch, scan_id, src
        self.D = scan_dt(scan_id)
        self.Dl = dt_lit(self.D)
        self.version = version
        self.server_file = server_file
        self.force, self.allow_drop = force, allow_drop
        self.threads = threads
        self.pairs = pairs
        self.stage_dir = stage_dir
        self.log = log
        self.tag = scan_id.replace("-", "").replace("T", "_")
        self.t: dict[str, float] = {}
        self.set = {"max_threads": threads, "max_insert_threads": threads}
        self.file = src if server_file else STAGE_FILE

    def _time(self, k: str, f, *a, **kw):
        t0 = time.monotonic()
        out = f(*a, **kw)
        self.t[k] = round(time.monotonic() - t0, 2)
        self.log(f"ch-ingest {self.id}: {k} {self.t[k]}s")
        return out

    # state

    def prev(self) -> Prev | None:
        records = scan_epochs(self.ch)
        if not records:
            return None
        ident, dt, version, rows, epoch = records[-1]
        return Prev(dt, ident, version, rows, epoch)

    def recorded(self) -> bool:
        return int(self.ch.scalar(f"SELECT count() FROM scans WHERE scan = {self.Dl}") or 0) > 0

    @property
    def t_fan(self) -> str:
        return f"ingest_fan_{self.tag}"

    def mvs(self) -> list[str]:
        return [f"ingest_mv_{k}_{self.tag}" for k in ("open", "close", "copen", "cclose")]

    # steps

    def run(self) -> dict:
        t0 = time.monotonic()
        create(self.ch)
        if self.recorded():
            return {"scan": self.id, "nop": "already ingested"}
        prev = self.prev()
        if prev and prev.dt >= self.D:
            raise IngestError(f"scan {self.id} predates the newest scan ({prev.id}): the store only appends")
        self._cleanup()
        try:
            if not self.server_file:
                self._time("stage", self._stage)
            n_new = self._source()
            self._guard(n_new, prev)
            self._time("diff", self._diff, prev)
            self._time("names", self._names, first=prev is None)
        finally:
            self._drop()
        rec = self._record(prev, time.monotonic() - t0)
        return {**rec, "steps": self.t}

    def _drop(self) -> None:
        for mv in self.mvs():
            self.ch.exec(f"DROP VIEW IF EXISTS {mv}")
        self.ch.exec(f"DROP TABLE IF EXISTS {self.t_fan}")

    def _cleanup(self) -> None:
        """Whatever an interrupted attempt at this scan wrote: its own partitions."""
        self._drop()
        for table in ("nodes", "closures", "changes"):
            self.ch.exec(f"ALTER TABLE {table} DROP PARTITION {lit(self.D)}")

    def _stage(self) -> None:
        """A local or `gs://` source streamed into the server's `user_files` (a parquet file there, in the
        source's order, so its row-group statistics prune the range reads)."""
        path = self.src
        if path.startswith("gs://"):
            from ..bench import local

            if self.stage_dir is None:
                raise IngestError("a gs:// source needs a stage dir (-s)")
            path, s = local.stage_file(path, self.stage_dir / f"path-index-{self.tag}.parquet")
            self.t["download"] = round(s, 2)
        v = self.version or parquet_version(path)
        inp = V2_INPUT if v == 2 else V1_INPUT
        self.ch.insert(f"INSERT INTO FUNCTION file({lit(STAGE_FILE)}, Parquet) SELECT * FROM input({lit(inp)}) FORMAT ArrowStream",
                       arrow_batches(path, v), settings={"engine_file_truncate_on_insert": 1, "max_insert_threads": 1, "max_threads": 1,
                                                         "input_format_parallel_parsing": 0, "output_format_parquet_row_group_size": 65536})
        self.version = v

    def _source(self) -> int:
        cols = {line.split("\t")[0] for line in self.ch.exec(f"DESCRIBE file({lit(self.file)}, Parquet)", fmt=None).splitlines()}
        self.version = self.version or (2 if {"size", "kind"} <= cols else 1)
        return int(self.ch.scalar(f"SELECT count() FROM file({lit(self.file)}, Parquet)") or 0)

    @property
    def select(self) -> str:
        return V2_SELECT if self.version == 2 else V1_SELECT

    def _guard(self, n_new: int, prev: Prev | None) -> None:
        if not n_new:
            raise IngestError(f"scan {self.id}: no rows in {self.src}")
        if prev is None:
            return
        gone = [r[0] for r in self.ch.rows(f"""SELECT DISTINCT path FROM nodes WHERE depth = 1 AND {prev.live('depth = 1')}
            AND path NOT IN (SELECT path FROM file({lit(self.file)}, Parquet) WHERE depth = 1) ORDER BY path""")]
        if gone and not self.allow_drop:
            raise IngestError(f"scan {self.id} lacks {len(gone)} root(s) the store has ({', '.join(gone[:6])}): a partial scan? (--allow-drop to close them)")
        if n_new < prev.rows / 2 and not self.force:
            raise IngestError(f"scan {self.id} has {n_new} rows against {prev.rows} open: a partial scan? (--force to ingest it)")

    def _ranges(self, prev: Prev | None, n: int) -> list[str]:
        """About `n` key ranges, cut at the marks of `nodes`' primary index (or, on a first scan, at
        sampled rows of the source)."""
        if prev is not None:
            return sample_bounds(self.ch, n)
        else:
            total = int(self.ch.scalar(f"SELECT count() FROM file({lit(self.file)}, Parquet)") or 0)
            step = max(1, total // max(1, n))
            pts = self.ch.json(f"SELECT toUInt8(depth), path FROM file({lit(self.file)}, Parquet) WHERE rowNumberInAllBlocks() % {step} = 0",
                               settings=self.set)
        return _bounds([tuple(p) for p in pts])

    def pair_query(self, prev: Prev | None, condition: str) -> str:
        """Read-only changed rows for one owner-safe key range; never publish them."""
        n_aggs = ", ".join(f"{MERGED[c]} AS {c}1" for c in VALUE_COLS)
        o_cols = ", ".join(f"{c} AS {c}0" for c in VALUE_COLS)
        src = f"SELECT {self.select} FROM file({lit(self.file)}, Parquet)"
        n = f"(SELECT depth, path, usr, 1 AS hn, {n_aggs} FROM ({src} WHERE {condition}) GROUP BY depth, path, usr)"
        if prev is None:
            return f"SELECT depth, path, usr, hn, 0 AS ho, toDateTime(0, 'UTC') AS vf0, {', '.join(c + '1' for c in VALUE_COLS)}, {NO_OLD} FROM {n}"
        o = f"(SELECT depth, path, usr, 1 AS ho, vf AS vf0, {o_cols} FROM nodes WHERE ({condition}) AND {prev.live(condition)})"
        return f"""SELECT depth, path, usr, hn, ho, vf0, {', '.join(c + '1' for c in VALUE_COLS)}, {', '.join(c + '0' for c in VALUE_COLS)}
                    FROM {n} AS n FULL OUTER JOIN {o} AS o USING (depth, path, usr) WHERE hn = 0 OR ho = 0 OR {changed_values()}"""

    def _diff(self, prev: Prev | None) -> None:
        ch, D, tag = self.ch, self.Dl, self.tag
        ch.exec(_fan_ddl().format(t=self.t_fan))
        # A value change opens a version — but the size-weighted mean stamp is compared to the second:
        # the job's parallel float sums jitter its last bits scan to scan (gcs's v1 indexes: 80% of
        # rows "changed" that way on 2026-09-06), and the site shows it in days.
        changed = changed_values()
        new = ", ".join(f"{c}1 AS {c}" for c in VALUE_COLS)
        sign_cols = "kind, size, n_files, c2, c3, c4".split(", ")
        mv_open, mv_close, mv_copen, mv_cclose = self.mvs()
        ch.exec(f"""CREATE MATERIALIZED VIEW {mv_open} TO nodes AS
            SELECT depth, path, usr, {D} AS vf, {new}, {name_expr()} AS name FROM {self.t_fan} WHERE hn = 1""")
        ch.exec(f"""CREATE MATERIALIZED VIEW {mv_close} TO closures AS
            SELECT depth, path, usr, vf0 AS vf, {D} AS vt, {name_expr()} AS name FROM {self.t_fan} WHERE ho = 1 AND (hn = 0 OR {changed})""")
        # Day-to-day churn only: a first scan, or a switch of source format (every version reopens), records none.
        if prev is not None and prev.version == self.version:
            ch.exec(f"""CREATE MATERIALIZED VIEW {mv_copen} TO changes AS
                SELECT {D} AS at, toInt8(1) AS sign, depth, path, usr, {D} AS vf, {', '.join(f'{c}1 AS {c}' for c in sign_cols)}, {name_expr()} AS name
                FROM {self.t_fan} WHERE hn = 1""")
            ch.exec(f"""CREATE MATERIALIZED VIEW {mv_cclose} TO changes AS
                SELECT {D} AS at, toInt8(-1) AS sign, depth, path, usr, vf0 AS vf, {', '.join(f'{c}0 AS {c}' for c in sign_cols)}, {name_expr()} AS name
                FROM {self.t_fan} WHERE ho = 1 AND (hn = 0 OR {changed})""")
        # Bound each range's join/aggregation memory and spill oversized ranges;
        # `pairs` / `$CH_INGEST_PAIRS` controls how many run concurrently.
        ranges = self._ranges(prev, self.threads * 16)
        self.t["ranges"] = len(ranges)
        par = self.pairs if self.pairs is not None else int(os.environ.get("CH_INGEST_PAIRS") or max(1, self.threads // 2))
        if par <= 0:
            raise IngestError("ingest pairs must be positive")
        self.t["pairs"] = par

        def one(cond: str) -> float:
            start = time.monotonic()
            q = self.pair_query(prev, cond)
            ch.fork().exec(f"INSERT INTO {self.t_fan} {q}", settings=range_settings(max(1, self.threads // par)))
            return time.monotonic() - start

        with ThreadPoolExecutor(par) as pool:
            source = iter(ranges)
            completed = 0
            pending = {pool.submit(one, cond) for cond in islice(source, par)}
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    seconds = future.result()
                    completed += 1
                    self.log(f"ch-ingest {self.id}: ranges {completed}/{len(ranges)} completed ({seconds:.2f}s)")
                # Check every completed result before starting more work. A
                # failed range leaves only already-running peers to finish.
                for cond in islice(source, len(done)):
                    pending.add(pool.submit(one, cond))

    def _names(self, first: bool) -> None:
        big = {**self.set, "max_bytes_before_external_group_by": 2_000_000_000}
        new = f"SELECT name FROM nodes WHERE vf = {self.Dl}"
        if first:
            self.ch.exec(f"INSERT INTO names SELECT name FROM ({new}) GROUP BY name", settings=big)
            return
        self.ch.exec(f"INSERT INTO names SELECT name FROM ({new}) WHERE name NOT IN (SELECT l FROM names WHERE l IN ({new})) GROUP BY name",
                     settings=big)

    def _record(self, prev: Prev | None, s: float) -> dict:
        ch = self.ch
        opened = int(ch.scalar(f"SELECT count() FROM nodes WHERE vf = {self.Dl}") or 0)
        closed = int(ch.scalar(f"SELECT count() FROM closures WHERE vt = {self.Dl}") or 0)
        rows = (prev.rows if prev else 0) + opened - closed
        v = self.version or 2
        ch.exec(f"INSERT INTO scans (scan, id, version, rows, opened, closed, s, src) VALUES "
                f"({self.Dl}, {lit(self.id)}, {v}, {rows}, {opened}, {closed}, {round(s, 2)}, {lit(self.src)})")
        return {"scan": self.id, "version": v, "rows": rows, "opened": opened, "closed": closed, "s": round(s, 2)}


def audit_roots(
    ch: Ch,
    scan_id: str,
    server_file: str,
) -> dict:
    """Read-only comparison of every published bucket/owner root slice.

    The source is already staged under the server's user_files_path. Compare
    every value, with the same rounded-second mean used by ingestion's change
    detector. This is a root audit, not validation of every descendant row.
    """
    at = dt_lit(scan_dt(scan_id))
    version = ch.scalar(f"SELECT version FROM scans FINAL WHERE scan = {at}")
    if version is None or version == "":
        raise IngestError(f"scan {scan_id} is not published")
    version = int(version)
    if version not in (1, 2):
        raise IngestError(f"scan {scan_id}: unsupported source version {version}")
    def columns(suffix: str) -> str:
        values = [f"toString(kind{suffix})" if c == "kind" else f"round(mtime_mean{suffix})" if c == "mtime_mean" else f"{c}{suffix}" for c in VALUE_COLS]
        return ", ".join(["depth", "path", "usr", *values])

    # Distinct output aliases keep ClickHouse from replacing source references
    # inside weighted-mean expressions with another aggregate's alias.
    merged = ", ".join(f"{MERGED[c]} AS {c}1" for c in VALUE_COLS)
    source = V2_SELECT if version == 2 else V1_SELECT
    expected = ch.json(f"""SELECT {columns('1')} FROM (
        SELECT depth, path, usr, {merged} FROM (
            SELECT {source} FROM file({lit(server_file)}, Parquet) WHERE depth = 1
        ) GROUP BY depth, path, usr
    ) ORDER BY depth, path, usr""")
    actual = ch.json(f"SELECT {columns('')} FROM nodes WHERE depth = 1 AND {live(at, 'depth = 1')} ORDER BY depth, path, usr")
    if not expected:
        raise IngestError(f"source {server_file}: no bucket roots")
    if actual != expected:
        raise IngestError(f"scan {scan_id}: bucket/owner root slices differ from {server_file}")
    return {"scan": scan_id, "version": version, "buckets": len({row[1] for row in expected}), "owner_slices": len(expected),
            "bytes": sum(row[4] for row in expected), "objects": sum(row[5] for row in expected), "root_values_exact": True,
            "mtime_mean_comparison": "rounded seconds"}


def default_src(bucket: str, scan_id: str) -> str:
    """The newest generation's `path` sort for a scan: `gs://<bucket>/listing/<date>/index/<gen>/path-index.parquet`,
    else (a scan from before index generations) `listing/<date>/path-index.parquet`."""
    from google.cloud import storage

    date = scan_id.split("T")[0]
    client = storage.Client()
    blobs = [b.name for b in client.list_blobs(bucket, prefix=f"listing/{date}/index/") if b.name.endswith("/path-index.parquet")]
    if blobs:
        return f"gs://{bucket}/{max(blobs)}"
    if client.bucket(bucket).blob(f"listing/{date}/path-index.parquet").exists():
        return f"gs://{bucket}/listing/{date}/path-index.parquet"
    raise IngestError(f"no path-index.parquet under gs://{bucket}/listing/{date}/")


def sizes(ch: Ch) -> dict:
    """On-disk bytes and rows per table (projections included)."""
    out: dict = {}
    for t, b, rows in ch.rows(f"SELECT table, sum(bytes_on_disk), sum(rows) FROM system.parts WHERE active AND database = {lit(ch.db)} GROUP BY table"):
        out[t] = {"bytes": int(b), "rows": int(rows)}
    return out


def main_json(d: dict) -> str:
    return json.dumps(d, separators=(",", ":"))
