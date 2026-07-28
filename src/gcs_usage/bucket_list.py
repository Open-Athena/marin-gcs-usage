"""Direct GCS listing → canonical listing parquet shards.

Patch for buckets where Storage Insights is unavailable (`marin-us-central2`
502s at config creation): list the bucket ourselves, prefix-parallelized, and
write shards in the canonical listing schema so `prepare_listing` treats the
output like any other source. Sharding is by depth-2 prefix (plus one shard
of shallower objects) to bound per-`find` memory on huge trees.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import PurePosixPath

import pandas as pd

from .listing import SII_CLASS_IDS

err = partial(print, file=sys.stderr)


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
    workers: int = 32,
    prefix: str | None = None,
) -> int:
    """List ``bucket`` (optionally under ``prefix``) to parquet shards in ``out_dir``.

    ``out_dir`` may be local or ``gs://``. Returns total object count.
    """
    import fsspec
    import gcsfs

    fs = gcsfs.GCSFileSystem()
    out_fs, out_root = fsspec.core.url_to_fs(out_dir)
    out_fs.makedirs(out_root, exist_ok=True)

    root = f"{bucket}/{prefix.strip('/')}" if prefix else bucket
    # depth-2 shard prefixes + everything shallower than depth 2 in one shard
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
    err(f"{root}: {len(shard_prefixes)} depth-2 shards, {len(shallow)} shallow objects")

    total = 0

    def write(frame: pd.DataFrame, shard: str) -> int:
        if len(frame):
            frame.to_parquet(f"{out_root}/{shard}.parquet", index=False, filesystem=out_fs)
        return len(frame)

    total += write(rows_to_frame(bucket, shallow), "shallow-000000")

    def one(i_pfx: tuple[int, str]) -> int:
        i, pfx = i_pfx
        entries = [e for e in fs.find(pfx, detail=True).values() if e["type"] == "file"]
        return write(rows_to_frame(bucket, entries), f"shard-{i:06d}")

    with ThreadPoolExecutor(workers) as ex:
        for i, n in enumerate(ex.map(one, enumerate(shard_prefixes))):
            total += n
            if i % 200 == 0:
                err(f"  {i}/{len(shard_prefixes)} shards, {total:,} objects")
    err(f"{root}: {total:,} objects listed → {out_dir}")
    return total
