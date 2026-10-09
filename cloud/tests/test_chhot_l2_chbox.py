"""Opt-in artifact-only L2 reads use an independent, fail-fast authenticated lane."""

from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from json import dumps, loads
from pathlib import Path
from threading import BoundedSemaphore, Thread
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from dt_cloud.box import server as bs
from dt_cloud.chstore.hot_l2_pair_catalog import HotL2PairCatalog
from test_chhot_l1_chbox import Store
from test_chhot_l2_pair_catalog import expected_view, fixture


@pytest.fixture
def box(tmp_path: Path) -> bs.ChBox:
    artifact, check, _ = fixture(tmp_path)
    result = bs.ChBox(Store(), hot_l2_artifact=artifact, hot_l2_check=check, gate=BoundedSemaphore(1))
    result.start()
    assert result.gate.acquire(blocking=False) is True
    assert result.hot_l1_gate.acquire(blocking=False) is True
    assert result.hot_l1_gate.acquire(blocking=False) is True
    return result


@pytest.fixture
def server(box: bs.ChBox):
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), bs.make_handler(box, 'fixture-token'))
    httpd.daemon_threads = True
    thread = Thread(target=lambda: httpd.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        thread.join(timeout=2)
        httpd.server_close()


def request(server: ThreadingHTTPServer, query: str, *, token: str | None = 'fixture-token', method: str = 'GET') -> tuple[int, dict, object]:
    connection = HTTPConnection(*server.server_address, timeout=2)
    try:
        connection.request(method, '/api/hot-l2?' + query, headers={'Authorization': f'Bearer {token}'} if token else {})
        response = connection.getresponse()
        raw = response.read()
        body = loads(raw) if raw and response.getheader('content-type') == 'application/json' else raw.decode()
        return response.status, dict(response.getheaders()), body
    finally:
        connection.close()


def test_complete_l2_body_bypasses_both_occupied_rich_and_l1_gates(server: ThreadingHTTPServer, box: bs.ChBox) -> None:
    status, headers, body = request(server, 'date=2026-10-05&name=M&path=marin-a')
    assert (status, body) == (200, expected_view(box.hot_l2_artifact, '2026-10-05'))
    assert headers['cache-control'] == 'private, no-store'
    assert box.gate.acquire(blocking=False) is False
    assert box.hot_l1_gate.acquire(blocking=False) is False
    assert box.hot_l2_gate.acquire(blocking=False) is True
    box.hot_l2_gate.release()


@pytest.mark.parametrize('before,after,delta', [('2026-10-04', '2026-10-05', {'b': '-6', 'o': '-2'}),
                                              ('2026-10-05', '2026-10-04', {'b': '6', 'o': '2'}),
                                              ('2026-10-05', '2026-10-05', {'b': '0', 'o': '0'})])
def test_complete_common_partition_diff_for_forward_reversed_and_same_dates(server: ThreadingHTTPServer, box: bs.ChBox, before: str, after: str, delta: dict) -> None:
    status, headers, body = request(server, f'date={after}&name=m&from={before}&path=marin-a')
    assert (status, body) == (200, box.hot_l2_catalog.diff(before, after, 'm', path='marin-a'))
    assert body['root']['delta'] == delta
    assert headers['cache-control'] == 'private, no-store'


def test_zero_byte_objects_and_registered_zero_remain_exact(server: ThreadingHTTPServer, box: bs.ChBox) -> None:
    for pattern in ('m', '.npy'):
        status, headers, body = request(server, f'date=2026-10-05&name={pattern}&path=marin-b')
        assert (status, body) == (200, expected_view(box.hot_l2_artifact, '2026-10-05', pattern, 'marin-b'))
        assert headers['cache-control'] == 'private, no-store'


def test_busy_l2_lane_refuses_promptly_without_other_gates_or_source_fallback(server: ThreadingHTTPServer, box: bs.ChBox) -> None:
    assert box.hot_l2_gate.acquire(blocking=False) is True
    assert box.hot_l2_gate.acquire(blocking=False) is True
    try:
        status, headers, body = request(server, 'date=2026-10-05&name=m&path=marin-a')
        assert (status, headers['retry-after'], headers['cache-control'], body) == (
            503, '1', 'private, no-store', {'error': 'hot L2 serving slots busy; retry shortly'},
        )
    finally:
        box.hot_l2_gate.release()
        box.hot_l2_gate.release()


@pytest.mark.parametrize('query,message', [
    ('date=2026-10-05&name=m&name=.npy&path=marin-a', 'duplicate query parameter'),
    ('date=2026-10-05&date=2026-10-04&name=m&path=marin-a', 'duplicate query parameter'),
    ('date=2026-10-05&name=m&path=marin-a&q=owner:me', 'unknown query parameter'),
    ('date=2026-10-05&name=m&path=marin-a&depth=2', 'unknown query parameter'),
    ('date=2026-10-05&name=m&path=marin-a&from=', 'from must be nonempty when provided'),
    ('date=2026-10-05&path=marin-a', 'date and name are required and must be nonempty'),
    ('date=2026-10-05&name=m', 'paired L2 serves declared bucket roots only; no deeper drill or fallback'),
    ('date=2026-10-05&name=m&path=marin-a/x', 'paired L2 serves declared bucket roots only; no deeper drill or fallback'),
    ('date=2026-10-05&name=m&path=other', 'paired L2 serves declared bucket roots only; no deeper drill or fallback'),
    ('date=2026-10-05&name=cold&path=marin-a', 'paired L2 pattern/date is not registered; no scan fallback'),
    ('date=2026-10-03&name=m&path=marin-a', 'paired L2 pattern/date is not registered; no scan fallback'),
    ('date=2026-10-05&name=%FF&path=marin-a', 'invalid query parameters'),
    ('date=2026-10-05&name=%2&path=marin-a', 'invalid query parameters'),
])
def test_strict_read_only_parameters_and_unknown_scopes_are_private_refusals(server: ThreadingHTTPServer, query: str, message: str) -> None:
    status, headers, body = request(server, query)
    assert (status, headers['cache-control'], body) == (400, 'private, no-store', {'error': message})


@pytest.mark.parametrize('token', [None, 'wrong'])
def test_same_box_bearer_auth_precedes_query_and_catalog_checks(server: ThreadingHTTPServer, box: bs.ChBox, token: str | None) -> None:
    box.hot_l2_catalog = None
    status, headers, body = request(server, 'bad=%FF', token=token)
    assert (status, headers['cache-control'], body) == (401, 'private, no-store', 'unauthorized')


def test_unselected_l2_explicitly_refuses_without_loading_or_fallback(server: ThreadingHTTPServer, box: bs.ChBox) -> None:
    box.hot_l2_catalog = None
    status, headers, body = request(server, 'date=2026-10-05&name=m&path=marin-a')
    assert (status, headers['cache-control'], body) == (501, 'private, no-store', {'error': 'hot L2 catalog is not selected; no scan fallback'})


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'])
def test_unsupported_l2_methods_are_authenticated_private_refusals(server: ThreadingHTTPServer, method: str) -> None:
    status, headers, body = request(server, '', method=method)
    assert (status, headers['cache-control'], headers['allow'], body) == (
        405, 'private, no-store', 'GET', '' if method == 'HEAD' else {'error': 'hot L2 supports GET only'},
    )
    status, headers, body = request(server, '', token=None, method=method)
    assert (status, headers['cache-control'], body) == (401, 'private, no-store', '' if method == 'HEAD' else 'unauthorized')


def test_catalog_failure_is_sanitized_and_releases_its_slot(box: bs.ChBox, monkeypatch: pytest.MonkeyPatch) -> None:
    box.hot_l2_gate = BoundedSemaphore(1)
    messages, sends = [], []
    monkeypatch.setattr(bs, 'err', lambda *args: messages.append(args))

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError('private source details must not escape')

    box.hot_l2_catalog = SimpleNamespace(view=broken)
    handler = object.__new__(bs.make_handler(box, None))
    handler._send = lambda status, body, t0, **kwargs: sends.append((status, loads(body), kwargs))
    handler._hot_l2('date=2026-10-05&name=m&path=marin-a', 0.)
    assert sends == [(503, {'error': 'hot L2 catalog unavailable; no scan fallback'},
                      {'headers': {'cache-control': 'private, no-store', 'retry-after': '1'}})]
    assert messages == [('serve-query: /api/hot-l2: RuntimeError',)]
    assert box.hot_l2_gate.acquire(blocking=False) is True
    box.hot_l2_gate.release()


def test_disconnect_during_response_releases_slot_without_second_error_response(box: bs.ChBox) -> None:
    box.hot_l2_gate = BoundedSemaphore(1)
    handler = object.__new__(bs.make_handler(box, None))
    sends = []

    def send(status: int, body: str, t0: float, **kwargs: object) -> None:
        sends.append((status, loads(body), kwargs))
        assert box.hot_l2_gate.acquire(blocking=False) is False
        raise BrokenPipeError('disconnected')

    handler._send = send
    with pytest.raises(BrokenPipeError) as caught:
        handler._hot_l2('date=2026-10-05&name=m&path=marin-a', 0.)
    assert str(caught.value) == 'disconnected'
    assert sends == [(200, expected_view(box.hot_l2_artifact, '2026-10-05'), {'headers': {'cache-control': 'private, no-store'}})]
    assert box.hot_l2_gate.acquire(blocking=False) is True
    box.hot_l2_gate.release()


def test_health_adds_only_selected_count_metadata_and_defaults_remain_unchanged(box: bs.ChBox) -> None:
    base = {'state': 'ready', 'engine': 'ch', 'scans': [{'date': '2026-10-05', 'version': 'v1'}]}
    assert box.health() == {**base, 'hot_l2': box.hot_l2_catalog.metadata()}
    box.hot_l2_catalog = None
    assert box.health() == base


def test_load_once_before_bind_and_invalid_check_refuses_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact, check, proof = fixture(tmp_path)
    calls, real_load = [], HotL2PairCatalog.load

    def load(a: Path, c: Path) -> HotL2PairCatalog:
        calls.append(('load', a, c))
        return real_load(a, c)

    monkeypatch.setattr(HotL2PairCatalog, 'load', load)
    selected = bs.ChBox(Store(), hot_l2_artifact=artifact, hot_l2_check=check)
    selected.start()
    selected.start()
    assert calls == [('load', artifact, check)]
    proof['complete'] = False
    check.write_text(dumps(proof) + '\n')
    selected.start()
    assert calls == [('load', artifact, check)]
    assert selected.hot_l2_catalog.view('2026-10-05', 'm', path='marin-a') == expected_view(artifact, '2026-10-05')
    monkeypatch.setattr(bs, 'ThreadingHTTPServer', lambda *args: calls.append(('bind',)))
    with pytest.raises(ValueError) as caught:
        bs.serve(bs.ChBox(Store(), hot_l2_artifact=artifact, hot_l2_check=check), bind='127.0.0.1', port=8091, token='fixture-token')
    assert str(caught.value) == 'paired L2 catalog requires a complete matching check proof'
    assert calls == [('load', artifact, check), ('load', artifact, check)]


@pytest.mark.parametrize('other', ['numeric', 'l1'])
def test_frozen_target_mismatch_refuses_before_clickhouse_and_bind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, other: str) -> None:
    artifact, check, _ = fixture(tmp_path)
    binds = []
    monkeypatch.setattr(bs, 'ThreadingHTTPServer', lambda *args: binds.append(args))
    selected = bs.ChBox(Store(), hot_l2_artifact=artifact, hot_l2_check=check,
                        narrow_target='other_fleet' if other == 'numeric' else None,
                        hot_l1_catalog=SimpleNamespace(target='other_fleet') if other == 'l1' else None)
    with pytest.raises(ValueError) as caught:
        bs.serve(selected, bind='127.0.0.1', port=8091, token='fixture-token')
    assert str(caught.value) == ('accepted hot L2 catalog target differs from the selected numeric target' if other == 'numeric' else
                                 'accepted hot L2 catalog target differs from the selected hot L1 catalog')
    assert binds == []


@pytest.mark.parametrize('artifact,check', [(Path('artifact'), None), (None, Path('check'))])
def test_unpaired_startup_inputs_refuse_before_any_load_or_bind(monkeypatch: pytest.MonkeyPatch, artifact: Path | None, check: Path | None) -> None:
    calls = []
    monkeypatch.setattr(HotL2PairCatalog, 'load', lambda *args: calls.append(('load',)))
    monkeypatch.setattr(bs, 'ThreadingHTTPServer', lambda *args: calls.append(('bind',)))
    with pytest.raises(ValueError) as caught:
        bs.serve(bs.ChBox(Store(), hot_l2_artifact=artifact, hot_l2_check=check), bind='127.0.0.1', port=8091, token='fixture-token')
    assert str(caught.value) == 'hot L2 artifact and check are required together'
    assert calls == []


@pytest.mark.parametrize('args,message', [
    (['-H', 'artifact'], '--hot-l2-artifact and --hot-l2-check are required together'),
    (['-J', 'check'], '--hot-l2-artifact and --hot-l2-check are required together'),
    (['-H', 'artifact', '-J', 'check'], '--hot-l2-artifact/--hot-l2-check require --engine ch'),
])
def test_cli_requires_paired_explicit_inputs_and_ch(args: list[str], message: str) -> None:
    from dt_cloud.cli import main
    result = CliRunner().invoke(main, ['serve-query', '-A', *args, 'unused'])
    assert (result.exit_code, result.output.splitlines()) == (2, [
        'Usage: main serve-query [OPTIONS] ROOT', "Try 'main serve-query --help' for help.", '', 'Error: ' + message,
    ])


@pytest.mark.parametrize('selected', [False, True])
def test_cli_preserves_default_and_forwards_selected_inputs_auth_and_existing_l1(monkeypatch: pytest.MonkeyPatch, selected: bool) -> None:
    from dt_cloud.cli import main
    calls = []
    monkeypatch.setattr(bs, 'token_from_env', lambda var: 'fixture-token')
    monkeypatch.setattr(bs, 'serve', lambda box, **kwargs: calls.append((box.hot_l2_artifact, box.hot_l2_check, box.hot_l2_catalog,
                                                                       box.hot_l1_generation, box.narrow_target, kwargs)))
    args = ['serve-query', '-e', 'ch', '-b', '127.0.0.1', '-p', '8091']
    if selected:
        args.extend(['-H', 'artifact', '-J', 'check', '-g', 'published', '-N', 'fleet'])
    result = CliRunner().invoke(main, [*args, 'unused'])
    assert (result.exit_code, result.stdout, result.stderr) == (0, '', '')
    assert calls == [(Path('artifact') if selected else None, Path('check') if selected else None, None,
                      Path('published') if selected else None, 'fleet' if selected else None,
                      {'bind': '127.0.0.1', 'port': 8091, 'token': 'fixture-token'})]
