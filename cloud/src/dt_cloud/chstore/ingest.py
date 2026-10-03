"""`dt-cloud ch-ingest`: one scan into the store (specs/ch-store.md §3).

The scan's path store (`path-index.parquet`, the `path` sort: a v2 store
generation, or a v1 dirs-only index) is diffed against the open versions:

1. **stage**: the scan's rows (`s = 1`) and the open rows (`s = 0`, with their
   `vf`) into one table sorted by key;
2. **pair**: one in-order `GROUP BY (depth, path, usr)` over it, into a
   `Null` table whose materialized views fan each key out — its open version
   for the scan (`vf` kept when unchanged, else the scan), the closed old
   version (`vt` = the scan) when it changed or vanished, and both as
   `changes` events;
3. **swap**: the new open versions replace the open partition
   (`REPLACE PARTITION`, atomic); a sentinel row in it (`depth = 0`) records
   which scan it holds;
4. **record**: new names into `names`, the scan into `scans`.

Idempotent and re-runnable: a scan already in `scans` is a no-op; one whose
swap happened (the sentinel says so) only gets recorded; anything else is
redone from scratch, after deleting whatever a crashed attempt appended
(`vt` / `at` = the scan: a lightweight delete on a small partition, the one
mutation, and only on that path). Scans are appended in order: an older
scan than the open one is refused (history is rebuilt, not back-filled).

Guards against a partial scan: a depth-1 root (bucket) the open versions
have and the scan lacks refuses the ingest (`allow_drop` overrides), as does
a scan of fewer than half the open rows (`force`)."""

from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

from .client import Ch, lit
from .schema import KEY_COLS, OPEN, OPEN_PART, STAGE, VALUE_COLS, create, dt_lit, name_expr, scan_dt

V2_INPUT = ("path String, usr Nullable(String), size Int64, depth Int32, kind String, n_files Int64, n_children Nullable(Int64), "
            "n_desc Nullable(Int64), mtime Nullable(Int64), mtime_mean Nullable(Float64), last_read Nullable(Int32), "
            "sum_storage_class_id_2 Nullable(Int64), sum_storage_class_id_3 Nullable(Int64), sum_storage_class_id_4 Nullable(Int64)")
V1_INPUT = "path String, depth Int64, usr Nullable(String), b Int64, o Int64, wts Nullable(Float64), wb Nullable(Int64), c2 Nullable(Int64), c3 Nullable(Int64), c4 Nullable(Int64), a Nullable(Int32)"

# Source columns → the stage's (`s = 1`, no `vf`).
V2_SELECT = """toUInt8(depth) AS depth, path, ifNull(usr, '') AS usr, 1 AS s, toDateTime(0, 'UTC') AS vf,
    if(kind = 'file', 'file', 'dir') AS kind, size, n_files, ifNull(n_children, -1) AS n_children, ifNull(n_desc, -1) AS n_desc,
    ifNull(mtime, -1) AS mtime, ifNull(mtime_mean, 0) AS mtime_mean, if(mtime_mean IS NULL, 0, size) AS mtime_w,
    ifNull(last_read, -1) AS last_read, ifNull(sum_storage_class_id_2, 0) AS c2, ifNull(sum_storage_class_id_3, 0) AS c3,
    ifNull(sum_storage_class_id_4, 0) AS c4"""
V1_SELECT = """toUInt8(depth) AS depth, path, ifNull(usr, '') AS usr, 1 AS s, toDateTime(0, 'UTC') AS vf,
    'dir' AS kind, b AS size, o AS n_files, -1 AS n_children, -1 AS n_desc, -1 AS mtime,
    if(ifNull(wb, 0) > 0, ifNull(wts, 0) / wb, 0) AS mtime_mean, ifNull(wb, 0) AS mtime_w,
    ifNull(a, -1) AS last_read, ifNull(c2, 0) AS c2, ifNull(c3, 0) AS c3, ifNull(c4, 0) AS c4"""


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
    from .schema import VALUES_DDL

    types = dict(line.strip().rstrip(",").split(" ", 1) for line in VALUES_DDL.splitlines())
    cols = ["depth UInt8", "path String", "usr LowCardinality(String)", "nn UInt64", "no UInt64", "vf0 DateTime('UTC')", "dup UInt8"]
    cols += [f"{c}1 {types[c]}" for c in VALUE_COLS] + [f"{c}0 {types[c]}" for c in VALUE_COLS]
    return "CREATE TABLE {t} (" + ", ".join(cols) + ") ENGINE = Null"


class Ingest:
    """One scan's ingest into the store behind `ch` (its database)."""

    def __init__(self, ch: Ch, scan_id: str, src: str, *, version: int | None = None, server_file: bool = False, force: bool = False,
                 allow_drop: bool = False, threads: int = 8, stage_dir: Path | None = None, log=err):
        self.ch, self.id, self.src = ch, scan_id, src
        self.D = scan_dt(scan_id)
        self.Dl = dt_lit(self.D)
        self.version = version
        self.server_file = server_file
        self.force, self.allow_drop = force, allow_drop
        self.threads = threads
        self.stage_dir = stage_dir
        self.log = log
        self.tag = scan_id.replace("-", "").replace("T", "_")
        self.t: dict[str, float] = {}
        self.set = {"max_threads": threads, "max_insert_threads": threads}

    def _time(self, k: str, f, *a, **kw):
        t0 = time.monotonic()
        out = f(*a, **kw)
        self.t[k] = round(time.monotonic() - t0, 2)
        self.log(f"ch-ingest {self.id}: {k} {self.t[k]}s")
        return out

    # state

    def open_asof(self) -> str | None:
        """The scan the open partition holds (its sentinel's `vf`), or None."""
        return self.ch.scalar(f"SELECT toString(vf) FROM nodes WHERE depth = 0 AND vt = {dt_lit(OPEN)} ORDER BY vf DESC LIMIT 1") or None

    def recorded(self) -> bool:
        return int(self.ch.scalar(f"SELECT count() FROM scans WHERE scan = {self.Dl}") or 0) > 0

    # steps

    def run(self) -> dict:
        t0 = time.monotonic()
        ch = self.ch
        create(ch)
        if self.recorded():
            return {"scan": self.id, "nop": "already ingested"}
        asof = self.open_asof()
        if asof and asof > self.D:
            raise IngestError(f"scan {self.id} predates the open versions ({asof}): the store only appends")
        if asof != self.D:
            self._cleanup()
            n_new = self._time("load", self._load)
            try:
                self._guard(n_new, asof)
            except IngestError:
                self._drop()
                raise
            self._time("stage_open", self._stage_open, asof)
            self._time("pair", self._pair)
            self._time("names", self._names, first=asof is None)
            self._time("swap", lambda: ch.exec(f"ALTER TABLE nodes REPLACE PARTITION ID '{OPEN_PART}' FROM {self.t_open}"))
        self._drop()
        rec = self._record(time.monotonic() - t0)
        return {**rec, "steps": self.t}

    @property
    def t_stage(self) -> str:
        return f"ingest_stage_{self.tag}"

    @property
    def t_open(self) -> str:
        return f"ingest_open_{self.tag}"

    @property
    def t_fan(self) -> str:
        return f"ingest_fan_{self.tag}"

    def _drop(self) -> None:
        for mv in ("open", "close", "copen", "cclose"):
            self.ch.exec(f"DROP VIEW IF EXISTS ingest_mv_{mv}_{self.tag}")
        for t in (self.t_fan, self.t_stage, self.t_open):
            self.ch.exec(f"DROP TABLE IF EXISTS {t}")

    def _cleanup(self) -> None:
        """Whatever an interrupted attempt at this scan left behind."""
        self._drop()
        for table, col in (("nodes", "vt"), ("changes", "at")):
            if int(self.ch.scalar(f"SELECT count() FROM {table} WHERE {col} = {self.Dl}") or 0):
                self.log(f"ch-ingest {self.id}: deleting a previous attempt's {table} rows")
                self.ch.exec(f"DELETE FROM {table} WHERE {col} = {self.Dl}", settings={"lightweight_deletes_sync": 2})

    def _load(self) -> int:
        ch = self.ch
        ch.exec(STAGE.format(t=self.t_stage))
        cols = ", ".join(["depth", "path", "usr", "s", "vf", *VALUE_COLS])
        if self.server_file:
            # A file under the server's `user_files_path`.
            schema = ch.exec(f"DESCRIBE file({lit(self.src)}, Parquet)", fmt=None)
            names = {line.split("\t")[0] for line in schema.splitlines()}
            v = self.version or (2 if {"size", "kind"} <= names else 1)
            sel = V2_SELECT if v == 2 else V1_SELECT
            ch.exec(f"INSERT INTO {self.t_stage} ({cols}) SELECT {sel} FROM file({lit(self.src)}, Parquet)", settings=self.set)
        else:
            path = self.src
            if path.startswith("gs://"):
                from ..bench import local

                if self.stage_dir is None:
                    raise IngestError("a gs:// source needs a stage dir (-s)")
                path, s = local.stage_file(path, self.stage_dir / f"path-index-{self.tag}.parquet")
                self.t["download"] = round(s, 2)
            v = self.version or parquet_version(path)
            sel, inp = (V2_SELECT, V2_INPUT) if v == 2 else (V1_SELECT, V1_INPUT)
            ch.insert(f"INSERT INTO {self.t_stage} ({cols}) SELECT {sel} FROM input({lit(inp)}) FORMAT ArrowStream", arrow_batches(path, v),
                      settings=self.set)
        self.version = v
        return int(ch.scalar(f"SELECT count() FROM {self.t_stage}") or 0)

    def _guard(self, n_new: int, asof: str | None) -> None:
        if not n_new:
            raise IngestError(f"scan {self.id}: no rows in {self.src}")
        if asof is None:
            return
        ch = self.ch
        n_old = int(ch.scalar(f"SELECT count() FROM nodes WHERE vt = {dt_lit(OPEN)} AND depth > 0") or 0)
        gone = [r[0] for r in ch.rows(f"""SELECT DISTINCT path FROM nodes WHERE vt = {dt_lit(OPEN)} AND depth = 1
            AND path NOT IN (SELECT path FROM {self.t_stage} WHERE depth = 1) ORDER BY path""")]
        if gone and not self.allow_drop:
            raise IngestError(f"scan {self.id} lacks {len(gone)} root(s) the store has ({', '.join(gone[:6])}): a partial scan? (--allow-drop to close them)")
        if n_new < n_old / 2 and not self.force:
            raise IngestError(f"scan {self.id} has {n_new} rows against {n_old} open: a partial scan? (--force to ingest it)")

    def _stage_open(self, asof: str | None) -> None:
        if asof is None:
            return
        cols = ", ".join(["depth", "path", "usr", "s", "vf", *VALUE_COLS])
        self.ch.exec(f"INSERT INTO {self.t_stage} ({cols}) SELECT depth, path, usr, 0, vf, {', '.join(VALUE_COLS)} FROM nodes "
                     f"WHERE vt = {dt_lit(OPEN)} AND depth > 0", settings=self.set)

    def _pair(self) -> None:
        ch, D, tag = self.ch, self.Dl, self.tag
        ch.exec(f"CREATE TABLE {self.t_open} AS nodes")
        ch.exec(_fan_ddl().format(t=self.t_fan))
        changed = f"tuple({', '.join(c + '1' for c in VALUE_COLS)}) != tuple({', '.join(c + '0' for c in VALUE_COLS)})"
        new = ", ".join(f"{c}1 AS {c}" for c in VALUE_COLS)
        old = ", ".join(f"{c}0 AS {c}" for c in VALUE_COLS)
        sign_cols = "kind, size, n_files, c2, c3, c4"
        ch.exec(f"""CREATE MATERIALIZED VIEW ingest_mv_open_{tag} TO {self.t_open} AS
            SELECT depth, path, usr, if(no = 1 AND NOT ({changed}), vf0, {D}) AS vf, {dt_lit(OPEN)} AS vt, {new}, {name_expr()} AS name
            FROM {self.t_fan} WHERE nn = 1""")
        ch.exec(f"""CREATE MATERIALIZED VIEW ingest_mv_close_{tag} TO nodes AS
            SELECT depth, path, usr, vf0 AS vf, {D} AS vt, {old}, {name_expr()} AS name
            FROM {self.t_fan} WHERE no = 1 AND (nn = 0 OR {changed})""")
        ch.exec(f"""CREATE MATERIALIZED VIEW ingest_mv_copen_{tag} TO changes AS
            SELECT {D} AS at, toInt8(1) AS sign, depth, path, usr, {D} AS vf, {', '.join(f'{c}1 AS {c}' for c in sign_cols.split(', '))}, {name_expr()} AS name
            FROM {self.t_fan} WHERE nn = 1 AND (no = 0 OR {changed})""")
        ch.exec(f"""CREATE MATERIALIZED VIEW ingest_mv_cclose_{tag} TO changes AS
            SELECT {D} AS at, toInt8(-1) AS sign, depth, path, usr, vf0 AS vf, {', '.join(f'{c}0 AS {c}' for c in sign_cols.split(', '))}, {name_expr()} AS name
            FROM {self.t_fan} WHERE no = 1 AND (nn = 0 OR {changed})""")
        aggs = [f"anyIf({c}, s = 1) AS {c}1" for c in VALUE_COLS] + [f"anyIf({c}, s = 0) AS {c}0" for c in VALUE_COLS]
        ch.exec(f"""INSERT INTO {self.t_fan}
            SELECT depth, path, usr, countIf(s = 1) AS nn, countIf(s = 0) AS no, anyIf(vf, s = 0) AS vf0,
                   throwIf(nn > 1 OR no > 1, 'a key twice in one scan') AS dup, {', '.join(aggs)}
            FROM {self.t_stage} GROUP BY {', '.join(KEY_COLS)}""",
                settings={**self.set, "optimize_aggregation_in_order": 1})
        for mv in ("open", "close", "copen", "cclose"):
            ch.exec(f"DROP VIEW IF EXISTS ingest_mv_{mv}_{tag}")
        zeros = ", ".join("0" for _ in VALUE_COLS[1:])
        ch.exec(f"INSERT INTO {self.t_open} (depth, path, usr, vf, vt, {', '.join(VALUE_COLS)}, name) "
                f"VALUES (0, '', '', {D}, {dt_lit(OPEN)}, 'dir', {zeros}, '')")

    def _names(self, first: bool) -> None:
        big = {**self.set, "max_bytes_before_external_group_by": 2_000_000_000}
        if first:
            self.ch.exec(f"INSERT INTO names SELECT name FROM {self.t_open} WHERE depth > 0 GROUP BY name", settings=big)
            return
        new = f"SELECT name FROM changes WHERE at = {self.Dl} AND sign = 1"
        self.ch.exec(f"INSERT INTO names SELECT name FROM ({new}) WHERE name NOT IN (SELECT l FROM names WHERE l IN ({new})) GROUP BY name",
                     settings=big)

    def _record(self, s: float) -> dict:
        ch = self.ch
        opened, closed = (int(x) for x in ch.one(f"SELECT countIf(sign = 1), countIf(sign = -1) FROM changes WHERE at = {self.Dl}"))
        rows = int(ch.scalar(f"SELECT count() FROM nodes WHERE vt = {dt_lit(OPEN)} AND depth > 0") or 0)
        v = self.version or int(ch.scalar(f"SELECT if(countIf(n_children >= 0) > 0, 2, 1) FROM nodes WHERE vt = {dt_lit(OPEN)} AND depth = 1") or 2)
        ch.exec(f"INSERT INTO scans (scan, id, version, rows, opened, closed, s, src) VALUES "
                f"({self.Dl}, {lit(self.id)}, {v}, {rows}, {opened}, {closed}, {round(s, 2)}, {lit(self.src)})")
        return {"scan": self.id, "version": v, "rows": rows, "opened": opened, "closed": closed, "s": round(s, 2)}


def default_src(bucket: str, scan_id: str) -> str:
    """The newest generation's `path` sort for a scan: `gs://<bucket>/listing/<date>/index/<gen>/path-index.parquet`."""
    from google.cloud import storage

    date = scan_id.split("T")[0]
    blobs = [b.name for b in storage.Client().list_blobs(bucket, prefix=f"listing/{date}/index/") if b.name.endswith("/path-index.parquet")]
    if not blobs:
        raise IngestError(f"no path-index.parquet under gs://{bucket}/listing/{date}/index/")
    return f"gs://{bucket}/{max(blobs)}"


def sizes(ch: Ch) -> dict:
    """On-disk bytes per table (projections included), and the open / closed split of `nodes`."""
    out: dict = {}
    for t, b, rows in ch.rows(f"SELECT table, sum(bytes_on_disk), sum(rows) FROM system.parts WHERE active AND database = {lit(ch.db)} GROUP BY table"):
        out[t] = {"bytes": int(b), "rows": int(rows)}
    split = ch.rows(f"""SELECT partition_id = '{OPEN_PART}', sum(bytes_on_disk), sum(rows) FROM system.parts
        WHERE active AND database = {lit(ch.db)} AND table = 'nodes' GROUP BY 1""")
    out["nodes_split"] = {("open" if o == "1" else "closed"): {"bytes": int(b), "rows": int(r)} for o, b, r in split}
    return out


def main_json(d: dict) -> str:
    return json.dumps(d, separators=(",", ":"))
