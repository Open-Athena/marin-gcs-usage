"""Complete-body HTTP acceptance against one pinned published local catalog.

No labels are removed. This verifies registered response parity, not an
independent scan of every query in the catalog. Tokens and response bodies
are never emitted in benchmark results or errors.
"""

from json import dumps, loads
from math import ceil, isfinite
from pathlib import Path
from statistics import median
from urllib.parse import urlencode, urlsplit

from .bench import fetch
from .hot_l1_catalog import _unique_object
from .hot_l1_http import token_from_env
from .hot_l1_publish import load


def bench(
    root: Path,
    base: str,
    date: str,
    patterns: tuple[str, ...],
    out: Path,
    *,
    token_env: str,
    compare_from: str | None = None,
    trials: int = 3,
    timeout: float = 30,
    all_registered: bool = False,
) -> dict:
    if type(trials) is not int or trials < 1 or type(timeout) not in (int, float) or not isfinite(timeout) or timeout <= 0:
        raise ValueError("hot L1 HTTP benchmark requires positive trials and a finite positive timeout")
    if type(all_registered) is not bool:
        raise ValueError("HTTP benchmark all_registered must be a boolean")
    if bool(patterns) == all_registered:
        raise ValueError("HTTP benchmark requires either explicit patterns or all_registered, not both")
    url = urlsplit(base)
    if url.scheme not in ("http", "https") or not url.hostname or url.username is not None or url.password is not None or url.query or url.fragment:
        raise ValueError("HTTP benchmark requires an explicit HTTP(S) base URL without credentials/query/fragment")
    if out.exists():
        raise ValueError("hot L1 HTTP benchmark output must be new")
    token = token_from_env(token_env)
    if not token or "\n" in token or "\r" in token:
        raise ValueError("HTTP benchmark requires a nonempty single-line bearer token environment variable")
    catalog = load(root)
    if all_registered:
        patterns = catalog.registered_patterns(date)
    expected = [(pattern, catalog.diff(compare_from, date, pattern) if compare_from is not None else catalog.view(date, pattern)) for pattern in patterns]
    records = []
    for pattern, body in expected:
        query = {"date": date, "name": pattern}
        if compare_from is not None:
            query["from"] = compare_from
        path = "/api/hot-l1?" + urlencode(query)
        comparable = dumps(body, sort_keys=True, separators=(",", ":"))
        times, sizes = [], []
        for _ in range(trials):
            status, ms, _, data = fetch(base, path, token, timeout)
            if status != 200:
                raise RuntimeError(f"hot L1 HTTP benchmark requires HTTP 200; received status {status}")
            actual = loads(data, object_pairs_hook=_unique_object)
            if dumps(actual, sort_keys=True, separators=(",", ":")) != comparable:
                raise AssertionError("hot L1 HTTP response disagrees with the exact registered body")
            times.append(ms)
            sizes.append(len(data))
        ordered = sorted(times)
        records.append({"pattern": pattern, "status": 200, "responses": trials,
                        "latency_ms": {"samples": times, "median": median(times), "p90": ordered[ceil(.9 * trials) - 1], "max": max(times)},
                        "response_bytes": {"samples": sizes, "min": min(sizes), "max": max(sizes)}})
    result = {"schema": "hot-l1-http-bench-v1", "base_url": base, "catalog_root": str(root), "date": date, "compare_from": compare_from,
              "trials": trials, "responses": trials * len(patterns), "timeout_seconds": timeout, "results": records,
              "validation": "every HTTP 200 body exactly equals the pinned registered reader; not an independent full-catalog scan",
              "p90_method": "nearest rank", "cache_state": "uncontrolled; resident root-only registered L1 HTTP responses"}
    if all_registered:
        result["all_registered"] = True
    output = out.open("x")
    try:
        with output:
            output.write(dumps(result) + "\n")
    except BaseException:
        out.unlink()
        raise
    return result
