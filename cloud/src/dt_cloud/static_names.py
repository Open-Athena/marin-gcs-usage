"""Static name search (specs/architecture/static-name-search.md): the
suffix-ordered, denormalized postings, rebuilt from primary sources.

Two stages, each embarrassingly parallel on GCP Batch (`job/static-names.sh`):

1. **Intervals.** Every published scan's `path` sort (`listing/<date>/…/path-index.parquet`)
   becomes SCD-2 version intervals: one row per `(depth, path, usr)` version with `[vf, vt)`
   (`vt` = 2106 while open) and its values, exactly the versions the ClickHouse store's
   `nodes`/`closures` hold (`chstore/ingest.py`): a key's rows merged per scan as the
   ingest merges them, and a new version whenever any value changes (the weighted mean
   stamp compared to the second, banker's rounding as ClickHouse's `round`) or the key
   was absent from a scan in between. The kernel is pyrmts' gaps-and-islands
   (`pyrmts_engine.multiscan_duckdb`, the one `overtime.py` uses), run per `(depth, path)`
   key range (`ranges.json`), so a range task reads only its row groups of each scan.
   `append` adds one scan to a range's intervals (open versions × the scan: a full
   join), the daily delta, and must equal a rebuild.
2. **Suffixes.** From the intervals: one row per (lowercase name suffix of three or more
   characters, version): `(s, depth, path, usr, vf, vt, size, n_files)`, sorted by `s`,
   in shard files split by the suffix's first three characters (`shards.json`, cut from
   the intervals stage's per-range prefix histograms), 8K-row groups, zstd, and a
   per-row-group sidecar `(file, rg, s_min, s_max, offset, length, rows)` (for D1).
   Every rare literal's rows are then one contiguous byte range of one or two files.

Every output is written through pyarrow with fixed row-group sizes and a total sort
order, so a rerun on the same inputs (and image) is byte-identical.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from time import monotonic
from typing import Iterable, Iterator

import pyarrow as pa
import pyarrow.parquet as pq
from click import IntRange, argument, group, option

err = partial(print, file=sys.stderr, flush=True)

DATA_BUCKET = "oa-gcs-usage-dvx"
PREFIX = "static-names"
OPEN = 4291747200  # 2106-01-01 00:00:00 UTC: a version's `vt` while open (`chstore.schema.OPEN`)
U64 = 1 << 64
#: The values whose change opens a version (`chstore.schema.VALUE_COLS`); `mtime_mean` is stored rounded.
VALUE_COLS = ["kind", "size", "n_files", "n_children", "n_desc", "mtime", "mtime_mean", "mtime_w", "last_read", "c2", "c3", "c4"]
KEY_COLS = ["depth", "path", "usr"]
INTERVAL_RG = 65536
SX_RG = 8192
CODEC = "zstd"

INTERVAL_SCHEMA = pa.schema([
    pa.field("depth", pa.uint8(), nullable=False),
    pa.field("path", pa.string(), nullable=False),
    pa.field("usr", pa.string(), nullable=False),
    pa.field("vf", pa.int64(), nullable=False),
    pa.field("vt", pa.int64(), nullable=False),
    pa.field("kind", pa.string(), nullable=False),
    pa.field("size", pa.int64(), nullable=False),
    pa.field("n_files", pa.int64(), nullable=False),
    pa.field("n_children", pa.int64(), nullable=False),
    pa.field("n_desc", pa.int64(), nullable=False),
    pa.field("mtime", pa.int64(), nullable=False),
    pa.field("mtime_mean", pa.float64(), nullable=False),
    pa.field("mtime_w", pa.int64(), nullable=False),
    pa.field("last_read", pa.int32(), nullable=False),
    pa.field("c2", pa.int64(), nullable=False),
    pa.field("c3", pa.int64(), nullable=False),
    pa.field("c4", pa.int64(), nullable=False),
])
SX_SCHEMA = pa.schema([
    pa.field("s", pa.string(), nullable=False),
    pa.field("depth", pa.uint8(), nullable=False),
    pa.field("path", pa.string(), nullable=False),
    pa.field("usr", pa.string(), nullable=False),
    pa.field("vf", pa.timestamp("ms", tz="UTC"), nullable=False),
    pa.field("vt", pa.timestamp("ms", tz="UTC"), nullable=False),
    pa.field("size", pa.int64(), nullable=False),
    pa.field("n_files", pa.int64(), nullable=False),
])
SIDECAR_SCHEMA = pa.schema([
    pa.field("file", pa.string(), nullable=False),
    pa.field("rg", pa.int32(), nullable=False),
    pa.field("s_min", pa.string(), nullable=False),
    pa.field("s_max", pa.string(), nullable=False),
    pa.field("offset", pa.int64(), nullable=False),
    pa.field("length", pa.int64(), nullable=False),
    pa.field("rows", pa.int32(), nullable=False),
])


def q(s: str) -> str:
    """A DuckDB string literal."""
    return "'" + s.replace("'", "''") + "'"


def scan_epoch(scan_id: str) -> int:
    """A scan id (`2026-10-01` or `2026-10-01T0003`, UTC) as epoch seconds (`chstore.schema.scan_dt`)."""
    fmt = "%Y-%m-%dT%H%M" if "T" in scan_id else "%Y-%m-%d"
    return int(datetime.strptime(scan_id, fmt).replace(tzinfo=timezone.utc).timestamp())


# ── Scans ──────────────────────────────────────────────────────────────────


def list_scans(bucket: str = DATA_BUCKET, *, start: str | None = None, through: str | None = None) -> dict:
    """Every scan's `path` sort, as the store ingests it (`chstore.ingest.default_src`): the newest
    generation's `listing/<date>/index/<gen>/path-index.parquet`, else (before generations)
    `listing/<date>/path-index.parquet`; pinned by GCS generation, size, md5 and crc32c."""
    from google.cloud import storage

    client = storage.Client()
    found: dict[str, dict] = {}
    for glob in ("listing/*/path-index.parquet", "listing/*/index/*/path-index.parquet"):
        for b in client.list_blobs(bucket, match_glob=glob):
            date = b.name.split("/")[1]
            if (start and date < start) or (through and date > through):
                continue
            gen = b.name.split("/")[3] if "/index/" in b.name else ""
            cur = found.get(date)
            if cur is None or gen > cur["gen"]:
                found[date] = {"id": date, "gen": gen, "src": b.name, "generation": int(b.generation), "size": int(b.size),
                               "md5": base64.b64decode(b.md5_hash).hex() if b.md5_hash else None, "crc32c": b.crc32c}
    scans = [found[d] for d in sorted(found)]
    for s in scans:
        del s["gen"]
        s["ts"] = scan_epoch(s["id"])
    return {"bucket": bucket, "scans": scans}


# ── Key ranges ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Piece:
    """A conjunctive `(depth, path)` filter: depths `[dlo, dhi]`, paths `[plo, phi)` (only within one depth)."""
    dlo: int
    dhi: int
    plo: str | None = None
    phi: str | None = None

    def sql(self) -> str:
        parts = [f"depth >= {self.dlo}", f"depth <= {self.dhi}"]
        if self.plo:
            parts.append(f"path >= {q(self.plo)}")
        if self.phi is not None:
            parts.append(f"path < {q(self.phi)}")
        return " AND ".join(parts)


MAX_DEPTH = 255


def pieces(lo: tuple[int, str], hi: tuple[int, str] | None) -> list[Piece]:
    """The range `[lo, hi)` of `(depth, path)` keys (`hi` None = unbounded) as conjunctive pieces, so
    each piece's filter prunes a `(depth, path)`-sorted file by its row-group statistics."""
    (d0, p0) = lo
    if hi is None:
        out = [Piece(d0, d0, p0 or None, None)] if p0 else [Piece(d0, MAX_DEPTH)]
        if p0:
            out.append(Piece(d0 + 1, MAX_DEPTH))
        return out
    (d1, p1) = hi
    if (d1, p1) <= (d0, p0):
        raise ValueError(f"empty range {lo} → {hi}")
    if d0 == d1:
        return [Piece(d0, d0, p0 or None, p1)]
    out = []
    first_whole = d0 if not p0 else d0 + 1
    if p0:
        out.append(Piece(d0, d0, p0, None))
    last_whole = d1 - 1
    if first_whole <= last_whole:
        out.append(Piece(first_whole, last_whole))
    if p1:
        out.append(Piece(d1, d1, None, p1))
    return out


def plan_ranges(scans: dict, k: int, mount: str | None = None) -> dict:
    """`k` key ranges of about equal input rows over every scan: cut at row-group starts of the newest
    scan of each source format (v1: dirs only; v2: every object), weighted by how many scans have
    that format. Boundaries are `(depth, path)` keys; range `i` is `[b_i, b_{i+1})`."""
    by_version: dict[int, list[dict]] = {}
    for s in scans["scans"]:
        by_version.setdefault(s["version"], []).append(s)
    points: list[tuple[int, str, int]] = []
    for v, ss in by_version.items():
        newest = ss[-1]
        md = _metadata(scans["bucket"], newest["src"], mount)
        names = md.schema.names
        di, pi = names.index("depth"), names.index("path")
        for g in range(md.num_row_groups):
            rg = md.row_group(g)
            ds, ps_ = rg.column(di).statistics, rg.column(pi).statistics
            if ds.min == ds.max:
                points.append((int(ds.min), ps_.min, rg.num_rows * len(ss)))
        err(f"plan-ranges: v{v} {newest['id']}: {md.num_row_groups:,} row groups × {len(ss)} scans")
    points.sort()
    total = sum(w for *_, w in points)
    cuts: list[tuple[int, str]] = []
    acc, step = 0, total / k
    for d, p, w in points:
        if acc >= step * (len(cuts) + 1) and (not cuts or (d, p) > cuts[-1]):
            cuts.append((d, p))
        acc += w
    bounds = [(0, "")] + [c for c in cuts if c > (0, "")]
    ranges = []
    for i, lo in enumerate(bounds):
        hi = bounds[i + 1] if i + 1 < len(bounds) else None
        ranges.append({"i": i, "lo": list(lo), "hi": list(hi) if hi else None})
    return {"k": len(ranges), "ranges": ranges}


def _metadata(bucket: str, key: str, mount: str | None) -> "pq.FileMetaData":
    """A parquet's footer metadata: from the mount, else two ranged GCS reads (never the whole file)."""
    if mount:
        return pq.ParquetFile(f"{mount}/{key}").metadata
    from google.cloud import storage

    blob = storage.Client().bucket(bucket).get_blob(key)
    size = int(blob.size)
    tail = blob.download_as_bytes(start=size - 8, end=size - 1)
    flen = int.from_bytes(tail[:4], "little")
    foot = blob.download_as_bytes(start=max(0, size - max(8 + flen, 1 << 16)), end=size - 1)
    return pq.ParquetFile(Spans(size, [(size - len(foot), foot)])).metadata


def _src(bucket: str, key: str, mount: str | None) -> str:
    return f"{mount}/{key}" if mount else f"gs://{bucket}/{key}"


# ── Interval kernel ────────────────────────────────────────────────────────

V2_SELECT = """depth::UTINYINT AS depth, path, coalesce(usr, '') AS usr,
    CASE WHEN kind = 'file' THEN 'file' ELSE 'dir' END AS kind, size::BIGINT AS size, n_files::BIGINT AS n_files,
    coalesce({n_children}, -1)::BIGINT AS n_children, coalesce({n_desc}, -1)::BIGINT AS n_desc, coalesce({mtime}, -1)::BIGINT AS mtime,
    coalesce({mtime_mean}, 0)::DOUBLE AS mtime_mean, CASE WHEN {mtime_mean} IS NULL THEN 0 ELSE size END::BIGINT AS mtime_w,
    coalesce({last_read}, -1)::INTEGER AS last_read, coalesce({c2}, 0)::BIGINT AS c2, coalesce({c3}, 0)::BIGINT AS c3, coalesce({c4}, 0)::BIGINT AS c4"""
V2_OPTIONAL = {"n_children": "n_children", "n_desc": "n_desc", "mtime": "mtime", "mtime_mean": "mtime_mean", "last_read": "last_read",
               "c2": "sum_storage_class_id_2", "c3": "sum_storage_class_id_3", "c4": "sum_storage_class_id_4"}
V1_SELECT = """depth::UTINYINT AS depth, path, coalesce(usr, '') AS usr, 'dir' AS kind, b::BIGINT AS size, o::BIGINT AS n_files,
    -1::BIGINT AS n_children, -1::BIGINT AS n_desc, -1::BIGINT AS mtime,
    CASE WHEN coalesce(wb, 0) > 0 THEN coalesce(wts, 0) / wb ELSE 0 END::DOUBLE AS mtime_mean, coalesce(wb, 0)::BIGINT AS mtime_w,
    coalesce(a, -1)::INTEGER AS last_read, coalesce(c2, 0)::BIGINT AS c2, coalesce(c3, 0)::BIGINT AS c3, coalesce(c4, 0)::BIGINT AS c4"""
#: A scan's rows for one key merged as the ingest merges them (`chstore.ingest.MERGED`), the mean stamp then
#: rounded to the second the way ClickHouse's `round` does (half to even) — the value the change test compares.
MERGED = """any_value(kind) AS kind, sum(size)::BIGINT AS size, sum(n_files)::BIGINT AS n_files, max(n_children) AS n_children,
    max(n_desc) AS n_desc, max(mtime) AS mtime,
    round_even(CASE WHEN count(*) = 1 THEN any_value(mtime_mean) ELSE sum(mtime_mean * mtime_w) / greatest(sum(mtime_w), 1) END, 0) AS mtime_mean,
    sum(mtime_w)::BIGINT AS mtime_w, max(last_read) AS last_read, sum(c2)::BIGINT AS c2, sum(c3)::BIGINT AS c3, sum(c4)::BIGINT AS c4"""


def source_version(con, path: str) -> int:
    """2 for a store generation's `path` sort (`size`, `kind`), 1 for a v1 index (`b`, `o`)."""
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({q(path)})").fetchall()}
    if {"size", "kind", "n_files"} <= cols:
        return 2
    if {"b", "o"} <= cols:
        return 1
    raise ValueError(f"{path}: neither a v2 store sort nor a v1 index ({sorted(cols)})")


def scan_sql(con, path: str, ps: list[Piece], version: int | None = None) -> str:
    """One scan's rows in the range, merged per key."""
    version = version or source_version(con, path)
    if version == 2:
        cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({q(path)})").fetchall()}
        select = V2_SELECT.format(**{k: (c if c in cols else "NULL") for k, c in V2_OPTIONAL.items()})
    else:
        select = V1_SELECT
    parts = [f"SELECT {select} FROM read_parquet({q(path)}) WHERE {p.sql()}" for p in ps]
    return f"SELECT depth, path, usr, {MERGED} FROM ({' UNION ALL '.join(parts)}) GROUP BY depth, path, usr"


def interval_pyramid():
    """The interval shape as a pyrmts `Pyramid` (key `(depth, path, usr)`, one state column per value):
    the gaps-and-islands kernel only reads its key and state column names."""
    from pyrmts.types import Dim, Metric, Pyramid

    from .overtime import _NoStore

    return Pyramid(storage=_NoStore(), keyTemplate="", binCol="depth", dims=[Dim("path", "string"), Dim("usr", "string")],
                   metrics=[Metric(c, "count") for c in VALUE_COLS], tiers=[])


def intervals_sql(con, sources: list[tuple[str, int, int | None]], ps: list[Piece]) -> str:
    """The range's intervals over `sources` (`(path, epoch, version)`, oldest first) as a query of
    `INTERVAL_SCHEMA` rows (unsorted): pyrmts' gaps-and-islands over the per-scan merged rows, its
    scan indices mapped to epochs (`vt` = the first scan after the run, or `OPEN`)."""
    from pyrmts_engine.multiscan_duckdb import _intervals_sql, _union_sql

    pyr = interval_pyramid()
    union = _union_sql([f"({scan_sql(con, p, ps, v)})" for p, _, v in sources])
    kernel = _intervals_sql(union, KEY_COLS, VALUE_COLS, pyr)
    ts = [e for _, e, _ in sources] + [OPEN]
    lst = "[" + ", ".join(str(t) for t in ts) + "]::BIGINT[]"
    vals = ", ".join(VALUE_COLS)
    return f"""SELECT depth, path, usr, ({lst})[__scan_lo + 1] AS vf, ({lst})[__scan_hi + 2] AS vt, {vals}
        FROM ({kernel})"""


def connect(threads: int, mem: str, tmp: str | Path | None):
    import duckdb

    con = duckdb.connect()
    con.execute(f"SET threads={threads}; SET memory_limit='{mem}'; SET preserve_insertion_order=false")
    con.execute("SET parquet_metadata_cache=true")
    if tmp:
        Path(tmp).mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory={q(str(tmp))}")
    return con


def write_sorted(batches: Iterable[pa.RecordBatch], out: Path, schema: pa.Schema, rg_rows: int,
                 on_group=None, dictionary: list[str] | bool = False) -> int:
    """Write already-ordered batches as exact `rg_rows`-row groups (the last may be short), so the file's
    bytes depend only on its rows. `on_group(table)` sees each row group as written. Returns rows."""
    out.parent.mkdir(parents=True, exist_ok=True)
    pending: list[pa.RecordBatch] = []
    n_pending = 0
    total = 0
    with pq.ParquetWriter(out, schema, compression=CODEC, use_dictionary=dictionary, write_statistics=True,
                          coerce_timestamps="ms", allow_truncated_timestamps=False) as w:
        def flush(final: bool) -> None:
            nonlocal pending, n_pending
            if not pending:
                return
            t = pa.Table.from_batches(pending, schema=schema).combine_chunks()
            off = 0
            while t.num_rows - off >= rg_rows or (final and off < t.num_rows):
                g = t.slice(off, min(rg_rows, t.num_rows - off))
                w.write_table(g, row_group_size=rg_rows)
                if on_group:
                    on_group(g)
                off += g.num_rows
            rest = t.slice(off)
            pending, n_pending = ([rest.combine_chunks().to_batches()[0]] if rest.num_rows else []), rest.num_rows

        for b in batches:
            if b.num_rows == 0:
                continue
            b = b.cast(schema) if b.schema != schema else b
            pending.append(b)
            n_pending += b.num_rows
            total += b.num_rows
            if n_pending >= rg_rows:
                flush(False)
        flush(True)
    return total


def _batches(con, sql: str, size: int = 1 << 17) -> Iterator[pa.RecordBatch]:
    reader = con.execute(sql).to_arrow_reader(size)
    for b in reader:
        yield b


def _sx_cast(b: pa.RecordBatch) -> pa.RecordBatch:
    """Interval epoch seconds → the suffix files' millisecond timestamps."""
    import pyarrow.compute as pc

    cols = []
    for f in SX_SCHEMA:
        c = b.column(f.name)
        if f.name in ("vf", "vt"):
            c = pc.multiply(c.cast(pa.int64()), 1000)
        cols.append(c.cast(f.type))
    return pa.record_batch(cols, schema=SX_SCHEMA)


NAME = "lower(string_split(path, '/')[-1])"


def hist_sql(table: str) -> str:
    """Suffix rows per three-character prefix (versions × suffix positions of ≥ 3 characters, depth ≥ 1)."""
    return f"""SELECT substring(l, p, 3) AS p3, count(*)::BIGINT AS n FROM (
            SELECT l, unnest(generate_series(1, length(l) - 2)) AS p FROM (SELECT {NAME} AS l FROM {table} WHERE depth >= 1) WHERE length(l) >= 3
        ) GROUP BY p3 ORDER BY p3"""


DIGEST_OPEN = "md5_number_upper(concat_ws('|', depth::VARCHAR, path, usr, vf::VARCHAR, size::VARCHAR, n_files::VARCHAR))"
DIGEST_CLOSE = "md5_number_upper(concat_ws('|', depth::VARCHAR, path, usr, vf::VARCHAR, vt::VARCHAR))"


def digests(con, table: str) -> dict:
    """Per scan epoch: versions opened (count, Σ md5 mod 2⁶⁴ of `depth|path|usr|vf|size|n_files`) and closed
    (of `depth|path|usr|vf|vt`) — the same strings `verify-intervals` hashes in ClickHouse."""
    out: dict[str, dict] = {}
    for ts, n, h in con.execute(f"SELECT vf, count(*), (sum({DIGEST_OPEN}) % {U64})::UBIGINT FROM {table} GROUP BY vf").fetchall():
        out.setdefault(str(ts), {})["opened"] = [int(n), int(h)]
    for ts, n, h in con.execute(f"SELECT vt, count(*), (sum({DIGEST_CLOSE}) % {U64})::UBIGINT FROM {table} WHERE vt <> {OPEN} GROUP BY vt").fetchall():
        out.setdefault(str(ts), {})["closed"] = [int(n), int(h)]
    return dict(sorted(out.items()))


def build_range(scans: dict, ranges: dict, i: int, out: Path, *, mount: str | None, threads: int, mem: str, tmp: Path | None,
                con=None) -> dict:
    """One key range's intervals over every scan in `scans`: `intervals/r####.parquet` (sorted
    `(depth, path, usr, vf)`), `hist/r####.parquet` and `digest/r####.json` under `out`. Pass `con` to
    build several ranges on one connection: each scan's footer is then parsed once (DuckDB's
    `parquet_metadata_cache`), not once per range."""
    t0 = monotonic()
    r = ranges["ranges"][i]
    ps = pieces(tuple(r["lo"]), tuple(r["hi"]) if r["hi"] else None)
    con = con or connect(threads, mem, tmp)
    sources = [(_src(scans["bucket"], s["src"], mount), s["ts"], s.get("version")) for s in scans["scans"]]
    con.execute("DROP TABLE IF EXISTS iv")
    con.execute(f"CREATE TABLE iv AS {intervals_sql(con, sources, ps)}")
    t_kernel = monotonic() - t0
    doc = _finish_range(con, "iv", out, i, {"range": r, "kernel_s": round(t_kernel, 1)}, t0)
    con.execute("DROP TABLE iv")
    return doc


def _finish_range(con, table: str, out: Path, i: int, doc: dict, t0: float) -> dict:
    name = f"r{i:04d}"
    rows = write_sorted(_batches(con, f"SELECT * FROM {table} ORDER BY depth, path, usr, vf"), out / "intervals" / f"{name}.parquet",
                        INTERVAL_SCHEMA, INTERVAL_RG, dictionary=["usr", "kind"])
    (out / "hist").mkdir(parents=True, exist_ok=True)
    pq.write_table(con.execute(hist_sql(table)).to_arrow_table(), out / "hist" / f"{name}.parquet", compression=CODEC)
    doc = {**doc, "rows": rows, "digests": digests(con, table), "s": round(monotonic() - t0, 1)}
    (out / "digest").mkdir(parents=True, exist_ok=True)
    (out / "digest" / f"{name}.json").write_text(json.dumps(doc, sort_keys=True) + "\n")
    err(f"range {i}: {rows:,} intervals in {doc['s']}s")
    return doc


def append_range(prev: Path, scan: dict, ranges: dict, i: int, out: Path, *, bucket: str, mount: str | None,
                 threads: int, mem: str, tmp: Path | None) -> dict:
    """Append one scan to a range's intervals (`prev`, `INTERVAL_SCHEMA` sorted): the open versions and the
    scan's merged rows in the range, fully joined by key — a version whose key is gone or whose values
    changed closes at the scan, and a new or changed key opens a version. Writes the range's new
    intervals (equal to a rebuild through the scan), hist and digest, plus `delta/<scan>/r####.parquet`:
    the opened versions (`op` = 1) and the closed ones (`op` = −1, with their new `vt`)."""
    t0 = monotonic()
    r = ranges["ranges"][i]
    ps = pieces(tuple(r["lo"]), tuple(r["hi"]) if r["hi"] else None)
    con = connect(threads, mem, tmp)
    D = scan["ts"]
    src = _src(bucket, scan["src"], mount)
    con.execute(f"CREATE TABLE old AS SELECT * FROM read_parquet({q(str(prev))})")
    last = con.execute("SELECT max(vf) FROM old").fetchone()[0]
    if last is not None and last >= D:
        raise ValueError(f"range {i}: intervals already reach {last} ≥ the appended scan {D}")
    con.execute(f"CREATE TABLE new AS {scan_sql(con, src, ps, scan.get('version'))}")
    differ = " OR ".join(f"o.{c} IS DISTINCT FROM n.{c}" for c in VALUE_COLS)
    con.execute(f"""CREATE TABLE j AS SELECT o.depth AS od, o.path AS op, o.usr AS ou, o.vf AS ovf,
            n.depth AS nd, n.path AS np, n.usr AS nu, (o.depth IS NULL) OR (n.depth IS NULL) OR ({differ}) AS changed
        FROM (SELECT * FROM old WHERE vt = {OPEN}) AS o FULL OUTER JOIN new AS n USING (depth, path, usr)""")
    con.execute(f"""CREATE TABLE closes AS SELECT od AS depth, op AS path, ou AS usr, ovf AS vf FROM j WHERE od IS NOT NULL AND changed""")
    vals = ", ".join(f"n.{c}" for c in VALUE_COLS)
    con.execute(f"""CREATE TABLE opens AS SELECT n.depth, n.path, n.usr, {D}::BIGINT AS vf, {OPEN}::BIGINT AS vt, {vals}
        FROM new AS n SEMI JOIN (SELECT nd, np, nu FROM j WHERE nd IS NOT NULL AND changed) AS c ON n.depth = c.nd AND n.path = c.np AND n.usr = c.nu""")
    olds = ", ".join(f"CASE WHEN c.depth IS NULL THEN o.vt ELSE {D} END::BIGINT AS vt" if c == "vt" else f"o.{c}" for c in INTERVAL_SCHEMA.names)
    con.execute(f"""CREATE TABLE ivs AS
        SELECT {olds} FROM old AS o LEFT JOIN closes AS c ON o.depth = c.depth AND o.path = c.path AND o.usr = c.usr AND o.vf = c.vf
        UNION ALL SELECT {', '.join(INTERVAL_SCHEMA.names)} FROM opens""")
    name = f"r{i:04d}"
    delta = out / "delta" / scan["id"] / f"{name}.parquet"
    delta_schema = INTERVAL_SCHEMA.append(pa.field("op", pa.int8(), nullable=False))
    write_sorted(_batches(con, f"""SELECT *, 1::TINYINT AS op FROM ivs WHERE vf = {D}
                    UNION ALL SELECT *, -1::TINYINT AS op FROM ivs WHERE vt = {D} ORDER BY depth, path, usr, vf"""),
                 delta, delta_schema, INTERVAL_RG, dictionary=["usr", "kind"])
    n_open, n_close = (con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("opens", "closes"))
    return _finish_range(con, "ivs", out, i, {"range": r, "appended": scan["id"], "opened": int(n_open), "closed": int(n_close)}, t0)


# ── Suffix shards ──────────────────────────────────────────────────────────


def plan_shards(hists: list[Path], target_rows: int, tasks: int) -> dict:
    """Shards of about `target_rows` suffix rows, each a run of three-character prefixes (a prefix is
    never split, so a literal's range is in one shard), grouped into `tasks` contiguous task groups."""
    import duckdb

    con = duckdb.connect()
    rows = con.execute(f"""SELECT p3, sum(n)::BIGINT FROM read_parquet([{', '.join(q(str(h)) for h in hists)}])
        GROUP BY p3 ORDER BY p3""").fetchall()
    total = sum(n for _, n in rows)
    shards: list[dict] = []
    cur = None
    for p3, n in rows:
        if cur is None or cur["rows"] + n > target_rows and cur["rows"] > 0:
            cur = {"lo": p3, "rows": 0, "prefixes": 0}
            shards.append(cur)
        cur["rows"] += n
        cur["prefixes"] += 1
    for k, s in enumerate(shards):
        s["i"] = k
        s["hi"] = shards[k + 1]["lo"] if k + 1 < len(shards) else None
    per = total / tasks
    groups: list[list[int]] = [[]]
    acc = 0
    for s in shards:
        if acc >= per * len(groups) and groups[-1] and len(groups) < tasks:
            groups.append([])
        groups[-1].append(s["i"])
        acc += s["rows"]
    biggest = max(rows, key=lambda r: r[1]) if rows else (None, 0)
    return {"target_rows": target_rows, "total_rows": total, "prefixes": len(rows), "largest_prefix": list(biggest),
            "shards": [{k: s[k] for k in ("i", "lo", "hi", "rows", "prefixes")} for s in shards],
            "tasks": [{"t": t, "shards": g, "rows": sum(shards[i]["rows"] for i in g)} for t, g in enumerate(groups)]}


def build_shards(intervals: list[str], plan: dict, t: int, out: Path, *, threads: int, mem: str, tmp: Path) -> dict:
    """Task `t`'s shards: one pass over every interval (depth ≥ 1) keeps the suffix positions whose
    first three characters fall in the task's shards, partitioned by shard on local disk; then each
    shard is sorted `(s, path, usr, vf)` and written as `sx/s####.parquet` in `SX_RG`-row groups, with
    its per-row-group sidecar `sidecar/s####.parquet`."""
    t0 = monotonic()
    task = plan["tasks"][t]
    shards = [plan["shards"][i] for i in task["shards"]]
    lo, hi = shards[0]["lo"], shards[-1]["hi"]
    con = connect(threads, mem, tmp)
    con.execute("CREATE TABLE sh (lo VARCHAR, shard INTEGER)")
    con.executemany("INSERT INTO sh VALUES (?, ?)", [(s["lo"], s["i"]) for s in shards])
    files = "[" + ", ".join(q(f) for f in intervals) + "]"
    rng = f"p3 >= {q(lo)}" + (f" AND p3 < {q(hi)}" if hi is not None else "")
    part = tmp / f"part-{t}"
    if part.exists():
        shutil.rmtree(part)
    con.execute(f"""COPY (
        SELECT substring(x.l, x.p) AS s, x.depth, x.path, x.usr, x.vf, x.vt, x.size, x.n_files, sh.shard
        FROM (SELECT *, substring(l, p, 3) AS p3 FROM (
                SELECT depth, path, usr, vf, vt, size, n_files, l, unnest(generate_series(1, length(l) - 2)) AS p
                FROM (SELECT depth, path, usr, vf, vt, size, n_files, {NAME} AS l FROM read_parquet({files}) WHERE depth >= 1)
                WHERE length(l) >= 3)
              WHERE {rng}) AS x
        ASOF JOIN sh ON x.p3 >= sh.lo
    ) TO {q(str(part))} (FORMAT parquet, PARTITION_BY (shard), COMPRESSION zstd)""")
    t_pass = monotonic() - t0
    docs = []
    for s in shards:
        t1 = monotonic()
        name = f"s{s['i']:04d}"
        src = part / f"shard={s['i']}"
        dst = out / "sx" / f"{name}.parquet"
        stats: list[tuple[str, str, int]] = []

        def on_group(g: pa.Table) -> None:
            col = g.column("s")
            stats.append((col[0].as_py(), col[g.num_rows - 1].as_py(), g.num_rows))

        if src.exists():
            sql = f"SELECT s, depth, path, usr, vf, vt, size, n_files FROM read_parquet({q(str(src / '*.parquet'))}) ORDER BY s, path, usr, vf"
            rows = write_sorted((_sx_cast(b) for b in _batches(con, sql)), dst, SX_SCHEMA, SX_RG, on_group=on_group, dictionary=["usr"])
        else:
            rows = write_sorted(iter(()), dst, SX_SCHEMA, SX_RG, on_group=on_group, dictionary=["usr"])
        side = sidecar_rows(dst, f"sx/{name}.parquet", stats)
        (out / "sidecar").mkdir(parents=True, exist_ok=True)
        pq.write_table(side, out / "sidecar" / f"{name}.parquet", compression=CODEC)
        if src.exists():
            shutil.rmtree(src)
        doc = {"shard": s["i"], "rows": rows, "expected": s["rows"], "groups": len(stats), "bytes": dst.stat().st_size,
               "s": round(monotonic() - t1, 1)}
        if rows != s["rows"]:
            raise RuntimeError(f"shard {s['i']}: {rows:,} rows written, {s['rows']:,} planned")
        err(f"shard {s['i']}: {rows:,} rows, {doc['bytes']:,} B in {doc['s']}s")
        docs.append(doc)
    return {"task": t, "pass_s": round(t_pass, 1), "shards": docs, "s": round(monotonic() - t0, 1)}


def sidecar_rows(path: Path, name: str, stats: list[tuple[str, str, int]]) -> pa.Table:
    """Per row group: its `s` range (from the rows written, never truncated statistics) and the
    contiguous byte span of its column chunks."""
    md = pq.ParquetFile(path).metadata
    if md.num_row_groups != len(stats):
        raise RuntimeError(f"{path}: {md.num_row_groups} row groups, {len(stats)} recorded")
    rows = {k: [] for k in SIDECAR_SCHEMA.names}
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        starts = [rg.column(c).dictionary_page_offset or rg.column(c).data_page_offset for c in range(rg.num_columns)]
        ends = [st + rg.column(c).total_compressed_size for c, st in enumerate(starts)]
        lo, hi, n = stats[g]
        if n != rg.num_rows:
            raise RuntimeError(f"{path} rg {g}: {rg.num_rows} rows, {n} recorded")
        for k, v in zip(SIDECAR_SCHEMA.names, (name, g, lo, hi, min(starts), max(ends) - min(starts), n)):
            rows[k].append(v)
    return pa.table(rows, schema=SIDECAR_SCHEMA)


# ── Reader (the Worker's logic) ────────────────────────────────────────────


class Spans:
    """A seekable file over a few cached byte spans (the footer and the fetched range)."""

    def __init__(self, size: int, spans: list[tuple[int, bytes]]):
        self.size, self.spans, self.pos = size, spans, 0
        self.closed = False

    def seekable(self): return True
    def readable(self): return True
    def writable(self): return False
    def tell(self): return self.pos
    def close(self): self.closed = True

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        for start, data in self.spans:
            if start <= self.pos and self.pos + n <= start + len(data):
                out = data[self.pos - start:self.pos - start + n]
                self.pos += n
                return out
        raise IOError(f"read outside cached spans: {self.pos}+{n}")


class Reader:
    """Answer a literal from the suffix shards: the sidecar locates the row groups whose `s` range can
    hold suffixes starting with it (contiguous, in one or two files), one ranged read per file, then
    the first-hit filter and per-bucket sums for each date."""

    def __init__(self, fetch, size_of, sidecar: pa.Table):
        self.fetch, self.size_of = fetch, size_of
        side = sidecar.sort_by([("file", "ascending"), ("rg", "ascending")]).to_pylist()
        self.groups = side
        self.mins = [g["s_min"] for g in side]
        self.maxs = [g["s_max"] for g in side]
        self.footers: dict[str, tuple[int, bytes]] = {}

    def footer(self, file: str) -> tuple[int, bytes]:
        if file not in self.footers:
            size = self.size_of(file)
            tail = self.fetch(file, size - 8, size)
            flen = int.from_bytes(tail[:4], "little")
            # pyarrow's first read is a speculative tail of up to 64 KiB: cache at least that much
            self.footers[file] = (size, self.fetch(file, max(0, size - max(8 + flen, 1 << 16)), size))
        return self.footers[file]

    def rows(self, term: str) -> tuple[list[dict], dict]:
        from bisect import bisect_left

        key = term.lower()
        a = bisect_left(self.maxs, key)
        b = bisect_left(self.mins, key + "\U0010ffff")
        sel = self.groups[a:b]
        by_file: dict[str, list[dict]] = {}
        for g in sel:
            by_file.setdefault(g["file"], []).append(g)
        out: list[dict] = []
        io = {"groups": len(sel), "bytes": 0, "rows_read": 0, "files": len(by_file)}
        for file, gs in by_file.items():
            lo = gs[0]["offset"]
            hi = gs[-1]["offset"] + gs[-1]["length"]
            data = self.fetch(file, lo, hi)
            io["bytes"] += len(data)
            size, foot = self.footer(file)
            pf = pq.ParquetFile(Spans(size, [(size - len(foot), foot), (lo, data)]))
            tab = pf.read_row_groups([g["rg"] for g in gs])
            io["rows_read"] += tab.num_rows
            out += [r for r in tab.to_pylist() if r["s"].startswith(key)]
        return out, io

    def answer(self, term: str, dates: list[str]) -> dict:
        """Per date: `{bucket: [bytes, objects]}` of the first hits live on that scan."""
        key = term.lower()
        hit, io = self.rows(key)
        hit = [r for r in hit if key in r["path"].rsplit("/", 1)[-1].lower()]
        answers = {}
        for d in dates:
            D = scan_epoch(d) * 1000
            seen, totals = set(), {}
            for r in hit:
                vf, vt = _ms(r["vf"]), _ms(r["vt"])
                if not (vf <= D < vt) or r["depth"] < 1:
                    continue
                k = (r["path"], r["usr"], vf)
                if k in seen:
                    continue
                seen.add(k)
                parent = r["path"].rsplit("/", 1)[0] if "/" in r["path"] else ""
                if key in parent.lower():
                    continue
                bkt = r["path"].split("/", 1)[0]
                b_, o_ = totals.get(bkt, (0, 0))
                totals[bkt] = (b_ + r["size"], o_ + r["n_files"])
            answers[d] = {k: list(v) for k, v in sorted(totals.items())}
        return {"q": key, "io": io, "rows_matching": len(hit), "answers": answers}


def _ms(v) -> int:
    return int(v.timestamp() * 1000) if hasattr(v, "timestamp") else int(v)


def gcs_reader(bucket: str, prefix: str) -> Reader:
    from google.cloud import storage

    b = storage.Client().bucket(bucket)
    side = pq.read_table(_download(b, f"{prefix}/sidecar.parquet"))
    sizes: dict[str, int] = {}

    def size_of(file: str) -> int:
        if file not in sizes:
            blob = b.get_blob(f"{prefix}/{file}")
            sizes[file] = int(blob.size)
        return sizes[file]

    def fetch(file: str, lo: int, hi: int) -> bytes:
        return b.blob(f"{prefix}/{file}").download_as_bytes(start=lo, end=hi - 1)

    return Reader(fetch, size_of, side)


def _download(bucket, key: str) -> pa.BufferReader:
    return pa.BufferReader(bucket.blob(key).download_as_bytes())


# ── GCS IO ─────────────────────────────────────────────────────────────────


def upload_tree(local: Path, bucket: str, prefix: str, *, workers: int = 8) -> list[dict]:
    """Upload every file under `local` to `gs://bucket/prefix/<rel>`; returns `{key, size, md5}` per file."""
    from concurrent.futures import ThreadPoolExecutor

    from google.cloud import storage

    b = storage.Client().bucket(bucket)
    files = sorted(p for p in local.rglob("*") if p.is_file())

    def one(p: Path) -> dict:
        key = f"{prefix}/{p.relative_to(local).as_posix()}"
        blob = b.blob(key)
        blob.chunk_size = 64 << 20
        blob.upload_from_filename(str(p), checksum="crc32c")
        blob.reload()
        return {"key": key, "size": int(blob.size), "md5": base64.b64decode(blob.md5_hash).hex() if blob.md5_hash else None}

    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(one, files))


def read_json(uri: str) -> dict:
    if uri.startswith("gs://"):
        from google.cloud import storage

        bucket, key = uri[5:].split("/", 1)
        return json.loads(storage.Client().bucket(bucket).blob(key).download_as_bytes())
    return json.loads(Path(uri).read_text())


def manifest(bucket: str, prefix: str, sub: str) -> list[dict]:
    """`{key, size, md5, generation}` of every object under `prefix/sub/`, sorted."""
    from google.cloud import storage

    out = []
    for blob in storage.Client().list_blobs(bucket, prefix=f"{prefix}/{sub}/"):
        out.append({"key": blob.name.removeprefix(prefix + "/"), "size": int(blob.size), "md5": base64.b64decode(blob.md5_hash).hex() if blob.md5_hash else None,
                    "crc32c": blob.crc32c, "generation": int(blob.generation)})
    return sorted(out, key=lambda o: o["key"])


# ── CLI ────────────────────────────────────────────────────────────────────


@group("static-names")
def cli() -> None:
    """Static name search: interval and suffix-shard builds (specs/architecture/static-name-search.md)."""


def _task(index: int | None) -> int:
    if index is not None:
        return index
    v = os.environ.get("BATCH_TASK_INDEX")
    if v is None:
        raise SystemExit("no task index: pass -i or run as a Batch task ($BATCH_TASK_INDEX)")
    return int(v)


@cli.command("scans")
@option("-b", "--bucket", default=DATA_BUCKET, help="Data bucket")
@option("-s", "--start", help="First scan date (inclusive)")
@option("-t", "--through", help="Last scan date (inclusive)")
def scans_cmd(bucket: str, start: str | None, through: str | None) -> None:
    """Print the scans manifest (JSON): each date's newest `path` sort, pinned by generation and md5."""
    from google.cloud import storage

    doc = list_scans(bucket, start=start, through=through)
    b = storage.Client().bucket(bucket)
    for s in doc["scans"]:
        s["version"] = _version_from_footer(b, s["src"])
    print(json.dumps(doc, indent=1))


def _version_from_footer(bucket, key: str) -> int:
    """The source format from the parquet schema (footer read; the column names decide)."""
    blob = bucket.get_blob(key)
    size = int(blob.size)
    tail = blob.download_as_bytes(start=size - 8, end=size - 1)
    flen = int.from_bytes(tail[:4], "little")
    # The schema leads the footer's thrift: a few KB reach every column name.
    head = blob.download_as_bytes(start=size - 8 - flen, end=min(size - 9, size - 8 - flen + 16383))
    if b"n_files" in head and b"kind" in head:
        return 2
    if b"wts" in head and b"wb" in head:
        return 1
    raise ValueError(f"{key}: unknown source format")


@cli.command("ranges")
@option("-k", "--ranges", "k", default=128, type=IntRange(min=1), help="Number of key ranges")
@option("-m", "--mount", help="Local mount of the data bucket (default: read gs:// directly)")
@argument("scans_json")
def ranges_cmd(k: int, mount: str | None, scans_json: str) -> None:
    """Print `k` `(depth, path)` key ranges of about equal input rows (JSON)."""
    print(json.dumps(plan_ranges(read_json(scans_json), k, mount), indent=1))


@cli.command("intervals")
@option("-b", "--bucket", default=DATA_BUCKET, help="Output bucket")
@option("-g", "--gen", required=True, help="Output generation: gs://BUCKET/static-names/GEN/")
@option("-f", "--force", is_flag=True, help="Rebuild ranges whose digest is already uploaded")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", help="Local mount of the data bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--per-task", default=1, type=IntRange(min=1), help="Ranges per task: task t builds ranges [t·n, (t+1)·n)")
@option("-o", "--out", default="/stage/out", help="Local output dir (uploaded, then removed)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-r", "--range", "only", help="Comma-separated range indices (overrides -i/-n; a partial build)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
@option("-U", "--no-upload", is_flag=True, help="Keep the outputs local")
def intervals_cmd(bucket, gen, force, index, mount, mem, per_task, out, threads, only, tmp, no_upload) -> None:
    """Build key ranges' intervals over every scan of the generation's `scans.json`, one connection
    per task (each range uploaded as it finishes; its digest, written last, marks it done)."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    scans = read_json(f"gs://{bucket}/{prefix}/scans.json")
    ranges = read_json(f"gs://{bucket}/{prefix}/ranges.json")
    if only:
        todo = [int(x) for x in only.split(",")]
    else:
        t = _task(index)
        todo = list(range(t * per_task, min((t + 1) * per_task, ranges["k"])))
    b = storage.Client().bucket(bucket)
    con = connect(threads, mem, tmp)
    import duckdb

    err(f"intervals: duckdb {duckdb.__version__}, pyarrow {pa.__version__}, ranges {todo}")
    for i in todo:
        if not force and b.blob(f"{prefix}/digest/r{i:04d}.json").exists():
            err(f"range {i}: already built")
            continue
        outp = Path(out) / f"r{i}"
        doc = build_range(scans, ranges, i, outp, mount=mount, threads=threads, mem=mem, tmp=Path(tmp), con=con)
        if not no_upload:
            digest = outp / "digest"
            moved = Path(out) / f"r{i}-digest"
            shutil.move(str(digest), moved)
            upload_tree(outp, bucket, prefix)
            upload_tree(moved.parent / moved.name, bucket, f"{prefix}/digest")
            shutil.rmtree(outp)
            shutil.rmtree(moved)
        print(json.dumps({k: v for k, v in doc.items() if k != "digests"}), flush=True)


@cli.command("append")
@option("-b", "--bucket", default=DATA_BUCKET, help="Output bucket")
@option("-f", "--from-gen", "from_gen", required=True, help="Generation whose intervals the scan is appended to")
@option("-g", "--gen", required=True, help="Output generation (its scans.json = FROM's plus the scan)")
@option("-i", "--index", type=int, help="Range index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", help="Local mount of the data bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
@option("-U", "--no-upload", is_flag=True, help="Keep the outputs local")
def append_cmd(bucket, from_gen, gen, index, mount, mem, out, threads, tmp, no_upload) -> None:
    """Append the newest scan of GEN's `scans.json` to one range of FROM's intervals."""
    prefix = f"{PREFIX}/{gen}"
    scans = read_json(f"gs://{bucket}/{prefix}/scans.json")
    ranges = read_json(f"gs://{bucket}/{prefix}/ranges.json")
    prev_scans = read_json(f"gs://{bucket}/{PREFIX}/{from_gen}/scans.json")
    if scans["scans"][:-1] != prev_scans["scans"]:
        raise SystemExit(f"{gen}'s scans are not {from_gen}'s plus one")
    i = _task(index)
    src = Path(mount) / PREFIX / from_gen / "intervals" / f"r{i:04d}.parquet" if mount else None
    if src is None:
        raise SystemExit("append reads the previous intervals through the bucket mount (-m)")
    outp = Path(out) / f"r{i}"
    doc = append_range(src, scans["scans"][-1], ranges, i, outp, bucket=scans["bucket"], mount=mount, threads=threads, mem=mem, tmp=Path(tmp))
    if not no_upload:
        upload_tree(outp, bucket, prefix)
        shutil.rmtree(outp)
    print(json.dumps(doc))


@cli.command("plan-shards")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-n", "--target-rows", default=50_000_000, type=int, help="Suffix rows per shard file")
@option("-t", "--tasks", default=32, type=int, help="Batch tasks (contiguous groups of shards)")
def plan_shards_cmd(bucket, gen, target_rows, tasks) -> None:
    """Print the shard plan (JSON) from the generation's per-range prefix histograms."""
    import tempfile

    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    b = storage.Client().bucket(bucket)
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for blob in storage.Client().list_blobs(bucket, prefix=f"{prefix}/hist/"):
            p = Path(d) / Path(blob.name).name
            b.blob(blob.name).download_to_filename(str(p))
            paths.append(p)
        if not paths:
            raise SystemExit(f"no histograms under gs://{bucket}/{prefix}/hist/")
        print(json.dumps(plan_shards(sorted(paths), target_rows, tasks), indent=1))


@cli.command("shards")
@option("-b", "--bucket", default=DATA_BUCKET, help="Output bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-I", "--intervals-gen", help="Generation whose intervals to read (default: GEN)")
@option("-m", "--mount", required=True, help="Local mount of the bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill + partition dir")
@option("-U", "--no-upload", is_flag=True, help="Keep the outputs local")
def shards_cmd(bucket, gen, index, intervals_gen, mount, mem, out, threads, tmp, no_upload) -> None:
    """Build one task group's suffix shards (and their sidecars)."""
    prefix = f"{PREFIX}/{gen}"
    plan = read_json(f"gs://{bucket}/{prefix}/shards.json")
    ig = intervals_gen or gen
    ranges = read_json(f"gs://{bucket}/{PREFIX}/{ig}/ranges.json")
    files = [f"{mount}/{PREFIX}/{ig}/intervals/r{r['i']:04d}.parquet" for r in ranges["ranges"]]
    t = _task(index)
    outp = Path(out) / f"t{t}"
    doc = build_shards(files, plan, t, outp, threads=threads, mem=mem, tmp=Path(tmp))
    if not no_upload:
        upload_tree(outp, bucket, prefix)
        shutil.rmtree(outp)
    print(json.dumps(doc))


@cli.command("sidecar")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
def sidecar_cmd(bucket, gen) -> None:
    """Concatenate the per-shard sidecars into `sidecar.parquet` (sorted `(file, rg)`, which is `s`
    order) and print the suffix manifest (JSON): every shard file and the sidecar, with md5s."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    client = storage.Client()
    b = client.bucket(bucket)
    tables = [pq.read_table(_download(b, blob.name)) for blob in client.list_blobs(bucket, prefix=f"{prefix}/sidecar/")]
    side = pa.concat_tables(tables).sort_by([("file", "ascending"), ("rg", "ascending")])
    mins, maxs = side.column("s_min").to_pylist(), side.column("s_max").to_pylist()
    for k in range(1, len(mins)):
        if mins[k] < maxs[k - 1]:
            raise SystemExit(f"sidecar row {k}: s_min {mins[k]!r} < previous s_max {maxs[k - 1]!r}: shards overlap")
    sink = pa.BufferOutputStream()
    pq.write_table(side, sink, compression=CODEC, row_group_size=1 << 20)
    blob = b.blob(f"{prefix}/sidecar.parquet")
    blob.upload_from_string(sink.getvalue().to_pybytes(), content_type="application/vnd.apache.parquet")
    files = manifest(bucket, prefix, "sx")
    blob.reload()
    print(json.dumps({"gen": gen, "rows": int(sum(side.column("rows").to_pylist())), "row_groups": side.num_rows,
                      "bytes": sum(f["size"] for f in files), "files": files,
                      "sidecar": {"key": "sidecar.parquet", "size": int(blob.size), "md5": base64.b64decode(blob.md5_hash).hex() if blob.md5_hash else None,
                                  "generation": int(blob.generation)}}, indent=1))


@cli.command("manifest")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
@argument("subdirs", nargs=-1, required=True)
def manifest_cmd(bucket, gen, subdirs) -> None:
    """Print `{subdir: [{key, size, md5, generation}]}` for the generation's outputs (JSON); `digest`
    adds the per-scan opened/closed totals summed over ranges (counts, digests mod 2⁶⁴)."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    doc: dict = {"gen": gen}
    for sub in subdirs:
        doc[sub] = manifest(bucket, prefix, sub)
    if "digest" in subdirs:
        b = storage.Client().bucket(bucket)
        totals: dict[str, dict] = {}
        rows = 0
        for f in doc["digest"]:
            d = json.loads(b.blob(f"{prefix}/{f['key']}").download_as_bytes())
            rows += d["rows"]
            for ts, kinds in d["digests"].items():
                for kind, (n, h) in kinds.items():
                    cur = totals.setdefault(ts, {}).setdefault(kind, [0, 0])
                    cur[0] += n
                    cur[1] = (cur[1] + h) % U64
        doc["rows"] = rows
        doc["per_scan"] = {datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d"): v for ts, v in sorted(totals.items(), key=lambda kv: int(kv[0]))}
    print(json.dumps(doc, indent=1))


@cli.command("query")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-d", "--date", "dates", multiple=True, required=True, help="Scan date; repeat")
@option("-g", "--gen", required=True, help="Generation")
@argument("terms", nargs=-1, required=True)
def query_cmd(bucket, dates, gen, terms) -> None:
    """Answer literals from the suffix shards on GCS (sidecar → one range per file): one JSON line per
    term with its I/O and per-date `{bucket: [bytes, objects]}`."""
    r = gcs_reader(bucket, f"{PREFIX}/{gen}")
    for t in terms:
        t0 = monotonic()
        out = r.answer(t, list(dates))
        out["s"] = round(monotonic() - t0, 3)
        print(json.dumps(out), flush=True)


if __name__ == "__main__":
    cli()
