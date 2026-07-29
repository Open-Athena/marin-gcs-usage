"""Direct GCS listing → canonical listing parquet shards.

Patch for buckets where Storage Insights is unavailable (`marin-us-central2`
502s at config creation): list the bucket ourselves, prefix-parallelized, and
write shards in the canonical listing schema so `prepare_listing` treats the
output like any other source.

Two levels of parallelism: depth-2 prefixes are round-robined across worker
*processes* (page parsing is pure-Python — threads alone are GIL-bound to
~1M objects/min), and each process streams several prefixes concurrently on
threads. Memory stays bounded everywhere: workers stream ``list_blobs`` pages
(field-projected) into small frames through a bounded queue, and each
process's consumer is the only writer of its namespaced shards, flushed
straight to ``gs://`` (FUSE writes would buffer whole files in RAM).
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from multiprocessing import get_context
from queue import Queue

import pandas as pd

from .listing import SII_CLASS_IDS

err = partial(print, file=sys.stderr)

ROWS_PER_SHARD = 1_000_000
BATCH_ROWS = 200_000
BLOB_FIELDS = "items(name,size,timeCreated,storageClass),nextPageToken"


def dedupe_prefixes(prefixes: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Sorted, de-duplicated prefixes plus the (descendant, ancestor) pairs dropped.

    Placeholder "folder" objects (zero-byte ``dir/`` blobs) make gcsfs ``ls``
    return a directory inside its own listing, so the depth-2 enumeration can
    emit both ``scratch/`` and ``scratch/<user>/`` — streaming those subtrees
    twice. Keeping only ancestors makes double-listing structurally impossible.
    """
    kept: list[str] = []
    dropped: list[tuple[str, str]] = []
    for p in sorted(set(prefixes)):
        if kept and p.startswith(kept[-1]):
            dropped.append((p, kept[-1]))
            continue
        kept.append(p)
    return kept, dropped


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


def _write_shard(out_dir: str, name: str, frame: pd.DataFrame) -> None:
    import fsspec

    out_fs, out_root = fsspec.core.url_to_fs(out_dir)
    out_fs.makedirs(out_root, exist_ok=True)
    frame.to_parquet(f"{out_root}/{name}.parquet", index=False, filesystem=out_fs)


SUCCESS_MARKER = "_SUCCESS.json"


def resolve_existing(out_fs, out_root: str, exists: str) -> dict | None:
    """Apply the exists-policy to a target dir; returns the completed run's
    ``_SUCCESS`` payload when it should be reused, else None (proceed to list).

    - ``error``: refuse if any shards are present
    - ``reuse``: short-circuit iff a completed run's marker is present;
      clear partial output (shards without a marker) and proceed
    - ``clear``: always delete existing shards and proceed
    """
    import json

    shards = out_fs.glob(f"{out_root}/*.parquet")
    marker = f"{out_root}/{SUCCESS_MARKER}"
    complete = out_fs.exists(marker)
    if not shards and not complete:
        return None
    if exists == "error":
        raise ValueError(
            f"{out_root} already has {len(shards)} shards"
            f"{' and a completion marker' if complete else ' (no completion marker — partial run?)'};"
            " pass --exists clear|reuse or choose another --out"
        )
    if exists == "reuse" and complete:
        payload = json.loads(out_fs.cat(marker))
        err(f"{out_root}: reusing completed listing ({payload.get('objects', '?'):,} objects)")
        return payload
    # 'clear', or 'reuse' over a partial run
    err(f"{out_root}: clearing {len(shards)} existing shard(s){' (partial run)' if exists == 'reuse' else ''}")
    if shards:
        out_fs.rm(shards)
    if complete:
        out_fs.rm(marker)
    return None


def _stream_prefixes(
    bucket: str,
    prefixes: list[str],
    out_dir: str,
    ns: int,
    threads: int,
) -> int:
    """Worker-process body: stream ``prefixes`` to shards named ``shard-<ns>-*``."""
    from google.cloud import storage

    q: Queue = Queue(maxsize=threads)
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
            with ThreadPoolExecutor(threads) as ex:
                for _ in ex.map(one, prefixes):
                    pass
        except BaseException as e:  # surfaced in the consumer loop below
            produce_error.append(e)
        finally:
            q.put(done)

    threading.Thread(target=produce, daemon=True).start()

    total = 0
    n_out = 0
    buffer: list[pd.DataFrame] = []
    buffered = 0

    def flush() -> None:
        nonlocal n_out, buffered
        if not buffered:
            return
        _write_shard(out_dir, f"shard-{ns:02d}-{n_out:04d}", pd.concat(buffer, ignore_index=True))
        n_out += 1
        buffer.clear()
        buffered = 0

    while (item := q.get()) is not done:
        buffer.append(item)
        buffered += len(item)
        total += len(item)
        if buffered >= ROWS_PER_SHARD:
            flush()
            err(f"  [w{ns}] {total:,} objects, {n_out} shards")
    if produce_error:
        raise produce_error[0]
    flush()
    err(f"  [w{ns}] done: {total:,} objects, {n_out} shards")
    return total


def list_bucket_to_parquet(
    bucket: str,
    out_dir: str,
    procs: int = 6,
    threads: int = 8,
    prefix: str | None = None,
    exists: str = "error",
) -> int:
    """List ``bucket`` (optionally under ``prefix``) to parquet shards in ``out_dir``.

    ``out_dir`` may be local or ``gs://``. Returns total object count. A
    ``_SUCCESS.json`` marker (with counts) is written on completion;
    ``exists`` governs behavior when the target already has output (see
    :func:`resolve_existing`).
    """
    import json

    import fsspec
    import gcsfs

    out_fs, out_root = fsspec.core.url_to_fs(out_dir)
    out_fs.makedirs(out_root, exist_ok=True)
    reused = resolve_existing(out_fs, out_root, exists)
    if reused is not None:
        return int(reused["objects"])

    fs = gcsfs.GCSFileSystem(use_listings_cache=False)
    root = f"{bucket}/{prefix.strip('/')}" if prefix else bucket
    # depth-2 stream prefixes + everything shallower than depth 2 in one shard
    shallow: list[tuple] = []
    stream_prefixes: list[str] = []
    for e1 in fs.ls(root, detail=True):
        for e2 in [e1] if e1["type"] == "file" else fs.ls(e1["name"], detail=True):
            if e2["type"] == "file":
                shallow.append((e2["name"].split("/", 1)[1], e2["size"], e2.get("timeCreated"), e2.get("storageClass")))
            else:
                stream_prefixes.append(e2["name"].split("/", 1)[1] + "/")
    stream_prefixes, nested = dedupe_prefixes(stream_prefixes)
    for p, parent in nested:
        err(f"WARN: dropping nested prefix {p!r} (inside {parent!r})")
    # de-nested depth-1 parents stream their whole subtree, which includes
    # depth-2 files that also landed in `shallow` — keep them stream-side only
    parents = tuple({parent for _, parent in nested})
    if parents:
        before = len(shallow)
        shallow = [row for row in shallow if not row[0].startswith(parents)]
        err(f"{root}: dropped {before - len(shallow)} shallow objects covered by de-nested parents")
    err(f"{root}: {len(stream_prefixes)} depth-2 prefixes, {len(shallow)} shallow objects; {procs} procs × {threads} threads")

    _write_shard(out_dir, "shard-shallow", entries_to_frame(bucket, shallow))
    total = len(shallow)

    chunks = [stream_prefixes[i::procs] for i in range(procs)]
    with ProcessPoolExecutor(procs, mp_context=get_context("spawn")) as ex:
        futures = [
            ex.submit(_stream_prefixes, bucket, chunk, out_dir, ns, threads)
            for ns, chunk in enumerate(chunks)
            if chunk
        ]
        for f in futures:
            total += f.result()
    out_fs.pipe(f"{out_root}/{SUCCESS_MARKER}", json.dumps({"bucket": bucket, "prefix": prefix, "objects": total}).encode())
    err(f"{root}: {total:,} objects listed → {out_dir}")
    return total
