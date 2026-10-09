"""The stitched opt-in route is private and leaves the resident hot lane alone."""

from json import loads
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from dt_cloud.box import server as bs
from dt_cloud.chstore.hot_l1_catalog import CatalogRequest
from dt_cloud.cli import main


BODY = {'schema': 'name-summary-v1', 'root': {'b': 12, 'o': 3}, 'plan': 'bounded-name-postings'}


class Store:
    url, db, root_label = 'http://unused.invalid', 'source', 'fixture'

    def session(self):
        raise AssertionError('dispatch called ClickHouse')

    def scans(self, *, refresh: bool) -> dict:
        assert refresh is True
        return {}


class Runtime:
    target = 'fleet'

    def __init__(self) -> None:
        self.calls = []
        self.error = None

    def view(self, date: str, pattern: str, *, path: str = '') -> dict:
        self.calls.append(('view', date, pattern, path))
        if self.error is not None:
            raise self.error
        return BODY

    def diff(self, before: str, after: str, pattern: str, *, path: str = '') -> dict:
        self.calls.append(('diff', before, after, pattern, path))
        if self.error is not None:
            raise self.error
        return {**BODY, 'schema': 'name-summary-diff-v1'}

    def metadata(self) -> dict:
        return {'schema': 'name-summary-runtime-v1', 'target': 'fleet'}


def handler(
    runtime: Runtime | None,
    query: str,
    *,
    token: str | None = 'fixture-token',
) -> tuple[object, list]:
    box = bs.ChBox(Store(), name_summary_runtime=runtime)
    result = object.__new__(bs.make_handler(box, 'fixture-token'))
    result.path = '/api/name-summary?' + query
    result.headers = Message()
    if token:
        result.headers['Authorization'] = f'Bearer {token}'
    result.command = 'GET'
    calls = []
    result._send = lambda status, body, t0, *args, **kwargs: calls.append((status, body, args, kwargs))
    return result, calls


@pytest.mark.parametrize('query,expected,call', [
    ('date=2026-10-05&name=datakit', BODY, ('view', '2026-10-05', 'datakit', '')),
    ('date=2026-10-05&name=datakit&from=2026-10-04', {**BODY, 'schema': 'name-summary-diff-v1'}, ('diff', '2026-10-04', '2026-10-05', 'datakit', '')),
])
def test_private_summary_dispatches_the_complete_strict_body(query: str, expected: dict, call: tuple) -> None:
    runtime = Runtime()
    request, calls = handler(runtime, query)
    request.do_GET()
    assert runtime.calls == [call]
    assert [(status, loads(body), args, kwargs) for status, body, args, kwargs in calls] == [
        (200, expected, (), {'headers': {'cache-control': 'private, no-store'}}),
    ]


def test_auth_precedes_runtime_and_disabled_route() -> None:
    runtime = Runtime()
    request, calls = handler(runtime, 'not-even-valid', token=None)
    request.do_GET()
    assert runtime.calls == []
    assert calls == [(401, 'unauthorized', ('text/plain',), {'headers': {'cache-control': 'private, no-store'}})]


def test_disabled_summary_is_explicit_not_a_fallback() -> None:
    request, calls = handler(None, 'date=2026-10-05&name=datakit')
    request.do_GET()
    assert [(status, loads(body), args, kwargs) for status, body, args, kwargs in calls] == [
        (501, {'error': 'name summary is not selected'}, (), {'headers': {'cache-control': 'private, no-store'}}),
    ]


@pytest.mark.parametrize('query,message', [
    ('date=2026-10-05&name=datakit&owner=alice', 'unknown query parameter'),
    ('date=2026-10-05&name=datakit&name=.json', 'duplicate query parameter'),
    ('date=2026-10-05', 'date and name are required and must be nonempty'),
])
def test_bad_parameters_never_reach_runtime(query: str, message: str) -> None:
    runtime = Runtime()
    request, calls = handler(runtime, query)
    request.do_GET()
    assert runtime.calls == []
    assert [(status, loads(body), args, kwargs) for status, body, args, kwargs in calls] == [
        (400, {'error': message}, (), {'headers': {'cache-control': 'private, no-store'}}),
    ]


def test_runtime_request_refusal_is_private_and_not_zero() -> None:
    runtime = Runtime()
    runtime.error = CatalogRequest('scan is unavailable')
    request, calls = handler(runtime, 'date=2026-10-03&name=datakit')
    request.do_GET()
    assert [(status, loads(body), args, kwargs) for status, body, args, kwargs in calls] == [
        (400, {'error': 'scan is unavailable'}, (), {'headers': {'cache-control': 'private, no-store'}}),
    ]


@pytest.mark.parametrize('error', [RuntimeError('private SQL and diagnostics'), ValueError('private serialization error')])
def test_runtime_failure_is_sanitized_without_partial_body(error: Exception) -> None:
    runtime = Runtime()
    runtime.error = error
    request, calls = handler(runtime, 'date=2026-10-05&name=datakit')
    request.do_GET()
    assert [(status, loads(body), args, kwargs) for status, body, args, kwargs in calls] == [
        (503, {'error': 'name summary unavailable or exceeded work budget; retry or narrow the literal. This is not a zero-match result.'}, (),
         {'headers': {'cache-control': 'private, no-store', 'retry-after': '1'}}),
    ]


def test_unsupported_method_authenticates_before_refusal() -> None:
    runtime = Runtime()
    request, calls = handler(runtime, 'date=2026-10-05&name=datakit')
    request.command = 'POST'
    request.do_POST()
    assert runtime.calls == []
    assert [(status, loads(body), args, kwargs) for status, body, args, kwargs in calls] == [
        (405, {'error': 'name summary supports GET only'}, (), {'headers': {'cache-control': 'private, no-store', 'allow': 'GET'}}),
    ]


@pytest.mark.parametrize('arguments,message', [
    (['-L'], '--name-summary requires --engine ch, --hot-l1-generation and --narrow-target'),
    (['-e', 'ch', '-L'], '--name-summary requires --engine ch, --hot-l1-generation and --narrow-target'),
    (['-e', 'ch', '-g', 'published', '-L'], '--name-summary requires --engine ch, --hot-l1-generation and --narrow-target'),
])
def test_cli_refuses_incomplete_opt_in_before_startup(arguments: list[str], message: str) -> None:
    result = CliRunner().invoke(main, ['serve-query', '-A', *arguments, 'unused'])
    assert (result.exit_code, result.output.splitlines()) == (2, [
        'Usage: main serve-query [OPTIONS] ROOT', "Try 'main serve-query --help' for help.", '', f'Error: {message}',
    ])


def test_cli_forwards_only_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(bs, 'serve', lambda box, **kwargs: calls.append((box.name_summary_enabled, box.name_summary_runtime, box.hot_l1_generation, box.narrow_target)))
    result = CliRunner().invoke(main, ['serve-query', '-A', '-e', 'ch', '-g', 'published', '-N', 'fleet', '-L', 'unused'])
    assert (result.exit_code, result.stdout, result.stderr) == (0, '', '')
    assert calls == [(True, None, Path('published'), 'fleet')]


def test_startup_refuses_missing_generation_before_io() -> None:
    box = bs.ChBox(Store(), name_summary_enabled=True, narrow_target='fleet')
    with pytest.raises(ValueError) as caught:
        box.start()
    assert str(caught.value) == 'name summary requires a published hot L1 generation and numeric target'


def test_startup_pins_once_and_reuses_the_verified_catalog_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud.chstore import hot_l1_publish, name_summary

    calls = []
    published = {'generation': 'fixture'}
    catalog = SimpleNamespace(target='fleet')
    runtime = Runtime()

    def pin(root: Path) -> dict:
        calls.append(('pin', root))
        return published

    def load_pinned(root: Path, manifest: dict) -> object:
        assert manifest is published
        calls.append(('load_pinned', root))
        return catalog

    def load(root: Path, url: str, *, target: str, **reuse: object) -> object:
        assert reuse == {'catalog': catalog, 'published': published}
        calls.append(('runtime', root, url, target))
        return runtime

    monkeypatch.setattr(hot_l1_publish, 'pin', pin)
    monkeypatch.setattr(hot_l1_publish, 'load_pinned', load_pinned)
    monkeypatch.setattr(name_summary.NameSummaryRuntime, 'load', load)
    box = bs.ChBox(Store(), name_summary_enabled=True, hot_l1_generation=tmp_path, narrow_target='fleet', narrow_manifest={})
    box.start()
    box.start()
    assert calls == [('pin', tmp_path), ('load_pinned', tmp_path), ('runtime', tmp_path, 'http://unused.invalid', 'fleet')]
    assert box.hot_l1_catalog is catalog
    assert box.hot_l1_published is published
    assert box.name_summary_runtime is runtime


def test_runtime_target_mismatch_refuses_before_bind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = Runtime()
    runtime.target = 'other'
    calls = []
    monkeypatch.setattr(bs, 'ThreadingHTTPServer', lambda *args: calls.append(('bind',)))
    box = bs.ChBox(Store(), name_summary_enabled=True, name_summary_runtime=runtime, hot_l1_generation=tmp_path,
                   hot_l1_catalog=SimpleNamespace(target='fleet'), narrow_target='fleet', narrow_manifest={})
    with pytest.raises(ValueError) as caught:
        bs.serve(box, bind='127.0.0.1', port=8087, token='fixture-token')
    assert str(caught.value) == 'name summary runtime target differs from the selected numeric target'
    assert calls == []


def test_health_only_adds_opted_in_runtime_metadata() -> None:
    box = bs.ChBox(Store(), name_summary_runtime=Runtime())
    assert box.health() == {'state': 'ready', 'engine': 'ch', 'scans': [], 'name_summary': {'schema': 'name-summary-runtime-v1', 'target': 'fleet'}}
    box.name_summary_runtime = None
    assert box.health() == {'state': 'ready', 'engine': 'ch', 'scans': []}
