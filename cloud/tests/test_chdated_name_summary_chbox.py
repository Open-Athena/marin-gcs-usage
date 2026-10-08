"""Dated root dispatch is explicit, pinned and private; old routes stay intact."""

from email.message import Message
from json import loads
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from dt_cloud.box import server as bs
from dt_cloud.chstore import dated_hot_l1_publish, dated_name_summary
from dt_cloud.cli import main
from test_chname_summary_chbox import BODY, Runtime, Store


PRIVATE = {'cache-control': 'private, no-store'}
METADATA = {'schema': 'dated-name-summary-registry-v1', 'logical_store': 'gcs_fleet', 'dates': []}


class DatedRuntime(Runtime):
    def metadata(self) -> dict:
        self.calls.append(('metadata',))
        if self.error is not None:
            raise self.error
        return METADATA


def request(box: bs.ChBox, path: str, *, token: str | None = 'fixture-token') -> tuple[object, list]:
    handler = object.__new__(bs.make_handler(box, 'fixture-token'))
    handler.path, handler.command, handler.headers = path, 'GET', Message()
    if token is not None:
        handler.headers['Authorization'] = f'Bearer {token}'
    sent = []
    handler._send = lambda status, body, t0, *args, **kwargs: sent.append((status, body, args, kwargs))
    return handler, sent


def decoded(sent: list) -> list:
    return [(status, loads(body), args, kwargs) for status, body, args, kwargs in sent]


def test_dated_lane_dispatches_without_ch_or_legacy_lookup() -> None:
    legacy, daily = Runtime(), DatedRuntime()
    box = bs.ChBox(Store(), name_summary_runtime=legacy, dated_name_summary_runtime=daily)
    handler, sent = request(box, '/api/name-summary?date=2026-10-06&name=.json&from=2026-10-05')
    handler.do_GET()
    assert daily.calls == [('diff', '2026-10-05', '2026-10-06', '.json', '')]
    assert legacy.calls == []
    assert decoded(sent) == [(200, {**BODY, 'schema': 'name-summary-diff-v1'}, (), {'headers': PRIVATE})]


@pytest.mark.parametrize('dated', [False, True])
def test_registry_uses_exact_selected_metadata_without_ch(dated: bool) -> None:
    legacy, daily = Runtime(), DatedRuntime()
    box = bs.ChBox(Store(), name_summary_runtime=legacy, dated_name_summary_runtime=daily if dated else None)
    handler, sent = request(box, '/api/name-summary-registry')
    handler.do_GET()
    expected = METADATA if dated else {'schema': 'name-summary-runtime-v1', 'target': 'fleet'}
    assert decoded(sent) == [(200, expected, (), {'headers': PRIVATE})]
    assert daily.calls == ([('metadata',)] if dated else [])
    assert legacy.calls == []


@pytest.mark.parametrize('token', [None, 'wrong'])
def test_registry_auth_precedes_bad_query_and_runtime(token: str | None) -> None:
    daily = DatedRuntime()
    handler, sent = request(bs.ChBox(Store(), dated_name_summary_runtime=daily), '/api/name-summary-registry?owner=private', token=token)
    handler.do_GET()
    assert sent == [(401, 'unauthorized', ('text/plain',), {'headers': PRIVATE})]
    assert daily.calls == []


def test_registry_parameters_refuse_before_lookup() -> None:
    daily = DatedRuntime()
    handler, sent = request(bs.ChBox(Store(), dated_name_summary_runtime=daily), '/api/name-summary-registry?date=2026-10-06')
    handler.do_GET()
    assert decoded(sent) == [(400, {'error': 'name-summary registry accepts no query parameters'}, (), {'headers': PRIVATE})]
    assert daily.calls == []


def test_disabled_registry_is_explicit_not_zero_or_source_fallback() -> None:
    handler, sent = request(bs.ChBox(Store()), '/api/name-summary-registry')
    handler.do_GET()
    assert decoded(sent) == [(501, {'error': 'name summary is not selected'}, (), {'headers': PRIVATE})]


@pytest.mark.parametrize('method', ['POST', 'HEAD', 'OPTIONS'])
def test_registry_unsupported_methods_are_private_authenticated_refusals(method: str) -> None:
    daily = DatedRuntime()
    handler, sent = request(bs.ChBox(Store(), dated_name_summary_runtime=daily), '/api/name-summary-registry')
    handler.command = method
    getattr(handler, 'do_' + method)()
    assert decoded(sent) == [(405, {'error': 'name-summary registry supports GET only'}, (), {'headers': {**PRIVATE, 'allow': 'GET'}})]
    assert daily.calls == []


@pytest.mark.parametrize('failure', ['exception', 'oversized', 'nan'])
def test_registry_failures_never_expose_partial_metadata(failure: str) -> None:
    daily = DatedRuntime()
    if failure == 'exception':
        daily.error = RuntimeError('private filesystem diagnostics')
    else:
        daily.metadata = lambda: {'value': 'x' * (64 << 10) if failure == 'oversized' else float('nan')}
    handler, sent = request(bs.ChBox(Store(), dated_name_summary_runtime=daily), '/api/name-summary-registry')
    handler.do_GET()
    assert decoded(sent) == [(503, {'error': 'name-summary scan registry unavailable'}, (), {'headers': {**PRIVATE, 'retry-after': '1'}})]


@pytest.mark.parametrize('fields,error', [
    ({'dated_l1_generation': Path('daily')}, 'dated root generation and explicit logical store are required together'),
    ({'dated_name_store': 'gcs_fleet'}, 'dated root generation and explicit logical store are required together'),
    ({'dated_l1_generation': Path('daily'), 'dated_name_store': 'gcs_fleet'}, 'dated roots require the existing stitched name-summary lane'),
    ({'dated_cold': True}, 'dated cold fallback requires a dated root generation'),
])
def test_startup_refuses_incomplete_dated_opt_in_before_io(fields: dict, error: str) -> None:
    with pytest.raises(ValueError) as caught:
        bs.ChBox(Store(), **fields).start()
    assert str(caught.value) == error


def test_startup_loads_dated_generation_once_with_explicit_legacy_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy, daily, calls = Runtime(), DatedRuntime(), []
    legacy.binding = SimpleNamespace(buckets=((1, 3, 'a'), (4, 5, 'b')))
    pinned = object()

    def load(root):
        calls.append(('load', root))
        return pinned

    def compose(old, published, *, logical_store, bucket_paths, cold, mega, catalog):
        calls.append(("compose", old is legacy, published is pinned, logical_store, bucket_paths, cold, mega, catalog))
        return daily

    monkeypatch.setattr(dated_hot_l1_publish, 'load', load)
    monkeypatch.setattr(dated_name_summary, 'DatedNameSummaryRuntime', compose)
    box = bs.ChBox(Store(), name_summary_enabled=True, name_summary_runtime=legacy, hot_l1_generation=tmp_path / 'old',
                   hot_l1_catalog=SimpleNamespace(target='fleet'), narrow_target='fleet', narrow_manifest={},
                   dated_l1_generation=tmp_path / 'daily', dated_name_store='gcs_fleet')
    box.start()
    box.start()
    assert calls == [('load', tmp_path / 'daily'), ('compose', True, True, 'gcs_fleet', ('a', 'b'), {}, None, None)]
    assert box.name_summary_runtime is legacy
    assert box.dated_name_summary_runtime is daily


@pytest.mark.parametrize('args,error', [
    (['-G', 'daily'], '--dated-l1-generation and --dated-name-store are required together'),
    (['-f', 'gcs_fleet'], '--dated-l1-generation and --dated-name-store are required together'),
    (['-G', 'daily', '-f', 'gcs_fleet'], '--dated-l1-generation requires --engine ch and --name-summary'),
    (['-e', 'ch', '-G', 'daily', '-f', 'gcs_fleet'], '--dated-l1-generation requires --engine ch and --name-summary'),
    (['-e', 'ch', '-L', '-g', 'old', '-N', 'fleet', '-C'], '--dated-cold requires --dated-l1-generation'),
])
def test_cli_refuses_incomplete_daily_selection(args: list[str], error: str) -> None:
    result = CliRunner().invoke(main, ['serve-query', '-A', *args, 'unused'])
    assert (result.exit_code, result.output.splitlines()) == (2, [
        'Usage: main serve-query [OPTIONS] ROOT', "Try 'main serve-query --help' for help.", '', 'Error: ' + error,
    ])


def test_cli_forwards_explicit_dated_flags_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(bs, 'serve', lambda box, **kwargs: calls.append((box.name_summary_enabled, box.dated_l1_generation, box.dated_name_store, box.dated_cold)))
    for extra in ([], ['-C']):
        result = CliRunner().invoke(main, ['serve-query', '-A', '-e', 'ch', '-L', '-g', 'old', '-N', 'fleet', '-G', 'daily', '-f', 'gcs_fleet', *extra, 'unused'])
        assert (result.exit_code, result.stdout, result.stderr) == (0, '', '')
    assert calls == [(True, Path('daily'), 'gcs_fleet', False), (True, Path('daily'), 'gcs_fleet', True)]


def test_startup_binds_each_dated_scans_name_index_with_cold_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud.chstore import daily_name_index

    legacy, daily, calls = Runtime(), DatedRuntime(), []
    legacy.binding = SimpleNamespace(buckets=((1, 3, 'a'), (4, 5, 'b')))
    catalog = SimpleNamespace(metadata=lambda: {'source': {'snapshot_db': 'daily_scalar_oct06'}})
    pinned = SimpleNamespace(catalogs={'2026-10-06': catalog})
    monkeypatch.setattr(dated_hot_l1_publish, 'load', lambda root: pinned)
    monkeypatch.setattr(daily_name_index, 'load', lambda ch, target: calls.append(('index', target)) or {'target': target})
    monkeypatch.setattr(dated_name_summary, 'DatedNameSummaryRuntime', lambda old, published, *, logical_store, bucket_paths, cold, mega, catalog: calls.append(("compose", cold, mega, catalog)) or daily)
    box = bs.ChBox(Store(), name_summary_enabled=True, name_summary_runtime=legacy, hot_l1_generation=tmp_path / 'old',
                   hot_l1_catalog=SimpleNamespace(target='fleet'), narrow_target='fleet', narrow_manifest={},
                   dated_l1_generation=tmp_path / 'daily', dated_name_store='gcs_fleet', dated_cold=True)
    box.start()
    assert calls == [('index', 'daily_scalar_oct06'), ('compose', {'2026-10-06': {'target': 'daily_scalar_oct06'}}, None, None)]
