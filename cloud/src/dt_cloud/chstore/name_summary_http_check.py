"""Bounded whole-body acceptance using a pinned catalog and trusted cold oracles.

Only startup source-identity reads are permitted. Expected cold responses come
from explicit independently checked benchmark artifacts, never runtime.view.
Operator-supplied immutable references are trusted; hashes bind their bytes,
not their producer's honesty. No source oracle or scan is run by this checker.
"""

from copy import deepcopy
from hashlib import sha256
from json import dumps, loads
from math import isfinite
from os import O_CREAT, O_EXCL, O_NOFOLLOW, O_WRONLY, close, fchmod, fdopen, open as open_fd
from pathlib import Path
from statistics import median
from time import monotonic
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

from .dated_hot_l1_publish import load as load_dated
from .dated_name_summary import DatedNameSummaryRuntime
from .hot_l1_batch_catalog import REFERENCE_VALIDATIONS, _date, _entry, _literal
from .hot_l1_catalog import _unique_object
from .hot_l1_http import token_from_env
from .hot_l1_publish import load_pinned, pin
from .hot_l2_http_bench import fetch
from .name_summary import CAPS, NameSummaryRuntime, SourceBinding, _diff, _view

MAX_BYTES = 64 << 10
STAGES = frozenset(('vocabulary_s', 'postings_s', 'directory_roots_s', 'aggregate_s'))
VALIDATION = 'every whole deterministic HTTP body equals pinned catalog or trusted independent cold reference; exactly four finite nonnegative cold stage timings normalized; not a new source oracle or edge SLA'


def _stages(value: object) -> None:
    if (not isinstance(value, dict) or set(value) != STAGES or
            any(type(number) not in (int, float) or not isfinite(number) or number < 0 for number in value.values())):
        raise ValueError('name summary cold stages require exactly four finite nonnegative numeric timings')


def _reference(path: Path, binding: SourceBinding) -> tuple[tuple[str, str], dict, dict]:
    with path.open('rb') as source:
        raw = source.read((1 << 20) + 1)
    if len(raw) > 1 << 20:
        raise ValueError('name summary cold reference exceeds its 1 MiB input cap')
    body = loads(raw, object_pairs_hook=_unique_object)
    if (not isinstance(body, dict) or body.get('schema') != 'hot-l1-v1' or not isinstance(body.get('validation'), str) or
            body.get('validation') not in REFERENCE_VALIDATIONS or
            type(body.get('oracle_s')) not in (int, float) or not isfinite(body['oracle_s']) or body['oracle_s'] < 0):
        raise ValueError('name summary cold reference requires completed independent full-path frontier/leaf validation')
    day, pattern = _date(body.get('date')), _literal(body.get('pattern'))
    if (body.get('target') != binding.target or day not in dict(binding.dates) or
            body.get('snapshot_db') != dict(binding.dates)[day] or pattern != body['pattern']):
        raise ValueError('name summary cold reference target/date/snapshot/pattern differs from pinned source')
    entry = _entry(body, binding.target, day, 512)
    if tuple(sorted((row.pre, row.post, row.path) for row in entry.buckets)) != binding.buckets:
        raise ValueError('name summary cold reference bucket geometry differs from pinned source')
    bounds = body.get('work_bounds')
    if (not isinstance(bounds, dict) or set(bounds) != {'max_names', 'max_postings', 'max_outer_roots'} or
            any(type(number) is not int or number <= 0 for number in bounds.values())):
        raise ValueError('name summary cold reference requires explicit positive complete work bounds')
    fields = {'staged_vocabulary_names': ('max_names', CAPS['max_names']),
              'direct_matching_rows': ('max_postings', CAPS['max_postings']),
              'outer_directory_roots': ('max_outer_roots', CAPS['max_roots'])}
    for field, (cap, current) in fields.items():
        number = body.get(field)
        if type(number) is not int or not 0 <= number <= min(bounds[cap], current):
            raise ValueError('name summary cold reference exceeds declared or current work bounds')
    nonleaf = body.get('matching_nonleaf_rows')
    if (type(nonleaf) is not int or not 0 <= nonleaf <= body['direct_matching_rows'] or
            body['outer_directory_roots'] > nonleaf or
            (body['validation'] == 'complete independent full-path leaf scan' and nonleaf != 0)):
        raise ValueError('name summary cold reference has inconsistent directory/posting counts')
    _stages(body.get('stages'))
    body['work_bounds'] = {'max_names': CAPS['max_names'], 'max_postings': CAPS['max_postings'], 'max_outer_roots': CAPS['max_roots']}
    expected = _view(binding, body, 'bounded-name-postings', day, pattern)
    return (day, pattern), expected, {'sha256': sha256(raw).hexdigest(), 'bytes': len(raw), 'date': day,
                                    'pattern': pattern, 'validation': body['validation']}


def _canonical(body: object, expected: dict) -> str:
    actual = deepcopy(body)
    for path in (('before',), ('after',)) if expected['schema'] in ('name-summary-diff-v1', 'dated-name-summary-diff-v1') else ((),):
        reference, side = expected, actual
        for key in path:
            reference = reference[key]
            if not isinstance(side, dict) or key not in side:
                raise ValueError('name summary response lacks a complete comparison side')
            side = side[key]
        if reference.get('plan') == 'bounded-name-postings':
            if not isinstance(side, dict):
                raise ValueError('name summary response lacks a complete cold side')
            _stages(side.get('stages'))
            side['stages'] = dict.fromkeys(sorted(STAGES), '<timing>')
    return dumps(actual, sort_keys=True, separators=(',', ':'), allow_nan=False)


def check(
    generation_root: Path,
    ch_url: str,
    base: str,
    date: str,
    patterns: tuple[str, ...],
    out: Path,
    *,
    references: tuple[Path, ...] = (),
    compare_from: str | None = None,
    trials: int = 3,
    token_env: str = 'QUERY_BOX_TOKEN',
    timeout: float = 8,
    dated_generation_root: Path | None = None,
    logical_store: str | None = None,
) -> dict:
    if (dated_generation_root is None) != (logical_store is None):
        raise ValueError('name summary HTTP check dated publication root and logical store are required together')
    if (type(trials) is not int or not 1 <= trials <= 10 or type(timeout) not in (int, float) or
            not isfinite(timeout) or not 0 < timeout <= 8 or not 1 <= len(patterns) <= 16 or len(references) > 32):
        raise ValueError('name summary HTTP check requires 1..16 selected patterns, at most 32 refs, 1..10 trials and timeout in (0,8]')
    try:
        url = urlsplit(base)
        port = url.port
    except (ValueError, TypeError):
        raise ValueError('name summary HTTP check requires an explicit HTTP(S) origin without credentials/path/query/fragment') from None
    if (url.scheme not in ('http', 'https') or not url.hostname or url.username is not None or url.password is not None or
            url.path not in ('', '/') or url.query or url.fragment or port == 0):
        raise ValueError('name summary HTTP check requires an explicit HTTP(S) origin without credentials/path/query/fragment')
    if out.exists() or out.is_symlink() or not out.parent.is_dir():
        raise ValueError('name summary HTTP check output must be new in an existing directory')
    normalized = tuple(_literal(pattern) for pattern in patterns)
    if len(set(normalized)) != len(normalized):
        raise ValueError('name summary HTTP check selected patterns must be unique after normalization')
    dates = (_date(compare_from), _date(date)) if compare_from is not None else (_date(date),)
    if len(dates) == 2 and dates[0] >= dates[1]:
        raise ValueError('name summary HTTP check baseline must precede the selected scan')
    token = token_from_env(token_env)
    if not token or '\n' in token or '\r' in token:
        raise ValueError('name summary HTTP check requires a nonempty single-line bearer token environment variable')
    published = pin(generation_root)
    catalog = load_pinned(generation_root, published)
    runtime = NameSummaryRuntime.load(generation_root, ch_url, target=published['metadata']['target'], catalog=catalog, published=published)
    daily = load_dated(dated_generation_root) if dated_generation_root is not None else None
    if any(day not in runtime.registered and (daily is None or day not in daily.catalogs) for day in dates):
        raise ValueError('name summary HTTP check date is outside the pinned source')
    cold, provenance = {}, []
    for path in references:
        key, body, proof = _reference(path, runtime.binding)
        if key in cold:
            raise ValueError('name summary HTTP check has duplicate dated cold references')
        cold[key] = body
        provenance.append(proof)
    def legacy_view(
        day: str,
        pattern: str,
        *,
        path: str = '',
    ) -> dict:
        if path != '':
            raise ValueError('name summary HTTP check expected responses are root-only')
        if pattern in runtime.registered[day]:
            return _view(runtime.binding, catalog.view(day, pattern), 'catalog', day, pattern)
        body = cold.get((day, pattern))
        if body is None:
            raise ValueError('name summary HTTP check requires an independently accepted cold reference for every unregistered selected side')
        return deepcopy(body)
    def legacy_diff(
        before: str,
        after: str,
        pattern: str,
        *,
        path: str = '',
    ) -> dict:
        return _diff(legacy_view(before, pattern, path=path), legacy_view(after, pattern, path=path))
    pure_legacy = SimpleNamespace(binding=runtime.binding, catalog=catalog, metadata=runtime.metadata,
                                  view=legacy_view, diff=legacy_diff)
    reader = DatedNameSummaryRuntime(pure_legacy, daily, logical_store=logical_store,
                                     bucket_paths=tuple(row[2] for row in runtime.binding.buckets)) if daily is not None else pure_legacy
    expected = {}
    for pattern in normalized:
        body = reader.view(dates[0], pattern) if len(dates) == 1 else reader.diff(*dates, pattern)
        expected[pattern] = body, _canonical(body, body)
    start, latencies = monotonic(), []
    min_bytes, max_bytes = MAX_BYTES, 0
    def checked_response(
        path: str,
        body: dict,
        comparable: str,
    ) -> tuple[float, int]:
        try:
            status, ms, raw = fetch(base, path, token, timeout, max_bytes=MAX_BYTES)
        except Exception:
            raise RuntimeError('name summary HTTP transport failed or exceeded its byte/deadline bound') from None
        if status != 200:
            raise RuntimeError(f'name summary HTTP check requires HTTP 200; received status {status}')
        if len(raw) > MAX_BYTES:
            raise RuntimeError('name summary HTTP response exceeds its 64 KiB byte cap')
        if type(ms) not in (int, float) or not isfinite(ms) or not 0 <= ms <= timeout * 1000:
            raise RuntimeError('name summary HTTP response timing exceeds its deadline or is invalid')
        try:
            actual = loads(raw, object_pairs_hook=_unique_object)
            actual_canonical = _canonical(actual, body)
        except (ValueError, UnicodeError, TypeError, KeyError):
            raise RuntimeError('name summary HTTP response is not complete unique-key finite JSON with valid cold stage timings') from None
        if actual_canonical != comparable:
            raise AssertionError('name summary HTTP response differs from the complete pinned deterministic body')
        return ms, len(raw)
    registry_check = None
    if daily is not None:
        metadata = reader.metadata()
        ms, size = checked_response('/api/name-summary-registry', metadata, _canonical(metadata, metadata))
        registry_check = {'responses': 1, 'latency_ms': ms, 'bytes': size,
                          'validation': 'complete registry body equals pinned composite metadata'}
        min_bytes, max_bytes = min(min_bytes, size), max(max_bytes, size)
    for pattern in normalized:
        body, comparable = expected[pattern]
        query = {'date': date, 'name': pattern}
        if compare_from is not None:
            query['from'] = compare_from
        path, timings = '/api/name-summary?' + urlencode(query), []
        for _ in range(trials):
            ms, size = checked_response(path, body, comparable)
            timings.append(ms)
            min_bytes, max_bytes = min(min_bytes, size), max(max_bytes, size)
        latencies.append({'pattern': pattern, 'responses': trials, 'latency_ms': {'min': min(timings), 'median': median(timings), 'max': max(timings)}})
    result = {'schema': 'name-summary-http-check-v1', 'complete': True, 'target': runtime.target,
              'date': date, 'compare_from': compare_from, 'generation': published['generation'],
              'published_manifest_sha256': sha256(dumps(published, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
              'history_manifest_sha256': runtime.binding.manifest_sha256,
              'artifacts': published['artifacts'], 'cold_references': provenance,
              'patterns': len(normalized), 'trials': trials, 'responses': len(normalized) * trials,
              'timeout_seconds': timeout, 'max_response_bytes': MAX_BYTES, 'elapsed_s': monotonic() - start,
              'response_bytes': {'min': min_bytes, 'max': max_bytes}, 'per_pattern': latencies,
              'validation': VALIDATION, 'cache_state': 'uncontrolled; backend HTTP check, not browser/edge SLA'}
    if daily is not None:
        manifest = daily.manifest
        result.update(logical_store=logical_store, registry=registry_check,
                      responses=result['responses'] + 1,
                      dated_publication={'generation': manifest['generation'], 'artifacts': manifest['artifacts'],
                                         'proofs': manifest['proofs'], 'published_manifest_sha256': sha256(dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                                         'validation': 'whole-body parity against pinned dated reader/composition; not an independent source oracle'})
    owned, descriptor = False, None
    try:
        descriptor = open_fd(out, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        owned = True
        with fdopen(descriptor, 'w') as output:
            descriptor = None
            fchmod(output.fileno(), 0o600)
            output.write(dumps(result, allow_nan=False) + '\n')
    except BaseException:
        if descriptor is not None:
            close(descriptor)
        if owned:
            out.unlink()
        raise
    return result
