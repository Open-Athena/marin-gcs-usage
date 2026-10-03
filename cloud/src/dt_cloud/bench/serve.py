"""Load a serving-box engine once, then answer bench queries over HTTP
(`dt-cloud bench-serve`): what the suspend/resume and stop/start
experiments time (specs/serving-options.md), where `bench-engine`'s
load-then-score process can't outlive a VM's suspension.

- `GET /health` → `{"ok": true, "load": {…}, "up_s": …}` once loaded;
- `GET /run[?k=<id>…][&r=N]` → scores those query ids (default: the whole
  set) against the truth, one at a time: `{"tally", "latency", "results":
  [{id, view, verdict, ms}], "wall_ms"}`.

Requests are served one at a time (no threads), so each answer's latency is
the engine's alone.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def load_engine(
    engine: str,
    gen: str,
    *,
    index: str | None = None,
    stage: Path | None = None,
    evict: bool = False,
    threads: int = 16,
    mem: str = "48GB",
    tmp_dir: str | None = None,
    url: str | None = None,
    cold: bool = False,
) -> tuple[object, str, dict]:
    """(the engine's index object, `LocalEngine` kind, load stats)."""
    from . import local, mem as bm

    g = gen.rstrip("/")
    load: dict = {"rss_before": bm.rss()}
    t0 = time.monotonic()
    if engine == "mem":
        if not index:
            raise ValueError("-e mem needs an index dir")
        d = Path(index)
        if index.startswith("gs://"):
            if not stage:
                raise ValueError("a gs:// index needs a stage dir")
            d = stage / "mem-index"
            load["download"] = local.download_dir(index, d)
        if evict:
            bm.evict(d)
        t1 = time.monotonic()
        ix = bm.MemIndex.load(d, threads=threads)
        load["load_s"] = round(time.monotonic() - t1, 2)
        load["nbytes"] = ix.nbytes()
        load["nodes"] = ix.n
        load["names"] = len(ix.names)
        kind = "mem"
    elif engine == "duckdb":
        from . import duck

        path_file, names = f"{g}/path-index.parquet", f"{g}/path-index.names.parquet"
        if stage and g.startswith("gs://"):
            path_file, s1 = local.stage_file(path_file, stage / "path-index.parquet")
            names, s2 = local.stage_file(names, stage / "path-index.names.parquet")
            load["download"] = {"path_s": s1, "names_s": s2}
        if evict:
            bm.evict(Path(path_file).parent)
        ix = duck.DuckIndex(path_file, names, threads=threads, mem=mem, tmp=tmp_dir)
        load.update(ix.stats)
        load["duckdb_memory"] = ix.memory()
        kind = "duck"
    elif engine == "ch":
        from . import ch

        ix = ch.ChIndex(url or ch.DEFAULT_URL, threads=threads, cold=cold)
        load.update(ix.stats)
        kind = "ch"
    else:
        raise ValueError(f"unknown engine {engine!r}")
    load["total_s"] = round(time.monotonic() - t0, 2)
    load["rss"] = bm.rss()
    return ix, kind, load


def serve(eng, cases, truth, load: dict, host: str = "0.0.0.0", port: int = 8765) -> None:
    from . import local, score as bs

    by_id = {c.id: c for c in cases}
    t_up = time.monotonic()

    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):  # noqa: N802
            err(f"{self.address_string()} {fmt % a}")

        def _send(self, code: int, body: dict) -> None:
            b = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):  # noqa: N802
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            if u.path == "/health":
                return self._send(200, {"ok": True, "load": load, "up_s": round(time.monotonic() - t_up, 1)})
            if u.path != "/run":
                return self._send(404, {"error": "no such path"})
            ids = qs.get("k") or list(by_id)
            bad = [k for k in ids if k not in by_id]
            if bad:
                return self._send(400, {"error": f"unknown ids {bad}"})
            r = int((qs.get("r") or ["1"])[0])
            eng.timings.clear()
            t0 = time.monotonic()
            scores = bs.run(eng, [by_id[k] for k in ids], truth, repeat=r, log=err)
            wall = round((time.monotonic() - t0) * 1000)
            results = [{"id": s.id, "view": s.view, "verdict": s.verdict, "ms": s.wall, "roots": s.roots} for s in scores]
            return self._send(200, {
                "tally": bs.tally(scores), "latency": local.latency_summary(eng.timings), "results": results,
                "timings": [asdict(t) for t in eng.timings], "wall_ms": wall,
            })

    srv = HTTPServer((host, port), H)
    err(f"bench-serve: listening on {host}:{port}")
    srv.serve_forever()
