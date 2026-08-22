"""Incremental GCS usage-log ingest (specs/access-logs-and-cost.md § Productionize).

Google delivers hourly usage CSVs *flat* under ``usage/`` in the log bucket,
with the source bucket as a filename prefix (not a directory):

    gs://<log-bucket>/usage/<bucket>_usage_<YYYY_MM_DD_HH_MM_SS>_<id>_v0

Each run, per source bucket:

1. list names past the watermark (names embed the log hour and sort
   lexicographically per bucket, so a name watermark is a time watermark);
2. chunk new CSVs into ≤ ``max_chunk_gb`` groups;
3. per chunk: stage to local disk → parse (``disk_tree.access.parsers.gcs``)
   → lossless layer-1a parquet (zstd) → layer-2a agg shard
   (``disk_tree.access.aggregate``) → upload both → advance the watermark.

Chunks are the unit of atomicity: a crash reprocesses at most one chunk, and
re-uploads land on the same object names (idempotent). Google may deliver an
hour's file late; the state keeps an ``ingested_tail`` of recently-ingested
names and each run re-lists from ``LAG_HOURS`` before the watermark, so
stragglers inside the lag window are picked up instead of skipped.

Data-bucket layout::

    access/state/<bucket>.json          watermark + ingested tail
    access/raw/<bucket>/part-<first>--<last>.parquet    layer-1a (lossless)
    access/agg/<bucket>/part-<first>--<last>.parquet    layer-2a (tree shards)
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path

err = partial(print, file=sys.stderr)

USAGE_LOG_BUCKET = "marin-usage-logs"
FLEET = (
    "marin-us-central1",
    "marin-us-central2",
    "marin-us-east1",
    "marin-us-east5",
    "marin-eu-west4",
    "marin-us-west1",
    "marin-us-west4",
)
# Hours of name-time overlap re-listed each run to catch late-delivered files.
LAG_HOURS = 6

_TS_RE = re.compile(r"_usage_(\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2})_")


def name_ts(basename: str) -> str | None:
    """``…_usage_2026_08_13_19_00_00_<id>_v0`` → ``2026_08_13_19_00_00``."""
    m = _TS_RE.search(basename)
    return m.group(1) if m else None


def _ts_minus_hours(ts: str, hours: int) -> str:
    t = dt.datetime.strptime(ts, "%Y_%m_%d_%H_%M_%S")
    return (t - dt.timedelta(hours=hours)).strftime("%Y_%m_%d_%H_%M_%S")


@dataclass
class Chunk:
    names: list[str]  # blob names (full paths in the log bucket)
    bytes: int


def plan_chunks(names_sizes: list[tuple[str, int]], max_chunk_bytes: int) -> list[Chunk]:
    """Greedy fixed-order packing — order is the delivery (name) order, so a
    chunk is a contiguous time span and the watermark can advance per chunk."""
    chunks: list[Chunk] = []
    cur: list[str] = []
    cur_b = 0
    for name, size in names_sizes:
        if cur and cur_b + size > max_chunk_bytes:
            chunks.append(Chunk(cur, cur_b))
            cur, cur_b = [], 0
        cur.append(name)
        cur_b += size
    if cur:
        chunks.append(Chunk(cur, cur_b))
    return chunks


LEASE_TTL_HOURS = 8


def acquire_lease(client, data_bucket: str, owner: str) -> bool:
    """Single-writer lease over the whole ingest (atomic create-if-absent).

    Two concurrent ingests would race the per-bucket watermarks and cut
    different chunk spans over the same CSVs → double-counted rows under
    different part names. The scheduled /6h job and any manual/backfill run
    both take this lease; a crashed holder's lease is stolen after its TTL.
    """
    from google.api_core.exceptions import NotFound, PreconditionFailed

    blob = client.bucket(data_bucket).blob("access/state/.lease")
    body = json.dumps(
        {
            "owner": owner,
            "expires": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=LEASE_TTL_HOURS)).isoformat(
                timespec="seconds"
            ),
        }
    )
    for _ in range(2):
        try:
            blob.upload_from_string(body, content_type="application/json", if_generation_match=0)
            return True
        except PreconditionFailed:
            blob.reload()
            cur = json.loads(blob.download_as_bytes())
            if dt.datetime.fromisoformat(cur["expires"]) > dt.datetime.now(dt.timezone.utc):
                err(f"ingest lease held by {cur.get('owner')} until {cur['expires']} — skipping")
                return False
            try:  # expired: steal (generation-matched delete, then retry the create)
                blob.delete(if_generation_match=blob.generation)
            except (PreconditionFailed, NotFound):
                pass
    return False


def release_lease(client, data_bucket: str) -> None:
    from google.api_core.exceptions import NotFound

    try:
        client.bucket(data_bucket).blob("access/state/.lease").delete()
    except NotFound:
        pass


def _state_blob(client, data_bucket: str, bucket: str):
    return client.bucket(data_bucket).blob(f"access/state/{bucket}.json")


def load_state(client, data_bucket: str, bucket: str) -> dict:
    b = _state_blob(client, data_bucket, bucket)
    if not b.exists():
        return {"watermark": None, "ingested_tail": []}
    return json.loads(b.download_as_bytes())


def save_state(client, data_bucket: str, bucket: str, watermark: str, tail: list[str]) -> None:
    _state_blob(client, data_bucket, bucket).upload_from_string(
        json.dumps(
            {
                "watermark": watermark,
                "ingested_tail": sorted(tail),
                "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
        )
        + "\n",
        content_type="application/json",
    )


def list_new(client, log_bucket: str, bucket: str, state: dict) -> list[tuple[str, int]]:
    """Names (with sizes) needing ingest: everything past the watermark, plus
    any straggler inside the lag window not in the ingested tail."""
    prefix = f"usage/{bucket}_usage_"
    wm = state.get("watermark")
    start = None
    if wm:
        wm_ts = name_ts(wm)
        if wm_ts:
            start = prefix + _ts_minus_hours(wm_ts, LAG_HOURS)
    done = set(state.get("ingested_tail") or [])
    out = []
    for b in client.list_blobs(log_bucket, prefix=prefix, start_offset=start):
        base = b.name.rsplit("/", 1)[-1]
        if base in done:
            continue
        # Remaining names are either past the watermark (new) or inside the
        # lag window but missing from the tail (a late-delivered straggler);
        # both get ingested.
        out.append((b.name, int(b.size or 0)))
    out.sort()
    return out


def _span_name(first: str, last: str) -> str:
    a = name_ts(first.rsplit("/", 1)[-1]) or "unknown"
    b = name_ts(last.rsplit("/", 1)[-1]) or "unknown"
    return f"part-{a}--{b}"


def ingest_bucket(
    client,
    bucket: str,
    log_bucket: str,
    data_bucket: str,
    stage_dir: Path,
    memory_limit: str,
    max_chunk_bytes: int,
    workers: int = 16,
    max_chunks: int | None = None,
) -> dict:
    """Ingest all new usage CSVs for one source bucket. Returns stats."""
    import duckdb
    from google.cloud.storage import transfer_manager

    from disk_tree.access.aggregate import aggregate_access
    from disk_tree.access.parsers import parser_for

    state = load_state(client, data_bucket, bucket)
    todo = list_new(client, log_bucket, bucket, state)
    if not todo:
        err(f"[{bucket}] up to date (watermark {state.get('watermark')})")
        return {"bucket": bucket, "files": 0, "bytes": 0, "chunks": 0}

    chunks = plan_chunks(todo, max_chunk_bytes)
    if max_chunks is not None:
        chunks = chunks[:max_chunks]
    total_b = sum(c.bytes for c in chunks)
    err(f"[{bucket}] {sum(len(c.names) for c in chunks)} files / {total_b / 1e9:.1f} GB in {len(chunks)} chunk(s)")

    log_bkt = client.bucket(log_bucket)
    data_bkt = client.bucket(data_bucket)
    tail = set(state.get("ingested_tail") or [])
    watermark = state.get("watermark")
    n_rows_total = 0

    for ci, chunk in enumerate(chunks):
        csv_dir = stage_dir / bucket / "csv"
        if csv_dir.exists():
            shutil.rmtree(csv_dir)
        csv_dir.mkdir(parents=True, exist_ok=True)
        err(f"[{bucket}] chunk {ci + 1}/{len(chunks)}: {len(chunk.names)} files, {chunk.bytes / 1e9:.1f} GB — staging")
        results = transfer_manager.download_many_to_path(
            log_bkt,
            chunk.names,
            destination_directory=str(csv_dir),
            max_workers=workers,
        )
        failed = [(n, r) for n, r in zip(chunk.names, results) if isinstance(r, Exception)]
        if failed:
            for n, r in failed[:5]:
                err(f"[{bucket}] stage FAILED {n}: {r}")
            raise RuntimeError(f"{len(failed)}/{len(chunk.names)} downloads failed for {bucket}")

        span = _span_name(chunk.names[0], chunk.names[-1])
        raw_local = stage_dir / bucket / f"{span}.raw.parquet"
        agg_local = stage_dir / bucket / f"{span}.agg.parquet"
        con = duckdb.connect()
        con.execute(f"SET memory_limit = '{memory_limit}'")
        con.execute("SET preserve_insertion_order = false")
        tmp = stage_dir / bucket / ".duckdb-tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory = '{tmp}'")

        rel = parser_for("gcs")(f"{csv_dir}/usage/*", store="gcs", con=con)
        rel.write_parquet(str(raw_local), compression="zstd")
        n_rows = con.execute(f"SELECT COUNT(*) FROM read_parquet('{raw_local}')").fetchone()[0]
        n_rows_total += n_rows
        stats = aggregate_access(con, f"(SELECT * FROM read_parquet('{raw_local}'))", str(agg_local))
        con.close()
        err(
            f"[{bucket}] chunk {ci + 1}: {n_rows:,} rows → {raw_local.stat().st_size / 1e9:.2f} GB raw, "
            f"{stats['paths_out']:,} paths / {stats['days']} day(s) agg"
        )

        # Upload both, then (and only then) advance the watermark — a crash
        # in between reprocesses this chunk onto the same object names.
        for local, kind in ((raw_local, "raw"), (agg_local, "agg")):
            data_bkt.blob(f"access/{kind}/{bucket}/{span}.parquet").upload_from_filename(
                str(local), timeout=600,
            )
            local.unlink()
        shutil.rmtree(csv_dir)

        bases = [n.rsplit("/", 1)[-1] for n in chunk.names]
        watermark = max(watermark or "", *bases)
        wm_ts = name_ts(watermark)
        floor = _ts_minus_hours(wm_ts, LAG_HOURS) if wm_ts else ""
        tail |= set(bases)
        tail = {t for t in tail if (name_ts(t) or "") >= floor}
        save_state(client, data_bucket, bucket, watermark, sorted(tail))

    return {
        "bucket": bucket,
        "files": sum(len(c.names) for c in chunks),
        "bytes": total_b,
        "chunks": len(chunks),
        "rows": n_rows_total,
    }


def ingest(
    buckets: tuple[str, ...],
    log_bucket: str,
    data_bucket: str,
    stage_dir: Path,
    memory_limit: str,
    max_chunk_gb: float,
    workers: int,
    max_chunks: int | None = None,
) -> list[dict]:
    import os
    import socket

    from google.cloud import storage

    client = storage.Client()
    owner = os.environ.get("BATCH_JOB_UID") or f"{socket.gethostname()}:{os.getpid()}"
    if not acquire_lease(client, data_bucket, owner):
        return []
    out = []
    try:
        for b in buckets:
            out.append(
                ingest_bucket(
                    client,
                    b,
                    log_bucket=log_bucket,
                    data_bucket=data_bucket,
                    stage_dir=stage_dir,
                    memory_limit=memory_limit,
                    max_chunk_bytes=int(max_chunk_gb * 1e9),
                    workers=workers,
                    max_chunks=max_chunks,
                )
            )
    finally:
        release_lease(client, data_bucket)
    done = [r for r in out if r["files"]]
    err(
        "access ingest done: "
        + (", ".join(f"{r['bucket']}: {r['files']}f/{r['bytes'] / 1e9:.0f}GB" for r in done) if done else "all up to date")
    )
    return out
