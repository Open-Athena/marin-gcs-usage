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
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..bench import mem
from ..bench.query import QueryError, parse
from . import view as bv

SCAN_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T\d{4})?$")


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
    return sorted(n for n in names if SCAN_RE.match(n))


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
    if not SCAN_RE.match(date):
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
    if not SCAN_RE.match(prev) or not SCAN_RE.match(curr):
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
    gate: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(4))

    @property
    def root_label(self) -> str:
        return self.store.root_label

    def start(self) -> None:
        pass

    def health(self) -> dict:
        try:
            scans = self.store.scans(refresh=True)
        except Exception as e:  # noqa: BLE001 — reported, the process stays up
            return {"state": "error", "engine": "ch", "error": f"{type(e).__name__}: {e}"[:500], "scans": []}
        return {"state": "ready", "engine": "ch", "scans": [{"date": s.id, "version": s.version} for s in scans.values()]}


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
    if not SCAN_RE.match(date):
        raise HttpError(400, "bad date")
    path = _path(qs)
    w, h = _quant(qs, "w", 1280), _quant(qs, "h", 800)
    min_area, atten = _num(qs, "minArea", bv.MIN_AREA_DEFAULT), _num(qs, "atten", bv.ATTEN_DEFAULT)
    depth = int(_num(qs, "depth", 0)) or None
    q, ast = _ch_query(box, qs)
    s = _ch_scan(box, date)
    ch = box.store.session()
    if ast is None:
        v = cs.plain_view(ch, s, path, w=w, h=h, min_area=min_area, atten=atten, max_depth=depth)
    else:
        pr = cs.filter_prepare(ch, s, path, ast)
        v = cs.filter_view(ch, pr, w=w, h=h, min_area=min_area, atten=atten, max_depth=depth) if pr else None
    yield from cs.subtree_body(ch, v, date=date, path=path, w=w, h=h, min_area=min_area, atten=atten, q=q, root_label=box.root_label)


def ch_diff(box: ChBox, qs: dict):
    from ..chstore import serve as cs

    prev, curr = qs.get("from", [""])[0], qs.get("to", [""])[0]
    if not SCAN_RE.match(prev) or not SCAN_RE.match(curr):
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
    yield from cs.diff_body(box.store.session(), sa, sb, path=path, w=w, h=h, min_area=min_area, atten=atten, top=top, ast=ast, q=q,
                            summary=qs.get("summary", [""])[0] == "1", depth=depth)


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
    yield cs.series_body(box.store.session(), scans, path=path, paths=paths, split=bool(split))


CH_ROUTES = {"/api/subtree": ch_subtree, "/api/diff": ch_diff, "/api/series": ch_series}


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
            self.wfile.write(data)
            self._done(status, t0, len(data))

        def _engine(self, t0: float, state: str | None = None) -> None:
            dur = round((time.monotonic() - t0) * 1000)
            self.send_header("x-query-engine", f"box;dur={dur}" + (f";state={state}" if state else ""))
            self.send_header("server-timing", f"box;dur={dur}, total;dur={dur}")

        def _authed(self) -> bool:
            if token is None:
                return True
            got = self.headers.get("authorization", "")
            return got.startswith("Bearer ") and hmac.compare_digest(got[7:].encode(), token.encode())

        def do_GET(self):  # noqa: N802
            t0 = time.monotonic()
            u = urlparse(self.path)
            if u.path == "/healthz":
                hb = box.health()
                return self._send(500 if hb["state"] == "error" else 200, json.dumps(hb), t0, state=box.state)
            if not self._authed():
                return self._send(401, "unauthorized", t0, "text/plain")
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
                with box.gate:
                    gen = route(box, qs)
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
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("transfer-encoding", "chunked")
            self._engine(t0)
            self.end_headers()
            n = 0
            for piece in _chain(first, gen):
                data = piece.encode()
                if data:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                    n += len(data)
            self.wfile.write(b"0\r\n\r\n")
            self._done(200, t0, n)

    return Handler


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
