"""The static name index's daily append (specs/static-daily-append.md): each new scan becomes a small
**run** beside an immutable base generation, published by an immutable per-day manifest, merged into bigger
runs on a binary counter, read by merging base and runs (the smallest `vt` of a version's rows wins).

Per scan `D`, under `static-names/<gen>/deltas/<D>/`:

1. `append` (per key range, Batch): the range's open coalesced versions (`copen`; the base's `cintervals`
   with `vt` = OPEN on the first day) and `D`'s rows → `pyrmts.intervals.append_intervals` on the answer
   columns alone (`size`, `n_files`) → `cdelta/r####.parquet` (opened `op` 1, closed `op` −1), `dhist/`
   (its suffix rows per three-character prefix) and the next `copen` (scratch bucket).
2. `shards` (one task): the delta's suffix rows, opens and closes alike (a close record is the version's
   rows with their final `vt`), planned and sorted into the run's own `shards.json`, `sx/`, `sidecar/`.
3. `catalog` (one task): `static_catalog.append` from the base + live runs' merged catalog; the run's
   catalog is what that adds (new cells, changed or new headers).
4. `publish`: the binary counter's merges, then `manifests/<D>.json` (written last).
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

import pyarrow as pa
import pyarrow.parquet as pq
from click import IntRange, group, option

from . import static_catalog as sc
from .static_names import (
    ANSWER_COLS, CINTERVAL_SCHEMA, CODEC, DATA_BUCKET, INTERVAL_RG, KEY_COLS, OPEN, PREFIX, SCRATCH_BUCKET, SX_RG,
    SX_SCHEMA, Reader, _batches, _src, _sx_cast, _task, answer_rows, connect, err, hist_sql, plan_shards, q, range_preds,
    read_json, scan_epoch, scan_sql, sidecar_rows, suffix_sql, upload_tree, write_sorted,
)

CDELTA_SCHEMA = CINTERVAL_SCHEMA.append(pa.field("op", pa.int8(), nullable=False))
#: A run's shards: about this many suffix rows each (the base's target).
RUN_SHARD_ROWS = 50_000_000
#: The binary counter folds runs into a new base generation (a compaction) at this level (2^5 = 32 days).
COMPACT_LEVEL = 5


def scan_id(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def run_key(first: str, last: str) -> str:
    """A run's directory under the generation: `deltas/<first>` (one scan) or `deltas/<first>_<last>`."""
    return f"deltas/{first}" if first == last else f"deltas/{first}_{last}"


# ── 1. Coalesced append on the open versions ───────────────────────────────


def append_open(con, prev_sql: str, scan: dict, r: dict, name: str, out: Path, *, bucket: str, mount: str | None) -> dict:
    """Append `scan` to one key range's open coalesced versions (`prev_sql`: `CINTERVAL_SCHEMA` rows; closed ones
    may ride along and are ignored), keyed on `ANSWER_COLS` alone, so the runs are the coalesced versions
    directly. Writes `cdelta/<name>.parquet` (`CDELTA_SCHEMA`, sorted `(depth, path, usr, vf, op)`),
    `copen/<name>.parquet` (the open versions after `scan`, sorted) and `dhist/<name>.parquet` (the delta's
    suffix rows per three-character prefix) under `out`."""
    from pyrmts.intervals import append_intervals, delta_sql

    t0 = monotonic()
    D = scan["ts"]
    cols = ", ".join([*KEY_COLS, *ANSWER_COLS])
    new = f"SELECT {cols} FROM ({scan_sql(con, _src(bucket, scan['src'], mount), range_preds(r), scan.get('version'))})"
    con.execute(f"CREATE OR REPLACE TABLE pv AS SELECT {', '.join(CINTERVAL_SCHEMA.names)} FROM ({prev_sql}) WHERE vt = {OPEN}")
    n_open, n_close = append_intervals(con, "SELECT * FROM pv", new, KEY_COLS, ANSWER_COLS, D, OPEN, out="civ")
    con.execute(f"CREATE OR REPLACE TABLE cdl AS SELECT * FROM ({delta_sql('civ', D)})")
    write_sorted(_batches(con, "SELECT * FROM cdl ORDER BY depth, path, usr, vf, op"), out / "cdelta" / f"{name}.parquet",
                 CDELTA_SCHEMA, INTERVAL_RG, dictionary=["usr"])
    rows = write_sorted(_batches(con, f"SELECT * FROM civ WHERE vt = {OPEN} ORDER BY depth, path, usr, vf"),
                        out / "copen" / f"{name}.parquet", CINTERVAL_SCHEMA, INTERVAL_RG, dictionary=["usr"])
    (out / "dhist").mkdir(parents=True, exist_ok=True)
    pq.write_table(con.execute(hist_sql("cdl")).to_arrow_table(), out / "dhist" / f"{name}.parquet", compression=CODEC)
    con.execute("DROP TABLE pv; DROP TABLE civ; DROP TABLE cdl")
    doc = {"range": name, "scan": scan["id"], "opened": n_open, "closed": n_close, "open": rows, "s": round(monotonic() - t0, 1)}
    err(f"append {name} {scan['id']}: {n_open:,} opened, {n_close:,} closed, {rows:,} open in {doc['s']}s")
    return doc


# ── 2. A run's shards ──────────────────────────────────────────────────────


def write_run_shards(con, table: str, out: Path, target_rows: int = RUN_SHARD_ROWS) -> dict:
    """`table`'s suffix rows (`s, depth, path, usr, vf, vt, size, n_files, p3`, epoch seconds) as a run's shards:
    `shards.json` (`plan_shards` over the rows' prefix counts, one task), `sx/s####.parquet` sorted `(s, path,
    usr, vf)` in `SX_RG`-row groups, `sidecar/s####.parquet` and `sidecar.parquet`."""
    out.mkdir(parents=True, exist_ok=True)
    hist = out / "hist.tmp.parquet"
    con.execute(f"COPY (SELECT p3, count(*)::BIGINT AS n FROM {table} GROUP BY p3 ORDER BY p3) TO {q(str(hist))} (FORMAT parquet)")
    plan = plan_shards([hist], target_rows, 1)
    hist.unlink()
    (out / "shards.json").write_text(json.dumps(plan, indent=1) + "\n")
    sides, total = [], 0
    for s in plan["shards"]:
        name = f"s{s['i']:04d}"
        cond = f"p3 >= {q(s['lo'])}" + (f" AND p3 < {q(s['hi'])}" if s["hi"] is not None else "")
        stats: list[tuple[str, str, int]] = []

        def on_group(g: pa.Table) -> None:
            col = g.column("s")
            stats.append((col[0].as_py(), col[g.num_rows - 1].as_py(), g.num_rows))

        dst = out / "sx" / f"{name}.parquet"
        sql = f"SELECT s, depth, path, usr, vf, vt, size, n_files FROM {table} WHERE {cond} ORDER BY s, path, usr, vf"
        rows = write_sorted((_sx_cast(b) for b in _batches(con, sql)), dst, SX_SCHEMA, SX_RG, on_group=on_group, dictionary=["usr"])
        if rows != s["rows"]:
            raise RuntimeError(f"run shard {s['i']}: {rows:,} rows written, {s['rows']:,} planned")
        side = sidecar_rows(dst, f"sx/{name}.parquet", stats)
        (out / "sidecar").mkdir(parents=True, exist_ok=True)
        pq.write_table(side, out / "sidecar" / f"{name}.parquet", compression=CODEC)
        sides.append(side)
        total += rows
    side = pa.concat_tables(sides) if sides else sidecar_rows_empty()
    pq.write_table(side, out / "sidecar.parquet", compression=CODEC, row_group_size=1 << 20)
    return {"rows": total, "shards": len(plan["shards"]), "row_groups": side.num_rows,
            "bytes": sum(f.stat().st_size for f in (out / "sx").glob("*.parquet")) if (out / "sx").exists() else 0}


def sidecar_rows_empty() -> pa.Table:
    from .static_names import SIDECAR_SCHEMA

    return pa.table({f.name: pa.array([], f.type) for f in SIDECAR_SCHEMA}, schema=SIDECAR_SCHEMA)


def delta_shards(con, cdelta_files: list[str], out: Path, target_rows: int = RUN_SHARD_ROWS) -> dict:
    """One scan's run shards from its `cdelta` files: every version's suffix rows (depth ≥ 1), the opened ones
    open and the closed ones with their final `vt` (close records)."""
    con.execute(f"CREATE OR REPLACE TABLE rx AS {suffix_sql(cdelta_files)}")
    doc = write_run_shards(con, "rx", out, target_rows)
    con.execute("DROP TABLE rx")
    return doc


def merge_shards(con, run_dirs: list[Path], out: Path, target_rows: int = RUN_SHARD_ROWS) -> dict:
    """Runs' suffix rows merged into one run: rows equal on `(s, path, usr, vf)` are one version's row, and the
    smallest `vt` wins (a close record over the open row it closes)."""
    files = [str(f) for d in run_dirs for f in sorted((d / "sx").glob("*.parquet"))]
    if not files:
        con.execute("CREATE OR REPLACE TABLE mx (s VARCHAR, depth UTINYINT, path VARCHAR, usr VARCHAR, vf BIGINT, vt BIGINT, size BIGINT, n_files BIGINT, p3 VARCHAR)")
    else:
        con.execute(f"""CREATE OR REPLACE TABLE mx AS SELECT s, any_value(depth) AS depth, path, usr, epoch(vf)::BIGINT AS vf,
                epoch(min(vt))::BIGINT AS vt, any_value(size) AS size, any_value(n_files) AS n_files, substring(s, 1, 3) AS p3
            FROM read_parquet([{', '.join(q(f) for f in files)}]) GROUP BY s, path, usr, vf""")
    doc = write_run_shards(con, "mx", out, target_rows)
    con.execute("DROP TABLE mx")
    return doc


# ── 3. Catalogs ────────────────────────────────────────────────────────────


def merged_cells_sql(tiers: list[Path]) -> str:
    """The cells of tiers (catalog dirs, oldest first) merged: every cell, and per literal the newest tier's
    header. Sorted `(q, bucket, vf)`."""
    parts = [f"SELECT *, {k} AS tier FROM read_parquet({q(str(t / 'cells.parquet'))})" for k, t in enumerate(tiers)]
    return f"""WITH t AS ({' UNION ALL '.join(parts)}),
        h AS (SELECT q, arg_max(b, tier) AS b, arg_max(o, tier) AS o FROM t WHERE bucket = '' GROUP BY q)
        SELECT q, '' AS bucket, 0::BIGINT AS vf, b, o FROM h
        UNION ALL SELECT q, bucket, vf, b, o FROM t WHERE bucket <> ''
        ORDER BY q, bucket, vf"""


def merge_catalogs(con, tiers: list[Path], out: Path) -> dict:
    """Tiers' catalogs merged into `out/{cells,index}.parquet` (the base plus every run: the whole catalog, equal
    to a rebuild; or runs alone: a merged run's catalog)."""
    out.mkdir(parents=True, exist_ok=True)
    rows, index = sc.write_cells(_batches(con, merged_cells_sql(tiers)), out / "cells.parquet")
    pq.write_table(index, out / "index.parquet", compression=CODEC)
    return {"cells_rows": rows, "row_groups": index.num_rows, "bytes": (out / "cells.parquet").stat().st_size}


def catalog_delta(con, tiers: list[Path], base: sc.BaseShards, deltas: list[list[str]], V: int, out: Path, tmp: Path) -> dict:
    """A run's catalog: `static_catalog.append` from the merged `tiers` (the base and every live run) and every
    scan's `cdelta` since the base (`deltas`, oldest first, the last = this run's scan), minus what the tiers
    hold: the new cells and the new or changed headers."""
    prev, full = tmp / "prev", tmp / "full"
    for d in (prev, full):
        if d.exists():
            shutil.rmtree(d)
    merge_catalogs(con, tiers, prev)
    doc = sc.append(con, prev=prev, base=base, deltas=deltas, V=V, out=full)
    pf, ff = q(str(prev / "cells.parquet")), q(str(full / "cells.parquet"))
    gone = con.execute(f"SELECT count(*) FILTER (WHERE bucket <> ''), count(*) FROM (SELECT * FROM read_parquet({pf}) EXCEPT SELECT * FROM read_parquet({ff}))").fetchone()
    if gone[0]:
        raise RuntimeError(f"catalog append dropped {gone[0]} cells: cells are only ever added")
    out.mkdir(parents=True, exist_ok=True)
    rows, index = sc.write_cells(_batches(con, f"SELECT * FROM (SELECT * FROM read_parquet({ff}) EXCEPT SELECT * FROM read_parquet({pf})) ORDER BY q, bucket, vf"),
                                 out / "cells.parquet")
    pq.write_table(index, out / "index.parquet", compression=CODEC)
    heads = con.execute(f"SELECT count(*) FROM read_parquet({q(str(out / 'cells.parquet'))}) WHERE bucket = ''").fetchone()[0]
    meta = {"cells_rows": rows, "row_groups": index.num_rows, "bytes": (out / "cells.parquet").stat().st_size,
            "index_bytes": (out / "index.parquet").stat().st_size, "cell_rg": sc.CELL_RG, "headers": heads,
            "headers_changed": gone[1], "membership": {"max_rows": V}, "append": doc}
    (out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    return meta


# ── Readers over base + runs (the Worker's merge) ──────────────────────────


def combine_rows(rows: list[dict]) -> list[dict]:
    """Suffix rows from several tiers: one per `(s, path, usr, vf)`, the smallest `vt` (a close record wins)."""
    best: dict[tuple, dict] = {}
    for r in rows:
        k = (r["s"], r["path"], r["usr"], r["vf"])
        cur = best.get(k)
        if cur is None or r["vt"] < cur["vt"]:
            best[k] = r
    return list(best.values())


class TieredReader:
    """The suffix reader over a base and its runs: every tier's range rows, combined, then `answer_rows`."""

    def __init__(self, readers: list[Reader]):
        self.readers = readers

    def rows(self, key: str) -> tuple[list[dict], dict]:
        rows, io = [], {"groups": 0, "bytes": 0, "rows_read": 0, "files": 0, "tiers": len(self.readers)}
        for r in self.readers:
            got, i = r.rows(key)
            rows += got
            for k in ("groups", "bytes", "rows_read", "files"):
                io[k] += i[k]
        return combine_rows(rows), io

    def answer(self, term: str, dates: list[str]) -> dict:
        key = term.lower()
        rows, io = self.rows(key)
        return answer_rows(key, rows, io, dates)

    def hits(self, term: str) -> list[tuple]:
        """Every first hit `(path, usr, vf, vt, size, n_files)` (ms stamps), sorted: what the map filter cuts by path."""
        from .static_names import _ms

        key = term.lower()
        rows, _ = self.rows(key)
        out = set()
        for r in rows:
            name = r["path"].rsplit("/", 1)[-1].lower()
            parent = r["path"].rsplit("/", 1)[0].lower() if "/" in r["path"] else ""
            if r["depth"] >= 1 and key in name and key not in parent:
                out.add((r["path"], r["usr"], _ms(r["vf"]), _ms(r["vt"]), r["size"], r["n_files"]))
        return sorted(out)


class TieredCatalog:
    """The catalog over a base and its runs: a literal is a member iff some tier holds its header; its header is
    the newest tier's, its cells the union (their count checked against the header)."""

    def __init__(self, catalogs: list[sc.Catalog]):
        self.catalogs = catalogs

    def rows(self, key: str) -> list[dict] | None:
        head, cells = None, []
        for c in self.catalogs:
            got = c.rows(key)
            if got is None:
                continue
            head = got[0]
            cells += got[1:]
        if head is None:
            return None
        cells.sort(key=lambda c: (c["bucket"], c["vf"]))
        if len(cells) != head["o"]:
            raise RuntimeError(f"catalog {key!r}: header counts {head['o']} cells, the tiers hold {len(cells)}")
        return [head, *cells]

    def answer(self, term: str, dates: list[str]) -> dict | None:
        key = term.lower()
        cells = self.rows(key)
        if cells is None:
            return None
        head, body = cells[0], cells[1:]
        out = {}
        for d in dates:
            D = scan_epoch(d)
            cur: dict[str, tuple[int, int]] = {}
            for c in body:
                if c["vf"] <= D:
                    cur[c["bucket"]] = (c["b"], c["o"])
            out[d] = {k: list(v) for k, v in sorted(cur.items()) if v != (0, 0)}
        return {"q": key, "rows": head["b"], "cells": head["o"], "answers": out}


# ── 4. The binary counter and manifests ────────────────────────────────────


def push_run(runs: list[dict], new: dict) -> tuple[list[dict], list[tuple[list[dict], dict]]]:
    """Add a level-0 run (oldest first) and carry: while the two newest runs share a level, they merge into one a
    level up. Returns the runs after, and the merges to perform in order (`(inputs, output)`)."""
    runs = [*runs, {**new, "level": 0}]
    merges = []
    while len(runs) >= 2 and runs[-1]["level"] == runs[-2]["level"]:
        a, b = runs[-2], runs[-1]
        m = {"key": run_key(a["first"], b["last"]), "first": a["first"], "last": b["last"], "level": a["level"] + 1,
             "scans": [*a["scans"], *b["scans"]]}
        merges.append(([a, b], m))
        runs = [*runs[:-2], m]
    return runs, merges


def manifest(gen: str, base_scans: list[str], runs: list[dict]) -> dict:
    scans = [*base_scans, *(s for r in runs for s in r["scans"])]
    return {"gen": gen, "date": scans[-1], "base_scans": len(base_scans), "scans": scans,
            "runs": [{k: r[k] for k in ("key", "first", "last", "level", "scans", "rows", "bytes") if k in r} for r in runs]}


# ── CLI ────────────────────────────────────────────────────────────────────


@group("daily")
def cli() -> None:
    """The static name index's daily append: per-scan runs beside a base generation (specs/static-daily-append.md)."""


def _gcs():
    from google.cloud import storage

    return storage.Client()


def _latest_manifest(bucket: str, gen: str, before: str | None = None) -> dict | None:
    """The newest `manifests/<D>.json` of the generation (strictly before `before`, when given), or None."""
    keys = sorted(b.name for b in _gcs().list_blobs(bucket, prefix=f"{PREFIX}/{gen}/manifests/"))
    if before:
        keys = [k for k in keys if Path(k).stem < before]
    return read_json(f"gs://{bucket}/{keys[-1]}") if keys else None


def _state(bucket: str, gen: str, date: str) -> tuple[dict, list[dict]]:
    """The generation's base scans and the runs live before `date` (from the newest earlier manifest)."""
    base = read_json(f"gs://{bucket}/{PREFIX}/{gen}/scans.json")
    m = _latest_manifest(bucket, gen, before=date)
    return base, (m["runs"] if m else [])


def _day_scans(base: dict, runs: list[dict]) -> list[str]:
    return [*(s["id"] for s in base["scans"]), *(s for r in runs for s in r["scans"])]


@cli.command("prepare")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-d", "--date", required=True, help="The scan to append")
@option("-g", "--gen", required=True, help="Base generation")
def prepare_cmd(bucket, date, gen) -> None:
    """Pin the scan to append (its newest `path` sort, by GCS generation and md5) as the run's `scans.json`, checking
    it is the next scan after the base and the live runs."""
    from .static_names import _version_from_footer, list_scans

    base, runs = _state(bucket, gen, date)
    have = _day_scans(base, runs)
    found = list_scans(bucket, start=have[-1])["scans"]
    nxt = [s for s in found if s["id"] > have[-1]]
    if not nxt or nxt[0]["id"] != date:
        raise SystemExit(f"the next scan after {have[-1]} is {nxt[0]['id'] if nxt else 'none'}, not {date}")
    s = nxt[0]
    s["version"] = _version_from_footer(_gcs().bucket(bucket), s["src"])
    doc = {"bucket": bucket, "scans": [s]}
    _gcs().bucket(bucket).blob(f"{PREFIX}/{gen}/{run_key(date, date)}/scans.json").upload_from_string(json.dumps(doc, indent=1) + "\n")
    print(json.dumps(doc, indent=1))


@cli.command("append")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-d", "--date", required=True, help="The scan to append (its run's `scans.json`, from `prepare`)")
@option("-f", "--force", is_flag=True, help="Redo ranges already done")
@option("-g", "--gen", required=True, help="Base generation")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", required=True, help="Local mount of the data bucket (the scratch bucket beside it)")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--per-task", default=1, type=IntRange(min=1), help="Ranges per task")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-S", "--scratch", default=SCRATCH_BUCKET, help="Bucket for the open versions (`state/<D>/copen/`)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def append_cmd(bucket, date, force, gen, index, mount, mem, per_task, out, threads, scratch, tmp) -> None:
    """Append the scan to key ranges' open coalesced versions: `deltas/<D>/{cdelta,dhist}/r####.parquet` (data
    bucket; `dhist` written last marks a range done) and `state/<D>/copen/r####.parquet` (scratch). The open
    versions come from the previous run's `copen`, else (the first run) the base's `cintervals`."""
    prefix = f"{PREFIX}/{gen}"
    run = f"{prefix}/{run_key(date, date)}"
    scan = read_json(f"gs://{bucket}/{run}/scans.json")["scans"][0]
    ranges = read_json(f"gs://{bucket}/{PREFIX}/{gen}/ranges.json")
    base, runs = _state(bucket, gen, date)
    prev_day = _day_scans(base, runs)[-1]
    smount = str(Path(mount).parent / scratch)
    t = _task(index)
    todo = list(range(t * per_task, min((t + 1) * per_task, ranges["k"])))
    b, sb = _gcs().bucket(bucket), _gcs().bucket(scratch)
    con = connect(threads, mem, tmp)
    for i in todo:
        name = f"r{i:04d}"
        if not force and b.blob(f"{run}/dhist/{name}.parquet").exists():
            err(f"append {name}: done")
            continue
        if runs:
            src = f"{smount}/{prefix}/state/{prev_day}/copen/{name}.parquet"
        else:
            src = f"{mount}/{prefix}/cintervals/{name}.parquet"
        outp = Path(out) / name
        doc = append_open(con, f"SELECT * FROM read_parquet({q(src)})", scan, ranges["ranges"][i], name, outp,
                          bucket=read_json(f"gs://{bucket}/{run}/scans.json")["bucket"], mount=mount)
        upload_tree(outp / "copen", scratch, f"{prefix}/state/{date}/copen")
        upload_tree(outp / "cdelta", bucket, f"{run}/cdelta")
        upload_tree(outp / "dhist", bucket, f"{run}/dhist")
        shutil.rmtree(outp)
        sb.blob(f"{prefix}/state/{date}/done/{name}.json").upload_from_string(json.dumps(doc) + "\n")
        print(json.dumps(doc), flush=True)


@cli.command("shards")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-d", "--date", required=True, help="The scan")
@option("-g", "--gen", required=True, help="Base generation")
@option("-m", "--mount", required=True, help="Local mount of the data bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--target-rows", default=RUN_SHARD_ROWS, type=int, help="Suffix rows per shard")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def shards_cmd(bucket, date, gen, mount, mem, target_rows, out, threads, tmp) -> None:
    """The run's suffix shards from its `cdelta/` (every range's): `shards.json`, `sx/`, `sidecar/`, `sidecar.parquet`."""
    run = f"{PREFIX}/{gen}/{run_key(date, date)}"
    ranges = read_json(f"gs://{bucket}/{PREFIX}/{gen}/ranges.json")
    files = sorted(str(f) for f in (Path(mount) / run / "cdelta").glob("*.parquet"))
    if len(files) != ranges["k"]:
        raise SystemExit(f"{len(files)} of {ranges['k']} ranges appended")
    con = connect(threads, mem, tmp)
    t0 = monotonic()
    outp = Path(out) / "run"
    doc = delta_shards(con, files, outp, target_rows)
    upload_tree(outp, bucket, run)
    shutil.rmtree(outp)
    doc["s"] = round(monotonic() - t0, 1)
    print(json.dumps(doc))


@cli.command("catalog")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-d", "--date", required=True, help="The scan")
@option("-g", "--gen", required=True, help="Base generation")
@option("-m", "--mount", required=True, help="Local mount of the data bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def catalog_cmd(bucket, date, gen, mount, mem, threads, tmp) -> None:
    """The run's catalog delta → `deltas/<D>/catalog/{cells,index}.parquet`, `meta.json`."""
    prefix = f"{PREFIX}/{gen}"
    run = f"{prefix}/{run_key(date, date)}"
    _, runs = _state(bucket, gen, date)
    days = [s for r in runs for s in r["scans"]] + [date]
    deltas = [sorted(str(f) for f in (Path(mount) / prefix / run_key(d, d) / "cdelta").glob("*.parquet")) for d in days]
    tiers = [Path(mount) / prefix / "catalog", *(Path(mount) / prefix / r["key"] / "catalog" for r in runs)]
    V = read_json(f"gs://{bucket}/{prefix}/catalog/meta.json")["membership"]["max_rows"]
    side = pq.read_table(f"{mount}/{prefix}/sidecar.parquet")
    con = connect(threads, mem, tmp)
    t0 = monotonic()
    out = Path(tmp) / "catalog-run"
    meta = catalog_delta(con, tiers, sc.BaseShards(f"{mount}/{prefix}", side), deltas, V, out, Path(tmp) / "catalog-work")
    upload_tree(out, bucket, f"{run}/catalog")
    shutil.rmtree(out)
    meta["s"] = round(monotonic() - t0, 1)
    print(json.dumps(meta, indent=1))


@cli.command("publish")
@option("-b", "--bucket", default=DATA_BUCKET, help="Bucket")
@option("-d", "--date", required=True, help="The scan")
@option("-g", "--gen", required=True, help="Base generation")
@option("-m", "--mount", help="Local mount of the data bucket (needed when the counter carries: the merges read the runs)")
@option("-n", "--dry-run", is_flag=True, help="Print the manifest and merges; write nothing")
@option("-T", "--tmp", default="/stage/tmp", help="Scratch dir for merges")
def publish_cmd(bucket, date, gen, mount, dry_run, tmp) -> None:
    """Carry the binary counter (merging runs as it says, each into a new run dir) and write `manifests/<D>.json`
    last. A level reaching `COMPACT_LEVEL` is reported: time for a new base generation."""
    prefix = f"{PREFIX}/{gen}"
    base, runs = _state(bucket, gen, date)
    run = f"{prefix}/{run_key(date, date)}"
    b = _gcs().bucket(bucket)
    meta = {"rows": 0, "bytes": 0}
    for blob in _gcs().list_blobs(bucket, prefix=f"{run}/sx/"):
        meta["bytes"] += int(blob.size)
    plan = read_json(f"gs://{bucket}/{run}/shards.json")
    meta["rows"] = plan["total_rows"]
    if not b.blob(f"{run}/catalog/meta.json").exists():
        raise SystemExit(f"{run}: no catalog yet")
    after, merges = push_run(runs, {"key": run_key(date, date), "first": date, "last": date, "scans": [date], **meta})
    if dry_run:
        print(json.dumps({"merges": [[[r["key"] for r in ins], m["key"]] for ins, m in merges],
                          "manifest": manifest(gen, [s["id"] for s in base["scans"]], after)}, indent=1))
        return
    if merges:
        if not mount:
            raise SystemExit("the counter carries: pass -m (the merges read the runs)")
        import duckdb

        con = duckdb.connect()
        for ins, m in merges:
            outp = Path(tmp) / "merge" / m["key"]
            dirs = [Path(mount) / prefix / r["key"] for r in ins]
            doc = merge_shards(con, dirs, outp)
            merge_catalogs(con, [d / "catalog" for d in dirs], outp / "catalog")
            (outp / "meta.json").write_text(json.dumps({**m, **doc}, indent=1) + "\n")
            upload_tree(outp, bucket, f"{prefix}/{m['key']}")
            shutil.rmtree(outp)
            m.update(rows=doc["rows"], bytes=doc["bytes"])
    doc = manifest(gen, [s["id"] for s in base["scans"]], after)
    if max(r["level"] for r in after) >= COMPACT_LEVEL:
        err(f"level {COMPACT_LEVEL} reached: compact into a new base generation")
    b.blob(f"{run}/meta.json").upload_from_string(json.dumps({"gen": gen, "first": date, "last": date, "level": 0, "scans": [date], **meta}, indent=1) + "\n")
    key = f"{prefix}/manifests/{date}.json"
    if b.blob(key).exists():
        raise SystemExit(f"{key} exists: manifests are never rewritten")
    b.blob(key).upload_from_string(json.dumps(doc, indent=1) + "\n", if_generation_match=0)
    print(json.dumps(doc, indent=1))


if __name__ == "__main__":
    cli()
