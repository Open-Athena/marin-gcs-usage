"""In-process engines for the bake-off (specs/filter-query-service.md §6
phase 2): the serving box's candidates (`mem`, `duck`) scored by the same
`score` as the Worker, with each answer's compute time split from the time
to materialize its root paths (a response draws the tree; only the bench
lists every root).
"""

from __future__ import annotations

import hashlib
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .query import QueryError, parse
from .queryset import Case
from .score import Answer


LIST_MAX = 50_000  # above: the answer carries the roots' count + md5, not the list


def md5_sorted(arr, chunk: int = 1 << 20) -> str:
    """`score.md5_paths` of an already-sorted Arrow string array, without
    one Python list of it all."""
    h = hashlib.md5()
    for i in range(0, len(arr), chunk):
        if i:
            h.update(b"\n")
        h.update("\n".join(arr.slice(i, chunk).to_pylist()).encode())
    return h.hexdigest()


@dataclass
class Timing:
    id: str
    view: str
    ms: int  # roots + totals
    materialize_ms: int  # root paths, sorted
    roots: int | None
    stats: dict


@dataclass
class LocalEngine:
    """`score.Engine` over an in-process `evaluate`: `kind` is `mem`, `duck` or `ch`."""

    kind: str
    ix: object
    name: str = ""
    timings: list[Timing] = field(default_factory=list)

    def __post_init__(self):
        self.name = self.name or self.kind

    def answer(self, case: Case, view: str) -> Answer:
        from . import ch, duck, mem

        try:
            ast = parse(case.q, case.qs)
            prepare = getattr(self.ix, "prepare", None)
            if prepare:
                prepare()
            t0 = time.monotonic()
            if self.kind == "mem":
                r = mem.evaluate(self.ix, ast, view)
            else:
                r = self.ix.evaluate(ast, view)
            ms = round((time.monotonic() - t0) * 1000)
            t1 = time.monotonic()
            if self.kind == "ch":
                listed, n, md5 = self.ix.roots_summary(LIST_MAX)
                roots, kw = (listed, {}) if listed is not None else ([], {"n_roots": n, "roots_md5": md5})
            else:
                if self.kind == "mem":
                    import pyarrow as pa

                    arr = pa.array([view], pa.large_string()) if r.hit else self.ix.paths_arrow(np.asarray(r.roots), sort=True)
                else:
                    arr = self.ix.roots_arrow()
                n = len(arr)
                if n <= LIST_MAX:
                    roots, kw = arr.to_pylist(), {}
                else:
                    roots, kw = [], {"n_roots": n, "roots_md5": md5_sorted(arr)}
            mat = round((time.monotonic() - t1) * 1000)
        except (mem.Unsupported, ch.Unsupported, QueryError, KeyError) as e:
            self.timings.append(Timing(case.id, view, 0, 0, None, {"error": str(e)}))
            return Answer(501, 0, None, 0, None, None, None, reason=str(e))
        self.timings.append(Timing(case.id, view, ms, mat, n, r.stats))
        return Answer(200, ms, ms, 0, roots, r.b, r.o, **kw)


def pct(xs: list[float], q: float) -> float | None:
    s = sorted(xs)
    if not s:
        return None
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def latency_summary(timings: list[Timing]) -> dict:
    ms = [t.ms for t in timings if t.roots is not None]
    tot = [t.ms + t.materialize_ms for t in timings if t.roots is not None]
    return {
        "n": len(ms),
        "p50_ms": pct(ms, 0.5), "p90_ms": pct(ms, 0.9), "max_ms": max(ms) if ms else None,
        "with_paths_p50_ms": pct(tot, 0.5), "with_paths_p90_ms": pct(tot, 0.9), "with_paths_max_ms": max(tot) if tot else None,
    }


# --- staging: GCS ↔ local disk ---------------------------------------------------------


_CLIENT = None


def _get_range(bucket: str, key: str, generation: int, dst: str, start: int, end: int) -> int:
    """Worker: bytes [start, end] of an object into `dst` at `start`."""
    global _CLIENT
    from google.cloud import storage

    if _CLIENT is None:
        _CLIENT = storage.Client()
    blob = _CLIENT.bucket(bucket).blob(key, generation=generation)
    with open(dst, "r+b") as f:
        f.seek(start)
        blob.download_to_file(f, start=start, end=end, checksum=None)
    return end - start + 1


def fetch(blob, dst: Path, workers: int = 32, chunk: int = 64 << 20, deadline: float | None = None) -> str:
    """One blob to a local file by parallel ranged GETs in worker processes
    (≈ 850 MB/s on Batch; a thread pool manages ≈ 130). A process pool once
    hung there, so past `deadline` its workers are killed and the copy is
    redone with threads. Returns the mode that finished."""
    import concurrent.futures as cf

    size = blob.size
    deadline = deadline or max(60.0, size / (50 << 20))
    with open(dst, "wb") as f:
        f.truncate(size)
    ranges = [(s, min(s + chunk, size) - 1) for s in range(0, size, chunk)]
    args = (blob.bucket.name, blob.name, blob.generation, str(dst))
    import multiprocessing as mp

    # spawn, not fork: forking a process whose gRPC threads are live aborts the children.
    ex = cf.ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn"))
    futs = [ex.submit(_get_range, *args, s, e) for s, e in ranges]
    done, pending = cf.wait(futs, timeout=deadline)
    if not pending:
        ex.shutdown()
        for f in done:
            f.result()
        return "process"
    print(f"fetch {blob.name}: {len(pending)} ranges past {deadline:.0f}s; killing the pool, retrying with threads", file=sys.stderr, flush=True)
    for p in list(getattr(ex, "_processes", {}).values()):
        p.kill()
    ex.shutdown(wait=False, cancel_futures=True)
    with cf.ThreadPoolExecutor(workers) as tx:
        list(tx.map(lambda r: _get_range(*args, *r), ranges))
    return "thread"


def download_dir(prefix: str, dst: Path, workers: int = 32) -> dict:
    """Every object directly under a `gs://` prefix into `dst` (parallel
    ranged GETs per file: phase 0's "copy, don't mount")."""
    from google.cloud import storage

    bucket, _, key = prefix[5:].partition("/")
    dst.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    n = files = 0
    for b in storage.Client().list_blobs(bucket, prefix=key.rstrip("/") + "/", delimiter="/"):
        name = b.name.rsplit("/", 1)[1]
        if not name:
            continue
        fetch(b, dst / name, workers)
        n += b.size
        files += 1
    return {"bytes": n, "files": files, "s": round(time.monotonic() - t0, 2)}


def stage_file(uri: str, dst: Path, workers: int = 32) -> tuple[str, float]:
    """A `gs://` file copied to `dst` (`fetch`), else as is. Returns (local
    path, seconds)."""
    if not uri.startswith("gs://"):
        return uri, 0.0
    from google.cloud import storage

    bucket, _, key = uri[5:].partition("/")
    blob = storage.Client().bucket(bucket).blob(key)
    blob.reload()
    dst.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    fetch(blob, dst, workers)
    s = round(time.monotonic() - t0, 2)
    print(f"downloaded {uri} ({blob.size} B) in {s}s", file=sys.stderr, flush=True)
    return str(dst), s


def upload_dir(src: Path, prefix: str, workers: int = 32, only: list[str] | None = None) -> dict:
    from google.cloud import storage
    from google.cloud.storage import transfer_manager as tm

    bucket, _, key = prefix[5:].partition("/")
    bk = storage.Client().bucket(bucket)
    t0 = time.monotonic()
    n = 0
    for f in sorted(src.iterdir()):
        if not f.is_file() or (only is not None and f.name not in only):
            continue
        blob = bk.blob(f"{key.rstrip('/')}/{f.name}")
        if f.stat().st_size > 256 << 20:
            tm.upload_chunks_concurrently(str(f), blob, chunk_size=64 << 20, max_workers=workers, worker_type=tm.THREAD)
        else:
            blob.upload_from_filename(str(f))
        n += f.stat().st_size
    return {"bytes": n, "s": round(time.monotonic() - t0, 2)}
