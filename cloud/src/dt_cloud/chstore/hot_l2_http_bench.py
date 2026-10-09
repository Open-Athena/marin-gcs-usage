"""Bounded scan-free whole-response parity for one pinned paired L2 catalog."""

from array import array
from hashlib import sha256
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from json import dumps, loads
from math import ceil, isfinite
from pathlib import Path
from statistics import median
from time import monotonic
from urllib.parse import urlencode, urlsplit

from .hot_l1_catalog import _unique_object
from .hot_l1_http import token_from_env
from .hot_l2_pair_catalog import HotL2PairCatalog

MAX_BYTES = 256 << 10


def fetch(
    base: str,
    path: str,
    token: str,
    timeout: float,
    *,
    max_bytes: int = MAX_BYTES,
) -> tuple[int, float, bytes]:
    """No redirects; cumulative read cap and total deadline, not per-read renewal."""
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_BYTES:
        raise ValueError('HTTP response byte cap must be a positive integer at most 256 KiB')
    url = urlsplit(base)
    connection = (HTTPSConnection if url.scheme == 'https' else HTTPConnection)(url.hostname, url.port, timeout=timeout)
    start, response = monotonic(), None
    try:
        connection.request('GET', path, headers={'Authorization': f'Bearer {token}', 'User-Agent': 'dt-cloud-hot-l2-http-bench'})
        socket = connection.sock
        remaining = timeout - (monotonic() - start)
        if remaining <= 0:
            raise TimeoutError()
        socket.settimeout(remaining)
        response = connection.getresponse()
        if response.status != 200:
            return response.status, (monotonic() - start) * 1000, b''
        data = bytearray()
        while True:
            remaining = timeout - (monotonic() - start)
            if remaining <= 0:
                raise TimeoutError()
            socket.settimeout(remaining)
            chunk = response.read1(min(65536, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > max_bytes:
                raise RuntimeError('paired L2 HTTP response exceeds its 256 KiB byte cap' if max_bytes == MAX_BYTES else 'HTTP response exceeds its selected byte cap')
            if response.isclosed():
                break
        elapsed = monotonic() - start
        if elapsed > timeout:
            raise TimeoutError()
        return response.status, elapsed * 1000, bytes(data)
    except (OSError, HTTPException, ValueError):
        raise RuntimeError('paired L2 HTTP transport failed or exceeded its deadline') from None
    finally:
        if response is not None:
            response.close()
        connection.close()


def bench(
    artifact: Path,
    check: Path,
    base: str,
    date: str,
    patterns: tuple[str, ...],
    out: Path,
    *,
    token_env: str,
    paths: tuple[str, ...] = (),
    compare_from: str | None = None,
    trials: int = 1,
    timeout: float = 30,
    all_registered: bool = False,
) -> dict:
    if type(trials) is not int or not 1 <= trials <= 100 or type(timeout) not in (int, float) or not isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError('paired L2 HTTP benchmark requires 1..100 trials and a finite timeout in (0,30]')
    if type(all_registered) is not bool or bool(patterns) == all_registered:
        raise ValueError('paired L2 HTTP benchmark requires explicit patterns or all_registered, not both')
    try:
        url = urlsplit(base)
        port = url.port
    except (ValueError, TypeError):
        raise ValueError('paired L2 HTTP benchmark requires an explicit HTTP(S) origin without credentials/path/query/fragment') from None
    if url.scheme not in ('http', 'https') or not url.hostname or url.username is not None or url.password is not None or url.path not in ('', '/') or url.query or url.fragment or port == 0:
        raise ValueError('paired L2 HTTP benchmark requires an explicit HTTP(S) origin without credentials/path/query/fragment')
    if out.exists() or out.is_symlink() or not out.parent.is_dir():
        raise ValueError('paired L2 HTTP benchmark output must be new in an existing directory')
    token = token_from_env(token_env)
    if not token or '\n' in token or '\r' in token:
        raise ValueError('paired L2 HTTP benchmark requires a nonempty single-line bearer token environment variable')
    catalog = HotL2PairCatalog.load(artifact, check)
    selected = catalog.patterns if all_registered else patterns
    buckets = paths or catalog.paths
    # Validate selections without materializing H-by-bucket response bodies.
    normalized = tuple(catalog._key(date, pattern, catalog.paths[0])[1] for pattern in selected)
    if len(set(normalized)) != len(normalized) or len(set(buckets)) != len(buckets):
        raise ValueError('paired L2 HTTP benchmark selections must be unique')
    for path in buckets:
        catalog._key(date, normalized[0], path)
    if compare_from is not None:
        catalog._key(compare_from, normalized[0], buckets[0])
    start = monotonic()
    timings, min_bytes, max_bytes = array('d'), MAX_BYTES, 0
    for pattern in normalized:
        for bucket in buckets:
            body = catalog.view(date, pattern, path=bucket) if compare_from is None else catalog.diff(compare_from, date, pattern, path=bucket)
            expected = dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False)
            query = {'date': date, 'name': pattern, 'path': bucket}
            if compare_from is not None:
                query['from'] = compare_from
            path = '/api/hot-l2?' + urlencode(query)
            for _ in range(trials):
                status, ms, data = fetch(base, path, token, timeout)
                if status != 200:
                    raise RuntimeError(f'paired L2 HTTP benchmark requires HTTP 200; received status {status}')
                if len(data) > MAX_BYTES:
                    raise RuntimeError('paired L2 HTTP response exceeds its 256 KiB byte cap')
                try:
                    actual = loads(data, object_pairs_hook=_unique_object)
                    comparable = dumps(actual, sort_keys=True, separators=(',', ':'), allow_nan=False)
                except (ValueError, UnicodeError):
                    raise RuntimeError('paired L2 HTTP response is not unique-key finite JSON') from None
                if comparable != expected:
                    raise AssertionError('paired L2 HTTP response differs from the complete pinned catalog body')
                if type(ms) not in (int, float) or not isfinite(ms) or ms < 0:
                    raise RuntimeError('paired L2 HTTP response timing is invalid')
                timings.append(ms)
                min_bytes, max_bytes = min(min_bytes, len(data)), max(max_bytes, len(data))
    ordered = sorted(timings)
    proof = catalog.view(date, normalized[0], path=buckets[0])['validation']
    result = {'schema': 'hot-l2-http-bench-v1', 'complete': True, 'target': catalog.target, 'date': date, 'compare_from': compare_from,
              'artifact_sha256': proof['artifact_sha256'], 'artifact_bytes': proof['artifact_bytes'],
              'patterns': len(normalized), 'buckets': len(buckets), 'trials': trials, 'responses': len(timings), 'all_registered': all_registered,
              'selection_sha256': sha256(dumps([normalized, buckets], separators=(',', ':')).encode()).hexdigest(),
              'timeout_seconds': timeout, 'max_response_bytes': MAX_BYTES, 'elapsed_s': monotonic() - start,
              'latency_ms': {'min': min(timings), 'median': median(timings), 'p90': ordered[ceil(.9 * len(ordered)) - 1], 'max': max(timings)},
              'response_bytes': {'min': min_bytes, 'max': max_bytes}, 'p90_method': 'nearest rank',
              'validation': 'every complete parsed HTTP body equals the pinned catalog; not independent source truth',
              'cache_state': 'uncontrolled; scan-free resident-catalog HTTP parity, not browser/edge or rich-query latency'}
    owned = False
    try:
        with out.open('x') as output:
            owned = True
            output.write(dumps(result) + '\n')
    except BaseException:
        if owned:
            out.unlink()
        raise
    return result
