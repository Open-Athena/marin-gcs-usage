"""Direct GCS listing → canonical listing parquet shards.

Patch for buckets where Storage Insights is unavailable (`marin-us-central2`
502s at config creation): list the bucket ourselves, prefix-parallelized, and
write shards in the canonical listing schema so `prepare_listing` treats the
output like any other source.

Memory discipline (the first Cloud Run execution OOMed without it):

- listings cache disabled — gcsfs would otherwise retain every listed entry
  in its dircache (~GBs at 100M objects);
- results stream back to the main thread, which buffers rows and flushes
  chunky sequential shards (~1M rows) instead of one file per prefix;
- shard output goes straight to ``gs://`` via gcsfs — writing through a FUSE
  mount buffers whole files in container memory.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pandas as pd

from .listing import SII_CLASS_IDS

err = partial(print, file=sys.stderr)

ROWS_PER_SHARD = 1_000_000


def rows_to_frame(bucket: str, entries: list[dict]) -> pd.DataFrame:
    """gcsfs ``detail=True`` entries → canonical listing columns."""
    return pd.DataFrame(
        {
            "bucket": bucket,
            "name": [e["name"].split("/", 1)[1] for e in entries],
            "size_bytes": [int(e["size"]) for e in entries],
            "created": pd.to_datetime([e.get("timeCreated") for e in entries], utc=True, format="ISO8601"),
            "storage_class_id": [SII_CLASS_IDS.get(e.get("storageClass", ""), 0) for e in entries],
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

    fs = gcsfs.GCSFileSystem(use_listings_cache=False)
    out_fs, out_root = fsspec.core.url_to_fs(out_dir)
    out_fs.makedirs(out_root, exist_ok=True)

    root = f"{bucket}/{prefix.strip('/')}" if prefix else bucket
    # depth-2 shard prefixes + everything shallower than depth 2 in one batch
    shallow: list[dict] = []
    shard_prefixes: list[str] = []
    for e1 in fs.ls(root, detail=True):
        if e1["type"] == "file":
            shallow.append(e1)
            continue
        for e2 in fs.ls(e1["name"], detail=True):
            if e2["type"] == "file":
                shallow.append(e2)
            else:
                shard_prefixes.append(e2["name"])
    err(f"{root}: {len(shard_prefixes)} depth-2 prefixes, {len(shallow)} shallow objects")

    total = 0
    n_out = 0
    buffer: list[pd.DataFrame] = [rows_to_frame(bucket, shallow)]
    buffered = len(shallow)

    def flush() -> None:
        nonlocal n_out, buffered
        if not buffered:
            return
        frame = pd.concat(buffer, ignore_index=True)
        frame.to_parquet(f"{out_root}/shard-{n_out:05d}.parquet", index=False, filesystem=out_fs)
        n_out += 1
        buffer.clear()
        buffered = 0

    def one(pfx: str) -> pd.DataFrame:
        entries = [e for e in fs.find(pfx, detail=True).values() if e["type"] == "file"]
        return rows_to_frame(bucket, entries)

    with ThreadPoolExecutor(workers) as ex:
        for i, frame in enumerate(ex.map(one, shard_prefixes)):
            total += len(frame)
            if len(frame):
                buffer.append(frame)
                buffered += len(frame)
            if buffered >= ROWS_PER_SHARD:
                flush()
            if i % 500 == 0:
                err(f"  {i}/{len(shard_prefixes)} prefixes, {total:,} objects, {n_out} shards written")
    flush()
    total += len(shallow)
    err(f"{root}: {total:,} objects listed → {out_dir} ({n_out} shards)")
    return total
