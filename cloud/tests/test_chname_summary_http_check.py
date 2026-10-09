"""Exact stitched HTTP acceptance without a cold runtime/source scan."""

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from json import dumps, loads
from os import umask
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import name_summary_http_check as module
from dt_cloud.chstore import hot_l2_http_bench as transport
from dt_cloud.chstore.hot_l1_batch_catalog import HotL1BatchCatalog
from dt_cloud.chstore.hot_l1_catalog import SCOPE
from dt_cloud.chstore.name_summary import NameSummaryRuntime, SourceBinding
from test_chdated_name_summary import CAPABILITIES, REGISTRY, raw_daily
from test_chhot_l1_batch_catalog import artifact

BASE, CH, ENV, TOKEN = 'http://fixture.invalid:8082', 'http://fixture.invalid:8123', 'SUMMARY_FIXTURE_TOKEN', 'private-token'
BEFORE, AFTER = '2026-10-04', '2026-10-05'
NEW = '2026-10-06'
TIMINGS = {'vocabulary_s': .1, 'postings_s': .2, 'directory_roots_s': .3, 'aggregate_s': .4}


def cold_body(day: str, pattern: str) -> dict:
    sample = artifact(day)
    return {'schema': 'hot-l1-v1', 'target': 'fleet', 'snapshot_db': sample['snapshot_db'],
            'date': day, 'pattern': pattern, 'exact': True, 'incremental': False, 'scope': SCOPE,
            'root': sample['results'][0]['root'], 'buckets': sample['results'][0]['buckets'],
            'staged_vocabulary_names': 2, 'direct_matching_rows': 3, 'matching_nonleaf_rows': 1,
            'outer_directory_roots': 1, 'all_buckets_covered': False,
            'work_bounds': {'max_names': 200000, 'max_postings': 100100, 'max_outer_roots': 100100},
            'stages': dict(TIMINGS), 'build_s': .7, 'oracle_s': 1.5,
            'validation': 'complete independent full-path frontier scan',
            'limits': {'memory_gib': 4, 'spill_gib': 4, 'seconds': 60, 'threads': 4}}


def side(state, day: str, pattern: str) -> dict:
    hot = pattern in state.runtime.registered[day]
    source = state.catalog.view(day, pattern) if hot else cold_body(day, pattern)
    result = {'schema': 'name-summary-v1', 'target': 'fleet', 'date': day, 'pattern': pattern,
              'path': '', 'exact': True, 'incremental': False, 'levels': 1, 'scope': SCOPE,
              'plan': 'catalog' if hot else 'bounded-name-postings',
              'root': deepcopy(source['root']), 'buckets': deepcopy(source['buckets']),
              'source': 'registered precomputed batch artifact' if hot else 'bounded dated name postings; directory rollups are atomic',
              'validation': {'source_prefix_proofs_checked': True, 'independent_query_source_oracle': False,
                             'description': 'pinned catalog validation' if hot else 'bounded exact first-hit coverage; no per-request source oracle'},
              'source_identity': {'generation': 'a' * 32, 'snapshot_db': 'snapshot_' + day.replace('-', ''), 'history_manifest_sha256': 'b' * 64}}
    if hot:
        result['validation']['catalog'] = source['validation']
    else:
        result.update(work_bounds={'max_names': 200000, 'max_postings': 100000, 'max_outer_roots': 100000},
                      direct_matching_rows=3, stages={key: number + 10 for key, number in TIMINGS.items()})
    return result


def response(state, day: str, pattern: str, before: str | None) -> dict:
    after = side(state, day, pattern)
    if before is None:
        return after
    left = side(state, before, pattern)
    weights = lambda a, b: {key: b[key] - a[key] for key in ('b', 'o')}
    return {'schema': 'name-summary-diff-v1', 'target': 'fleet', 'pattern': pattern, 'path': '',
            'exact': True, 'incremental': False, 'levels': 1, 'scope': SCOPE, 'before': left, 'after': after,
            'delta': weights(left['root'], after['root']),
            'buckets': [{'pre': a['pre'], 'post': a['post'], 'path': a['path'],
                         'before': {key: a[key] for key in ('b', 'o')}, 'after': {key: b[key] for key in ('b', 'o')},
                         'delta': weights(a, b)} for a, b in zip(left['buckets'], after['buckets'], strict=True)]}


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bodies = [artifact(BEFORE), artifact(AFTER)]
    bodies[1]['results'][1]['pattern'] = 'cold'
    catalog = HotL1BatchCatalog.from_bytes(dumps(body).encode() for body in bodies)
    binding = SourceBinding('fleet', ((BEFORE, 'snapshot_20261004'), (AFTER, 'snapshot_20261005')),
                            ((1, 4, 'a'), (5, 8, 'b')), 'a' * 32, 'b' * 64)
    runtime = NameSummaryRuntime(catalog, binding, CH)
    published = {'generation': 'a' * 32, 'metadata': catalog.metadata(),
                 'artifacts': [{'file': 'generations/' + 'a' * 32 + '/artifact-0001.json', 'sha256': 'c' * 64, 'bytes': 100}]}
    state = SimpleNamespace(catalog=catalog, runtime=runtime, published=published, calls=[], raw=None, change=None,
                            status=200, ms=10., received=[], refs=[])
    for day, pattern in [(BEFORE, 'plain'), (AFTER, 'plain'), (BEFORE, 'cold')]:
        path = tmp_path / (day + '-' + pattern + '.json')
        path.write_text(dumps(cold_body(day, pattern)) + '\n')
        state.refs.append(path)
    def pin(root):
        state.calls.append(('pin', root))
        return published
    def load(root, manifest):
        state.calls.append(('catalog', root, manifest))
        return catalog
    def bind(root, url, **kwargs):
        state.calls.append(('bind', root, url, kwargs))
        return runtime
    def forbidden(*args, **kwargs):
        raise AssertionError('runtime.view/diff must never construct expected responses')
    def fetch(base, path, token, timeout, *, max_bytes):
        state.calls.append(('fetch', base, path, token, timeout, max_bytes))
        query = dict(parse_qsl(urlsplit(path).query))
        body = response(state, query['date'], query['name'], query.get('from'))
        if state.change:
            state.change(body)
        raw = state.raw if state.raw is not None else dumps(body).encode()
        state.received.append(raw)
        return state.status, state.ms, raw
    monkeypatch.setenv(ENV, TOKEN)
    monkeypatch.setattr(module, 'pin', pin)
    monkeypatch.setattr(module, 'load_pinned', load)
    monkeypatch.setattr(module.NameSummaryRuntime, 'load', bind)
    monkeypatch.setattr(runtime, 'view', forbidden)
    monkeypatch.setattr(runtime, 'diff', forbidden)
    monkeypatch.setattr(module, 'fetch', fetch)
    monkeypatch.setattr(module, 'monotonic', lambda: 1.)
    return state


def run(fake, tmp_path, **kwargs):
    return module.check(tmp_path, CH, BASE, AFTER, ('.JSON', 'PLAIN', 'COLD'), tmp_path / 'check.json',
                        references=tuple(fake.refs), token_env=ENV, **kwargs)


@pytest.mark.parametrize('before', [None, BEFORE])
def test_complete_hot_cold_and_mixed_diffs_precise_compact_result(fake, tmp_path: Path, before) -> None:
    previous = umask(0o002)
    try:
        actual = run(fake, tmp_path, compare_from=before, trials=2)
    finally:
        umask(previous)
    refs = [{'sha256': sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size,
             'date': day, 'pattern': pattern, 'validation': 'complete independent full-path frontier scan'}
            for path, (day, pattern) in zip(fake.refs, [(BEFORE, 'plain'), (AFTER, 'plain'), (BEFORE, 'cold')], strict=True)]
    expected = {'schema': 'name-summary-http-check-v1', 'complete': True, 'target': 'fleet', 'date': AFTER, 'compare_from': before,
                'generation': 'a' * 32, 'published_manifest_sha256': sha256(dumps(fake.published, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                'history_manifest_sha256': 'b' * 64, 'artifacts': fake.published['artifacts'], 'cold_references': refs,
                'patterns': 3, 'trials': 2, 'responses': 6, 'timeout_seconds': 8, 'max_response_bytes': 65536, 'elapsed_s': 0.,
                'response_bytes': {'min': min(map(len, fake.received)), 'max': max(map(len, fake.received))},
                'per_pattern': [{'pattern': name, 'responses': 2, 'latency_ms': {'min': 10., 'median': 10., 'max': 10.}} for name in ('.json', 'plain', 'cold')],
                'validation': 'every whole deterministic HTTP body equals pinned catalog or trusted independent cold reference; exactly four finite nonnegative cold stage timings normalized; not a new source oracle or edge SLA',
                'cache_state': 'uncontrolled; backend HTTP check, not browser/edge SLA'}
    assert actual == expected
    assert loads((tmp_path / 'check.json').read_bytes()) == expected
    assert (tmp_path / 'check.json').stat().st_mode & 0o777 == 0o600
    assert fake.calls[:3] == [('pin', tmp_path), ('catalog', tmp_path, fake.published),
                             ('bind', tmp_path, CH, {'target': 'fleet', 'catalog': fake.catalog, 'published': fake.published})]
    suffix = '&from=' + BEFORE if before else ''
    assert fake.calls[3:] == [('fetch', BASE, f'/api/name-summary?date={AFTER}&name={name}{suffix}', TOKEN, 8, 65536)
                             for name in ('.json', 'plain', 'cold') for _ in range(2)]


@pytest.mark.parametrize('change', [
    lambda b: b.update(extra='new'), lambda b: b['root'].update(o=99),
    lambda b: b['source_identity'].update(generation='d' * 32),
    lambda b: b['validation'].update(independent_query_source_oracle=True),
    lambda b: b['work_bounds'].update(max_postings=100100),
    lambda b: b.update(direct_matching_rows=3.0),
])
def test_whole_deterministic_fields_never_stripped(fake, tmp_path: Path, change) -> None:
    fake.change = change
    with pytest.raises(AssertionError) as caught:
        module.check(tmp_path, CH, BASE, AFTER, ('plain',), tmp_path / 'check.json', references=tuple(fake.refs), token_env=ENV)
    assert str(caught.value) == 'name summary HTTP response differs from the complete pinned deterministic body'
    assert len(fake.received) == 1
    assert (tmp_path / 'check.json').exists() is False


@pytest.mark.parametrize('stages', [None, {}, {**TIMINGS, 'extra': 1}, {**TIMINGS, 'postings_s': True},
                                    {**TIMINGS, 'postings_s': -1}, {**TIMINGS, 'postings_s': float('inf')},
                                    {**TIMINGS, 'postings_s': '1'}])
def test_stage_normalization_only_exact_four_finite_nonnegative_numbers(fake, tmp_path: Path, stages) -> None:
    fake.change = lambda body: body.update(stages=stages)
    with pytest.raises(RuntimeError) as caught:
        module.check(tmp_path, CH, BASE, AFTER, ('plain',), tmp_path / 'check.json', references=tuple(fake.refs), token_env=ENV)
    assert str(caught.value) == 'name summary HTTP response is not complete unique-key finite JSON with valid cold stage timings'
    assert (tmp_path / 'check.json').exists() is False


def test_hot_stages_are_not_variable_fields(fake, tmp_path: Path) -> None:
    fake.change = lambda body: body.update(stages=TIMINGS)
    with pytest.raises(AssertionError) as caught:
        run(fake, tmp_path)
    assert str(caught.value) == 'name summary HTTP response differs from the complete pinned deterministic body'


@pytest.mark.parametrize('change', [
    lambda b: b['delta'].update(o=0),
    lambda b: b['buckets'][0]['before'].update(b=999),
    lambda b: b['before']['work_bounds'].update(max_outer_roots=100100),
    lambda b: b['after'].update(stages=TIMINGS),
])
def test_mixed_diff_preserves_complete_both_sides_delta_and_metadata(fake, tmp_path: Path, change) -> None:
    fake.change = change
    with pytest.raises(AssertionError) as caught:
        module.check(tmp_path, CH, BASE, AFTER, ('cold',), tmp_path / 'check.json', references=tuple(fake.refs), token_env=ENV, compare_from=BEFORE)
    assert str(caught.value) == 'name summary HTTP response differs from the complete pinned deterministic body'
    assert (tmp_path / 'check.json').exists() is False


def test_independent_leaf_reference_with_no_matching_dirs_is_accepted(fake, tmp_path: Path) -> None:
    for path in fake.refs:
        body = loads(path.read_bytes())
        body.update(validation='complete independent full-path leaf scan', matching_nonleaf_rows=0, outer_directory_roots=0)
        path.write_text(dumps(body))
    result = run(fake, tmp_path, compare_from=BEFORE, trials=1)
    assert [row['validation'] for row in result['cold_references']] == ['complete independent full-path leaf scan'] * 3
    assert result['responses'] == 3


def test_all_missing_refs_preflight_before_first_http(fake, tmp_path: Path) -> None:
    fake.refs = []
    with pytest.raises(ValueError) as caught:
        run(fake, tmp_path, compare_from=BEFORE)
    assert str(caught.value) == 'name summary HTTP check requires an independently accepted cold reference for every unregistered selected side'
    assert [row[0] for row in fake.calls] == ['pin', 'catalog', 'bind']
    assert fake.received == []


@pytest.mark.parametrize('change,message', [
    (lambda b: b.update(validation='exact agreement with accepted native batch reference; not independent full-source oracle'), 'name summary cold reference requires completed independent full-path frontier/leaf validation'),
    (lambda b: b.update(snapshot_db='wrong'), 'name summary cold reference target/date/snapshot/pattern differs from pinned source'),
    (lambda b: b['root'].update(b=999), 'batch catalog root totals disagree with complete buckets'),
    (lambda b: b.update(direct_matching_rows=100001), 'name summary cold reference exceeds declared or current work bounds'),
    (lambda b: b.update(validation='complete independent full-path leaf scan'), 'name summary cold reference has inconsistent directory/posting counts'),
    (lambda b: b.update(validation=[]), 'name summary cold reference requires completed independent full-path frontier/leaf validation'),
    (lambda b: b.pop('work_bounds'), 'name summary cold reference requires explicit positive complete work bounds'),
    (lambda b: b.update(oracle_s=True), 'name summary cold reference requires completed independent full-path frontier/leaf validation'),
    (lambda b: b.update(outer_directory_roots=2), 'name summary cold reference has inconsistent directory/posting counts'),
    (lambda b: b['stages'].update(extra=1), 'name summary cold stages require exactly four finite nonnegative numeric timings'),
])
def test_invalid_ref_provenance_counts_or_totals_refused_before_http(fake, tmp_path: Path, change, message) -> None:
    body = loads(fake.refs[0].read_bytes())
    change(body)
    fake.refs[0].write_text(dumps(body))
    with pytest.raises(ValueError) as caught:
        run(fake, tmp_path)
    assert str(caught.value) == message
    assert fake.received == []


@pytest.mark.parametrize('raw,message', [
    (b'x' * (65536 + 1), 'name summary HTTP response exceeds its 64 KiB byte cap'),
    (b'{"a":1,"a":2}', 'name summary HTTP response is not complete unique-key finite JSON with valid cold stage timings'),
    (b'private invalid payload', 'name summary HTTP response is not complete unique-key finite JSON with valid cold stage timings'),
])
def test_invalid_or_oversized_responses_sanitized(fake, tmp_path: Path, raw, message) -> None:
    fake.raw = raw
    with pytest.raises(RuntimeError) as caught:
        run(fake, tmp_path)
    assert str(caught.value) == message
    assert (tmp_path / 'check.json').exists() is False


@pytest.mark.parametrize('status', [401, 503])
def test_failure_status_not_accepted(fake, tmp_path: Path, status: int) -> None:
    fake.status = status
    with pytest.raises(RuntimeError) as caught:
        run(fake, tmp_path)
    assert str(caught.value) == f'name summary HTTP check requires HTTP 200; received status {status}'


@pytest.mark.parametrize('ms', [True, -1, 8001, float('nan')])
def test_response_deadline_timing_refused(fake, tmp_path: Path, ms) -> None:
    fake.ms = ms
    with pytest.raises(RuntimeError) as caught:
        run(fake, tmp_path)
    assert str(caught.value) == 'name summary HTTP response timing exceeds its deadline or is invalid'


@pytest.mark.parametrize('override', [{'trials': True}, {'trials': 11}, {'timeout': 9}, {'timeout': False}, {'compare_from': AFTER}])
def test_invalid_limits_before_binding(fake, tmp_path: Path, override) -> None:
    with pytest.raises(ValueError):
        run(fake, tmp_path, **override)
    assert fake.calls == []


def test_output_existing_refused_before_binding(fake, tmp_path: Path) -> None:
    path = tmp_path / 'check.json'
    path.write_text('unchanged\n')
    with pytest.raises(ValueError) as caught:
        run(fake, tmp_path)
    assert str(caught.value) == 'name summary HTTP check output must be new in an existing directory'
    assert path.read_text() == 'unchanged\n'
    assert fake.calls == []


@pytest.mark.parametrize('base', ['http://user:private@host', 'http://host/api/name-summary', 'http://host?from=hidden', 'https://host#fragment', 'ftp://host', 'http://host:0'])
def test_no_unsupported_url_selections_before_binding(fake, tmp_path: Path, base: str) -> None:
    with pytest.raises(ValueError) as caught:
        module.check(tmp_path, CH, base, AFTER, ('.json',), tmp_path / 'check.json', token_env=ENV)
    assert str(caught.value) == 'name summary HTTP check requires an explicit HTTP(S) origin without credentials/path/query/fragment'
    assert fake.calls == []


@pytest.mark.parametrize('patterns', [('.JSON', '.json'), ('bad/name',), ('',), ('\0',)])
def test_invalid_literal_selections_before_binding(fake, tmp_path: Path, patterns) -> None:
    with pytest.raises(ValueError):
        module.check(tmp_path, CH, BASE, AFTER, patterns, tmp_path / 'check.json', token_env=ENV)
    assert fake.calls == []


def test_unknown_date_refused_before_http(fake, tmp_path: Path) -> None:
    with pytest.raises(ValueError) as caught:
        module.check(tmp_path, CH, BASE, '2026-10-06', ('.json',), tmp_path / 'check.json', token_env=ENV)
    assert str(caught.value) == 'name summary HTTP check date is outside the pinned source'
    assert [row[0] for row in fake.calls] == ['pin', 'catalog', 'bind']
    assert fake.received == []


@pytest.mark.parametrize('token', ['', 'private\nvalue'])
def test_invalid_token_before_binding(fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setenv(ENV, token)
    with pytest.raises(ValueError) as caught:
        run(fake, tmp_path)
    assert str(caught.value) == 'name summary HTTP check requires a nonempty single-line bearer token environment variable'
    assert fake.calls == []


def test_transport_error_detail_sanitized(fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError('private token/response exception')
    monkeypatch.setattr(module, 'fetch', fail)
    with pytest.raises(RuntimeError) as caught:
        run(fake, tmp_path)
    assert (str(caught.value), caught.value.__suppress_context__) == ('name summary HTTP transport failed or exceeded its byte/deadline bound', True)
    assert (tmp_path / 'check.json').exists() is False


def test_small_fetch_cap_stops_at_first_excess_byte(monkeypatch: pytest.MonkeyPatch) -> None:
    reads, calls = [], []
    source = BytesIO(b'x' * (256 << 10))
    class Response:
        status = 200
        def read1(self, size):
            reads.append(size)
            return source.read(size)
        def isclosed(self):
            return False
        def close(self):
            calls.append('response-close')
    class Connection:
        sock = SimpleNamespace(settimeout=lambda seconds: None)
        def __init__(self, *args, **kwargs):
            calls.append('connection')
        def request(self, *args, **kwargs):
            calls.append('request')
        def getresponse(self):
            return Response()
        def close(self):
            calls.append('connection-close')
    monkeypatch.setattr(transport, 'HTTPConnection', Connection)
    monkeypatch.setattr(transport, 'monotonic', lambda: 1.)
    with pytest.raises(RuntimeError) as caught:
        transport.fetch(BASE, '/api/name-summary', TOKEN, 8, max_bytes=65536)
    assert str(caught.value) == 'HTTP response exceeds its selected byte cap'
    assert reads == [65536, 1]
    assert source.tell() == 65537
    assert calls == ['connection', 'request', 'response-close', 'connection-close']


@pytest.mark.parametrize('cap', [True, 0, -1, 262145, 1.5])
def test_invalid_fetch_cap_before_connection(monkeypatch: pytest.MonkeyPatch, cap) -> None:
    calls = []
    monkeypatch.setattr(transport, 'HTTPConnection', lambda *a, **kw: calls.append('connection'))
    with pytest.raises(ValueError) as caught:
        transport.fetch(BASE, '/api/name-summary', TOKEN, 8, max_bytes=cap)
    assert str(caught.value) == 'HTTP response byte cap must be a positive integer at most 256 KiB'
    assert calls == []


@pytest.mark.parametrize('dated', [False, True])
def test_cli_exact_forwarding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dated: bool) -> None:
    from dt_cloud.cli import main
    calls = []
    def check(*args, **kwargs):
        calls.append((args, kwargs))
        return {'schema': 'fixture', 'responses': 6}
    monkeypatch.setattr(module, 'check', check)
    result = CliRunner().invoke(main, ['ch-name-summary-http-check', '-c', CH, '-d', AFTER, '-D', BEFORE,
                                     '-g', str(tmp_path), '-n', '.JSON', '-n', 'plain', '-o', str(tmp_path / 'out.json'),
                                     '-r', str(tmp_path / 'before.json'), '-r', str(tmp_path / 'after.json'), '-U', BASE, '-T', ENV,
                                     *(['-G', str(tmp_path / 'daily'), '-f', 'gcs_fleet'] if dated else [])])
    assert result.exit_code == 0
    assert result.output == '{"schema": "fixture", "responses": 6}\n'
    assert calls == [((tmp_path, CH, BASE, AFTER, ('.JSON', 'plain'), tmp_path / 'out.json'),
                      {'references': (tmp_path / 'before.json', tmp_path / 'after.json'), 'compare_from': BEFORE,
                       'trials': 3, 'token_env': ENV, 'timeout': 8.,
                       'dated_generation_root': tmp_path / 'daily' if dated else None,
                       'logical_store': 'gcs_fleet' if dated else None})]


@pytest.fixture
def dated(fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest = {'schema': 'dated-hot-l1-published-generation-v1', 'complete': True, 'generation': 'd' * 32,
                'logical_store': 'gcs_fleet', 'bucket_paths': ['a', 'b'], 'dates': [NEW],
                'artifacts': [{'date': NEW, 'file': 'daily/artifact.json', 'sha256': 'e' * 64, 'bytes': 123}],
                'proofs': [{'date': NEW, 'file': 'daily/proof.json', 'sha256': 'f' * 64, 'bytes': 100}]}
    def view(day, pattern):
        fake.calls.append(('daily.view', day, pattern))
        return raw_daily(day, pattern)
    catalog = SimpleNamespace(date=NEW, logical_store='gcs_fleet', paths=('a', 'b'),
                              selection=SimpleNamespace(patterns=('.json', 'plain', 'cold')), view=view,
                              metadata=lambda: {'registry': deepcopy(REGISTRY), 'source': deepcopy(raw_daily()['source'])})
    fake.daily = SimpleNamespace(manifest=manifest, catalogs={NEW: catalog})
    def load(root):
        fake.calls.append(('daily.load', root))
        return fake.daily
    monkeypatch.setattr(module, 'load_dated', load)
    fake.expected_registry = {'schema': 'dated-name-summary-registry-v1', 'logical_store': 'gcs_fleet',
        'bucket_paths': ['a', 'b'], 'dates': [
            {'date': day, 'plans': ['catalog', 'bounded-name-postings'], 'kind': 'frozen-history',
             'registry': {'qualification_dates': [AFTER], 'target': 'fleet',
                          'patterns': len(fake.catalog.registered_patterns(day)),
                          'selection_contract': 'membership on declared qualification dates; no current-scan frequency claim'}}
            for day in (BEFORE, AFTER)] + [
            {'date': NEW, 'plans': ['catalog'], 'kind': 'daily-scalar-source-v1', 'registry': deepcopy(REGISTRY),
             'source': deepcopy(raw_daily()['source']), 'generation': 'd' * 32}],
        'levels': 1, 'scope': SCOPE, 'daily_catalog_slots': 2, 'legacy': fake.runtime.metadata(),
        'capabilities': dict(CAPABILITIES)}
    def expected_view(day, pattern, *, convert=False):
        if day == NEW:
            raw = raw_daily(day, pattern)
            return {**raw, 'schema': 'dated-name-summary-v1', 'target': raw['source']['target'], 'plan': 'catalog',
                    'source': 'published dated precomputed batch artifact',
                    'source_identity': {**raw['source'], 'kind': 'daily-scalar-source-v1', 'generation': 'd' * 32}}
        body = side(fake, day, pattern)
        if convert:
            body.update(schema='dated-name-summary-v1', logical_store='gcs_fleet', capabilities=dict(CAPABILITIES),
                        source_identity={**body['source_identity'], 'kind': 'frozen-history'})
        return body
    def fetch(base, path, token, timeout, *, max_bytes):
        fake.calls.append(('fetch', base, path, token, timeout, max_bytes))
        if path == '/api/name-summary-registry':
            body = deepcopy(fake.expected_registry)
        else:
            query = dict(parse_qsl(urlsplit(path).query))
            if query['date'] != NEW:
                body = response(fake, query['date'], query['name'], query.get('from'))
            elif 'from' not in query:
                body = expected_view(NEW, query['name'])
            else:
                before, after = expected_view(query['from'], query['name'], convert=True), expected_view(NEW, query['name'])
                delta = lambda a, b: {key: b[key] - a[key] for key in ('b', 'o')}
                body = {'schema': 'dated-name-summary-diff-v1', 'logical_store': 'gcs_fleet', 'from': query['from'],
                        'date': NEW, 'pattern': query['name'], 'path': '', 'exact': True, 'incremental': False,
                        'levels': 1, 'scope': SCOPE, 'before': before, 'after': after, 'delta': delta(before['root'], after['root']),
                        'capabilities': dict(CAPABILITIES), 'buckets': [
                        {'path': a['path'], 'before': {key: a[key] for key in ('pre', 'post', 'b', 'o')},
                         'after': {key: b[key] for key in ('pre', 'post', 'b', 'o')}, 'delta': delta(a, b)}
                        for a, b in zip(before['buckets'], after['buckets'], strict=True)]}
        if fake.change:
            fake.change(body)
        raw = dumps(body).encode()
        fake.received.append(raw)
        return fake.status, fake.ms, raw
    monkeypatch.setattr(module, 'fetch', fetch)
    return fake


def run_dated(dated, tmp_path: Path, **kwargs):
    return module.check(tmp_path, CH, BASE, kwargs.pop('date', NEW), kwargs.pop('patterns', ('.JSON', 'PLAIN')),
                        tmp_path / 'check.json', dated_generation_root=tmp_path / 'daily', logical_store='gcs_fleet',
                        token_env=ENV, references=tuple(dated.refs), trials=2, **kwargs)


@pytest.mark.parametrize('date,before', [(NEW, None), (NEW, AFTER), (AFTER, BEFORE)])
def test_dated_single_mixed_and_unchanged_old_regression_plus_exact_registry(dated, tmp_path: Path, date, before) -> None:
    actual = run_dated(dated, tmp_path, date=date, compare_from=before)
    assert actual['dated_publication'] == {
        'generation': 'd' * 32, 'artifacts': dated.daily.manifest['artifacts'], 'proofs': dated.daily.manifest['proofs'],
        'published_manifest_sha256': sha256(dumps(dated.daily.manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'validation': 'whole-body parity against pinned dated reader/composition; not an independent source oracle'}
    assert actual['registry'] == {'responses': 1, 'latency_ms': 10., 'bytes': len(dated.received[0]),
                                 'validation': 'complete registry body equals pinned composite metadata'}
    assert (actual['logical_store'], actual['responses'], actual['patterns'], actual['trials']) == ('gcs_fleet', 5, 2, 2)
    assert loads((tmp_path / 'check.json').read_bytes()) == actual
    assert loads(dated.received[0]) == dated.expected_registry
    assert [call for call in dated.calls if call[0] == 'daily.load'] == [('daily.load', tmp_path / 'daily')]
    suffix = '&from=' + before if before else ''
    assert [call for call in dated.calls if call[0] == 'fetch'] == [
        ('fetch', BASE, '/api/name-summary-registry', TOKEN, 8, 65536),
        *[('fetch', BASE, f'/api/name-summary?date={date}&name={name}{suffix}', TOKEN, 8, 65536)
          for name in ('.json', 'plain') for _ in range(2)]]
    assert [loads(raw)['schema'] for raw in dated.received[1:]] == [
        'name-summary-diff-v1' if date == AFTER else ('dated-name-summary-diff-v1' if before else 'dated-name-summary-v1')] * 4


@pytest.mark.parametrize('before,change', [
    (None, lambda body: body['dates'][-1]['registry'].update(qualification_dates=[NEW]) if body['schema'] == 'dated-name-summary-registry-v1' else None),
    (None, lambda body: body['source_identity'].update(source_manifest_sha256='0' * 64) if body['schema'] == 'dated-name-summary-v1' else None),
    (AFTER, lambda body: body['after']['buckets'][1].update(post=8) if body['schema'] == 'dated-name-summary-diff-v1' else None),
])
def test_no_metadata_hash_or_side_geometry_is_discarded(dated, tmp_path: Path, before, change) -> None:
    dated.change = change
    with pytest.raises(AssertionError) as caught:
        run_dated(dated, tmp_path, compare_from=before)
    assert str(caught.value) == 'name summary HTTP response differs from the complete pinned deterministic body'
    assert (tmp_path / 'check.json').exists() is False


def test_unsupported_new_literal_cannot_compute_cold_expected_body_or_issue_http(dated, tmp_path: Path) -> None:
    with pytest.raises(ValueError) as caught:
        run_dated(dated, tmp_path, patterns=('missing',))
    assert str(caught.value) == 'dated name summary new scan/literal is not registered; no cold fallback'
    assert dated.received == []
    assert (tmp_path / 'check.json').exists() is False


@pytest.mark.parametrize('keywords', [{'dated_generation_root': Path('daily')}, {'logical_store': 'gcs_fleet'}])
def test_dated_pair_required_before_any_io(fake, tmp_path: Path, keywords) -> None:
    with pytest.raises(ValueError) as caught:
        run(fake, tmp_path, **keywords)
    assert str(caught.value) == 'name summary HTTP check dated publication root and logical store are required together'
    assert fake.calls == []


@pytest.mark.parametrize('option', ['-G', '-f'])
def test_cli_dated_pair_required_without_check_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, option: str) -> None:
    from dt_cloud.cli import main
    calls = []
    monkeypatch.setattr(module, 'check', lambda *args, **kwargs: calls.append((args, kwargs)))
    result = CliRunner().invoke(main, ['ch-name-summary-http-check', '-c', CH, '-d', NEW, '-g', str(tmp_path),
                                     '-n', '.json', '-o', str(tmp_path / 'out.json'), '-U', BASE,
                                     option, str(tmp_path / 'daily') if option == '-G' else 'gcs_fleet'])
    assert (result.exit_code, result.output.splitlines()) == (2, [
        'Usage: main ch-name-summary-http-check [OPTIONS]', "Try 'main ch-name-summary-http-check --help' for help.",
        '', 'Error: --dated-generation-root and --logical-store are required together'])
    assert calls == []
