"""Direct GCS listing → canonical listing parquet shards.

Patch for buckets where Storage Insights is unavailable (`marin-us-central2`
502s at config creation): list the bucket ourselves, prefix-parallelized, and
write shards in the canonical listing schema so `prepare_listing` treats the
output like any other source.

Memory discipline (the first two Cloud Run executions OOMed without it):
every stage is bounded. Workers stream ``list_blobs`` pages (field-projected)
and emit small frames; a bounded queue backpressures them; the main thread is
the only writer, flushing chunky sequential shards straight to ``gs://``
(FUSE writes would buffer whole files in container memory; gcsfs ``find``
would materialize whole prefixes; executor ``map`` would pile up ordered
results behind slow prefixes).
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from queue import Queue

import pandas as pd

from .listing import SII_CLASS_IDS

err = partial(print, file=sys.stderr)

ROWS_PER_SHARD = 1_000_000
BATCH_ROWS = 200_000
BLOB_FIELDS = "items(name,size,timeCreated,storageClass),nextPageToken"


def entries_to_frame(bucket: str, rows: list[tuple]) -> pd.DataFrame:
    """(name, size, created, storage_class) tuples → canonical listing columns."""
    names, sizes, created, classes = zip(*rows) if rows else ((), (), (), ())
    return pd.DataFrame(
        {
            "bucket": bucket,
            "name": list(names),
            "size_bytes": [int(s) for s in sizes],
            "created": pd.to_datetime(list(created), utc=True),
            "storage_class_id": [SII_CLASS_IDS.get(c or "", 0) for c in classes],
        }
    )


def list_bucket_to_parquet(
    bucket: str,
    out_dir: str,
    workers: int = 24,
    prefix: str | None = None,
) -> int:
    """List ``bucket`` (optionally under ``prefix``) to parquet shards in ``out_dir``.

    ``out_dir`` may be local or ``gs://``. Returns total object count.
    """
    import fsspec
    import gcsfs
    from google.cloud import storage

    fs = gcsfs.GCSFileSystem(use_listings_cache=False)
    out_fs, out_root = fsspec.core.url_to_fs(out_dir)
    out_fs.makedirs(out_root, exist_ok=True)

    root = f"{bucket}/{prefix.strip('/')}" if prefix else bucket
    # depth-2 stream prefixes + everything shallower than depth 2 in one batch
    shallow: list[tuple] = []
    stream_prefixes: list[str] = []
    for e1 in fs.ls(root, detail=True):
        for e2 in [e1] if e1["type"] == "file" else fs.ls(e1["name"], detail=True):
            if e2["type"] == "file":
                shallow.append((e2["name"].split("/", 1)[1], e2["size"], e2.get("timeCreated"), e2.get("storageClass")))
            else:
                stream_prefixes.append(e2["name"].split("/", 1)[1] + "/")
    err(f"{root}: {len(stream_prefixes)} depth-2 prefixes, {len(shallow)} shallow objects")

    q: Queue = Queue(maxsize=workers)
    local = threading.local()
    done = object()
    produce_error: list[BaseException] = []

    def one(pfx: str) -> None:
        client = getattr(local, "client", None)
        if client is None:
            client = local.client = storage.Client()
        rows: list[tuple] = []
        for blob in client.list_blobs(bucket, prefix=pfx, fields=BLOB_FIELDS):
            rows.append((blob.name, blob.size, blob.time_created, blob.storage_class))
            if len(rows) >= BATCH_ROWS:
                q.put(entries_to_frame(bucket, rows))
                rows = []
        if rows:
            q.put(entries_to_frame(bucket, rows))

    def produce() -> None:
        try:
            with ThreadPoolExecutor(workers) as ex:
                for _ in ex.map(one, stream_prefixes):
                    pass
        except BaseException as e:  # surfaced in the main thread below
            produce_error.append(e)
        finally:
            q.put(done)

    threading.Thread(target=produce, daemon=True).start()

    total = 0
    n_out = 0
    buffer: list[pd.DataFrame] = [entries_to_frame(bucket, shallow)]
    buffered = len(shallow)
    total += len(shallow)

    def flush() -> None:
        nonlocal n_out, buffered
        if not buffered:
            return
        frame = pd.concat(buffer, ignore_index=True)
        frame.to_parquet(f"{out_root}/shard-{n_out:05d}.parquet", index=False, filesystem=out_fs)
        n_out += 1
        buffer.clear()
        buffered = 0

    while (item := q.get()) is not done:
        buffer.append(item)
        buffered += len(item)
        total += len(item)
        if buffered >= ROWS_PER_SHARD:
            flush()
            err(f"  {total:,} objects, {n_out} shards written")
    if produce_error:
        raise produce_error[0]
    flush()
    err(f"{root}: {total:,} objects listed → {out_dir} ({n_out} shards)")
    return total
