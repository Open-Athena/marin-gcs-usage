"""`dt-cloud ch-bench`: time the same requests against any base URL — the
box's `serve-query` or a deployed Worker — warm, or cold (the box's
ClickHouse caches and the OS page cache dropped before each request), and
compare two runs for exactness (specs/ch-store.md §6).

A request is `NAME=/api/…?…`. With `jitter`, subtree and diff requests get a
`minArea` of 12.xxxxxx drawn from (seed, name, trial): past any edge cache,
the default's cost, and the same parameters in every run with that seed, so a
box run and a Worker run compare body for body. Bodies are compared
normalized: parsed, less the engine's labels (`tier`, `index`), the Worker's
coverage flags and its `pv` provenance (the Worker lays it on the box's tree
itself)."""

from __future__ import annotations

import hashlib
import json
import random
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Callable

UA = "dt-cloud-probe/1.0"
LABELS = {"tier", "index", "partial", "partialReason", "approximate", "approximateReason", "firstPaint"}
DROP_CACHES = ("MARK CACHE", "UNCOMPRESSED CACHE", "INDEX MARK CACHE", "INDEX UNCOMPRESSED CACHE", "QUERY CONDITION CACHE", "PRIMARY INDEX CACHE",
               "TEXT INDEX TOKENS CACHE", "TEXT INDEX HEADER CACHE", "TEXT INDEX POSTINGS CACHE", "PAGE CACHE", "MMAP CACHE")


@dataclass
class Rec:
    name: str
    trial: int
    url: str
    status: int
    ms: int
    engine: str | None
    server: str | None
    cache: str | None
    bytes: int
    sha: str | None
    cold: bool
    clients: int = 1


def normalize(body: bytes) -> str | None:
    """A body's comparable digest: JSON less engine labels and `pv`; None if not JSON."""
    try:
        doc = json.loads(body)
    except ValueError:
        return None

    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if k != "pv" and not (k in LABELS and x is doc)}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x

    return hashlib.sha256(json.dumps(strip(doc), sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def with_jitter(path: str, name: str, trial: int, seed: int) -> str:
    if not (path.startswith("/api/subtree") or path.startswith("/api/diff")):
        return path
    r = random.Random(f"{seed}:{name}:{trial}")
    u = urllib.parse.urlsplit(path)
    qs = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True) if k != "minArea"] + [("minArea", f"12.{r.randrange(10**6):06d}")]
    return f"{u.path}?{urllib.parse.urlencode(qs, quote_via=urllib.parse.quote)}"


def drop_caches(ch_url: str) -> None:
    """ClickHouse's caches, then the OS page cache (needs root / a privileged container)."""
    import os

    from .client import Ch

    ch = Ch(ch_url, session=False, timeout=60)
    for c in DROP_CACHES:
        try:
            ch.exec(f"SYSTEM DROP {c}")
        except RuntimeError:
            pass  # a cache this server version doesn't have
    os.sync()
    with open("/proc/sys/vm/drop_caches", "w") as f:
        f.write("3\n")


def fetch(base: str, path: str, token: str | None, timeout: float) -> tuple[int, int, dict, bytes]:
    req = urllib.request.Request(base.rstrip("/") + path, headers={"User-Agent": UA, **({"Authorization": f"Bearer {token}"} if token else {})})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, hd, body = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        status, hd, body = e.code, e.headers, e.read()
    except Exception:  # noqa: BLE001 — a transport failure is a failed request
        return 0, round((time.monotonic() - t0) * 1000), {}, b""
    return status, round((time.monotonic() - t0) * 1000), dict(hd), body


def run(
    base: str,
    requests: list[tuple[str, str]],
    *,
    token: str | None,
    trials: int = 1,
    seed: int | None = None,
    cold: bool = False,
    ch_url: str | None = None,
    timeout: float = 300,
    log: Callable[[str], None] | None = None,
    record_path: str | None = None,
    parallel: int = 1,
) -> list[Rec]:
    """Record completed requests immediately; parallel mode is warm-only."""
    if parallel <= 0 or trials <= 0:
        raise ValueError("parallel clients and trials must be positive")
    if cold and parallel > 1:
        raise ValueError("independent per-request cold resets cannot overlap parallel requests")

    def request(
        name: str,
        path: str,
        trial: int,
    ) -> Rec:
        p = with_jitter(path, name, trial, seed) if seed is not None else path
        if cold:
            drop_caches(ch_url or "http://localhost:8123")
        status, ms, hd, body = fetch(base, p, token, timeout)
        hd = {k.lower(): v for k, v in hd.items()}
        st = hd.get("server-timing") or ""
        return Rec(name, trial, p, status, ms, hd.get("x-query-engine"), st[:200] or None, hd.get("x-cache"), len(body),
                   normalize(body) if status == 200 else None, cold, clients=parallel)

    out: list[Rec] = []
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        for trial in range(trials):
            if parallel == 1:
                completed = (request(name, path, trial) for name, path in requests)
            else:
                futures = [pool.submit(request, name, path, trial) for name, path in requests]
                completed = (future.result() for future in as_completed(futures))
            for rec in completed:
                out.append(rec)
                if record_path:
                    dump([rec], record_path)
                if log:
                    log(f"{rec.name:<40} t{trial} {rec.status} {rec.ms:>7} ms {rec.bytes:>10} B {rec.engine or ''} {rec.sha or ''}")
    return out


def queryset_requests(path: str, date: str, canvas: str = "w=1408&h=896") -> list[tuple[str, str]]:
    """Every query × view of a bench query set (`dt_cloud.bench.queryset`) as a subtree request."""
    from ..bench import queryset

    q = urllib.parse.quote
    return [(f"{c.id}@{v or '/'}", f"/api/subtree?{canvas}&date={date}&path={q(v, safe='')}&q={q(c.q, safe='')}&qs={c.qs}&full=1")
            for c in queryset.load(path) for v in c.views]


def summary(recs: list[Rec]) -> list[dict]:
    by: dict[str, list[Rec]] = {}
    for r in recs:
        by.setdefault(r.name, []).append(r)
    out = []
    for name, rs in by.items():
        ms = [r.ms for r in rs if r.status == 200]
        out.append({"name": name, "n": len(rs), "ok": len(ms), "p50": statistics.median(ms) if ms else None, "max": max(ms) if ms else None,
                    "statuses": sorted({r.status for r in rs}), "bytes": max(r.bytes for r in rs), "engines": sorted({(r.engine or "").split(";")[0] for r in rs})})
    return out


def compare(a: list[Rec], b: list[Rec]) -> list[dict]:
    """Pair identical requests; count failed/non-JSON trials as unverified.

    Cache state and client concurrency may differ for an intentional A/B,
    but request names, trials and URL parameters must match exactly.
    """
    def index(rows: list[Rec]) -> dict[tuple[str, int], Rec]:
        if not rows:
            raise ValueError("an empty HTTP run cannot be compared")
        result = {}
        for row in rows:
            key = (row.name, row.trial)
            if key in result:
                raise ValueError(f"duplicate HTTP request case: {key}")
            result[key] = row
        return result

    cases_a, cases_b = index(a), index(b)
    if cases_a.keys() != cases_b.keys():
        raise ValueError("HTTP workloads differ; names and trials must match")
    for key, row in cases_a.items():
        if row.url != cases_b[key].url:
            raise ValueError(f"HTTP request parameters differ: {key}")
    sa = {r["name"]: r for r in summary(a)}
    sb = {r["name"]: r for r in summary(b)}
    out = []
    for name in sa:
        rs = [r for r in a if r.name == name]
        same = sum(1 for r in rs if r.status == cases_b[(r.name, r.trial)].status == 200
                   and r.sha is not None and cases_b[(r.name, r.trial)].sha == r.sha)
        out.append({"name": name, "a_p50": sa[name]["p50"], "a_max": sa[name]["max"], "b_p50": sb[name]["p50"],
                    "b_max": sb[name]["max"], "exact": f"{same}/{len(rs)}"})
    return out


def load(path: str) -> list[Rec]:
    with open(path) as f:
        return [Rec(**json.loads(line)) for line in f if line.strip()]


def dump(recs: list[Rec], path: str) -> None:
    with open(path, "a") as f:
        for r in recs:
            f.write(json.dumps(asdict(r)) + "\n")
