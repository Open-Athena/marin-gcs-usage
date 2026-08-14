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
SAMPLE_ROWS = 500_000
BLOB_FIELDS = "items(name,size,timeCreated,storageClass),nextPageToken"


def discover_prefixes(fs, root: str) -> tuple[list[tuple], list[str], list[str]]:
    """Walk ``root`` two levels: (shallow file rows, depth-2 stream prefixes,
    self-dir placeholder names).

    Listings can contain the listed path *itself* as a directory entry when a
    placeholder object (``tmp/``, or ``/`` at bucket root) backs it —
    ``marin-us-east5`` shows its own bucket in ``ls(bucket)``. Recursing into
    those re-lists the parent (IndexError on the bucket name at best, subtree
    double-streams at worst), so they're skipped at both levels; their names
    are returned so the caller can fetch the placeholder *objects* (invisible
    to gcsfs, which folds them into directory entries) and count them.
    """
    shallow: list[tuple] = []
    stream_prefixes: list[str] = []
    self_dirs: list[str] = []
    for e1 in fs.ls(root, detail=True):
        if e1["name"].rstrip("/") == root.rstrip("/"):
            err(f"WARN: skipping self entry {e1['name']!r} in {root!r} listing")
            self_dirs.append("/")
            continue
        for e2 in [e1] if e1["type"] == "file" else fs.ls(e1["name"], detail=True):
            rel = e2["name"].split("/", 1)
            if e2["name"].rstrip("/") == e1["name"].rstrip("/") and e2["type"] != "file":
                err(f"WARN: skipping self entry {e2['name']!r} in {e1['name']!r} listing")
                self_dirs.append(rel[1] + "/" if len(rel) > 1 else "/")
                continue
            if len(rel) < 2 or not rel[1]:
                err(f"WARN: skipping unparseable listing entry {e2['name']!r} under {e1['name']!r}")
                continue
            if e2["type"] == "file":
                shallow.append((rel[1], e2["size"], e2.get("timeCreated"), e2.get("storageClass")))
            else:
                stream_prefixes.append(rel[1] + "/")
    return shallow, stream_prefixes, self_dirs


def placeholder_rows(bucket: str, self_dirs: list[str]) -> list[tuple]:
    """Fetch the zero-byte placeholder objects behind self-dir entries.

    They only match a streamed prefix when their whole depth-1 dir streams
    wholesale, so with children streaming individually they must be counted
    explicitly (central2 alone has 5 — e.g. an object literally named
    ``tmp/``)."""
    from google.cloud import storage

    bkt = storage.Client().bucket(bucket)
    rows = []
    for d in self_dirs:
        if blob := bkt.get_blob(d):
            created = blob.time_created.isoformat().replace("+00:00", "Z") if blob.time_created else None
            rows.append((blob.name, blob.size, created, blob.storage_class))
        else:
            err(f"WARN: no placeholder object behind self entry {d!r} in {bucket}")
    return rows


# A stream item is a prefix, optionally bounded to a [start, end) name range —
# hot prefixes are split into ranges so one flat 100M-object dir (eu-west4's
# datakit/store/ = 142M) can't serialize a whole bucket behind one stream.
Item = "str | tuple[str, str | None, str | None]"


def pack_chunks(items: list, weights: dict, n: int) -> list[list]:
    """Greedy bin-pack of stream items into ``n`` chunks by ``weights``
    (object counts from a prior listing; unknown items weight 1).

    Round-robin by count leaves one worker with the mega-prefixes (the first
    balanced run spent half its wall clock on a single straggler chunk).
    """
    import heapq

    bins: list[tuple[int, int, list]] = [(0, i, []) for i in range(n)]
    heapq.heapify(bins)
    for p in sorted(items, key=lambda p: (-weights.get(p, 1), str(p))):
        w, i, chunk = heapq.heappop(bins)
        chunk.append(p)
        heapq.heappush(bins, (w + weights.get(p, 1), i, chunk))
    return [chunk for _, _, chunk in sorted(bins, key=lambda b: b[1])]


def split_hot_prefixes(
    prefixes: list[str],
    weights: dict[str, int],
    weights_glob: str,
    n_streams: int,
) -> tuple[list, dict]:
    """Split prefixes heavier than the per-stream ideal into name ranges.

    Range boundaries are name-quantiles from the weights parquet (a prior
    listing or SII inventory — either has every object name), consumed by
    ``list_blobs(start_offset=, end_offset=)``: start inclusive, end
    exclusive, so quantile boundaries produce no gaps or dups even when
    stale. Returns (items, weights) with ranges replacing their prefix.
    """
    import math

    import duckdb

    total = sum(weights.get(p, 1) for p in prefixes)
    ideal = max(total // max(n_streams, 1), 1)
    items: list = []
    out_w: dict = dict(weights)
    con = None
    for p in sorted(prefixes):
        w = weights.get(p, 1)
        k = min(math.ceil(w / ideal), n_streams)
        if k < 2:
            items.append(p)
            continue
        if con is None:
            con = duckdb.connect()
            con.execute("SET memory_limit='4GB'")
        fracs = [i / k for i in range(1, k)]
        # Reservoir-sample the prefix's names before quantiling: exact
        # quantile_disc buffers every matching name (30M+ under one prefix →
        # OOM). Boundaries only partition [start, end) ranges, so sample
        # noise costs nothing — a 500k sample lands within ~0.3% of exact.
        [(bounds,)] = con.execute(
            f"SELECT quantile_disc(name, {fracs}) FROM ("
            f"SELECT name FROM read_parquet('{weights_glob}') WHERE starts_with(name, ?)"
            f" USING SAMPLE reservoir({SAMPLE_ROWS} ROWS))",
            [p],
        ).fetchall()
        if not bounds or len(set(bounds)) != len(bounds):
            items.append(p)  # degenerate names distribution — keep whole
            continue
        edges = [None, *bounds, None]
        err(f"splitting hot prefix {p!r} ({w:,} weighted objects) into {k} ranges")
        for start, end in zip(edges[:-1], edges[1:]):
            rng = (p, start, end)
            items.append(rng)
            out_w[rng] = w // k
    return items, out_w


def prefix_weights(weights_glob: str) -> dict[str, int]:
    """Object counts per depth-1 and depth-2 prefix from a canonical listing."""
    import duckdb

    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'")
    weights: dict[str, int] = {}
    for expr in (
        "split_part(name, '/', 1) || '/'",
        "split_part(name, '/', 1) || '/' || split_part(name, '/', 2) || '/'",
    ):
        for prefix, count in con.execute(
            f"SELECT {expr} AS p, count(*) FROM read_parquet('{weights_glob}') WHERE name LIKE '%/%' GROUP BY 1"
        ).fetchall():
            weights[prefix] = int(count)
    return weights


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


# Rows per parquet row group. Default (whole frame = 1 group) makes a shard
# unstreamable: a reader that pages by row group (e.g. a browser preview) must
# pull the entire shard at once. ~25k rows ≈ ~0.5 MB compressed here (~18 B/row)
# — sized for the interactive paged-table reader (fast first-page fetch/parse).
# Halving from 50k costs only ~2x row-group footer metadata (negligible: 5
# small columns) and a marginal compression hit; our other consumer (DuckDB
# full-scan aggregation) streams row groups and is ~indifferent to the size.
# (Row *rendering* still needs a display cap downstream; this only bounds
# per-group fetch+parse.)
ROW_GROUP_SIZE = 25_000


def _write_shard(out_dir: str, name: str, frame: pd.DataFrame) -> None:
    import fsspec

    out_fs, out_root = fsspec.core.url_to_fs(out_dir)
    out_fs.makedirs(out_root, exist_ok=True)
    frame.to_parquet(
        f"{out_root}/{name}.parquet", index=False, filesystem=out_fs, row_group_size=ROW_GROUP_SIZE
    )


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
    prefixes: list,
    out_dir: str,
    ns: int,
    threads: int,
) -> int:
    """Worker-process body: stream items (prefixes or ``(prefix, start, end)``
    name ranges) to shards named ``shard-<ns>-*``."""
    from google.cloud import storage

    q: Queue = Queue(maxsize=threads)
    local = threading.local()
    done = object()
    produce_error: list[BaseException] = []

    def one(item) -> None:
        pfx, start, end = item if isinstance(item, tuple) else (item, None, None)
        client = getattr(local, "client", None)
        if client is None:
            client = local.client = storage.Client()
        rows: list[tuple] = []
        for blob in client.list_blobs(
            bucket, prefix=pfx, fields=BLOB_FIELDS, start_offset=start, end_offset=end,
        ):
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
    weights_from: str | None = None,
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
    shallow, stream_prefixes, self_dirs = discover_prefixes(fs, root)
    if self_dirs:
        shallow.extend(placeholder_rows(bucket, self_dirs))
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

    weights = prefix_weights(weights_from) if weights_from else {}
    stream_items: list = stream_prefixes
    if weights:
        err(f"chunk balancing from {weights_from} ({len(weights)} weighted prefixes)")
        stream_items, weights = split_hot_prefixes(stream_prefixes, weights, weights_from, procs * threads)
    chunks = pack_chunks(stream_items, weights, procs)
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
