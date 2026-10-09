"""Bounded complete HTTP bodies, exact scalar types and private benchmark output."""

from copy import deepcopy
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
from json import dumps, loads
from pathlib import Path
from types import SimpleNamespace
from threading import Thread
from urllib.parse import parse_qsl, urlsplit

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import hot_l2_http_bench as module
from dt_cloud.chstore.hot_l1_catalog import CatalogRequest
from dt_cloud.chstore.hot_l2_pair_catalog import HotL2PairCatalog
from test_chhot_l2_pair_catalog import fixture

BASE, TOKEN_ENV, TOKEN = 'http://fixture.invalid:8082', 'HL2_FIXTURE_TOKEN', 'private-fixture-token'
REAL_FETCH = module.fetch


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    artifact, check, _ = fixture(tmp_path)
    catalog = HotL2PairCatalog.load(artifact, check)
    state = SimpleNamespace(artifact=artifact, check=check, catalog=catalog, calls=[], raw=None, change=None, status=200, bodies=[])
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    def load(path, proof):
        state.calls.append(('load', path, proof))
        return catalog
    def fetch(base, path, token, timeout):
        state.calls.append(('fetch', base, path, token, timeout))
        query = dict(parse_qsl(urlsplit(path).query))
        body = deepcopy(catalog.view(query['date'], query['name'], path=query['path']) if 'from' not in query else catalog.diff(query['from'], query['date'], query['name'], path=query['path']))
        if state.change:
            state.change(body)
        raw = state.raw if state.raw is not None else dumps(body).encode()
        state.bodies.append(raw)
        return state.status, 10 * len(state.bodies), raw
    monkeypatch.setattr(module.HotL2PairCatalog, 'load', load)
    monkeypatch.setattr(module, 'fetch', fetch)
    monkeypatch.setattr(module, 'monotonic', lambda: 1.)
    return state


def run(fake: SimpleNamespace, tmp_path: Path, **kwargs) -> dict:
    return module.bench(fake.artifact, fake.check, BASE, '2026-10-05', ('M', '.NPY'), tmp_path / 'bench.json', token_env=TOKEN_ENV, **kwargs)


@pytest.mark.parametrize('diff', [False, True])
def test_exact_bodies_all_bucket_paths_compact_metrics_and_token_safety(fake: SimpleNamespace, tmp_path: Path, diff: bool) -> None:
    result = run(fake, tmp_path, compare_from='2026-10-04' if diff else None, trials=2)
    sizes = [len(body) for body in fake.bodies]
    expected = {'schema': 'hot-l2-http-bench-v1', 'complete': True, 'target': 'fleet', 'date': '2026-10-05', 'compare_from': '2026-10-04' if diff else None,
                'artifact_sha256': sha256(fake.artifact.read_bytes()).hexdigest(), 'artifact_bytes': fake.artifact.stat().st_size,
                'patterns': 2, 'buckets': 6, 'trials': 2, 'responses': 24, 'all_registered': False,
                'selection_sha256': sha256(dumps([('m', '.npy'), fake.catalog.paths], separators=(',', ':')).encode()).hexdigest(),
                'timeout_seconds': 30, 'max_response_bytes': 256 << 10, 'elapsed_s': 0.,
                'latency_ms': {'min': 10., 'median': 125., 'p90': 220., 'max': 240.}, 'response_bytes': {'min': min(sizes), 'max': max(sizes)},
                'p90_method': 'nearest rank', 'validation': 'every complete parsed HTTP body equals the pinned catalog; not independent source truth',
                'cache_state': 'uncontrolled; scan-free resident-catalog HTTP parity, not browser/edge or rich-query latency'}
    assert result == expected
    assert loads((tmp_path / 'bench.json').read_bytes()) == expected
    suffix = '&from=2026-10-04' if diff else ''
    assert fake.calls == [('load', fake.artifact, fake.check)] + [
        ('fetch', BASE, f'/api/hot-l2?date=2026-10-05&name={pattern}&path={bucket}{suffix}', TOKEN, 30)
        for pattern in ('m', '.npy') for bucket in fake.catalog.paths for _ in range(2)]


@pytest.mark.parametrize('change', [
    lambda b: b.update(extra='new label'), lambda b: b.pop('validation'),
    lambda b: b['root'].update(b=2), lambda b: b['capabilities'].update(child_drill=0),
    lambda b: b['cutoff'].update(threshold_bytes='999'), lambda b: b['other'].update(o='99'),
])
def test_whole_body_field_and_type_mutations_fail(fake: SimpleNamespace, tmp_path: Path, change) -> None:
    fake.change = change
    with pytest.raises(AssertionError) as caught:
        run(fake, tmp_path, paths=('marin-a',))
    assert str(caught.value) == 'paired L2 HTTP response differs from the complete pinned catalog body'
    assert (tmp_path / 'bench.json').exists() is False
    assert len(fake.calls) == 2


@pytest.mark.parametrize('raw,message', [
    (b'x' * ((256 << 10) + 1), 'paired L2 HTTP response exceeds its 256 KiB byte cap'),
    (b'private invalid response', 'paired L2 HTTP response is not unique-key finite JSON'),
    (b'{"token":"private","token":"private"}', 'paired L2 HTTP response is not unique-key finite JSON'),
    (b'{"value":NaN}', 'paired L2 HTTP response is not unique-key finite JSON'),
])
def test_oversize_and_invalid_json_are_sanitized(fake: SimpleNamespace, tmp_path: Path, raw: bytes, message: str) -> None:
    fake.raw = raw
    with pytest.raises(RuntimeError) as caught:
        run(fake, tmp_path, paths=('marin-a',))
    assert str(caught.value) == message
    assert (tmp_path / 'bench.json').exists() is False


@pytest.mark.parametrize('status', [0, 401, 503])
def test_failed_status_never_accepts_partial_bodies(fake: SimpleNamespace, tmp_path: Path, status: int) -> None:
    fake.status = status
    with pytest.raises(RuntimeError) as caught:
        run(fake, tmp_path, paths=('marin-a',))
    assert str(caught.value) == f'paired L2 HTTP benchmark requires HTTP 200; received status {status}'
    assert (tmp_path / 'bench.json').exists() is False


@pytest.mark.parametrize('kwargs,message', [
    ({'paths': ('marin-a', 'marin-a')}, 'paired L2 HTTP benchmark selections must be unique'),
    ({'paths': ('marin-a/x',)}, 'paired L2 serves declared bucket roots only; no deeper drill or fallback'),
    ({'compare_from': '2026-10-06'}, 'paired L2 pattern/date is not registered; no scan fallback'),
])
def test_all_selection_preflight_before_any_traffic(fake: SimpleNamespace, tmp_path: Path, kwargs: dict, message: str) -> None:
    with pytest.raises((CatalogRequest, ValueError)) as caught:
        run(fake, tmp_path, **kwargs)
    assert str(caught.value) == message
    assert fake.calls == [('load', fake.artifact, fake.check)]


def test_all_registered_and_unknown_later_pattern_are_preflighted(fake: SimpleNamespace, tmp_path: Path) -> None:
    result = module.bench(fake.artifact, fake.check, BASE, '2026-10-05', (), tmp_path / 'all.json', token_env=TOKEN_ENV, all_registered=True, paths=('marin-a',))
    assert (result['patterns'], result['buckets'], result['responses'], result['all_registered']) == (2, 1, 2, True)
    fake.calls.clear()
    with pytest.raises(CatalogRequest) as caught:
        module.bench(fake.artifact, fake.check, BASE, '2026-10-05', ('m', 'unknown'), tmp_path / 'unknown.json', token_env=TOKEN_ENV)
    assert str(caught.value) == 'paired L2 pattern/date is not registered; no scan fallback'
    assert fake.calls == [('load', fake.artifact, fake.check)]


def test_encoded_unicode_bucket_and_pattern_use_only_supported_fields(fake: SimpleNamespace, tmp_path: Path) -> None:
    fake.catalog.paths = ('bucket å',)
    fake.catalog.patterns = ('å',)
    fake.catalog._patterns = frozenset(('å',))
    source = fake.catalog._buckets.pop(('m', 'marin-a'))
    source['path'] = 'bucket å'
    fake.catalog._buckets[('å', 'bucket å')] = source
    fake.catalog._children[('å', 'bucket å')] = []
    fake.catalog._selected['å'] = fake.catalog._proof['covered_control']
    result = module.bench(fake.artifact, fake.check, BASE, '2026-10-05', ('Å',), tmp_path / 'encoded.json', token_env=TOKEN_ENV)
    assert result['responses'] == 1
    assert fake.calls == [('load', fake.artifact, fake.check),
                          ('fetch', BASE, '/api/hot-l2?date=2026-10-05&name=%C3%A5&path=bucket+%C3%A5', TOKEN, 30)]


@pytest.mark.parametrize('kwargs,message', [
    ({'timeout': 31}, 'paired L2 HTTP benchmark requires 1..100 trials and a finite timeout in (0,30]'),
    ({'trials': 0}, 'paired L2 HTTP benchmark requires 1..100 trials and a finite timeout in (0,30]'),
    ({'patterns': ()}, 'paired L2 HTTP benchmark requires explicit patterns or all_registered, not both'),
    ({'patterns': ('m',), 'all_registered': True}, 'paired L2 HTTP benchmark requires explicit patterns or all_registered, not both'),
    ({'base': 'http://user:private@host'}, 'paired L2 HTTP benchmark requires an explicit HTTP(S) origin without credentials/path/query/fragment'),
    ({'base': 'http://host?filter=private'}, 'paired L2 HTTP benchmark requires an explicit HTTP(S) origin without credentials/path/query/fragment'),
    ({'base': 'http://host/api/hot-l2'}, 'paired L2 HTTP benchmark requires an explicit HTTP(S) origin without credentials/path/query/fragment'),
])
def test_invalid_inputs_refuse_before_catalog_and_traffic(fake: SimpleNamespace, tmp_path: Path, kwargs: dict, message: str) -> None:
    args = {'artifact': fake.artifact, 'check': fake.check, 'base': BASE, 'date': '2026-10-05', 'patterns': ('m',),
            'out': tmp_path / 'bench.json', 'token_env': TOKEN_ENV, **kwargs}
    with pytest.raises(ValueError) as caught:
        module.bench(**args)
    assert str(caught.value) == message
    assert fake.calls == []


def test_existing_output_and_multiline_missing_token_refuse_before_traffic(fake: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for token in ('', 'private\nvalue'):
        monkeypatch.setenv(TOKEN_ENV, token)
        with pytest.raises(ValueError) as caught:
            run(fake, tmp_path)
        assert str(caught.value) == 'paired L2 HTTP benchmark requires a nonempty single-line bearer token environment variable'
    (tmp_path / 'bench.json').write_text('keep\n')
    with pytest.raises(ValueError) as caught:
        run(fake, tmp_path)
    assert str(caught.value) == 'paired L2 HTTP benchmark output must be new in an existing directory'
    assert (fake.calls, (tmp_path / 'bench.json').read_text()) == ([], 'keep\n')


def test_transport_bound_reads_decreasing_deadline_no_redirect_and_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, clock = [], iter((0., 1., 2., 3., 4.))
    class Response:
        status = 200
        def isclosed(self): return False
        def read1(self, size):
            calls.append(('read', size))
            return b'{}' if sum(call[0] == 'read' for call in calls) == 1 else b''
        def close(self): calls.append(('response-close',))
    class Connection:
        sock = SimpleNamespace(settimeout=lambda timeout: calls.append(('timeout', timeout)))
        def __init__(self, *args, **kwargs): calls.append(('connect', args, kwargs))
        def request(self, *args, **kwargs): calls.append(('request', args, kwargs))
        def getresponse(self): return Response()
        def close(self): calls.append(('connection-close',))
    monkeypatch.setattr(module, 'HTTPConnection', Connection)
    monkeypatch.setattr(module, 'monotonic', lambda: next(clock))
    assert module.fetch(BASE, '/api/hot-l2?date=scan', TOKEN, 10) == (200, 4000., b'{}')
    assert calls == [('connect', ('fixture.invalid', 8082), {'timeout': 10}),
                     ('request', ('GET', '/api/hot-l2?date=scan'), {'headers': {'Authorization': 'Bearer ' + TOKEN, 'User-Agent': 'dt-cloud-hot-l2-http-bench'}}),
                     ('timeout', 9.), ('timeout', 8.), ('read', 65536), ('timeout', 7.), ('read', 65536),
                     ('response-close',), ('connection-close',)]


def test_transport_error_has_no_private_exception_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    class Connection:
        def __init__(self, *args, **kwargs): pass
        def request(self, *args, **kwargs): raise OSError('private-token-and-response')
        def close(self): calls.append('closed')
    monkeypatch.setattr(module, 'HTTPConnection', Connection)
    with pytest.raises(RuntimeError) as caught:
        module.fetch(BASE, '/api/hot-l2', TOKEN, 30)
    assert (str(caught.value), calls, caught.value.__suppress_context__) == ('paired L2 HTTP transport failed or exceeded its deadline', ['closed'], True)


def test_transport_deadline_refuses_before_waiting_for_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, clock = [], iter((0., 31.))
    class Connection:
        sock = SimpleNamespace(settimeout=lambda value: calls.append(('timeout', value)))
        def __init__(self, *args, **kwargs): pass
        def request(self, *args, **kwargs): calls.append('request')
        def getresponse(self): raise AssertionError('headers must not be read after deadline')
        def close(self): calls.append('closed')
    monkeypatch.setattr(module, 'HTTPConnection', Connection)
    monkeypatch.setattr(module, 'monotonic', lambda: next(clock))
    with pytest.raises(RuntimeError) as caught:
        module.fetch(BASE, '/api/hot-l2', TOKEN, 30)
    assert (str(caught.value), calls) == ('paired L2 HTTP transport failed or exceeded its deadline', ['request', 'closed'])


def test_real_http10_body_auth_and_no_redirect_following(fake: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud.chstore.hot_l1_http import query_catalog
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            requests.append((self.path, self.headers.get('Authorization')))
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', '/private-target')
                self.end_headers()
                return
            body = query_catalog(fake.catalog, urlsplit(self.path).query)
            data = dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    monkeypatch.setattr(module, 'fetch', REAL_FETCH)
    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            result = module.bench(fake.artifact, fake.check, base, '2026-10-05', ('M',), tmp_path / 'real.json',
                                  paths=('marin-a',), token_env=TOKEN_ENV)
            redirect, ms, data = REAL_FETCH(base, '/redirect', TOKEN, 30)
        finally:
            server.shutdown()
            thread.join(timeout=5)
    assert (result['complete'], result['responses'], redirect, data, thread.is_alive()) == (True, 1, 302, b'', False)
    assert ms >= 0
    assert requests == [('/api/hot-l2?date=2026-10-05&name=m&path=marin-a', 'Bearer ' + TOKEN), ('/redirect', 'Bearer ' + TOKEN)]


def test_cli_isolated_exact_forwarding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from dt_cloud.cli import main
    calls, body = [], {'schema': 'hot-l2-http-bench-v1', 'responses': 12}
    def bench(*args, **kwargs):
        calls.append((args, kwargs))
        return body
    monkeypatch.setattr(module, 'bench', bench)
    artifact, proof, out = tuple(tmp_path / name for name in ('artifact', 'check', 'out'))
    result = CliRunner().invoke(main, ['ch-hot-l2-http-bench', str(artifact), str(proof), '-U', BASE, '-T', TOKEN_ENV,
                                     '-d', '2026-10-05', '-D', '2026-10-04', '-n', 'M', '-p', 'marin-a', '-t', '2', '-w', '12', '-o', str(out)])
    assert (result.exit_code, result.stdout, result.stderr) == (0, dumps(body) + '\n', '')
    assert calls == [((artifact, proof, BASE, '2026-10-05', ('M',), out),
                     {'token_env': TOKEN_ENV, 'paths': ('marin-a',), 'compare_from': '2026-10-04', 'trials': 2, 'timeout': 12., 'all_registered': False})]
