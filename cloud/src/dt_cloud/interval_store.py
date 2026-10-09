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
from click import Choice, IntRange, group, option

from . import static_names as sn
from .static_names import OPEN, U64, connect, q, range_preds, read_json, upload_tree, write_sorted

err = partial(print, file=sys.stderr, flush=True)

PREFIX = "interval-store"
PROFILES = Path(__file__).parent / "interval_profiles"


def load_profile(name: str | None) -> dict:
    """A deployment profile (`interval_profiles/<name>.json`, or a path): its bucket and the scans the
    parity check samples. `$INTERVAL_STORE_PROFILE` names one when `-P` doesn't."""
    name = name or os.environ.get("INTERVAL_STORE_PROFILE")
    if not name:
        raise SystemExit("no profile: pass -P NAME|PATH or set $INTERVAL_STORE_PROFILE (e.g. `gcs`)")
    p = Path(name) if name.endswith(".json") else PROFILES / f"{name}.json"
    return json.loads(p.read_text())


def _bucket(bucket: str | None, profile: str | None) -> str:
    return bucket or load_profile(profile)["bucket"]
#: A served sort's row-group size: the per-scan path store's (decode cost per group is the same).
SERVED_RG = 8192
#: The range files' row-group size (an intermediate: only `cut` and the verifier read them).
RANGE_RG = 65536

KEY_COLS = ["depth", "path"]
#: The values whose change opens a version. `dr` is the size-weighted mean stamp rounded to the second
#: (the change test; the exact `wts` is carried from the version's first scan), `us` the owner slices.
#: `last_read` is not here: it moves from scan to scan on read paths and lives in its own intervals (`rd/`).
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


# ── Served sorts ───────────────────────────────────────────────────────────

#: A version's size bucket, `⌊log2 size⌋` as a bit length (exact for any int64; the path store's
#: `bysize` key, `find/tiers.py`); NULL for size 0, which sorts last.
BUCKET = "CASE WHEN size > 0 THEN length(bin(size)) - 1 END"
#: Each served sort: its source rows, its order, and which column bounds a group's sizes. Every sort is
#: two segments in one file — the versions open at the generation's last scan, then the closed ones —
#: so a read at that scan prunes the closed segment by `vt_max` alone.
SORTS = {
    "path": ("pv", "depth, path, vf", "size"),
    "bysize": ("pv", f"({BUCKET}) DESC NULLS LAST, path, vf", "size"),
    "reads": ("rd", "depth, path, vf", None),
}
SEGMENTS = (("open", f"vt = {OPEN}"), ("hist", f"vt <> {OPEN}"))
GROUPS_SCHEMA = pa.schema([
    pa.field("rg", pa.int32(), nullable=False),
    pa.field("seg", pa.int8(), nullable=False),
    pa.field("d_min", pa.int32(), nullable=False),
    pa.field("d_max", pa.int32(), nullable=False),
    pa.field("p_min", pa.string(), nullable=False),
    pa.field("p_max", pa.string(), nullable=False),
    pa.field("b_min", pa.int64(), nullable=False),
    pa.field("b_max", pa.int64(), nullable=False),
    pa.field("u_min", pa.string()),
    pa.field("u_max", pa.string()),
    pa.field("vf_min", pa.int64(), nullable=False),
    pa.field("vf_max", pa.int64(), nullable=False),
    pa.field("vt_min", pa.int64(), nullable=False),
    pa.field("vt_max", pa.int64(), nullable=False),
    pa.field("row_start", pa.int64(), nullable=False),
    pa.field("row_end", pa.int64(), nullable=False),
    pa.field("rg_json", pa.string(), nullable=False),
])
GROUPS_STAT_COLS = ["d_min", "d_max", "p_min", "p_max", "b_min", "b_max", "u_min", "u_max", "vf_min", "vf_max", "vt_min", "vt_max"]
#: The served store's `index_schema.version` analogue: 3 = interval rows (`vf`/`vt`, one row per path).
STORE_VERSION = 3


def _bounds(t: pa.Table, seg: int, size_col: str | None) -> dict:
    import pyarrow.compute as pc

    def mm(c):
        r = pc.min_max(t.column(c))
        return r["min"].as_py(), r["max"].as_py()

    d, p, vf, vt = mm("depth"), mm("path"), mm("vf"), mm("vt")
    b = mm(size_col) if size_col else (0, 0)
    return {"seg": seg, "d_min": d[0], "d_max": d[1], "p_min": p[0], "p_max": p[1], "b_min": b[0], "b_max": b[1],
            "u_min": None, "u_max": None, "vf_min": vf[0], "vf_max": vf[1], "vt_min": vt[0], "vt_max": vt[1], "rows": t.num_rows}


def write_served(con, src: str, sort: str, out: Path, schema: pa.Schema, *, rg_rows: int = SERVED_RG) -> dict:
    """One served sort of `src` (a relation of `schema` rows) to `out`: its open segment then its closed
    one, each in `rg_rows`-row groups (a segment's last may be short, so no group mixes them), zstd, in
    the sort's order. Writes `<out stem>.groups.parquet` beside it (`GROUPS_SCHEMA`: per group the exact
    bounds of its rows — never truncated statistics — and the compact metadata the Worker revives)."""
    _, order, size_col = SORTS[sort]
    cols = ", ".join(schema.names)
    bounds: list[dict] = []
    out.parent.mkdir(parents=True, exist_ok=True)
    dictionary = [c for c in ("kind", "us") if c in schema.names]
    with pq.ParquetWriter(out, schema, compression=sn.CODEC, use_dictionary=dictionary, write_statistics=["depth", "vf", "vt"]) as w:
        for seg, (name, where) in enumerate(SEGMENTS):
            pending: list[pa.RecordBatch] = []
            n = 0

            def flush(final: bool) -> None:
                nonlocal pending, n
                if not pending:
                    return
                t = pa.Table.from_batches(pending, schema=schema).combine_chunks()
                off = 0
                while t.num_rows - off >= rg_rows or (final and off < t.num_rows):
                    g = t.slice(off, min(rg_rows, t.num_rows - off))
                    w.write_table(g, row_group_size=rg_rows)
                    bounds.append(_bounds(g, seg, size_col))
                    off += g.num_rows
                rest = t.slice(off)
                pending, n = ([rest.combine_chunks().to_batches()[0]] if rest.num_rows else []), rest.num_rows

            for b in sn._batches(con, f"SELECT {cols} FROM {src} WHERE {where} ORDER BY {order}"):
                if b.num_rows:
                    pending.append(b.cast(schema) if b.schema != schema else b)
                    n += b.num_rows
                    if n >= rg_rows:
                        flush(False)
            flush(True)
            err(f"  {sort}/{name}: {sum(x['rows'] for x in bounds if x['seg'] == seg):,} rows")
        w.add_key_value_metadata({"store": "interval", "version": str(STORE_VERSION), "sort": sort, "order": order,
                                  "segments": ",".join(n for n, _ in SEGMENTS), "open": str(OPEN)})
    md = pq.read_metadata(out)
    rows, start = [], 0
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        chunks = [rg.column(c) for c in range(rg.num_columns)]
        codecs = {cc.compression for cc in chunks}
        cmeta = [[cc.data_page_offset, cc.total_compressed_size, cc.dictionary_page_offset or 0] for cc in chunks]
        bd = bounds[g]
        if bd["rows"] != rg.num_rows:
            raise RuntimeError(f"{out}: group {g} holds {rg.num_rows} rows, the writer saw {bd['rows']}")
        rows.append({"rg": g, **{k: v for k, v in bd.items() if k != "rows"}, "row_start": start, "row_end": start + rg.num_rows,
                     "rg_json": json.dumps([rg.num_rows, codecs.pop(), cmeta], separators=(",", ":"))})
        start += rg.num_rows
    from disk_tree.find.groups import schema_json

    sj = schema_json(md)
    t = pa.table({c: [r[c] for r in rows] for c in GROUPS_SCHEMA.names}, schema=GROUPS_SCHEMA)
    kv = {"groups_v": "1", "version": str(STORE_VERSION), "schema": json.dumps(sj["schema"], separators=(",", ":")), "sort": sort,
          "rows": str(start), "segments": json.dumps({n: sum(1 for r in rows if r["seg"] == i) for i, (n, _) in enumerate(SEGMENTS)})}
    gp = out.with_name(out.name.removesuffix(".parquet") + ".groups.parquet")
    with pq.ParquetWriter(gp, GROUPS_SCHEMA, compression="zstd", write_statistics=GROUPS_STAT_COLS, store_schema=False) as w:
        w.write_table(t, row_group_size=512)
        w.add_key_value_metadata(kv)
    return {"sort": sort, "rows": start, "groups": len(rows), "bytes": out.stat().st_size, "groups_bytes": gp.stat().st_size,
            "segments": {n: {"groups": sum(1 for r in rows if r["seg"] == i), "rows": sum(r["row_end"] - r["row_start"] for r in rows if r["seg"] == i)}
                         for i, (n, _) in enumerate(SEGMENTS)}}


# ── CLI ────────────────────────────────────────────────────────────────────


@group("interval-store")
def cli() -> None:
    """The change-interval path store (specs/interval-store.md)."""


@cli.command("build")
@option("-b", "--bucket", help="Data bucket (default: the profile's)")
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
@option("-P", "--profile", help="Deployment profile (`interval_profiles/<name>.json` or a path; default $INTERVAL_STORE_PROFILE)")
def build_cmd(bucket, profile, force, gen, index, mount, mem, per_task, out, threads, only, tmp, no_upload) -> None:
    """Build key ranges' path and read versions over every scan of GEN's `scans.json`, verifying each
    scan's reconstruction (digest written last: it marks the range done)."""
    bucket = _bucket(bucket, profile)
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


def churn_stats(con, root: str, scans: dict) -> dict:
    """Per scan: path versions opened and closed there, read versions likewise, and live rows — the
    delta a per-scan append writes — plus totals by kind."""
    ids = {s["ts"]: s["id"] for s in scans["scans"]}
    pv, rd = f"read_parquet({q(root + '/pv/r*.parquet')})", f"read_parquet({q(root + '/rd/r*.parquet')})"
    per = {i: {"id": i, "opened": 0, "closed": 0, "reads_opened": 0, "reads_closed": 0} for i in ids.values()}
    for col, key, src in (("vf", "opened", pv), ("vt", "closed", pv), ("vf", "reads_opened", rd), ("vt", "reads_closed", rd)):
        for t, n in con.execute(f"SELECT {col}, count(*) FROM {src} WHERE {col} <> {OPEN} GROUP BY {col}").fetchall():
            per[ids[t]][key] = n
    kinds = {k: {"versions": n, "open": o, "paths": p} for k, n, o, p in con.execute(
        f"SELECT kind, count(*), count(*) FILTER (WHERE vt = {OPEN}), count(DISTINCT path) FROM {pv} GROUP BY kind ORDER BY kind").fetchall()}
    tot = con.execute(f"SELECT count(*), count(*) FILTER (WHERE vt = {OPEN}), count(DISTINCT (depth, path)) FROM {pv}").fetchone()
    rtot = con.execute(f"SELECT count(*), count(*) FILTER (WHERE vt = {OPEN}) FROM {rd}").fetchone()
    return {"versions": tot[0], "open": tot[1], "paths": tot[2], "reads": rtot[0], "reads_open": rtot[1], "kinds": kinds,
            "scans": [per[s["id"]] for s in scans["scans"]]}


@cli.command("cut")
@option("-b", "--bucket", help="Data bucket (default: the profile's)")
@option("-g", "--gen", required=True, help="Generation (its pv/, rd/ range files)")
@option("-i", "--index", type=int, help="Task index → sort (path, bysize, reads, then `stats`: per-scan churn; default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", help="Local mount of the data bucket")
@option("-M", "--mem", default="80GB", help="DuckDB memory limit")
@option("-o", "--out", default="/stage/out", help="Local output dir (uploaded, then removed)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-r", "--rg-rows", default=SERVED_RG, type=int, help="Rows per served row group")
@option("-s", "--sort", "sorts", multiple=True, type=Choice(list(SORTS)), help="Sort(s) to cut (default: the task's)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
@option("-U", "--no-upload", is_flag=True, help="Keep the outputs local")
@option("-P", "--profile", help="Deployment profile (`interval_profiles/<name>.json` or a path; default $INTERVAL_STORE_PROFILE)")
def cut_cmd(bucket, profile, gen, index, mount, mem, out, threads, rg_rows, sorts, tmp, no_upload) -> None:
    """Cut the served sorts from the range files: `served/<sort>.parquet` + `.groups.parquet`."""
    bucket = _bucket(bucket, profile)
    prefix = f"{PREFIX}/{gen}"
    todo = list(sorts) or [[*SORTS, "stats"][sn._task(index)]]
    con = connect(threads, mem, tmp)
    root = f"{mount}/{prefix}" if mount else f"gs://{bucket}/{prefix}"
    if todo == ["stats"]:
        doc = churn_stats(con, root, read_json(f"gs://{bucket}/{prefix}/scans.json"))
        out_dir = Path(out) / "served"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "stats.json").write_text(json.dumps(doc, indent=1) + "\n")
        if not no_upload:
            upload_tree(out_dir, bucket, f"{prefix}/served")
        print(json.dumps({k: v for k, v in doc.items() if k != "scans"}), flush=True)
        return
    for sort in todo:
        sub, _, _ = SORTS[sort]
        schema = PV_SCHEMA if sub == "pv" else RD_SCHEMA
        t0 = monotonic()
        dst = Path(out) / "served" / f"{sort}.parquet"
        doc = write_served(con, f"read_parquet({q(root + '/' + sub + '/r*.parquet')})", sort, dst, schema, rg_rows=rg_rows)
        doc["s"] = round(monotonic() - t0, 1)
        err(f"cut {sort}: {doc['rows']:,} rows, {doc['groups']:,} groups, {doc['bytes']:,} B in {doc['s']}s")
        (Path(out) / "served" / f"{sort}.json").write_text(json.dumps(doc, sort_keys=True) + "\n")
        if not no_upload:
            upload_tree(Path(out) / "served", bucket, f"{prefix}/served")
            shutil.rmtree(Path(out) / "served")
        print(json.dumps(doc), flush=True)


def download_served(bucket: str, prefix: str, dst: Path, *, workers: int = 16) -> None:
    """The generation's served sorts (+ `.groups.parquet`) to `dst`, chunked in parallel."""
    from google.cloud import storage
    from google.cloud.storage import transfer_manager as tm

    b = storage.Client().bucket(bucket)
    dst.mkdir(parents=True, exist_ok=True)
    for blob in storage.Client().list_blobs(bucket, prefix=f"{prefix}/served/"):
        name = blob.name.rsplit("/", 1)[-1]
        if not name.endswith(".parquet"):
            continue
        out = dst / name
        if out.exists() and out.stat().st_size == blob.size:
            continue
        t0 = monotonic()
        tm.download_chunks_concurrently(b.blob(blob.name), str(out), chunk_size=64 << 20, max_workers=workers)
        err(f"served {name}: {blob.size:,} B in {monotonic() - t0:.0f}s")


@cli.command("verify")
@option("-b", "--bucket", help="Data bucket (default: the profile's)")
@option("-g", "--gen", required=True, help="Generation")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", help="Local mount of the data bucket (per-scan sorts are copied from it)")
@option("-n", "--tasks", default=1, type=IntRange(min=1), help="Tasks the sampled dates are split over")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-T", "--tmp", default="/stage/tmp", help="Local scratch (served store, per-scan copies, spill)")
@option("-P", "--profile", help="Deployment profile (`interval_profiles/<name>.json` or a path; default $INTERVAL_STORE_PROFILE)")
def verify_cmd(bucket, profile, gen, index, mount, tasks, out, tmp) -> None:
    """Parity against the per-scan path store over the profile's `verify_scans`: every
    view's tiles and every diff's rows, with both sides' read cost; `verify/t##.jsonl` + `.json`."""
    bucket = _bucket(bucket, profile)
    from . import interval_verify as iv

    prefix = f"{PREFIX}/{gen}"
    t = sn._task(index)
    tmp = Path(tmp)
    download_served(bucket, prefix, tmp / "served")
    scans = read_json(f"gs://{bucket}/{prefix}/scans.json")
    outp = Path(out) / "verify"
    outp.mkdir(parents=True, exist_ok=True)
    summaries = iv.verify_task(load_profile(profile)["verify_scans"], t, tasks, str(tmp / "served"), scans, outp / f"t{t:02d}.jsonl", tmp, mount)
    (outp / f"t{t:02d}.json").write_text(json.dumps(summaries, indent=1) + "\n")
    upload_tree(outp, bucket, f"{prefix}/verify")
    print(json.dumps(summaries), flush=True)


#: What the Worker reads: the scans and the served sorts with their group indexes.
R2_SERVED = ("scans.json", "served/path.", "served/bysize.", "served/reads.")


@cli.command("r2-copy")
@option("-b", "--bucket", help="Data bucket (default: the profile's)")
@option("-g", "--gen", required=True, help="Generation")
@option("-m", "--mount", help="Ignored (the Batch driver passes it)")
@option("-n", "--dry-run", is_flag=True, help="List what would be copied")
@option("-w", "--workers", default=8, type=int, help="Parallel copies")
@option("-P", "--profile", help="Deployment profile (`interval_profiles/<name>.json` or a path; default $INTERVAL_STORE_PROFILE)")
def r2_copy_cmd(bucket, profile, gen, mount, dry_run, workers) -> None:
    """Copy the generation's served files GCS → R2 under the same keys (`static-names r2-copy`'s streaming
    copy: objects already there with the same size and md5 are skipped). R2 via `R2_ENDPOINT`,
    `R2_BUCKET` and AWS_* keys."""
    bucket = _bucket(bucket, profile)
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
    with ThreadPoolExecutor(workers) as ex:
        for o in ex.map(lambda o: (pub.copy_one(bucket, s3, r2, o), o)[1], todo):
            err(f"  → {o.key} ({o.size:,} B)")
    print(json.dumps({"gen": gen, "objects": len(objs), "copied": len(todo), "bytes": total, "s": round(monotonic() - t0, 1)}))


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    cli()
