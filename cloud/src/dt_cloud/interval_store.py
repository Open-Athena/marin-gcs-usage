"""The change-interval path store (specs/interval-store.md): every scan's path-store rows as versions of
one row per `(depth, path)` — objects and dirs, owner slices folded into `us` — with `[vf, vt)` validity,
instead of a full per-scan copy. The build reuses the static name index's key ranges and pyrmts'
gaps-and-islands kernel (`static_names`); `cut` lays the versions out as the served sorts (`path`,
`bysize`, `reads`) with a `.groups.parquet` beside each, which a reader plans its range reads from.

    dt-cloud interval-store build -g GEN [-i TASK -n PER_TASK | -r RANGES]   # per range: pv/, rd/, digest/
    dt-cloud interval-store cut -g GEN [-s SORT]                             # served/{path,bysize,reads}.parquet + .groups.parquet
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from functools import partial
from pathlib import Path
from time import monotonic

import pyarrow as pa
import pyarrow.parquet as pq
from click import IntRange, group, option

from . import static_names as sn
from .static_names import OPEN, U64, connect, q, range_preds, read_json, upload_tree, write_sorted

err = partial(print, file=sys.stderr, flush=True)

DATA_BUCKET = sn.DATA_BUCKET
PREFIX = "interval-store"
#: A served sort's row-group size: the per-scan path store's (decode cost per group is the same).
SERVED_RG = 8192
#: The range files' row-group size (an intermediate: only `cut` and the verifier read them).
RANGE_RG = 65536

KEY_COLS = ["depth", "path"]
#: The values whose change opens a version. `dr` is the size-weighted mean stamp rounded to the second
#: (the change test; the exact `wts` is carried from the version's first scan), `us` the owner slices.
#: `last_read` is not here: it churns daily on read paths and lives in its own intervals (`rd/`).
CHANGE_COLS = ["kind", "size", "n_files", "n_children", "n_desc", "mtime", "dr", "wb", "c2", "c3", "c4", "us"]
CARRIED = {"wts": "first"}
STATE_COLS = [*CHANGE_COLS, "wts"]

PV_SCHEMA = pa.schema([
    pa.field("depth", pa.uint8(), nullable=False),
    pa.field("path", pa.string(), nullable=False),
    pa.field("vf", pa.int64(), nullable=False),
    pa.field("vt", pa.int64(), nullable=False),
    pa.field("kind", pa.string(), nullable=False),
    pa.field("size", pa.int64(), nullable=False),
    pa.field("n_files", pa.int64(), nullable=False),
    pa.field("n_children", pa.int64(), nullable=False),  # -1: the source had none (a v1 index)
    pa.field("n_desc", pa.int64(), nullable=False),
    pa.field("mtime", pa.int64(), nullable=False),
    pa.field("wts", pa.float64(), nullable=False),  # Σ mtime_mean·mtime_w over the path's rows
    pa.field("wb", pa.int64(), nullable=False),  # Σ mtime_w
    pa.field("c2", pa.int64(), nullable=False),
    pa.field("c3", pa.int64(), nullable=False),
    pa.field("c4", pa.int64(), nullable=False),
    pa.field("us", pa.string(), nullable=False),  # owner slices (`us_sql_from_raw`)
])
RD_SCHEMA = pa.schema([
    pa.field("depth", pa.uint8(), nullable=False),
    pa.field("path", pa.string(), nullable=False),
    pa.field("vf", pa.int64(), nullable=False),
    pa.field("vt", pa.int64(), nullable=False),
    pa.field("last_read", pa.int32(), nullable=False),
])
PV_COLS = PV_SCHEMA.names

# ── Per-scan rows → one row per path ───────────────────────────────────────

#: A scan's raw rows in the reader's terms (`index.ts` `toRowV1` / `toRowV2`): `mw` is the bytes the
#: mean stamp weighs and `wt` = `mtime_mean · mw` (`view.ts` `merge` adds both only where `mw > 0`).
V2_ROW = """depth::UTINYINT AS depth, path, coalesce(usr, '') AS usr, kind = 'file' AS is_file, size::BIGINT AS size,
    n_files::BIGINT AS n_files, coalesce({n_children}, -1)::BIGINT AS n_children, coalesce({n_desc}, -1)::BIGINT AS n_desc,
    coalesce({mtime}, -1)::BIGINT AS mtime, CASE WHEN {mtime_mean} IS NULL THEN 0 ELSE size END::BIGINT AS mw,
    CASE WHEN {mtime_mean} IS NULL THEN 0 ELSE {mtime_mean}::DOUBLE * size::DOUBLE END AS wt,
    coalesce({last_read}, -1)::INTEGER AS last_read, coalesce({c2}, 0)::BIGINT AS c2, coalesce({c3}, 0)::BIGINT AS c3, coalesce({c4}, 0)::BIGINT AS c4"""
V1_ROW = """depth::UTINYINT AS depth, path, coalesce(usr, '') AS usr, false AS is_file, b::BIGINT AS size, o::BIGINT AS n_files,
    -1::BIGINT AS n_children, -1::BIGINT AS n_desc, -1::BIGINT AS mtime, coalesce(wb, 0)::BIGINT AS mw,
    CASE WHEN coalesce(wb, 0) > 0 THEN (coalesce(wts, 0)::DOUBLE / wb::DOUBLE) * wb::DOUBLE ELSE 0 END AS wt,
    coalesce(a, -1)::INTEGER AS last_read, coalesce(c2, 0)::BIGINT AS c2, coalesce(c3, 0)::BIGINT AS c3, coalesce(c4, 0)::BIGINT AS c4"""


def path_rows_sql(con, src: str, preds: list[str], version: int | None = None) -> str:
    """One scan's rows in the range, one per `(depth, path)`: the sums and maxima the reader's `merge`
    takes over a path's rows (owner slices, a v1 index's duplicate rows), `kind` = `dir` if any row is,
    `us` = the owner slices, `dr` = the mean stamp to the second."""
    version = version or sn.source_version(con, src)
    if version == 2:
        cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({q(src)})").fetchall()}
        sel = V2_ROW.format(**{k: (c if c in cols else "NULL") for k, c in sn.V2_OPTIONAL.items()})
    else:
        sel = V1_ROW
    raw = " UNION ALL ".join(f"SELECT {sel} FROM read_parquet({q(src)}) WHERE {p}" for p in preds)
    one = """SELECT depth, path, CASE WHEN bool_and(is_file) THEN 'file' ELSE 'dir' END AS kind, sum(size)::BIGINT AS size,
        sum(n_files)::BIGINT AS n_files, max(n_children) AS n_children, max(n_desc) AS n_desc, max(mtime) AS mtime,
        sum(wt)::DOUBLE AS wts, sum(mw)::BIGINT AS wb, max(last_read) AS last_read,
        sum(c2)::BIGINT AS c2, sum(c3)::BIGINT AS c3, sum(c4)::BIGINT AS c4
        FROM raw GROUP BY depth, path"""
    return f"""WITH raw AS ({raw}), one AS ({one}), sl AS ({us_sql_from_raw()})
        SELECT one.depth, one.path, kind, size, n_files, n_children, n_desc, mtime,
            CASE WHEN wb > 0 THEN round(wts / wb, 0) ELSE 0 END::DOUBLE AS dr, wb, c2, c3, c4, sl.us, wts, last_read
        FROM one JOIN sl USING (depth, path)"""


def us_sql_from_raw() -> str:
    """Per `(depth, path)` of the CTE `raw` (raw rows with `usr`, `size`): the owner slices as one
    string — `''` with no named owner; the owner's name when the path is exactly one row and it is
    named (its bytes are the path's); else JSON `[["usr", bytes], …]` by `usr`, every named owner
    (zero bytes included: the reader's per-user map holds them). Unnamed bytes are the path's minus
    the named ones."""
    return """SELECT depth, path,
        CASE WHEN sum(nr) FILTER (WHERE usr <> '') IS NULL THEN ''
             WHEN sum(nr) = 1 THEN any_value(usr) FILTER (WHERE usr <> '')
             ELSE '[' || string_agg(CASE WHEN usr <> '' THEN '[' || to_json(usr)::VARCHAR || ',' || b::VARCHAR || ']' END, ',' ORDER BY usr) || ']' END AS us
        FROM (SELECT depth, path, usr, sum(size)::BIGINT AS b, count(*) AS nr FROM raw GROUP BY depth, path, usr)
        GROUP BY depth, path"""


#: Per-version hash of the change columns: what a version and a scan's row are compared by.
ROW_HASH = "hash(depth, path, kind, size, n_files, n_children, n_desc, mtime, dr, wb, c2, c3, c4, us)"
RD_HASH = "hash(depth, path, last_read)"


def _prefix_digests(con, table: str, h: str, stamps: list[int]) -> list[list[int]]:
    """Per scan j: `[count, Σ h mod 2⁶⁴]` of the versions live at scan j (`vf ≤ t_j < vt`), from a
    difference array over the versions' `vf` and `vt` — O(versions), never one row per scan."""
    idx = {t: j for j, t in enumerate(stamps)}
    d_n = [0] * (len(stamps) + 1)
    d_h = [0] * (len(stamps) + 1)
    for vf, n, s in con.execute(f"SELECT vf, count(*), sum({h}::HUGEINT) FROM {table} GROUP BY vf").fetchall():
        d_n[idx[vf]] += n
        d_h[idx[vf]] += int(s)
    for vt, n, s in con.execute(f"SELECT vt, count(*), sum({h}::HUGEINT) FROM {table} WHERE vt <> {OPEN} GROUP BY vt").fetchall():
        d_n[idx[vt]] -= n
        d_h[idx[vt]] -= int(s)
    out, n, s = [], 0, 0
    for j in range(len(stamps)):
        n += d_n[j]
        s += d_h[j]
        out.append([n, s % U64])
    return out


def build_range(scans: dict, ranges: dict, i: int, out: Path, con, *, mount: str | None) -> dict:
    """One key range over every scan: `pv/r####.parquet` (path versions, `PV_SCHEMA`, sorted
    `(depth, path, vf)`), `rd/r####.parquet` (`last_read` versions, `RD_SCHEMA`) and
    `digest/r####.json`: counts, and per scan the rows and Σ hash of the scan's per-path rows next to
    those of the versions live at it — equal iff the versions reconstruct every scan exactly."""
    from pyrmts.intervals import islands_sql, long_sql, stamped_sql

    t0 = monotonic()
    r = ranges["ranges"][i]
    preds = range_preds(r)
    srcs = [(sn._src(scans["bucket"], s["src"], mount), s["ts"], s.get("version")) for s in scans["scans"]]
    stamps = [ts for _, ts, _ in srcs]
    for t in ("lng", "pv", "rd"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    con.execute(f"CREATE TABLE lng AS {long_sql([path_rows_sql(con, p, preds, v) for p, _, v in srcs])}")
    t_long = monotonic() - t0
    src_dig = {j: [n, int(s) % U64] for j, n, s in con.execute(f"SELECT __scan, count(*), sum({ROW_HASH}::HUGEINT) FROM lng GROUP BY __scan").fetchall()}
    rd_src = {j: [n, int(s) % U64] for j, n, s in con.execute(f"SELECT __scan, count(*), sum({RD_HASH}::HUGEINT) FROM lng WHERE last_read >= 0 GROUP BY __scan").fetchall()}
    runs = islands_sql("SELECT * FROM lng", KEY_COLS, STATE_COLS, carried=CARRIED)
    con.execute(f"CREATE TABLE pv AS {stamped_sql(runs, KEY_COLS, STATE_COLS, stamps, OPEN)}")
    rd_runs = islands_sql("SELECT __scan, depth, path, last_read FROM lng WHERE last_read >= 0", KEY_COLS, ["last_read"])
    con.execute(f"CREATE TABLE rd AS {stamped_sql(rd_runs, KEY_COLS, ['last_read'], stamps, OPEN)}")
    t_kernel = monotonic() - t0 - t_long
    con.execute("DROP TABLE lng")
    pv_dig = _prefix_digests(con, "pv", ROW_HASH, stamps)
    rd_dig = _prefix_digests(con, "rd", RD_HASH, stamps)
    per_scan = []
    for j, s in enumerate(scans["scans"]):
        a, b = src_dig.get(j, [0, 0]), pv_dig[j]
        ra, rb = rd_src.get(j, [0, 0]), rd_dig[j]
        per_scan.append({"id": s["id"], "rows": a[0], "live": b[0], "eq": a == b, "reads": ra[0], "reads_live": rb[0], "reads_eq": ra == rb})
    name = f"r{i:04d}"
    cols = ", ".join(PV_COLS)
    n_pv = write_sorted(sn._batches(con, f"SELECT {cols} FROM pv ORDER BY depth, path, vf"), out / "pv" / f"{name}.parquet",
                        PV_SCHEMA, RANGE_RG, dictionary=["kind", "us"])
    n_rd = write_sorted(sn._batches(con, "SELECT depth, path, vf, vt, last_read FROM rd ORDER BY depth, path, vf"), out / "rd" / f"{name}.parquet",
                        RD_SCHEMA, RANGE_RG)
    kinds = {k: {"versions": n, "open": o, "paths": p} for k, n, o, p in con.execute(
        f"SELECT kind, count(*), count(*) FILTER (WHERE vt = {OPEN}), count(DISTINCT path) FROM pv GROUP BY kind ORDER BY kind").fetchall()}
    doc = {
        "range": r, "i": i, "versions": n_pv, "reads": n_rd,
        "open": con.execute(f"SELECT count(*) FROM pv WHERE vt = {OPEN}").fetchone()[0],
        "reads_open": con.execute(f"SELECT count(*) FROM rd WHERE vt = {OPEN}").fetchone()[0],
        "kinds": kinds, "scans": per_scan,
        "eq": all(s["eq"] and s["reads_eq"] for s in per_scan),
        "long_s": round(t_long, 1), "kernel_s": round(t_kernel, 1), "s": round(monotonic() - t0, 1),
    }
    con.execute("DROP TABLE pv")
    con.execute("DROP TABLE rd")
    (out / "digest").mkdir(parents=True, exist_ok=True)
    (out / "digest" / f"{name}.json").write_text(json.dumps(doc, sort_keys=True) + "\n")
    err(f"range {i}: {n_pv:,} versions ({doc['open']:,} open), {n_rd:,} read versions, eq={doc['eq']} in {doc['s']}s "
        f"(long {doc['long_s']}s, kernel {doc['kernel_s']}s)")
    return doc


# ── CLI ────────────────────────────────────────────────────────────────────


@group("interval-store")
def cli() -> None:
    """The change-interval path store (specs/interval-store.md)."""


@cli.command("build")
@option("-b", "--bucket", default=DATA_BUCKET, help="Data bucket")
@option("-f", "--force", is_flag=True, help="Rebuild ranges whose digest is already uploaded")
@option("-g", "--gen", required=True, help="Generation: gs://BUCKET/interval-store/GEN/ (its scans.json, ranges.json)")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", help="Local mount of the data bucket")
@option("-M", "--mem", default="80GB", help="DuckDB memory limit")
@option("-n", "--per-task", default=1, type=IntRange(min=1), help="Ranges per task: task t builds [t·n, (t+1)·n)")
@option("-o", "--out", default="/stage/out", help="Local output dir (uploaded, then removed)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-r", "--range", "only", help="Comma-separated range indices (overrides -i/-n)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB database + spill dir")
@option("-U", "--no-upload", is_flag=True, help="Keep the outputs local")
def build_cmd(bucket, force, gen, index, mount, mem, per_task, out, threads, only, tmp, no_upload) -> None:
    """Build key ranges' path and read versions over every scan of GEN's `scans.json`, verifying each
    scan's reconstruction (digest written last: it marks the range done)."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    scans = read_json(f"gs://{bucket}/{prefix}/scans.json")
    ranges = read_json(f"gs://{bucket}/{prefix}/ranges.json")
    if only:
        todo = [int(x) for x in only.split(",")]
    else:
        t = sn._task(index)
        todo = list(range(t * per_task, min((t + 1) * per_task, ranges["k"])))
    b = storage.Client().bucket(bucket)
    import duckdb

    Path(tmp).mkdir(parents=True, exist_ok=True)
    db = Path(tmp) / "build.duckdb"
    if db.exists():
        db.unlink()
    con = duckdb.connect(str(db))
    con.execute(f"SET threads={threads}; SET memory_limit='{mem}'; SET preserve_insertion_order=false; SET parquet_metadata_cache=true")
    con.execute(f"SET temp_directory={q(str(Path(tmp) / 'spill'))}")
    err(f"interval-store build: duckdb {duckdb.__version__}, pyarrow {pa.__version__}, {len(scans['scans'])} scans, ranges {todo}")
    for i in todo:
        if not force and b.blob(f"{prefix}/digest/r{i:04d}.json").exists():
            err(f"range {i}: already built")
            continue
        outp = Path(out) / f"r{i}"
        doc = build_range(scans, ranges, i, outp, con, mount=mount)
        if not no_upload:
            digest = outp / "digest"
            moved = Path(out) / f"r{i}-digest"
            shutil.move(str(digest), moved)
            upload_tree(outp, bucket, prefix)
            upload_tree(moved, bucket, f"{prefix}/digest")
            shutil.rmtree(outp)
            shutil.rmtree(moved)
        print(json.dumps({k: v for k, v in doc.items() if k != "scans"}), flush=True)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    cli()
