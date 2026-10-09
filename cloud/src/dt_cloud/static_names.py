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
#: Intermediates (the suffix shuffle, its markers): us-east1, no soft delete, objects deleted at 7 days.
SCRATCH_BUCKET = "oa-gcs-usage-scratch"
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


def islands_sql(long_sql: str, key_cols: list[str], state_cols: list[str]) -> str:
    """Gaps-and-islands over `long_sql` rows `(__scan, *key_cols, *state_cols)` → `(*key_cols, *state_cols,
    __scan_lo, __scan_hi)`: a new run opens when a key's state changes or its scan index skips (the key was
    absent in between). This is pyrmts' SCD-2 kernel (`pyrmts_engine.multiscan_duckdb._intervals_sql`, the
    one `overtime.py` consolidates with) without its final sort; the job image ships pyrmts but not
    pyrmts-engine (or polars), so it is restated here and `test_islands_equal_pyrmts` pins the two together."""
    key_by = ", ".join(key_cols)
    changed = " OR ".join(f"{c} IS DISTINCT FROM lag({c}) OVER w" for c in state_cols)
    firsts = ", ".join(f"any_value({c}) AS {c}" for c in state_cols)
    return f"""
    WITH marked AS (
        SELECT *, CASE WHEN row_number() OVER w = 1 OR __scan <> lag(__scan) OVER w + 1 OR {changed} THEN 1 ELSE 0 END AS __is_new
        FROM ({long_sql}) WINDOW w AS (PARTITION BY {key_by} ORDER BY __scan)
    ), grp AS (
        SELECT *, sum(__is_new) OVER (PARTITION BY {key_by} ORDER BY __scan ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS __grp
        FROM marked
    )
    SELECT {key_by}, {firsts}, min(__scan)::BIGINT AS __scan_lo, max(__scan)::BIGINT AS __scan_hi
    FROM grp GROUP BY {key_by}, __grp"""


def intervals_sql(con, sources: list[tuple[str, int, int | None]], ps: list[Piece]) -> str:
    """The range's intervals over `sources` (`(path, epoch, version)`, oldest first) as a query of
    `INTERVAL_SCHEMA` rows (unsorted): gaps-and-islands over the per-scan merged rows, scan indices
    mapped to epochs (`vt` = the first scan after the run, or `OPEN`). Every state column is equal
    within a run (the stamp is stored rounded), so the run's values are deterministic."""
    long = " UNION ALL ".join(f"SELECT {j}::BIGINT AS __scan, * FROM ({scan_sql(con, p, ps, v)})" for j, (p, _, v) in enumerate(sources))
    kernel = islands_sql(long, KEY_COLS, VALUE_COLS)
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


# ── Coalesced versions ─────────────────────────────────────────────────────

#: What a name answer reads of a version: its key, liveness and these values. Versions that differ only in
#: other values (`last_read`, the mean stamp, storage classes, …) are merged when adjacent (`vt` = the next
#: `vf`), so a name index over the coalesced versions answers exactly as one over every version.
ANSWER_COLS = ["size", "n_files"]
CINTERVAL_SCHEMA = pa.schema([INTERVAL_SCHEMA.field(c) for c in ["depth", "path", "usr", "vf", "vt", *ANSWER_COLS]])


def coalesce_sql(src: str) -> str:
    """`src`'s intervals (`INTERVAL_SCHEMA` rows, a table or `read_parquet(…)`) with adjacent versions of a key
    merged while `size` and `n_files` hold: a run opens at a key's first version, after an absence (`vf` ≠ the
    previous `vt`) or when either value changes. `CINTERVAL_SCHEMA` rows, unsorted."""
    same = " AND ".join(f"lag({c}) OVER w = {c}" for c in ANSWER_COLS)
    vals = ", ".join(f"any_value({c}) AS {c}" for c in ANSWER_COLS)
    return f"""WITH m AS (
            SELECT depth, path, usr, vf, vt, {', '.join(ANSWER_COLS)},
                CASE WHEN lag(vt) OVER w = vf AND {same} THEN 0 ELSE 1 END AS __is_new
            FROM {src} WINDOW w AS (PARTITION BY depth, path, usr ORDER BY vf)
        ), g AS (
            SELECT *, sum(__is_new) OVER (PARTITION BY depth, path, usr ORDER BY vf ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS __grp FROM m
        )
        SELECT depth, path, usr, min(vf) AS vf, max(vt) AS vt, {vals} FROM g GROUP BY depth, path, usr, __grp"""


def term_rows_sql(table: str, terms: list[str]) -> str:
    """Per term: the suffix rows its range holds over `table`'s versions (Σ versions × occurrences in the
    lowercase name, overlapping ones included: each is a suffix starting with the term), depth ≥ 1."""
    if not terms:
        return "SELECT NULL::VARCHAR AS term, 0::BIGINT AS n WHERE false"
    parts = []
    for t in terms:
        n = len(t)
        parts.append(f"""SELECT {q(t)} AS term, coalesce(sum(list_count(list_filter(range(1, length(l) - {n} + 2),
                lambda p: substring(l, p, {n}) = {q(t)}))), 0)::BIGINT AS n
            FROM (SELECT {NAME} AS l FROM {table} WHERE depth >= 1) WHERE contains(l, {q(t)})""")
    return " UNION ALL ".join(parts)


def suffix_count_sql(table: str) -> str:
    """`(versions, suffix rows)` at depth ≥ 1: a version's suffix rows are its name's positions of ≥ 3 characters."""
    return f"""SELECT count(*)::BIGINT, coalesce(sum(greatest(length(l) - 2, 0)), 0)::BIGINT FROM (SELECT {NAME} AS l FROM {table} WHERE depth >= 1)"""


def coalesce_range(src: str, i: int, out: Path, con, terms: list[str] | None = None) -> dict:
    """One range's coalesced versions: `cintervals/r####.parquet` (sorted `(depth, path, usr, vf)`),
    `chist/r####.parquet` (suffix rows per three-character prefix) and `cstats/r####.json` (versions and
    suffix rows before and after, and each of `terms`' range rows before and after)."""
    t0 = monotonic()
    name = f"r{i:04d}"
    con.execute("DROP TABLE IF EXISTS iv; DROP TABLE IF EXISTS civ")
    con.execute(f"CREATE TABLE iv AS SELECT depth, path, usr, vf, vt, {', '.join(ANSWER_COLS)} FROM read_parquet({q(src)})")
    con.execute(f"CREATE TABLE civ AS {coalesce_sql('iv')}")
    rows = write_sorted(_batches(con, "SELECT * FROM civ ORDER BY depth, path, usr, vf"), out / "cintervals" / f"{name}.parquet",
                        CINTERVAL_SCHEMA, INTERVAL_RG, dictionary=["usr"])
    (out / "chist").mkdir(parents=True, exist_ok=True)
    pq.write_table(con.execute(hist_sql("civ")).to_arrow_table(), out / "chist" / f"{name}.parquet", compression=CODEC)
    stats: dict = {"range": i, "versions": [con.execute("SELECT count(*) FROM iv").fetchone()[0], rows]}
    stats["dir_versions"] = [con.execute(f"SELECT count(*) FROM {t} WHERE depth >= 1").fetchone()[0] for t in ("iv", "civ")]
    stats["suffix_rows"] = [con.execute(suffix_count_sql(t)).fetchone()[1] for t in ("iv", "civ")]
    if terms:
        before = dict(con.execute(term_rows_sql("iv", terms)).fetchall())
        after = dict(con.execute(term_rows_sql("civ", terms)).fetchall())
        stats["terms"] = {t: [int(before.get(t, 0)), int(after.get(t, 0))] for t in terms}
    stats["s"] = round(monotonic() - t0, 1)
    (out / "cstats").mkdir(parents=True, exist_ok=True)
    (out / "cstats" / f"{name}.json").write_text(json.dumps(stats, sort_keys=True) + "\n")
    con.execute("DROP TABLE iv; DROP TABLE civ")
    err(f"coalesce range {i}: {stats['versions'][0]:,} → {rows:,} versions, suffix rows {stats['suffix_rows'][0]:,} → {stats['suffix_rows'][1]:,} in {stats['s']}s")
    return stats


def coalesce_append(prev: Path, delta: Path, scan: dict, out: Path, i: int, con) -> dict:
    """Append scan `D` to a range's coalesced versions (`prev`, `CINTERVAL_SCHEMA` sorted) from the intervals'
    delta for `D` (`delta/<D>/r####.parquet`: `op` 1 = a version opened at `D`, −1 = one closed at `D`). Per
    key: closed and reopened with the same answer values continues its coalesced version; closed alone (or
    reopened with others) closes it at `D`; opened alone (or with others) opens one. Writes the range's
    `cintervals/` (equal to coalescing the appended intervals) and `cdelta/<D>/r####.parquet` (the coalesced
    versions opened, `op` 1, and closed, `op` −1)."""
    name = f"r{i:04d}"
    D = scan["ts"]
    con.execute("DROP TABLE IF EXISTS cold; DROP TABLE IF EXISTS dl; DROP TABLE IF EXISTS cev; DROP TABLE IF EXISTS cnew")
    con.execute(f"CREATE TABLE cold AS SELECT * FROM read_parquet({q(str(prev))})")
    last = con.execute("SELECT max(vf) FROM cold").fetchone()[0]
    if last is not None and last >= D:
        raise ValueError(f"range {i}: coalesced versions already reach {last} ≥ the appended scan {D}")
    vals = ", ".join(ANSWER_COLS)
    con.execute(f"CREATE TABLE dl AS SELECT depth, path, usr, op, {vals} FROM read_parquet({q(str(delta))})")
    same = " AND ".join(f"o.{c} = c.{c}" for c in ANSWER_COLS)
    # per key: its closing interval's values (c) and its opening one's (o)
    con.execute(f"""CREATE TABLE cev AS SELECT coalesce(o.depth, c.depth) AS depth, coalesce(o.path, c.path) AS path,
            coalesce(o.usr, c.usr) AS usr, c.depth IS NOT NULL AS closed, o.depth IS NOT NULL AS opened,
            c.depth IS NOT NULL AND o.depth IS NOT NULL AND {same} AS continues, {', '.join(f'o.{c}' for c in ANSWER_COLS)}
        FROM (SELECT * FROM dl WHERE op = 1) AS o FULL OUTER JOIN (SELECT * FROM dl WHERE op = -1) AS c USING (depth, path, usr)""")
    con.execute(f"""CREATE TABLE cnew AS
        SELECT o.depth, o.path, o.usr, o.vf,
            CASE WHEN o.vt = {OPEN} AND e.closed AND NOT e.continues THEN {D} ELSE o.vt END::BIGINT AS vt, {', '.join(f'o.{c}' for c in ANSWER_COLS)}
        FROM cold AS o LEFT JOIN cev AS e ON o.vt = {OPEN} AND o.depth = e.depth AND o.path = e.path AND o.usr = e.usr
        UNION ALL SELECT depth, path, usr, {D}::BIGINT AS vf, {OPEN}::BIGINT AS vt, {vals} FROM cev WHERE opened AND NOT continues""")
    rows = write_sorted(_batches(con, "SELECT * FROM cnew ORDER BY depth, path, usr, vf"), out / "cintervals" / f"{name}.parquet",
                        CINTERVAL_SCHEMA, INTERVAL_RG, dictionary=["usr"])
    dschema = CINTERVAL_SCHEMA.append(pa.field("op", pa.int8(), nullable=False))
    write_sorted(_batches(con, f"""SELECT *, 1::TINYINT AS op FROM cnew WHERE vf = {D}
                    UNION ALL SELECT *, -1::TINYINT AS op FROM cnew WHERE vt = {D} ORDER BY depth, path, usr, vf, op"""),
                 out / "cdelta" / scan["id"] / f"{name}.parquet", dschema, INTERVAL_RG, dictionary=["usr"])
    (out / "chist").mkdir(parents=True, exist_ok=True)
    pq.write_table(con.execute(hist_sql("cnew")).to_arrow_table(), out / "chist" / f"{name}.parquet", compression=CODEC)
    n_open, n_close = con.execute(f"SELECT count(*) FILTER (WHERE vf = {D}), count(*) FILTER (WHERE vt = {D}) FROM cnew").fetchone()
    con.execute("DROP TABLE cold; DROP TABLE dl; DROP TABLE cev; DROP TABLE cnew")
    return {"range": i, "appended": scan["id"], "rows": rows, "opened": int(n_open), "closed": int(n_close)}


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
    return {"ranges": len(hists), "target_rows": target_rows, "total_rows": total, "prefixes": len(rows), "largest_prefix": list(biggest),
            "shards": [{k: s[k] for k in ("i", "lo", "hi", "rows", "prefixes")} for s in shards],
            "tasks": [{"t": t, "shards": g, "rows": sum(shards[i]["rows"] for i in g)} for t, g in enumerate(groups)]}


def suffix_sql(files: list[str], rng: str | None = None) -> str:
    """Every suffix position of three or more characters of the intervals' lowercase names (depth ≥ 1),
    as `(s, depth, path, usr, vf, vt, size, n_files, p3)` rows (`p3` = the suffix's first three
    characters), optionally restricted to a `p3` range."""
    lst = "[" + ", ".join(q(f) for f in files) + "]"
    return f"""SELECT substring(l, p) AS s, depth, path, usr, vf, vt, size, n_files, p3 FROM (
            SELECT *, substring(l, p, 3) AS p3 FROM (
                SELECT depth, path, usr, vf, vt, size, n_files, l, unnest(generate_series(1, length(l) - 2)) AS p
                FROM (SELECT depth, path, usr, vf, vt, size, n_files, {NAME} AS l FROM read_parquet({lst}) WHERE depth >= 1)
                WHERE length(l) >= 3)
            {f'WHERE {rng}' if rng else ''})"""


def _shard_table(con, plan: dict) -> None:
    """`sh(lo, shard, grp)`: each shard's first prefix and the task group building it (for an ASOF join)."""
    grp = {i: t["t"] for t in plan["tasks"] for i in t["shards"]}
    con.execute("DROP TABLE IF EXISTS sh")
    con.execute("CREATE TABLE sh (lo VARCHAR, shard INTEGER, grp INTEGER)")
    con.executemany("INSERT INTO sh VALUES (?, ?, ?)", [(s["lo"], s["i"], grp[s["i"]]) for s in plan["shards"]])


def map_range(interval_file: str, plan: dict, i: int, out: Path, con) -> dict:
    """The map side of the suffix shuffle: one range's intervals → its suffix rows tagged with their shard,
    written per task group as `sxmap/g###/r####-*.parquet` under `out` (each group's reduce task reads
    only its own directory), so the intervals are expanded once in all, not once per task."""
    t0 = monotonic()
    _shard_table(con, plan)
    part = out / f"map-{i}"
    if part.exists():
        shutil.rmtree(part)
    out.mkdir(parents=True, exist_ok=True)
    con.execute(f"""COPY (SELECT x.s, x.depth, x.path, x.usr, x.vf, x.vt, x.size, x.n_files, sh.shard, sh.grp
        FROM ({suffix_sql([interval_file])}) AS x ASOF JOIN sh ON x.p3 >= sh.lo
    ) TO {q(str(part))} (FORMAT parquet, PARTITION_BY (grp), COMPRESSION zstd)""")
    rows = 0
    for d in sorted(part.glob("grp=*")):
        g = int(d.name.split("=")[1])
        dst = out / "sxmap" / f"g{g:03d}"
        dst.mkdir(parents=True, exist_ok=True)
        for k, f in enumerate(sorted(d.glob("*.parquet"))):
            rows += pq.ParquetFile(f).metadata.num_rows
            shutil.move(str(f), dst / f"r{i:04d}-{k}.parquet")
    shutil.rmtree(part)
    doc = {"range": i, "rows": rows, "s": round(monotonic() - t0, 1)}
    err(f"map range {i}: {rows:,} suffix rows in {doc['s']}s")
    return doc


def build_shards(inputs: list[str], plan: dict, t: int, out: Path, *, threads: int, mem: str, tmp: Path) -> dict:
    """The reduce side: task `t`'s map outputs (`sxmap/g{t}/`, rows tagged with their shard) partitioned
    by shard on local disk in one pass; then each shard sorted `(s, path, usr, vf)` and written as
    `sx/s####.parquet` in `SX_RG`-row groups, with its per-row-group sidecar `sidecar/s####.parquet`."""
    t0 = monotonic()
    task = plan["tasks"][t]
    shards = [plan["shards"][i] for i in task["shards"]]
    con = connect(threads, mem, tmp)
    part = tmp / f"part-{t}"
    if part.exists():
        shutil.rmtree(part)
    tmp.mkdir(parents=True, exist_ok=True)
    lst = "[" + ", ".join(q(f) for f in inputs) + "]"
    if inputs:
        con.execute(f"""COPY (SELECT s, depth, path, usr, vf, vt, size, n_files, shard FROM read_parquet({lst}))
            TO {q(str(part))} (FORMAT parquet, PARTITION_BY (shard), COMPRESSION zstd)""")
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


def read_text(uri: str) -> str:
    if uri.startswith("gs://"):
        from google.cloud import storage

        bucket, key = uri[5:].split("/", 1)
        return storage.Client().bucket(bucket).blob(key).download_as_bytes().decode()
    return Path(uri).read_text()


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


@cli.command("coalesce")
@option("-b", "--bucket", default=DATA_BUCKET, help="Output bucket (the scratch bucket for a measurement)")
@option("-f", "--force", is_flag=True, help="Redo ranges whose stats are already uploaded")
@option("-g", "--gen", required=True, help="Output generation: gs://BUCKET/static-names/GEN/{cintervals,chist,cstats}/")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-I", "--intervals-gen", required=True, help="Generation whose intervals to coalesce (read through the mount)")
@option("-m", "--mount", required=True, help="Local mount of the data bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--per-task", default=1, type=IntRange(min=1), help="Ranges per task")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-r", "--range", "only", help="Comma-separated range indices (overrides -i/-n)")
@option("-t", "--terms", help="A file of literals (one per line; a path or gs:// URL) whose range rows to count before and after")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def coalesce_cmd(bucket, force, gen, index, intervals_gen, mount, mem, per_task, out, threads, only, terms, tmp) -> None:
    """Coalesce ranges' intervals to the versions a name answer distinguishes (`size`, `n_files`): each
    range's `cintervals/`, `chist/` and `cstats/` (written last: marks the range done)."""
    from google.cloud import storage

    ranges = read_json(f"gs://{DATA_BUCKET}/{PREFIX}/{intervals_gen}/ranges.json")
    todo = [int(x) for x in only.split(",")] if only else list(range(_task(index) * per_task, min((_task(index) + 1) * per_task, ranges["k"])))
    lits = [x for x in read_text(terms).splitlines() if x.strip()] if terms else None
    prefix = f"{PREFIX}/{gen}"
    b = storage.Client().bucket(bucket)
    con = connect(threads, mem, tmp)
    for i in todo:
        if not force and b.blob(f"{prefix}/cstats/r{i:04d}.json").exists():
            err(f"coalesce range {i}: already done")
            continue
        outp = Path(out) / f"c{i}"
        stats = coalesce_range(f"{mount}/{PREFIX}/{intervals_gen}/intervals/r{i:04d}.parquet", i, outp, con, lits)
        moved = Path(out) / f"c{i}-stats"
        shutil.move(str(outp / "cstats"), moved)
        upload_tree(outp, bucket, prefix)
        upload_tree(moved, bucket, f"{prefix}/cstats")
        shutil.rmtree(outp)
        shutil.rmtree(moved)
        print(json.dumps(stats), flush=True)


@cli.command("coalesce-report")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-g", "--gen", required=True, help="Generation holding `cstats/` and `chist/`")
@option("-H", "--hist-gen", required=True, help="Generation holding the uncoalesced `hist/`")
@option("-v", "--threshold", "thresholds", multiple=True, type=int, default=[100_000, 150_000], help="Range-row thresholds to count prefixes at; repeat")
def coalesce_report_cmd(bucket, gen, hist_gen, thresholds) -> None:
    """Sum the coalesce stage's per-range stats (versions, suffix rows, per-term range rows, before → after) and
    compare three-character prefixes' rows before and after (how many reach each threshold). JSON."""
    import tempfile

    import duckdb
    from google.cloud import storage

    client = storage.Client()
    tot: dict = {"ranges": 0, "versions": [0, 0], "dir_versions": [0, 0], "suffix_rows": [0, 0], "terms": {}}
    for blob in client.list_blobs(bucket, prefix=f"{PREFIX}/{gen}/cstats/"):
        d = json.loads(blob.download_as_bytes())
        tot["ranges"] += 1
        for k in ("versions", "dir_versions", "suffix_rows"):
            tot[k] = [tot[k][0] + d[k][0], tot[k][1] + d[k][1]]
        for t, (x, y) in d.get("terms", {}).items():
            cur = tot["terms"].setdefault(t, [0, 0])
            cur[0] += x
            cur[1] += y
    with tempfile.TemporaryDirectory() as tmpd:
        sets = {}
        for name, g, sub in (("before", hist_gen, "hist"), ("after", gen, "chist")):
            dd = Path(tmpd) / name
            dd.mkdir()
            for blob in client.list_blobs(DATA_BUCKET if name == "before" else bucket, prefix=f"{PREFIX}/{g}/{sub}/"):
                blob.download_to_filename(str(dd / Path(blob.name).name))
            sets[name] = str(dd / "*.parquet")
        con = duckdb.connect()
        rows = con.execute(f"""SELECT coalesce(a.p3, b.p3), coalesce(a.n, 0), coalesce(b.n, 0) FROM
            (SELECT p3, sum(n) AS n FROM read_parquet({q(sets['before'])}) GROUP BY p3) AS a FULL OUTER JOIN
            (SELECT p3, sum(n) AS n FROM read_parquet({q(sets['after'])}) GROUP BY p3) AS b USING (p3)""").fetchall()
    tot["prefixes"] = {str(v): [sum(1 for _, x, _ in rows if x >= v), sum(1 for _, _, y in rows if y >= v)] for v in thresholds}
    tot["heaviest"] = [[p, int(x), int(y)] for p, x, y in sorted(rows, key=lambda r: -r[1])[:20]]
    tot["ratio"] = {k: round(tot[k][1] / max(tot[k][0], 1), 4) for k in ("versions", "dir_versions", "suffix_rows")}
    print(json.dumps(tot, indent=1))


@cli.command("plan-shards")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-H", "--hist", "hist_dir", default="hist", help="Histogram subdir (`chist`: the coalesced versions')")
@option("-n", "--target-rows", default=50_000_000, type=int, help="Suffix rows per shard file")
@option("-t", "--tasks", default=32, type=int, help="Batch tasks (contiguous groups of shards)")
def plan_shards_cmd(bucket, gen, hist_dir, target_rows, tasks) -> None:
    """Print the shard plan (JSON) from the generation's per-range prefix histograms."""
    import tempfile

    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    b = storage.Client().bucket(bucket)
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for blob in storage.Client().list_blobs(bucket, prefix=f"{prefix}/{hist_dir}/"):
            p = Path(d) / Path(blob.name).name
            b.blob(blob.name).download_to_filename(str(p))
            paths.append(p)
        if not paths:
            raise SystemExit(f"no histograms under gs://{bucket}/{prefix}/{hist_dir}/")
        print(json.dumps(plan_shards(sorted(paths), target_rows, tasks), indent=1))


@cli.command("suffix-map")
@option("-b", "--bucket", default=DATA_BUCKET, help="Output bucket")
@option("-C", "--coalesced", is_flag=True, help="Expand GEN's coalesced versions (`cintervals/`) instead of intervals")
@option("-f", "--force", is_flag=True, help="Redo ranges already mapped")
@option("-g", "--gen", required=True, help="Generation (its `shards.json` plan; intervals from -I or GEN)")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-I", "--intervals-gen", help="Generation whose intervals to read (default: GEN)")
@option("-m", "--mount", required=True, help="Local mount of the bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--per-task", default=1, type=IntRange(min=1), help="Ranges per task")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-r", "--range", "only", help="Comma-separated range indices (overrides -i/-n)")
@option("-S", "--scratch", default=SCRATCH_BUCKET, help="Bucket for the shuffle (`sxmap/`, `sxmap-done/`)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def suffix_map_cmd(bucket, coalesced, force, gen, index, intervals_gen, mount, mem, per_task, out, threads, only, scratch, tmp) -> None:
    """Expand ranges' intervals into suffix rows tagged with their shard, written per reduce task
    (`gs://SCRATCH/static-names/GEN/sxmap/g###/`); a range's `sxmap-done/r####.json` there marks it done."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    plan = read_json(f"gs://{bucket}/{prefix}/shards.json")
    ig = intervals_gen or gen
    ranges = read_json(f"gs://{bucket}/{PREFIX}/{ig}/ranges.json")
    if only:
        todo = [int(x) for x in only.split(",")]
    else:
        t = _task(index)
        todo = list(range(t * per_task, min((t + 1) * per_task, ranges["k"])))
    b = storage.Client().bucket(scratch)
    con = connect(threads, mem, tmp)
    for i in todo:
        mark = f"{prefix}/sxmap-done/r{i:04d}.json"
        if not force and b.blob(mark).exists():
            err(f"map range {i}: already mapped")
            continue
        src = f"{mount}/{prefix}/cintervals/r{i:04d}.parquet" if coalesced else f"{mount}/{PREFIX}/{ig}/intervals/r{i:04d}.parquet"
        outp = Path(out) / f"m{i}"
        doc = map_range(src, plan, i, outp, con)
        upload_tree(outp, scratch, prefix)
        shutil.rmtree(outp)
        b.blob(mark).upload_from_string(json.dumps(doc) + "\n")
        print(json.dumps(doc), flush=True)


@cli.command("shards")
@option("-b", "--bucket", default=DATA_BUCKET, help="Output bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", required=True, help="Local mount of the bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-P", "--partial", is_flag=True, help="Accept fewer mapped ranges than ranges (a dev build over some ranges)")
@option("-S", "--scratch", default=SCRATCH_BUCKET, help="Bucket holding the shuffle (mounted beside -m: `<-m>/../SCRATCH`)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill + partition dir")
@option("-U", "--no-upload", is_flag=True, help="Keep the outputs local")
def shards_cmd(bucket, gen, index, mount, mem, out, threads, partial, scratch, tmp, no_upload) -> None:
    """Build one task group's suffix shards (and their sidecars) from its map outputs."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    plan = read_json(f"gs://{bucket}/{prefix}/shards.json")
    t = _task(index)
    done = sum(1 for _ in storage.Client().list_blobs(scratch, prefix=f"{prefix}/sxmap-done/"))
    k = plan.get("ranges")
    if k is not None and done != k and not partial:
        raise SystemExit(f"{done} of {k} ranges mapped: pass -P for a partial (dev) build")
    files = sorted(str(f) for f in (Path(mount).parent / scratch / prefix / "sxmap" / f"g{t:03d}").glob("*.parquet"))
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


def ch_lit(s: str) -> str:
    """A ClickHouse string literal."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def ch_digest_sql(ranges: dict, only: list[int] | None, nodes: str = "nodes", closures: str = "closures") -> str:
    """ClickHouse statements printing, per scan, the versions it opened and closed in the given ranges (all
    when `only` is None) as `kind ts count digest` TSV rows — the strings `digests` hashes, summed as
    `reinterpretAsUInt64(MD5(…))` (= DuckDB's `md5_number_upper`), which wraps mod 2⁶⁴ as the sum does here."""
    def where() -> str:
        if only is None:
            return "1"
        ors = []
        for i in only:
            r = ranges["ranges"][i]
            for pc in pieces(tuple(r["lo"]), tuple(r["hi"]) if r["hi"] else None):
                parts = [f"depth >= {pc.dlo}", f"depth <= {pc.dhi}"]
                if pc.plo:
                    parts.append(f"path >= {ch_lit(pc.plo)}")
                if pc.phi is not None:
                    parts.append(f"path < {ch_lit(pc.phi)}")
                ors.append("(" + " AND ".join(parts) + ")")
        return " OR ".join(ors)

    opened = ("concat(toString(depth), '|', path, '|', toString(usr), '|', toString(toUnixTimestamp(vf)), '|', toString(size), '|', "
              "toString(n_files))")
    closed = "concat(toString(depth), '|', path, '|', toString(usr), '|', toString(toUnixTimestamp(vf)), '|', toString(toUnixTimestamp(vt)))"
    w = where()
    return (f"SELECT 'opened', toUnixTimestamp(vf) AS ts, count(), sum(reinterpretAsUInt64(MD5({opened}))) FROM {nodes} WHERE {w} "
            f"GROUP BY ts ORDER BY ts SETTINGS max_threads = 16 FORMAT TSV;\n"
            f"SELECT 'closed', toUnixTimestamp(vt) AS ts, count(), sum(reinterpretAsUInt64(MD5({closed}))) FROM {closures} WHERE {w} "
            f"GROUP BY ts ORDER BY ts SETTINGS max_threads = 16 FORMAT TSV;\n")


@cli.command("ch-digest-sql")
@option("-c", "--closures", default="closures", help="ClickHouse closures table (`m_closures`: the name index's copy)")
@option("-n", "--nodes", default="nodes", help="ClickHouse nodes table (`m_nodes`: the name index's copy)")
@option("-r", "--range", "only", help="Comma-separated range indices (default: every key)")
@argument("ranges_json")
def ch_digest_sql_cmd(closures, nodes, only, ranges_json) -> None:
    """Print the ClickHouse per-scan digest statements (pipe to `job/ch-store.sh sql -`)."""
    sys.stdout.write(ch_digest_sql(read_json(ranges_json), [int(x) for x in only.split(",")] if only else None, nodes, closures))


@cli.command("verify-intervals")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-r", "--range", "only", help="Comma-separated range indices (default: every range)")
@argument("ch_tsv")
def verify_intervals_cmd(bucket, gen, only, ch_tsv) -> None:
    """Compare the generation's per-scan opened/closed counts and digests (summed over its ranges' digest
    files) with ClickHouse's (`ch-digest-sql` output). Prints a JSON report; exit 1 on any difference."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    b = storage.Client().bucket(bucket)
    ranges = read_json(f"gs://{bucket}/{prefix}/ranges.json")
    idx = [int(x) for x in only.split(",")] if only else [r["i"] for r in ranges["ranges"]]
    mine: dict[tuple[str, int], list[int]] = {}
    for i in idx:
        d = json.loads(b.blob(f"{prefix}/digest/r{i:04d}.json").download_as_bytes())
        for ts, kinds in d["digests"].items():
            for kind, (n, h) in kinds.items():
                cur = mine.setdefault((kind, int(ts)), [0, 0])
                cur[0] += n
                cur[1] = (cur[1] + h) % U64
    theirs: dict[tuple[str, int], list[int]] = {}
    for line in Path(ch_tsv).read_text().splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or parts[0] not in ("opened", "closed"):
            continue
        theirs[(parts[0], int(parts[1]))] = [int(parts[2]), int(parts[3]) % U64]
    keys = sorted(set(mine) | set(theirs), key=lambda k: (k[1], k[0]))
    diff = {f"{k[0]} {datetime.fromtimestamp(k[1], timezone.utc):%Y-%m-%d}": {"static": mine.get(k), "ch": theirs.get(k)}
            for k in keys if mine.get(k) != theirs.get(k)}
    report = {"gen": gen, "ranges": len(idx), "scans": len({k[1] for k in keys}), "keys": len(keys),
              "opened": sum(v[0] for k, v in mine.items() if k[0] == "opened"), "closed": sum(v[0] for k, v in mine.items() if k[0] == "closed"),
              "equal": not diff, "diff": diff}
    print(json.dumps(report, indent=1))
    if diff:
        raise SystemExit(1)


#: What the Worker reads (`r2-copy`): the shards, their sidecars, the plan and scans, and the catalog's served files
#: (not its census or per-shard cells).
R2_SERVED = ("sx/", "sidecar/", "sidecar.parquet", "shards.json", "scans.json", "catalog/cells.parquet", "catalog/index.parquet",
             "catalog/meta.json", "catalog/members.json",
             # the drilldown (static_roots): roots, rollups, their two-level indexes, aliases, meta
             "drill/meta.json", "drill/aliases.parquet", "drill/long/roots/", "drill/long/rollups/", "drill/short/roots/",
             "drill/short/rollups/", "drill/long-roots-index", "drill/long-rollups-index", "drill/short-roots-index",
             "drill/short-rollups-index")


@cli.command("r2-copy")
@option("-b", "--bucket", default=DATA_BUCKET, help="Source GCS bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-n", "--dry-run", is_flag=True, help="List what would be copied")
@option("-w", "--workers", default=8, type=int, help="Parallel copies")
def r2_copy_cmd(bucket, gen, dry_run, workers) -> None:
    """Copy the generation's served files (shards, sidecar, plan, scans) GCS → R2 under the same keys,
    skipping objects already there with the same size and md5 (`publish.copy_one`'s streaming copy,
    the GCS md5 stamped as metadata). R2 via `R2_ENDPOINT`, `R2_BUCKET` and AWS_* (or R2_*) keys."""
    from concurrent.futures import ThreadPoolExecutor

    from . import publish as pub

    prefix = f"{PREFIX}/{gen}"
    objs = [o for o in pub.list_source(bucket, [prefix + "/"]) if o.key.removeprefix(prefix + "/").startswith(R2_SERVED)]
    s3, r2 = pub.r2_client(), pub.r2_bucket()
    with ThreadPoolExecutor(workers) as ex:
        todo = [o for o, do in ex.map(lambda o: (o, pub.should_copy(o, pub.head_dest(s3, r2, o.key))), objs) if do]
    total = sum(o.size for o in todo)
    err(f"r2-copy {gen}: {len(objs)} objects, {len(todo)} to copy ({total:,} B)")
    if dry_run:
        for o in todo:
            print(o.key)
        return
    t0 = monotonic()

    def one(o):
        pub.copy_one(bucket, s3, r2, o)
        return o

    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for o in ex.map(one, todo):
            done += o.size
            err(f"  → {o.key} ({o.size:,} B; {done / total:.1%}, {done / max(monotonic() - t0, 1e-9) / 1e6:.0f} MB/s)")
    print(json.dumps({"gen": gen, "objects": len(objs), "copied": len(todo), "bytes": total, "s": round(monotonic() - t0, 1)}))


@cli.command("compare-answers")
@argument("ch_jsonl")
@argument("static_jsonl")
def compare_answers_cmd(ch_jsonl, static_jsonl) -> None:
    """Compare `query` answers with ClickHouse's (`job/static-names.sh ch-answers`): per (term, date), the
    nonzero buckets' bytes and objects must be equal. Prints a JSON report (with each term's I/O); exit 1
    on any difference."""
    ref = {}
    for line in Path(ch_jsonl).read_text().splitlines():
        if line.startswith("{"):
            d = json.loads(line)
            ref[(d["q"], d["date"])] = {k: list(v) for k, v in d["buckets"].items()}
    pairs, diffs, terms = 0, {}, {}
    for line in Path(static_jsonl).read_text().splitlines():
        if not line.startswith("{"):
            continue
        d = json.loads(line)
        terms[d["q"]] = {**d.get("io", {}), **{k: d[k] for k in ("rows_matching", "source", "rows", "cells") if k in d}, "s": d.get("s")}
        for date, buckets in (d["answers"] or {}).items():
            mine = {k: v for k, v in buckets.items() if v[0] or v[1]}
            if (d["q"], date) not in ref:
                continue
            pairs += 1
            if mine != ref[(d["q"], date)]:
                diffs[f"{d['q']} {date}"] = {"static": mine, "ch": ref[(d["q"], date)]}
    missing = sorted(f"{q} {d}" for q, d in ref if q not in terms)
    report = {"pairs": pairs, "equal": pairs - len(diffs), "missing": missing, "diff": diffs, "terms": terms}
    print(json.dumps(report, indent=1))
    if diffs or missing or not pairs:
        raise SystemExit(1)


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
