"""`dt-cloud serve-query`: the serving box over HTTP (specs/filter-query-service.md §3,
§4.4, §6.3). Answers what the Worker's filtered reads answer, from loaded
`MemIndex` scans:

- `GET /api/subtree?date=&path=&w=&h=&q=[&qs=][&minArea=][&atten=][&depth=][&o=][&cl=]`
- `GET /api/diff?from=&to=&path=&w=&h=&q=[…][&top=][&summary=1]`
- `GET /healthz` — `{state: loading|ready|error, scans: [...]}`, no auth (a
  probe's target); always 200 unless loading failed.
- `GET /warm` — blocks until the scans are loaded (on Cloud Run with
  request-based billing, an instance has CPU only while a request is in
  flight: the caller holds this one open while the index loads).

Every response carries `x-query-engine: box;dur=<ms>` (plus `;state=loading`
on a 503). A scan that isn't loaded is a 409 and a query the index can't
answer exactly a 501, so the Worker falls back to its own read; a missing
path is a 404, a bad parameter or query a 400. Reads need
`Authorization: Bearer <token>`, the token from an env var (never logged).

One process runs unchanged on a VM (index on local disk), on Cloud Run
(`-s` an in-memory dir; `-M` maps the copy, so it costs its RAM once;
`-D` reads `detail.parquet` from GCS in place) and locally over a fixture.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sys
import threading
import time
from contextlib import AbstractContextManager, contextmanager
from collections import OrderedDict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Generator, Iterator
from urllib.parse import parse_qs, urlparse

from ..bench import mem
from ..bench.query import QueryError, parse
from . import view as bv
from ..scan_id import is_scan_id



def err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


# --- scans ----------------------------------------------------------------------------


def list_scans(root: str) -> list[str]:
    """The scan dates under an index root (`<root>/<date>/meta.json`), sorted."""
    root = root.rstrip("/")
    if root.startswith("gs://"):
        from google.cloud import storage

        bucket, _, key = root[5:].partition("/")
        it = storage.Client().list_blobs(bucket, prefix=key + "/", delimiter="/")
        list(it)
        names = [p.rstrip("/").rsplit("/", 1)[-1] for p in it.prefixes]
    else:
        names = [p.name for p in Path(root).iterdir() if (p / mem.META).exists()]
    return sorted(n for n in names if is_scan_id(n))


def load_scan(src: str, *, stage: Path | None, mmap: bool, remote_detail: bool, threads: int) -> tuple[mem.MemIndex, dict]:
    """One scan's index: a local dir as is; a `gs://` one copied to `stage`
    first (parallel ranged GETs), its detail optionally left in place."""
    t0 = time.monotonic()
    info: dict = {"src": src}
    d = Path(src)
    if src.startswith("gs://"):
        if stage is None:
            raise ValueError(f"{src}: a gs:// index needs a stage dir (-s)")
        d = stage / src.rstrip("/").rsplit("/", 1)[-1]
        info["download"] = _download(src, d, skip={mem.DETAIL} if remote_detail else set())
    t1 = time.monotonic()
    ix = mem.MemIndex.load(d, threads=threads, mmap=mmap, detail=not remote_detail)
    if remote_detail and src.startswith("gs://"):
        ix.detail = mem.Detail(f"{src.rstrip('/')}/{mem.DETAIL}", str(d / mem.SLICES))
    info.update(load_s=round(time.monotonic() - t1, 2), total_s=round(time.monotonic() - t0, 2), nodes=ix.n, names=ix.V, mmap=mmap,
                bytes=sum(ix.nbytes().values()))
    return ix, info


def _download(prefix: str, dst: Path, skip: set[str]) -> dict:
    from google.cloud import storage

    from ..bench.local import fetch

    bucket, _, key = prefix[5:].partition("/")
    dst.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    n = files = 0
    for b in storage.Client().list_blobs(bucket, prefix=key.rstrip("/") + "/", delimiter="/"):
        name = b.name.rsplit("/", 1)[1]
        if not name or name in skip:
            continue
        if (dst / name).exists() and (dst / name).stat().st_size == b.size:
            continue
        fetch(b, dst / name)
        n += b.size
        files += 1
    return {"bytes": n, "files": files, "s": round(time.monotonic() - t0, 2)}


@dataclass
class Box:
    """The loaded scans and their loading state."""

    root: str
    dates: list[str] | None = None  # None: the latest (`n_latest` of them)
    n_latest: int = 1
    stage: Path | None = None
    mmap: bool = False
    remote_detail: bool = False
    threads: int = 8
    root_label: str = "marin GCS"
    syntax: str = "simple"
    scans: dict = field(default_factory=dict)  # date → MemIndex
    info: dict = field(default_factory=dict)  # date → load info
    state: str = "idle"
    error: str | None = None
    done: threading.Event = field(default_factory=threading.Event)
    gate: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(2))

    def start(self) -> None:
        if self.state != "idle":
            return
        self.state = "loading"
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self) -> None:
        try:
            dates = self.dates or list_scans(self.root)[-self.n_latest :]
            if not dates:
                raise ValueError(f"no scans under {self.root}")
            for d in dates:
                err(f"serve-query: loading {d}")
                ix, info = load_scan(f"{self.root.rstrip('/')}/{d}", stage=self.stage, mmap=self.mmap, remote_detail=self.remote_detail, threads=self.threads)
                self.scans[d], self.info[d] = ix, info
                err(f"serve-query: loaded {d}: {json.dumps(info)}")
            self.state = "ready"
        except Exception as e:  # noqa: BLE001 — reported on /healthz, the process stays up
            self.state, self.error = "error", f"{type(e).__name__}: {e}"
            err(f"serve-query: loading failed: {self.error}")
        finally:
            self.done.set()

    def health(self) -> dict:
        out = {"state": self.state, "scans": [{"date": d, **{k: v for k, v in self.info[d].items() if k != "src"}} for d in sorted(self.scans)]}
        if self.error:
            out["error"] = self.error
        return out


# --- requests -------------------------------------------------------------------------


class HttpError(Exception):
    def __init__(self, status: int, msg: str, headers: dict | None = None):
        super().__init__(msg)
        self.status, self.msg, self.headers = status, msg, headers or {}


def _num(qs: dict, k: str, default: float) -> float:
    try:
        v = float(qs.get(k, [""])[0] or 0)
    except ValueError:
        v = 0.0
    return v if v and v == v else default


def _quant(qs: dict, k: str, default: int) -> int:
    return int(-(-_num(qs, k, default) // bv.QUANT) * bv.QUANT)


def _match_limit(qs: dict) -> int:
    """Bound response-only match lists; the tree and its totals stay exact."""
    return max(0, min(50_000, int(_num(qs, "matchLimit", 5_000))))


def _path(qs: dict) -> str:
    p = re.sub(r"/+$", "", qs.get("path", [""])[0])
    if ".." in p or p.startswith("/"):
        raise HttpError(400, "bad path")
    return p


def _query(box: Box, qs: dict):
    q = qs.get("q", [""])[0]
    try:
        ast = parse(q, qs.get("qs", [""])[0] or box.syntax)
    except QueryError as e:
        raise HttpError(400, f"bad query: {e}") from e
    if ast is None:
        raise HttpError(400, "the box answers filtered reads only (q=)")
    if qs.get("lens"):
        raise HttpError(409, "a user lens isn't served by the box")
    return q, ast


def _scan(box: Box, date: str) -> mem.MemIndex:
    ix = box.scans.get(date)
    if ix is None:
        if box.state == "loading":
            raise HttpError(503, "loading", {"retry-after": "10"})
        raise HttpError(409, f"scan {date} not loaded (loaded: {', '.join(sorted(box.scans)) or 'none'})")
    return ix


def _scope(qs: dict) -> tuple[bv.Scope, str | None]:
    raw = qs.get("o", [None])[0]
    return bv.Scope(bv.parse_owner(raw), bv.parse_classes(qs.get("cl", [None])[0])), raw


def subtree(box: Box, qs: dict):
    date = qs.get("date", [""])[0]
    if not is_scan_id(date):
        raise HttpError(400, "bad date")
    path = _path(qs)
    w, h = _quant(qs, "w", 1280), _quant(qs, "h", 800)
    min_area, atten = _num(qs, "minArea", bv.MIN_AREA_DEFAULT), _num(qs, "atten", bv.ATTEN_DEFAULT)
    depth = int(_num(qs, "depth", 0)) or None
    q, ast = _query(box, qs)
    scope, raw_owner = _scope(qs)
    ix = _scan(box, date)
    r = bv.filter_view(ix, path, ast, w=w, h=h, min_area=min_area, atten=atten, scope=scope, max_depth=depth)
    return bv.subtree_body(ix, r, date=date, path=path, w=w, h=h, min_area=min_area, atten=atten, q=q, owner_raw=raw_owner, root_label=box.root_label)


def diff(box: Box, qs: dict):
    prev, curr = qs.get("from", [""])[0], qs.get("to", [""])[0]
    if not is_scan_id(prev) or not is_scan_id(curr):
        raise HttpError(400, "bad from/to")
    if prev >= curr:
        raise HttpError(400, "from must precede to")
    path = _path(qs)
    w, h = _quant(qs, "w", 1280), _quant(qs, "h", 800)
    min_area, atten = _num(qs, "minArea", bv.MIN_AREA_DEFAULT), _num(qs, "atten", bv.ATTEN_DEFAULT)
    top = int(min(5000, _num(qs, "top", 500)))
    depth = int(_num(qs, "depth", 0)) or None
    q, ast = _query(box, qs)
    scope, raw_owner = _scope(qs)
    ixa, ixb = _scan(box, prev), _scan(box, curr)
    return bv.diff_body(ixa, ixb, prev=prev, curr=curr, path=path, w=w, h=h, min_area=min_area, atten=atten, top=top, ast=ast, q=q, scope=scope,
                        summary=qs.get("summary", [""])[0] == "1", depth=depth, owner_raw=raw_owner)


ROUTES = {"/api/subtree": subtree, "/api/diff": diff}


# --- the ClickHouse engine (`-e ch`, specs/ch-store.md §4) ------------------------------


def _set(e: threading.Event) -> threading.Event:
    e.set()
    return e


@dataclass
class ChBox:
    """The ClickHouse store as the box's engine: every ingested scan, plain
    and filtered reads, series. Nothing to load; `/healthz` asks the store."""

    store: object  # chstore.serve.Store
    state: str = "ready"
    error: str | None = None
    done: threading.Event = field(default_factory=lambda: _set(threading.Event()))
    gate: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(2))
    narrow_target: str | None = None
    narrow_manifest: dict | None = None
    narrow_name_index: bool = False
    narrow_name_variant: str | None = None
    narrow_parent_index: bool = False
    narrow_plan: str = "legacy"
    coarse_indexes: OrderedDict = field(default_factory=OrderedDict)
    coarse_lock: threading.Lock = field(default_factory=threading.Lock)
    hot_l1_generation: Path | None = None
    hot_l1_catalog: object | None = None
    hot_l1_published: dict | None = None
    hot_l1_gate: threading.BoundedSemaphore = field(default_factory=lambda: threading.BoundedSemaphore(2))
    hot_l2_artifact: Path | None = None
    hot_l2_check: Path | None = None
    hot_l2_catalog: object | None = None
    hot_l2_gate: threading.BoundedSemaphore = field(default_factory=lambda: threading.BoundedSemaphore(2))
    name_summary_enabled: bool = False
    name_summary_runtime: object | None = None
    dated_l1_generation: Path | None = None
    dated_name_store: str | None = None
    dated_cold: bool = False
    mega_postings: str | None = None
    mega_catalog: str | None = None
    dated_name_summary_runtime: object | None = None

    @property
    def root_label(self) -> str:
        return self.store.root_label

    def start(self) -> None:
        if (self.dated_l1_generation is None) != (self.dated_name_store is None):
            raise ValueError("dated root generation and explicit logical store are required together")
        if self.dated_l1_generation is not None and not self.name_summary_enabled:
            raise ValueError("dated roots require the existing stitched name-summary lane")
        if self.dated_cold and self.dated_l1_generation is None:
            raise ValueError("dated cold fallback requires a dated root generation")
        if self.mega_postings and self.dated_l1_generation is None:
            raise ValueError("the consolidated name index requires a dated root generation")
        if self.mega_catalog and not self.mega_postings:
            raise ValueError("the consolidated catalog requires the consolidated name index")
        if self.name_summary_enabled and (self.hot_l1_generation is None or self.narrow_target is None):
            raise ValueError("name summary requires a published hot L1 generation and numeric target")
        if (self.hot_l2_artifact is None) != (self.hot_l2_check is None):
            raise ValueError("hot L2 artifact and check are required together")
        if self.hot_l1_generation is not None and self.hot_l1_catalog is None:
            from ..chstore.hot_l1_publish import load, load_pinned, pin

            if self.name_summary_enabled:
                self.hot_l1_published = pin(self.hot_l1_generation)
                self.hot_l1_catalog = load_pinned(self.hot_l1_generation, self.hot_l1_published)
            else:
                self.hot_l1_catalog = load(self.hot_l1_generation)
        if self.hot_l1_catalog is not None and self.narrow_target is not None and self.hot_l1_catalog.target != self.narrow_target:
            raise ValueError("published hot L1 catalog target differs from the selected numeric target")
        if self.hot_l2_artifact is not None and self.hot_l2_catalog is None:
            from ..chstore.hot_l2_pair_catalog import HotL2PairCatalog

            self.hot_l2_catalog = HotL2PairCatalog.load(self.hot_l2_artifact, self.hot_l2_check)
        if self.hot_l2_catalog is not None:
            if self.narrow_target is not None and self.hot_l2_catalog.target != self.narrow_target:
                raise ValueError("accepted hot L2 catalog target differs from the selected numeric target")
            if self.hot_l1_catalog is not None and self.hot_l2_catalog.target != self.hot_l1_catalog.target:
                raise ValueError("accepted hot L2 catalog target differs from the selected hot L1 catalog")
        if self.narrow_plan != "legacy":
            from ..chstore.narrow_serve import serving_options

            serving_options(self.narrow_plan)
            if not self.narrow_target:
                raise ValueError("a numeric serving plan requires an experimental numeric target")
        if self.narrow_name_variant is not None:
            from ..chstore.narrow import identifier

            identifier(self.narrow_name_variant)
            if not self.narrow_name_index:
                raise ValueError("a rich name-index variant requires the rich name index")
        if self.narrow_name_index and not self.narrow_target:
            raise ValueError("the rich name index requires an experimental numeric target")
        if self.narrow_parent_index and not self.narrow_target:
            raise ValueError("the directory parent index requires an experimental numeric target")
        if self.narrow_target and (self.narrow_manifest is None or self.narrow_name_variant is not None):
            from ..chstore.narrow import identifier, rich_name_variant_identity

            identifier(self.narrow_target)
            ch = self.store.session()
            try:
                manifest = json.loads(ch.scalar(f"SELECT doc FROM {self.narrow_target}.history_manifest"))
                if manifest["source_db"] != self.store.db:
                    raise ValueError("experimental history source differs from the serving store")
                suffix = f"_{self.narrow_name_variant}" if self.narrow_name_variant is not None else ""
                for label, enabled, table in (
                    ("rich name", self.narrow_name_index, f"rich_name_manifest{suffix}"),
                    ("directory parent", self.narrow_parent_index, "parent_index_manifest"),
                ):
                    if enabled:
                        if ch.scalar(f"EXISTS TABLE {self.narrow_target}.{table}") != "1":
                            raise ValueError(f"experimental {label} index has no completed checkpoint")
                        marker = json.loads(ch.scalar(f"SELECT doc FROM {self.narrow_target}.{table}"))
                        if marker["target"] != self.narrow_target:
                            raise ValueError(f"experimental {label} index checkpoint differs from the serving target")
                        if label == "rich name" and self.narrow_name_variant is not None:
                            expected = rich_name_variant_identity(self.narrow_target, self.narrow_name_variant)
                            if {key: marker.get(key) for key in expected} != expected:
                                raise ValueError("experimental rich name index variant checkpoint differs from the serving target")
                self.narrow_manifest = manifest
            finally:
                ch.close()
        if self.name_summary_enabled and self.name_summary_runtime is None:
            from ..chstore.name_summary import NameSummaryRuntime

            reuse = ({"catalog": self.hot_l1_catalog, "published": self.hot_l1_published}
                     if self.hot_l1_published is not None else {})
            self.name_summary_runtime = NameSummaryRuntime.load(self.hot_l1_generation, self.store.url, target=self.narrow_target, **reuse)
        if self.name_summary_enabled and self.name_summary_runtime.target != self.narrow_target:
            raise ValueError("name summary runtime target differs from the selected numeric target")
        if self.dated_l1_generation is not None and self.dated_name_summary_runtime is None:
            from ..chstore.dated_hot_l1_publish import load
            from ..chstore.dated_name_summary import DatedNameSummaryRuntime

            published = load(self.dated_l1_generation)
            paths = tuple(path for _, _, path in self.name_summary_runtime.binding.buckets)
            cold = {}
            if self.dated_cold:
                from ..chstore import daily_name_index
                from ..chstore.client import Ch

                ch = Ch(self.store.url, timeout=10, max_execution_time=10)
                try:
                    cold = {day: daily_name_index.load(ch, catalog.metadata()["source"]["snapshot_db"])
                            for day, catalog in published.catalogs.items()}
                finally:
                    ch.close()
            mega = catalog = None
            if self.mega_postings:
                from ..chstore import mega_names
                from ..chstore.client import Ch

                ch = Ch(self.store.url, db=self.store.db, timeout=60, max_execution_time=60)
                try:
                    mega = mega_names.binding(ch, self.mega_postings)
                    if self.mega_catalog:
                        from ..chstore import mega_catalog

                        catalog = mega_catalog.binding(ch, self.mega_catalog)
                finally:
                    ch.close()
            self.dated_name_summary_runtime = DatedNameSummaryRuntime(
                self.name_summary_runtime, published,
                logical_store=self.dated_name_store, bucket_paths=paths, cold=cold, mega=mega, catalog=catalog,
            )

    def narrow_covers(self, path: str, *dates: str) -> bool:
        if not self.narrow_manifest:
            return False
        prefix = self.narrow_manifest["prefix"]
        return (prefix == "" or path == prefix or path.startswith(prefix + "/")) and all(d in self.narrow_manifest["dates"] for d in dates)

    def health(self) -> dict:
        hot = {"hot_l1": self.hot_l1_catalog.metadata()} if self.hot_l1_catalog is not None else {}
        if self.hot_l2_catalog is not None:
            hot["hot_l2"] = self.hot_l2_catalog.metadata()
        if self.name_summary_runtime is not None:
            hot["name_summary"] = self.name_summary_runtime.metadata()
        if self.dated_name_summary_runtime is not None:
            hot["dated_name_summary"] = self.dated_name_summary_runtime.metadata()
        try:
            scans = self.store.scans(refresh=True)
        except Exception as e:  # noqa: BLE001 — reported, the process stays up
            return {"state": "error", "engine": "ch", "error": f"{type(e).__name__}: {e}"[:500], "scans": [], **hot}
        return {"state": "ready", "engine": "ch", "scans": [{"date": s.id, "version": s.version} for s in scans.values()],
                **({"narrow": {"target": self.narrow_target, "prefix": self.narrow_manifest["prefix"], "dates": self.narrow_manifest["dates"], "incremental": False,
                               "name_index": self.narrow_name_index, "parent_index": self.narrow_parent_index,
                               **({"plan": self.narrow_plan} if self.narrow_plan != "legacy" else {}),
                               **({"name_index_variant": self.narrow_name_variant} if self.narrow_name_variant is not None else {})}}
                   if self.narrow_manifest else {}), **hot}


def _ch_query(box: ChBox, qs: dict):
    """`q=` (optional for the store), and the scopes it doesn't serve."""
    if qs.get("lens"):
        raise HttpError(409, "a user lens isn't served by the box")
    if qs.get("o", [""])[0] or qs.get("cl", [""])[0] or qs.get("by", [""])[0]:
        raise HttpError(501, "owner / class scopes aren't served by the ch engine")
    q = qs.get("q", [""])[0]
    try:
        ast = parse(q, qs.get("qs", [""])[0] or box.store.syntax)
    except QueryError as e:
        raise HttpError(400, f"bad query: {e}") from e
    return (q, ast) if ast is not None else (None, None)


def _ch_scan(box: ChBox, date: str):
    s = box.store.scan(date)
    if s is None:
        raise HttpError(409, f"scan {date} not in the store")
    return s


def ch_subtree(box: ChBox, qs: dict):
    from ..chstore import serve as cs

    date = qs.get("date", [""])[0]
    if not is_scan_id(date):
        raise HttpError(400, "bad date")
    path = _path(qs)
    w, h = _quant(qs, "w", 1280), _quant(qs, "h", 800)
    min_area, atten = _num(qs, "minArea", bv.MIN_AREA_DEFAULT), _num(qs, "atten", bv.ATTEN_DEFAULT)
    depth = int(_num(qs, "depth", 0)) or None
    q, ast = _ch_query(box, qs)
    s = _ch_scan(box, date)
    if ast is not None and box.narrow_covers(path, date):
        from ..chstore.narrow_serve import response, serving_options

        result = response(box.store.url, box.narrow_target, date, q, path=path, syntax=qs.get("qs", [""])[0] or box.store.syntax,
                          threads=box.store.threads, w=w, h=h, min_area=min_area, atten=atten, max_depth=depth,
                          match_limit=_match_limit(qs), root_label=box.root_label, name_index=box.narrow_name_index,
                          name_index_variant=box.narrow_name_variant, parent_index=box.narrow_parent_index,
                          **serving_options(box.narrow_plan))
        yield cs._jdump(result["body"])
        return
    ch = box.store.session()
    try:
        if ast is None:
            v = cs.plain_view(ch, s, path, w=w, h=h, min_area=min_area, atten=atten, max_depth=depth)
        else:
            pr = cs.filter_prepare(ch, s, path, ast, compact=box.store.root_plan == "compact")
            v = cs.filter_view(ch, pr, w=w, h=h, min_area=min_area, atten=atten, max_depth=depth) if pr else None
        yield from cs.subtree_body(ch, v, date=date, path=path, w=w, h=h, min_area=min_area, atten=atten, q=q, root_label=box.root_label,
                                   match_limit=_match_limit(qs))
    finally:
        ch.close()


def ch_diff(box: ChBox, qs: dict):
    from ..chstore import serve as cs

    prev, curr = qs.get("from", [""])[0], qs.get("to", [""])[0]
    if not is_scan_id(prev) or not is_scan_id(curr):
        raise HttpError(400, "bad from/to")
    if prev >= curr:
        raise HttpError(400, "from must precede to")
    path = _path(qs)
    w, h = _quant(qs, "w", 1280), _quant(qs, "h", 800)
    min_area, atten = _num(qs, "minArea", bv.MIN_AREA_DEFAULT), _num(qs, "atten", bv.ATTEN_DEFAULT)
    top = int(min(5000, _num(qs, "top", 500)))
    depth = int(_num(qs, "depth", 0)) or None
    q, ast = _ch_query(box, qs)
    sa, sb = _ch_scan(box, prev), _ch_scan(box, curr)
    if ast is not None and box.narrow_covers(path, prev, curr):
        from ..chstore.narrow_serve import response, serving_options

        result = response(box.store.url, box.narrow_target, curr, q, previous=prev, path=path, syntax=qs.get("qs", [""])[0] or box.store.syntax,
                          threads=box.store.threads, w=w, h=h, min_area=min_area, atten=atten, max_depth=depth, top=top,
                          summary=qs.get("summary", [""])[0] == "1", match_limit=_match_limit(qs), root_label=box.root_label, name_index=box.narrow_name_index,
                          name_index_variant=box.narrow_name_variant, parent_index=box.narrow_parent_index,
                          **serving_options(box.narrow_plan))
        yield cs._jdump(result["body"])
        return
    ch = box.store.session()
    try:
        yield from cs.diff_body(ch, sa, sb, path=path, w=w, h=h, min_area=min_area, atten=atten, top=top, ast=ast, q=q,
                                summary=qs.get("summary", [""])[0] == "1", depth=depth, match_limit=_match_limit(qs), compact=box.store.root_plan == "compact")
    finally:
        ch.close()


def parse_paths(values: list[str]) -> list[str]:
    """`filter.ts` `parsePaths`: comma lists and repeats, trailing slashes dropped, blanks and repeats removed."""
    out: list[str] = []
    for v in values:
        for p in v.split(","):
            p = re.sub(r"/+$", "", p.strip())
            if p and p not in out:
                out.append(p)
    return out


def ch_series(box: ChBox, qs: dict):
    """`/api/series`: one point per ingested scan. The Worker passes the scans
    it knows (`n`, `first`, `last`); a store that doesn't hold exactly those
    is a 409, so the Worker answers instead."""
    from ..chstore import serve as cs

    if qs.get("lens"):
        raise HttpError(409, "a user lens isn't served by the box")
    if qs.get("o", [""])[0] or qs.get("cl", [""])[0]:
        raise HttpError(501, "owner / class scopes aren't served by the ch engine")
    path = _path(qs)
    paths = parse_paths(qs.get("paths", []))
    if any(".." in p or p.startswith("/") for p in paths):
        raise HttpError(400, "bad paths")
    split = qs.get("split", [""])[0]
    if split and split != "roots":
        raise HttpError(400, "bad split (want roots)")
    if split and (path or paths):
        raise HttpError(400, "split=roots is for the unscoped store root only")
    scans = list(box.store.scans(refresh=True).values())
    first, last, n = qs.get("first", [""])[0], qs.get("last", [""])[0], qs.get("n", [""])[0]
    if first or last or n:
        held = [s.id for s in scans if (not first or s.id >= first) and (not last or s.id <= last)]
        if (first and first not in held) or (last and last not in held) or (n and str(len(held)) != n):
            raise HttpError(409, f"the store holds {len(held)} scans in [{first or '…'}, {last or '…'}], not the Worker's {n or '?'}")
        keep = set(held)
        scans = [s for s in scans if s.id in keep]
    ch = box.store.session()
    try:
        yield cs.series_body(ch, scans, path=path, paths=paths, split=bool(split))
    finally:
        ch.close()


def ch_coarse(box: ChBox, qs: dict):
    """Separate experimental response: exact basename and byte/count rollups."""
    from ..chstore.client import Ch
    from ..chstore.coarse import CoarseRequest, NameIndex, diff
    from ..chstore.coverage import Coverage, diff as coverage_diff
    from ..chstore.range_bench import NonLeafMatches

    date, name, path = qs.get("date", [""])[0], qs.get("name", [""])[0].lower(), _path(qs)
    date0 = qs.get("date0", [""])[0]
    mode = qs.get("mode", ["exact"])[0]
    if not is_scan_id(date) or not name or len(name) > 512 or "/" in name:
        raise HttpError(400, "a valid scan and exact basename are required")
    if date0 and not is_scan_id(date0):
        raise HttpError(400, "a valid before scan is required")
    if mode not in ("exact", "contains", "suffix", "coverage"):
        raise HttpError(400, "mode must be exact, contains, suffix or coverage")
    if mode != "exact" and len(name) < 3:
        raise HttpError(400, "substring and suffix patterns require at least three characters")
    try:
        budget = int(qs.get("budget", ["64"])[0])
    except ValueError as e:
        raise HttpError(400, "budget must be an integer from 1 to 256") from e
    if not 1 <= budget <= 256:
        raise HttpError(400, "budget must be an integer from 1 to 256")
    if date0 and budget > 128:
        raise HttpError(400, "diff budget must be from 1 to 128 (at most twice that many children)")
    try:
        levels = int(qs.get("levels", ["1"])[0])
    except ValueError as e:
        raise HttpError(400, "levels must be an integer from 1 to 4") from e
    if not 1 <= levels <= 4:
        raise HttpError(400, "levels must be an integer from 1 to 4")
    if mode == "coverage" and levels != 1:
        raise HttpError(400, "directory coverage currently serves one level per drill")
    if any(qs.get(k, [""])[0] for k in ("q", "qs", "lens", "o", "cl", "by")):
        raise HttpError(501, "coarse preview serves exact leaf basenames and bytes/counts only")
    if not box.narrow_target or not box.narrow_covers(path, date) or (date0 and not box.narrow_covers(path, date0)):
        raise HttpError(409, "coarse preview has no frozen index for this path/scan")
    ch = Ch(box.store.url, max_threads=box.store.threads, max_memory_usage=8 << 30,
            max_bytes_before_external_group_by=256 << 20, max_bytes_before_external_sort=256 << 20, max_execution_time=20,
            timeout_before_checking_execution_speed=0, timeout_overflow_mode="throw", timeout=30)
    start, hits, indexes = time.monotonic(), [], []
    try:
        with box.coarse_lock:
            for scan in ([date0, date] if date0 else [date]):
                key = scan, name, mode
                index = box.coarse_indexes.pop(key, None)
                hits.append(index is not None)
                if index is None:
                    if mode == "coverage":
                        index = Coverage.build(ch, box.narrow_target, scan, name, resident_roots=True)
                    else:
                        index = NameIndex.build(ch, box.narrow_target, scan, name) if mode == "exact" else NameIndex.build_pattern(ch, box.narrow_target, scan, name, match_mode=mode, max_names=500_000)
                elif mode != "coverage":
                    index.prepare(ch)
                box.coarse_indexes[key] = index
                indexes.append(index)
            while len(box.coarse_indexes) > 4:
                box.coarse_indexes.popitem(last=False)
        if mode == "coverage":
            result = coverage_diff(ch, indexes[0], indexes[1], path, budget) if date0 else indexes[0].view(ch, path, budget)
        elif levels > 1:
            from ..chstore.coarse_walk import walk, walk_diff

            result = walk_diff(ch, indexes[0], indexes[1], path, budget, levels) if date0 else walk(ch, indexes[0], path, budget, levels)
        else:
            result = diff(ch, indexes[0], indexes[1], path, budget) if date0 else indexes[0].view(ch, path, budget)
        seconds = round(time.monotonic() - start, 4)
        result.update(cache_hit=all(hits), index_build_s=round(sum(index.build_s for index in indexes), 4), server_response_s=seconds)
        if date0:
            for side, hit in zip(("before", "after"), hits):
                result[side].update(cache_hit=hit, server_response_s=seconds)
        yield json.dumps(result, separators=(",", ":"))
    except NonLeafMatches as e:
        raise HttpError(501, str(e)) from e
    except CoarseRequest as e:
        raise HttpError(400, str(e)) from e
    finally:
        ch.close()


CH_ROUTES = {"/api/subtree": ch_subtree, "/api/diff": ch_diff, "/api/series": ch_series, "/api/coarse": ch_coarse}


def make_handler(box: Box, token: str | None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "dt-cloud-serve-query"

        def log_message(self, fmt, *args):  # one line per request, from `_done`
            pass

        def _done(self, status: int, t0: float, n: int) -> None:
            err(f"{self.command} {urlparse(self.path).path} {status} {n}B {round((time.monotonic() - t0) * 1000)}ms")

        def _send(self, status: int, body: str | bytes, t0: float, ctype: str = "application/json", headers: dict | None = None, state: str | None = None):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(data)))
            self._engine(t0, state)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            self._done(status, t0, 0 if self.command == "HEAD" else len(data))

        def _engine(self, t0: float, state: str | None = None) -> None:
            dur = round((time.monotonic() - t0) * 1000)
            self.send_header("x-query-engine", f"box;dur={dur}" + (f";state={state}" if state else ""))
            self.send_header("server-timing", f"box;dur={dur}, total;dur={dur}")

        def _authed(self) -> bool:
            if token is None:
                return True
            got = self.headers.get("authorization", "")
            return got.startswith("Bearer ") and hmac.compare_digest(got[7:].encode(), token.encode())

        def _hot_l1(self, raw_query: str, t0: float) -> None:
            from ..chstore.hot_l1_http import query_catalog
            from ..chstore.hot_l1_catalog import CatalogRequest

            if not isinstance(box, ChBox) or box.hot_l1_catalog is None:
                return self._send(501, json.dumps({"error": "hot L1 catalog is not selected; no scan fallback"}), t0)
            try:
                with _hot_l1_admission(box.hot_l1_gate):
                    body = query_catalog(box.hot_l1_catalog, raw_query)
                    self._send(200, json.dumps(body), t0, headers={"cache-control": "private, no-store"})
            except CatalogRequest as e:
                self._send(400, json.dumps({"error": str(e)}), t0)
            except HttpError as e:
                self._send(e.status, json.dumps({"error": e.msg}), t0, headers=e.headers)

        def _hot_l2(self, raw_query: str, t0: float) -> None:
            from ..chstore.hot_l1_http import query_catalog
            from ..chstore.hot_l1_catalog import CatalogRequest

            private = {"cache-control": "private, no-store"}
            if not isinstance(box, ChBox) or box.hot_l2_catalog is None:
                return self._send(501, json.dumps({"error": "hot L2 catalog is not selected; no scan fallback"}), t0, headers=private)
            try:
                with _hot_l2_admission(box.hot_l2_gate):
                    try:
                        data = json.dumps(query_catalog(box.hot_l2_catalog, raw_query))
                    except CatalogRequest as e:
                        return self._send(400, json.dumps({"error": str(e)}), t0, headers=private)
                    except Exception as e:  # noqa: BLE001 — refuse the catalog read without source fallback or private diagnostics
                        err(f"serve-query: /api/hot-l2: {type(e).__name__}")
                        return self._send(503, json.dumps({"error": "hot L2 catalog unavailable; no scan fallback"}), t0,
                                          headers={**private, "retry-after": "1"})
                    self._send(200, data, t0, headers=private)
            except HttpError as e:
                self._send(e.status, json.dumps({"error": e.msg}), t0, headers={**private, **e.headers})

        def _name_summary(self, raw_query: str, t0: float) -> None:
            from ..chstore.hot_l1_http import query_catalog
            from ..chstore.hot_l1_catalog import CatalogRequest

            private = {"cache-control": "private, no-store"}
            runtime = (box.dated_name_summary_runtime or box.name_summary_runtime) if isinstance(box, ChBox) else None
            if runtime is None:
                return self._send(501, json.dumps({"error": "name summary is not selected"}), t0, headers=private)
            try:
                data = json.dumps(query_catalog(runtime, raw_query), allow_nan=False)
                if len(data.encode("utf-8")) > 64 << 10:
                    raise ValueError("name summary body exceeds its byte cap")
            except CatalogRequest as e:
                return self._send(400, json.dumps({"error": str(e)}), t0, headers=private)
            except Exception as e:  # noqa: BLE001 — refuse without exposing private SQL or returning partial totals
                err(f"serve-query: /api/name-summary: {type(e).__name__}")
                return self._send(503, json.dumps({"error": "name summary unavailable or exceeded work budget; retry or narrow the literal. This is not a zero-match result."}),
                                  t0, headers={**private, "retry-after": "1"})
            self._send(200, data, t0, headers=private)

        def _name_summary_registry(self, raw_query: str, t0: float) -> None:
            private = {"cache-control": "private, no-store"}
            if raw_query:
                return self._send(400, json.dumps({"error": "name-summary registry accepts no query parameters"}), t0, headers=private)
            runtime = (box.dated_name_summary_runtime or box.name_summary_runtime) if isinstance(box, ChBox) else None
            if runtime is None:
                return self._send(501, json.dumps({"error": "name summary is not selected"}), t0, headers=private)
            try:
                data = json.dumps(runtime.metadata(), allow_nan=False)
                if len(data.encode("utf-8")) > 64 << 10:
                    raise ValueError("name-summary registry exceeds its byte cap")
            except Exception as e:  # noqa: BLE001 — refuse without private diagnostics
                err(f"serve-query: /api/name-summary-registry: {type(e).__name__}")
                return self._send(503, json.dumps({"error": "name-summary scan registry unavailable"}), t0,
                                  headers={**private, "retry-after": "1"})
            self._send(200, data, t0, headers=private)

        def do_GET(self):  # noqa: N802
            t0 = time.monotonic()
            u = urlparse(self.path)
            if u.path == "/healthz":
                hb = box.health()
                return self._send(500 if hb["state"] == "error" else 200, json.dumps(hb), t0, state=box.state)
            if not self._authed():
                return self._send(401, "unauthorized", t0, "text/plain",
                                  headers={"cache-control": "private, no-store"} if u.path in ("/api/hot-l2", "/api/name-summary", "/api/name-summary-registry") else None)
            if u.path == "/api/hot-l1":
                return self._hot_l1(u.query, t0)
            if u.path == "/api/hot-l2":
                return self._hot_l2(u.query, t0)
            if u.path == "/api/name-summary":
                return self._name_summary(u.query, t0)
            if u.path == "/api/name-summary-registry":
                return self._name_summary_registry(u.query, t0)
            if u.path == "/warm":
                box.start()
                box.done.wait()
                return self._send(200 if box.state == "ready" else 500, json.dumps(box.health()), t0, state=box.state)
            route = (CH_ROUTES if isinstance(box, ChBox) else ROUTES).get(u.path)
            if route is None:
                return self._send(404, "not found", t0, "text/plain")
            box.start()
            qs = parse_qs(u.query, keep_blank_values=True)
            try:
                gate = _coarse_admission(box.gate) if u.path == "/api/coarse" else box.gate
                gen = _gated_stream(gate, lambda: route(box, qs))
                first = next(gen)
            except HttpError as e:
                return self._send(e.status, e.msg, t0, "text/plain", e.headers, state="loading" if e.status == 503 else None)
            except bv.NotFound:
                return self._send(404, "path not found" if u.path == "/api/subtree" else "path not found in either scan", t0, "text/plain")
            except mem.Unsupported as e:
                return self._send(501, f"not supported by the box: {e}", t0, "text/plain")
            except bv.BadRequest as e:
                return self._send(400, str(e), t0, "text/plain")
            except (OSError, RuntimeError) as e:
                # The store unreachable or failing (ClickHouse down, a query error): the Worker answers.
                err(f"serve-query: {u.path}: {type(e).__name__}: {str(e)[:500]}")
                return self._send(503, f"backend error: {type(e).__name__}", t0, "text/plain", {"retry-after": "10"})
            n = 0
            try:
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("transfer-encoding", "chunked")
                self._engine(t0)
                self.end_headers()
                for piece in _chain(first, gen):
                    data = piece.encode()
                    if data:
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                        n += len(data)
                self.wfile.write(b"0\r\n\r\n")
            finally:
                gen.close()
            self._done(200, t0, n)

        def _unsupported_catalog_method(self) -> None:
            path = urlparse(self.path).path
            if path not in ("/api/hot-l2", "/api/name-summary", "/api/name-summary-registry"):
                return self.send_error(501, f"Unsupported method ({self.command!r})")
            t0 = time.monotonic()
            headers = {"cache-control": "private, no-store"}
            if not self._authed():
                return self._send(401, "unauthorized", t0, "text/plain", headers=headers)
            label = "hot L2" if path == "/api/hot-l2" else ("name-summary registry" if path == "/api/name-summary-registry" else "name summary")
            self._send(405, json.dumps({"error": f"{label} supports GET only"}), t0, headers={**headers, "allow": "GET"})

        do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_TRACE = do_CONNECT = _unsupported_catalog_method

    return Handler


@contextmanager
def _hot_l2_admission(gate: threading.BoundedSemaphore) -> Iterator[None]:
    """The artifact-only bucket lane never waits for ClickHouse or another reader."""
    if not gate.acquire(blocking=False):
        raise HttpError(503, "hot L2 serving slots busy; retry shortly", {"retry-after": "1"})
    try:
        yield
    finally:
        gate.release()


@contextmanager
def _hot_l1_admission(gate: threading.BoundedSemaphore) -> Iterator[None]:
    """The scan-free lane never waits for a rich query or another hot reader."""
    if not gate.acquire(blocking=False):
        raise HttpError(503, "hot L1 serving slots busy; retry shortly", {"retry-after": "1"})
    try:
        yield
    finally:
        gate.release()


@contextmanager
def _coarse_admission(gate: threading.Semaphore) -> Iterator[None]:
    """Refuse busy coarse reads instead of queueing work past client deadlines."""
    if not gate.acquire(blocking=False):
        raise HttpError(503, "coarse serving slots busy; retry shortly", {"retry-after": "1"})
    try:
        yield
    finally:
        gate.release()


def _gated_stream(gate: AbstractContextManager, produce: Callable[[], Generator[str, None, None]]) -> Iterator[str]:
    """Keep one concurrency slot through lazy backend reads and their cleanup."""
    with gate:
        chunks = produce()
        try:
            yield from chunks
        finally:
            chunks.close()


def _chain(first, rest):
    yield first
    yield from rest


def serve(box: "Box | ChBox", *, bind: str, port: int, token: str | None) -> None:
    box.start()
    httpd = ThreadingHTTPServer((bind, port), make_handler(box, token))
    httpd.daemon_threads = True
    src = f"the ClickHouse store at {box.store.url} ({box.store.db})" if isinstance(box, ChBox) else f"scans from {box.root}"
    err(f"serve-query: listening on {bind}:{port} ({'bearer auth' if token else 'NO auth'}), {src}")
    httpd.serve_forever()


def token_from_env(var: str) -> str | None:
    return os.environ.get(var) or None
