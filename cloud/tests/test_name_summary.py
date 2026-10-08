"""Stitched summaries conserve exact weights and fail closed under bounded work."""

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from json import dumps
from os import cpu_count, environ
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from typing import Callable

import pytest

from dt_cloud.chstore import name_summary as module
from dt_cloud.chstore.client import Ch
from dt_cloud.chstore.coarse import CoarseRequest
from dt_cloud.chstore.hot_l1_batch_catalog import HotL1BatchCatalog
from dt_cloud.chstore.hot_l1_catalog import CatalogRequest, SCOPE
from test_chhot_l1_batch_catalog import artifact
from test_chhot_l1 import fleet  # noqa: F401
from chserver import ch_db, ch_url  # noqa: F401


def artifacts() -> list[dict]:
    bodies = [artifact('2026-10-04'), artifact()]
    for body in bodies:
        for entry in body['results']:
            entry['buckets'].extend({'pre': pre, 'post': pre, 'path': path, 'b': 0, 'o': 0} for pre, path in zip(range(9, 13), 'cdef', strict=True))
    return bodies


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    bodies = artifacts()
    catalog = HotL1BatchCatalog.from_bytes(dumps(body).encode() for body in bodies)
    bounds = tuple((row['pre'], row['post'], row['path']) for row in bodies[0]['results'][0]['buckets'])
    binding = module.SourceBinding('fleet', (('2026-10-04', 'snapshot_20261004'), ('2026-10-05', 'snapshot_20261005')),
                                   bounds, 'a' * 32, 'b' * 64)
    state = SimpleNamespace(calls=[], bodies=bodies, binding=binding, catalog=catalog, clean_fail=False,
                            cancel_fail=False, block=None, entered=Event(), clients=[])

    class Client:
        def __init__(self, url, target, deadline, stopped, request_id):
            self.deadline, self.stopped = deadline, stopped
            state.clients.append(self)
            state.calls.append(('client', url, target, request_id))

        def cleanup(self):
            state.calls.append(('cleanup',))
            if state.clean_fail:
                raise RuntimeError('private cleanup failure')

    def cancel(ch, *, cancel):
        state.calls.append(('cancel', cancel))
        if state.cancel_fail:
            raise RuntimeError('private cancel failure')

    def build(ch, target, day, pattern, **caps):
        state.calls.append(('build', target, day, pattern, caps))
        state.entered.set()
        if state.block is not None:
            state.block.wait(1)
        body = state.bodies[0 if day == '2026-10-04' else 1]
        return {'target': target, 'date': day, 'pattern': pattern, 'snapshot_db': body['snapshot_db'],
                'scope': SCOPE, 'exact': True, 'incremental': False, **deepcopy(body['results'][0]),
                'pattern': pattern, 'work_bounds': {'max_names': 200000, 'max_postings': 100000, 'max_outer_roots': 100000},
                'direct_matching_rows': 7, 'stages': {'vocabulary_s': .01, 'postings_s': .02, 'directory_roots_s': .03, 'aggregate_s': .04}}

    monkeypatch.setattr(module, 'DeadlineCh', Client)
    monkeypatch.setattr(module, 'cancel_owned', cancel)
    monkeypatch.setattr(module, 'build', build)
    monkeypatch.setattr(module, 'uuid4', lambda: SimpleNamespace(hex='c' * 32))
    state.runtime = module.NameSummaryRuntime(catalog, binding, 'http://loopback:8123')
    state.runtime.recovery_seconds = 3600  # quarantine stays put unless a test shortens it
    return state


def expected_view(state: SimpleNamespace, date: str, pattern: str, plan: str) -> dict:
    body = state.bodies[0 if date == '2026-10-04' else 1]
    result = {'schema': 'name-summary-v1', 'target': 'fleet', 'date': date, 'pattern': pattern, 'path': '',
              'exact': True, 'incremental': False, 'levels': 1, 'scope': SCOPE, 'plan': plan,
              **{key: deepcopy(body['results'][0][key]) for key in ('root', 'buckets')},
              'source': 'registered precomputed batch artifact' if plan == 'catalog' else 'bounded dated name postings; directory rollups are atomic',
              'validation': {'source_prefix_proofs_checked': True, 'independent_query_source_oracle': False,
                             'description': 'pinned catalog validation' if plan == 'catalog' else 'bounded exact first-hit coverage; no per-request source oracle'},
              'source_identity': {'generation': 'a' * 32, 'snapshot_db': body['snapshot_db'], 'history_manifest_sha256': 'b' * 64}}
    if plan == 'catalog':
        result['validation']['catalog'] = body['validation']
    else:
        result.update(work_bounds={'max_names': 200000, 'max_postings': 100000, 'max_outer_roots': 100000}, direct_matching_rows=7,
                      stages={'vocabulary_s': .01, 'postings_s': .02, 'directory_roots_s': .03, 'aggregate_s': .04})
    return result


def test_hot_body_uses_no_source_and_cold_body_uses_exact_caps_and_verified_cleanup(runtime: SimpleNamespace) -> None:
    state = runtime
    assert state.runtime.view('2026-10-05', '.JSON') == expected_view(state, '2026-10-05', '.json', 'catalog')
    assert state.calls == []
    assert state.runtime.view('2026-10-05', 'datakit') == expected_view(state, '2026-10-05', 'datakit', 'bounded-name-postings')
    assert state.calls == [('client', 'http://loopback:8123', 'fleet', 'name_summary_' + 'c' * 32),
                           ('build', 'fleet', '2026-10-05', 'datakit', module.CAPS),
                           ('cancel', False), ('cleanup',), ('cancel', False)]
    assert (state.runtime.quarantined, state.runtime.gate.acquire(blocking=False)) == (False, True)
    state.runtime.gate.release()


def test_cold_diff_one_worker_deadline_six_buckets_zero_byte_counts_and_negative_delta(runtime: SimpleNamespace) -> None:
    state = runtime
    before, after = (expected_view(state, day, 'datakit', 'bounded-name-postings') for day in ('2026-10-04', '2026-10-05'))
    expected = {'schema': 'name-summary-diff-v1', 'target': 'fleet', 'pattern': 'datakit', 'path': '',
                'exact': True, 'incremental': False, 'levels': 1, 'scope': SCOPE, 'before': before, 'after': after,
                'delta': {'b': -6, 'o': 0}, 'buckets': [
                    {'pre': 1, 'post': 4, 'path': 'a', 'before': {'b': 8, 'o': 3}, 'after': {'b': 2, 'o': 1}, 'delta': {'b': -6, 'o': -2}},
                    {'pre': 5, 'post': 8, 'path': 'b', 'before': {'b': 0, 'o': 2}, 'after': {'b': 0, 'o': 4}, 'delta': {'b': 0, 'o': 2}},
                    *[{'pre': pre, 'post': pre, 'path': path, 'before': {'b': 0, 'o': 0}, 'after': {'b': 0, 'o': 0}, 'delta': {'b': 0, 'o': 0}} for pre, path in zip(range(9, 13), 'cdef', strict=True)],
                ]}
    assert state.runtime.diff('2026-10-04', '2026-10-05', 'datakit') == expected
    assert state.calls == [('client', 'http://loopback:8123', 'fleet', 'name_summary_' + 'c' * 32),
                           *[('build', 'fleet', day, 'datakit', module.CAPS) for day in ('2026-10-04', '2026-10-05')],
                           ('cancel', False), ('cleanup',), ('cancel', False)]


@pytest.mark.parametrize('date,pattern,path,message', [
    ('2026-10-03', 'datakit', '', 'name summary scan is outside the pinned frozen source; no fallback'),
    ('2026-02-30', 'datakit', '', 'name summary requires valid ISO scans and one UTF-8 NUL/slash-free literal of at most 512 characters'),
    ('2026-10-05', 'a/b', '', 'name summary requires valid ISO scans and one UTF-8 NUL/slash-free literal of at most 512 characters'),
    ('2026-10-05', 'a\0b', '', 'name summary requires valid ISO scans and one UTF-8 NUL/slash-free literal of at most 512 characters'),
    ('2026-10-05', '\ud800', '', 'name summary requires valid ISO scans and one UTF-8 NUL/slash-free literal of at most 512 characters'),
    ('2026-10-05', 'datakit', 'a', 'name summary serves the global root only; no drill fallback'),
])
def test_unsupported_requests_never_call_source(runtime: SimpleNamespace, date: str, pattern: str, path: str, message: str) -> None:
    with pytest.raises(CatalogRequest) as caught:
        runtime.runtime.view(date, pattern, path=path)
    assert (str(caught.value), runtime.calls) == (message, [])


def test_busy_lanes_are_fail_fast_independent_and_quarantine_preserves_catalog(runtime: SimpleNamespace) -> None:
    state = runtime
    assert state.runtime.gate.acquire(blocking=False) is True
    assert state.runtime.view('2026-10-05', '.json') == expected_view(state, '2026-10-05', '.json', 'catalog')
    with pytest.raises(module.SummaryBusy) as caught:
        state.runtime.view('2026-10-05', 'datakit')
    assert (str(caught.value), state.calls) == ('name summary cold serving slot busy; retry shortly', [])
    state.runtime.gate.release()
    assert [state.runtime.hot_gate.acquire(blocking=False) for _ in range(2)] == [True, True]
    with pytest.raises(module.SummaryBusy) as caught:
        state.runtime.view('2026-10-05', '.json')
    assert str(caught.value) == 'name summary catalog serving slots busy; retry shortly'
    assert state.runtime.view('2026-10-05', 'datakit') == expected_view(state, '2026-10-05', 'datakit', 'bounded-name-postings')
    for _ in range(2):
        state.runtime.hot_gate.release()
    state.clean_fail = True
    with pytest.raises(module.SummaryUnavailable) as caught:
        state.runtime.view('2026-10-05', 'datakit')
    assert str(caught.value) == 'name summary cleanup could not be verified; cold lane quarantined'
    assert (state.runtime.quarantined, state.runtime.gate.acquire(blocking=False)) == (True, False)
    assert state.runtime.view('2026-10-05', '.json') == expected_view(state, '2026-10-05', '.json', 'catalog')
    with pytest.raises(module.SummaryBusy) as caught:
        state.runtime.view('2026-10-05', 'datakit')
    assert str(caught.value) == 'name summary cold lane is quarantined; catalog reads remain available'


def test_deadline_returns_no_partial_body_and_retains_slot_until_owned_cleanup(runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    state = runtime
    state.block = Event()
    monkeypatch.setattr(module, 'COMPUTE_SECONDS', .03)
    with pytest.raises(module.SummaryDeadline) as caught:
        state.runtime.view('2026-10-05', 'datakit')
    assert str(caught.value) == 'name summary exceeded its total compute deadline; no partial result'
    assert (state.entered.is_set(), state.runtime.gate.acquire(blocking=False)) == (True, False)
    cleaned = Event()
    original = module.cancel_owned

    def cancel(source, *, cancel):
        original(source, cancel=cancel)
        if cancel is False:
            cleaned.set()

    monkeypatch.setattr(module, 'cancel_owned', cancel)
    state.block.set()
    assert cleaned.wait(1) is True
    assert state.clients[0].stopped.is_set() is True
    assert [call for call in state.calls if call[0] == 'build'] == [('build', 'fleet', '2026-10-05', 'datakit', module.CAPS)]


def test_deadline_client_unique_owned_ids_no_next_statement_after_expiry_and_strict_temp_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    clock, calls = [10.0], []
    monkeypatch.setattr(module, 'monotonic', lambda: clock[0])

    def opened(ch, sql, data=None, settings=None):
        calls.append((sql, settings, ch.timeout))
        return BytesIO(b'')

    monkeypatch.setattr(Ch, '_open', opened)
    source = module.DeadlineCh('http://unused:8123', 'fleet', 15., Event(), 'owned')
    source.exec('SELECT 1', fmt=None)
    clock[0] = 14.5
    source.exec('SELECT 2', fmt=None)
    clock[0] = 15.
    with pytest.raises(module.SummaryDeadline) as caught:
        source.exec('SELECT 3', fmt=None)
    assert str(caught.value) == 'name summary exceeded its total compute deadline; no partial result'
    source.stopped.set()
    source._tmp = ['hot_l1_names_owned', 'hot_l1_postings_owned']
    source.cleanup()
    assert calls == [
        # The socket outlives each statement's own limit by the transport grace (1 s): ClickHouse reports the timeout.
        ('SELECT 1', {'query_id': 'owned_0001', 'max_execution_time': 5., 'timeout_before_checking_execution_speed': 0, 'timeout_overflow_mode': 'throw'}, 6.),
        ('SELECT 2', {'query_id': 'owned_0002', 'max_execution_time': .5, 'timeout_before_checking_execution_speed': 0, 'timeout_overflow_mode': 'throw'}, 1.5),
        *[(f'DROP TEMPORARY TABLE IF EXISTS {table}', {'query_id': f'owned_{i:04d}', 'max_execution_time': 2., 'timeout_before_checking_execution_speed': 0, 'timeout_overflow_mode': 'throw'}, 3.) for i, table in enumerate(['hot_l1_postings_owned', 'hot_l1_names_owned'], 3)],
    ]
    assert (source.owned_ids(), source._tmp, source.cleanup_deadline, source.settings['session_timeout']) == (('owned_0001', 'owned_0002', 'owned_0003', 'owned_0004'), [], None, '60')


def test_cancel_only_owned_ids_verifies_quiescence_without_session_or_source_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class Control:
        def __init__(self, *args, **kwargs):
            calls.append(('open', args, kwargs))

        def exec(self, sql, *, fmt):
            calls.append(('exec', sql, fmt))

        def scalar(self, sql):
            calls.append(('scalar', sql))
            return '0'

        def close(self):
            calls.append(('close',))

    monkeypatch.setattr(module, 'Ch', Control)
    source = SimpleNamespace(url='http://unused:8123', db='fleet', owned_ids=lambda: ('owned_0001', 'owned_0002'))
    module.cancel_owned(source, cancel=True)
    assert calls == [('open', ('http://unused:8123',), {'db': 'fleet', 'session': False, 'timeout': 1, 'max_execution_time': 1,
                      'timeout_before_checking_execution_speed': 0, 'timeout_overflow_mode': 'throw'}),
                     ('exec', "KILL QUERY WHERE query_id IN ('owned_0001','owned_0002') SYNC", None),
                     ('scalar', "SELECT count() FROM system.processes WHERE query_id IN ('owned_0001','owned_0002')"), ('close',)]


def test_startup_binding_compares_exact_published_dates_databases_geometry_and_proofs(runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    state, calls = runtime, []
    raw = dumps({'prefix': '', 'dates': ['2026-10-04', '2026-10-05'], 'dbs': ['snapshot_20261004', 'snapshot_20261005']})
    published = {'metadata': state.catalog.metadata(), 'source_prefix_validation': {'checked': True}, 'generation': 'a' * 32}
    ch = SimpleNamespace(scalar=lambda sql: calls.append(sql) or raw)
    monkeypatch.setattr(module, '_buckets', lambda ch, target: [list(row) for row in state.binding.buckets])
    assert module.bind(ch, state.catalog, published, 'fleet') == module.SourceBinding('fleet', state.binding.dates, state.binding.buckets, 'a' * 32, sha256(raw.encode()).hexdigest())
    assert calls == ['SELECT doc FROM fleet.history_manifest']
    published['source_prefix_validation']['checked'] = False
    with pytest.raises(ValueError) as caught:
        module.bind(ch, state.catalog, published, 'fleet')
    assert str(caught.value) == 'name summary requires published artifact-bound prefix proofs'
    assert calls == ['SELECT doc FROM fleet.history_manifest']


@pytest.mark.parametrize('change,message', [
    ('target', 'name summary published target/metadata differs from selected source'),
    ('db', 'name summary frozen source dates/databases differ from published catalog'),
    ('dates', 'name summary frozen source dates/databases differ from published catalog'),
    ('bounds', 'name summary frozen bucket bounds differ from published catalog'),
])
def test_startup_wrong_identity_never_accepts(runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, change: str, message: str) -> None:
    state = runtime
    manifest = {'prefix': '', 'dates': ['2026-10-04', '2026-10-05'], 'dbs': ['snapshot_20261004', 'snapshot_20261005']}
    published = {'metadata': state.catalog.metadata(), 'source_prefix_validation': {'checked': True}, 'generation': 'a' * 32}
    bounds = [list(row) for row in state.binding.buckets]
    if change == 'db':
        manifest['dbs'][0] = 'wrong_db'
    elif change == 'dates':
        manifest['dates'].reverse()
    elif change == 'bounds':
        bounds[-1][1] += 1
    monkeypatch.setattr(module, '_buckets', lambda ch, target: bounds)
    ch = SimpleNamespace(scalar=lambda sql: dumps(manifest))
    with pytest.raises(ValueError) as caught:
        module.bind(ch, state.catalog, published, 'wrong_target' if change == 'target' else 'fleet')
    assert str(caught.value) == message


def test_real_publisher_pins_proofs_once_and_optional_verified_pair_avoids_reparse(runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from dt_cloud.chstore.hot_l1_publish import publish
    from test_chhot_l1_batch_catalog import write
    from test_chhot_l1_publish import prefix_proof

    state, calls = runtime, []
    paths = tuple(write(tmp_path / f'{i}.json', body) for i, body in enumerate(state.bodies))
    proofs = tuple(tmp_path / f'proof-{i}.json' for i in range(2))
    for path, proof in zip(paths, proofs, strict=True):
        prefix_proof(proof, path)
    root = tmp_path / 'published'
    published = publish(paths, root, prefix_proofs=proofs)
    raw = dumps({'prefix': '', 'dates': ['2026-10-04', '2026-10-05'], 'dbs': ['snapshot_20261004', 'snapshot_20261005']})

    class Control:
        def __init__(self, *args, **kwargs):
            calls.append(('open', args, kwargs))

        def scalar(self, sql):
            calls.append(('scalar', sql))
            return raw

        def close(self):
            calls.append(('close',))

    loader = module.load_pinned

    def load_pinned(root, pinned):
        calls.append(('load', root, pinned))
        return loader(root, pinned)

    monkeypatch.setattr(module, 'Ch', Control)
    monkeypatch.setattr(module, 'load_pinned', load_pinned)
    monkeypatch.setattr(module, '_buckets', lambda ch, target: [list(row) for row in state.binding.buckets])
    loaded = module.NameSummaryRuntime.load(root, 'http://source:8123', target='fleet')
    expected_client = [('open', ('http://source:8123',), {'db': 'fleet', 'timeout': 2, 'max_execution_time': 2,
                       'timeout_before_checking_execution_speed': 0, 'timeout_overflow_mode': 'throw'}),
                       ('scalar', 'SELECT doc FROM fleet.history_manifest'), ('close',)]
    assert calls == [('load', root, published), *expected_client]
    calls.clear()
    reused = module.NameSummaryRuntime.load(root, 'http://source:8123', target='fleet', catalog=loaded.catalog, published=published)
    assert calls == expected_client
    assert (reused.catalog is loaded.catalog, reused.binding == loaded.binding) == (True, True)
    assert reused.metadata() == {'schema': 'name-summary-registry-v1', 'target': 'fleet', 'dates': ['2026-10-04', '2026-10-05'],
        'levels': 1, 'scope': SCOPE, 'catalog_patterns': {'2026-10-04': 2, '2026-10-05': 2}, 'source_prefix_proofs_checked': True,
        'cold_slots': 1, 'catalog_slots': 2, 'compute_seconds': 5., 'work_bounds': module.CAPS, 'cold_quarantined': False}
    with pytest.raises(ValueError) as caught:
        module.NameSummaryRuntime.load(root, 'http://source:8123', target='fleet', catalog=loaded.catalog)
    assert str(caught.value) == 'name summary catalog reuse requires its exact verified pinned manifest'


def wait_for(predicate: Callable[[], bool], seconds: float = 3) -> bool:
    deadline = monotonic() + seconds
    while not predicate() and monotonic() < deadline:
        sleep(.01)
    return predicate()


def test_quarantine_recovers_once_owned_work_verifiably_gone(runtime: SimpleNamespace) -> None:
    state = runtime
    state.runtime.recovery_seconds = .2
    state.clean_fail = True
    with pytest.raises(module.SummaryUnavailable) as caught:
        state.runtime.view('2026-10-05', 'datakit')
    assert (str(caught.value), state.runtime.quarantined) == ('name summary cleanup could not be verified; cold lane quarantined', True)
    with pytest.raises(module.SummaryBusy) as caught:
        state.runtime.view('2026-10-05', 'datakit')
    assert str(caught.value) == 'name summary cold lane is quarantined; catalog reads remain available'
    state.calls.clear()
    state.clean_fail = False
    assert wait_for(lambda: not state.runtime.quarantined) is True
    # The recovery re-killed and re-verified the same owned client, then dropped its tables.
    assert state.calls == [('cancel', True), ('cleanup',), ('cancel', False)]
    assert state.runtime.view('2026-10-05', 'datakit') == expected_view(state, '2026-10-05', 'datakit', 'bounded-name-postings')
    assert (state.runtime.quarantined, state.runtime.gate.acquire(blocking=False)) == (False, True)


def test_quarantine_stays_when_every_recovery_attempt_fails(runtime: SimpleNamespace) -> None:
    state = runtime
    state.runtime.recovery_seconds = .01
    state.clean_fail = True
    with pytest.raises(module.SummaryUnavailable):
        state.runtime.view('2026-10-05', 'datakit')
    state.calls.clear()
    assert wait_for(lambda: state.calls.count(('cleanup',)) == module.RECOVERY_ATTEMPTS) is True
    sleep(.1)
    assert state.calls == [('cancel', True), ('cleanup',)] * module.RECOVERY_ATTEMPTS
    assert (state.runtime.quarantined, state.runtime.gate.acquire(blocking=False)) == (True, False)


@pytest.mark.parametrize('failure', ['cap', 'wrong_literal', 'transport', 'cancel'])
def test_cold_failure_never_returns_zero_or_partial_body_and_unknown_cleanup_quarantines(runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    state, original = runtime, module.build

    def build(*args, **kwargs):
        if failure == 'cap':
            raise CoarseRequest('direct-posting cap exceeded')
        body = original(*args, **kwargs)
        if failure == 'wrong_literal':
            body['pattern'] = 'wrong'
        if failure == 'transport':
            args[0].transport_uncertain = True
            raise OSError('private source transport failure')
        return body

    monkeypatch.setattr(module, 'build', build)
    state.cancel_fail = failure == 'cancel'
    with pytest.raises(module.SummaryUnavailable) as caught:
        state.runtime.view('2026-10-05', 'datakit')
    quarantined = failure in ('transport', 'cancel')
    expected_message = ('name summary cleanup could not be verified; cold lane quarantined' if quarantined else
                        'name summary returned an inconsistent frozen partition; no partial result' if failure == 'wrong_literal' else
                        'bounded name summary unavailable or over budget; no partial result')
    assert str(caught.value) == expected_message
    assert (state.runtime.quarantined, state.runtime.gate.acquire(blocking=False)) == (quarantined, not quarantined)
    if not quarantined:
        state.runtime.gate.release()
    assert state.runtime.view('2026-10-05', '.json') == expected_view(state, '2026-10-05', '.json', 'catalog')


def test_failed_temp_drop_is_not_suppressed_and_all_owned_drops_are_attempted(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(module, 'monotonic', lambda: 1.)

    def opened(ch, sql, data=None, settings=None):
        calls.append(sql)
        if sql == 'DROP TEMPORARY TABLE IF EXISTS second':
            raise RuntimeError('private CH drop failure')
        return BytesIO(b'')

    monkeypatch.setattr(Ch, '_open', opened)
    source = module.DeadlineCh('http://unused:8123', 'fleet', 6., Event(), 'owned')
    source._tmp = ['first', 'second']
    with pytest.raises(module.SummaryUnavailable) as caught:
        source.cleanup()
    assert str(caught.value) == 'name summary cleanup could not be verified; cold lane quarantined'
    assert (calls, source._tmp, source.cleanup_deadline) == (['DROP TEMPORARY TABLE IF EXISTS second', 'DROP TEMPORARY TABLE IF EXISTS first'], ['first', 'second'], None)


def test_real_deadline_ch_fractional_settings_owned_ids_and_temp_drop(ch_db: str, ch_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    now = monotonic()
    monkeypatch.setattr(module, 'monotonic', lambda: now)
    source = module.DeadlineCh(ch_url, ch_db, now + .25, Event(), 'name_summary_fraction_' + ch_db)
    try:
        assert source.json("SELECT getSetting('max_execution_time'),getSetting('timeout_before_checking_execution_speed')") == [[.25, 0.]]
        source.tmp('name_summary_fraction_temp', 'SELECT toUInt64(1) AS n')
        assert source.scalar('SELECT n FROM name_summary_fraction_temp') == '1'
        module.cancel_owned(source, cancel=False)
        source.cleanup()
        module.cancel_owned(source, cancel=False)
        assert source.scalar('EXISTS TABLE name_summary_fraction_temp') == '0'
        assert (source._tmp, source.transport_uncertain, source.settings['max_memory_usage'], source.settings['max_temporary_data_on_disk_size_for_query']) == ([], False, str(4 << 30), str(4 << 30))
        assert source.owned_ids() == tuple(f'name_summary_fraction_{ch_db}_{i:04d}' for i in range(1, 7))
    finally:
        module.cancel_owned(source, cancel=True)
        source.cleanup()


def test_real_runtime_tiny_fleet_exact_cold_diff_then_forced_expiry_no_orphans(
    fleet: dict,
    ch_db: str,
    ch_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dt_cloud.chstore.hot_l1 import build, _buckets

    ch = Ch(ch_url, db=ch_db)
    dates, bodies = ('2026-10-04', '2026-10-05'), []
    try:
        for day in dates:
            body = artifact(day)
            accepted = build(ch, ch_db, day, '.json', min_free_bytes=0)
            body.update(target=ch_db, snapshot_db=accepted['snapshot_db'])
            rows, path_bytes = ch.json(f"SELECT count(),sum(length(path)) FROM {accepted['snapshot_db']}.nodes")[0]
            body['source_validation'] = {'rows': rows, 'invalid_utf8_paths': 0, 'invalid_scalar_rows': 0, 'path_bytes': path_bytes}
            body['queries']['header']['target'] = ch_db
            body['results'][0].update(root=accepted['root'], buckets=accepted['buckets'])
            body['results'][1].update(buckets=[{**row, 'b': 0, 'o': 0} for row in accepted['buckets']])
            bodies.append(body)
        bounds = tuple(tuple(row) for row in _buckets(ch, ch_db))
    finally:
        ch.close()
    catalog = HotL1BatchCatalog.from_bytes(dumps(body).encode() for body in bodies)
    binding = module.SourceBinding(ch_db, tuple((day, body['snapshot_db']) for day, body in zip(dates, bodies, strict=True)), bounds, 'a' * 32, 'b' * 64)
    runtime = module.NameSummaryRuntime(catalog, binding, ch_url)
    sources, real_client = [], module.DeadlineCh

    def client(*args, **kwargs):
        source = real_client(*args, **kwargs)
        sources.append(source)
        return source

    monkeypatch.setattr(module, 'DeadlineCh', client)
    monkeypatch.setattr(module, 'build', lambda *args, **kwargs: build(*args, **kwargs, min_free_bytes=0))
    body = runtime.diff(*dates, 'json')
    for side, own in ((body['before'], fleet['own']), (body['after'], fleet['after'])):
        expected = []
        for lo, hi, path in bounds:
            values = [value for name, value in own.items() if name.split('/')[0] == path and 'json' in name.lower()]
            expected.append({'pre': lo, 'post': hi, 'path': path, 'b': sum(value[0] for value in values), 'o': sum(value[1] for value in values)})
        assert side['buckets'] == expected
        assert side['root'] == {'b': sum(row['b'] for row in expected), 'o': sum(row['o'] for row in expected)}
        assert (side['plan'], side['work_bounds']) == ('bounded-name-postings', {'max_names': 200000, 'max_postings': 100000, 'max_outer_roots': 100000})
    assert (body['schema'], body['delta'], len(sources), sources[0]._tmp, runtime.quarantined) == ('name-summary-diff-v1', {'b': -54, 'o': -6}, 1, [], False)
    module.cancel_owned(sources[0], cancel=False)

    # One tiny sleep (no large rows/scan) outlives the deadline. ClickHouse ends it (the statement's own limit, or the
    # watchdog's KILL) and says so over the open connection, so its cleanup is verified: the expired request alone
    # fails, and the slot serves the very next one rather than quarantining.
    def slow(source, *args, **kwargs):
        source.tmp('name_summary_expiry_temp', 'SELECT toUInt64(1) AS n')
        source.exec('SELECT sleep(0.2)')
        raise AssertionError('expired source must not publish a body')

    monkeypatch.setattr(module, 'build', slow)
    monkeypatch.setattr(module, 'COMPUTE_SECONDS', .05)
    with pytest.raises(module.SummaryDeadline) as caught:
        runtime.view('2026-10-05', 'cold-expiry')
    assert str(caught.value) == 'name summary exceeded its total compute deadline; no partial result'
    expired = sources[1]
    probe = Ch(ch_url, db=ch_db, session=False, timeout=1, session_id=expired.settings['session_id'])
    try:
        monkeypatch.setattr(module, 'build', lambda *args, **kwargs: build(*args, **kwargs, min_free_bytes=0))
        monkeypatch.setattr(module, 'COMPUTE_SECONDS', 5.)
        assert runtime.view('2026-10-05', 'json')['plan'] == 'bounded-name-postings'
        assert (runtime.quarantined, expired.transport_uncertain, probe.scalar('EXISTS TABLE name_summary_expiry_temp')) == (False, False, '0')
    finally:
        probe.close()
    assert runtime.view('2026-10-05', '.json')['plan'] == 'catalog'


def test_cold_statements_use_the_configured_thread_count(monkeypatch: pytest.MonkeyPatch) -> None:
    # The one cold slot may use the node (up to 16 threads); the env overrides it.
    assert module.COLD_THREADS == int(environ.get('NAME_SUMMARY_COLD_THREADS') or min(16, cpu_count() or 4))
    monkeypatch.setattr(module, 'COLD_THREADS', 12)
    ch = module.DeadlineCh('http://loopback:8123', 'fleet', monotonic() + 5, Event(), 'name_summary_' + 'd' * 32)
    assert (ch.settings['max_threads'], ch.settings['max_execution_time']) == ('12', '5.0')


@pytest.mark.parametrize('counts,ok', [(['1', '1', '0'], True), (['1'] * 1000, False)])
def test_quiescence_waits_briefly_for_finished_owned_queries_to_leave_the_process_list(monkeypatch: pytest.MonkeyPatch, counts, ok) -> None:
    """A query whose result was already read can stay in `system.processes` for a moment while it finalizes: that
    is not unverified cleanup (which quarantines the lane), so verification polls briefly before refusing."""
    remaining = list(counts)

    class Control:
        def __init__(self, *args, **kwargs):
            pass

        def scalar(self, sql):
            return remaining.pop(0)

        def close(self):
            pass

    monkeypatch.setattr(module, 'Ch', Control)
    monkeypatch.setattr(module, 'QUIESCENCE_SECONDS', .2)
    source = SimpleNamespace(url='http://unused:8123', db='fleet', owned_ids=lambda: ('owned_0001',))
    if ok:
        module.cancel_owned(source, cancel=False)
        assert remaining == []
    else:
        with pytest.raises(module.SummaryUnavailable) as caught:
            module.cancel_owned(source, cancel=False)
        assert (str(caught.value), len(remaining) < 1000) == ('name summary query quiescence could not be verified', True)


def test_a_request_waits_briefly_for_the_slot_a_finishing_request_releases(runtime: SimpleNamespace) -> None:
    """A request arriving while the previous one's owned work is still being cancelled and verified is served once the
    slot frees (within `SLOT_WAIT_SECONDS`), not refused as busy."""
    from threading import Timer

    state = runtime
    assert state.runtime.gate.acquire(blocking=False) is True
    Timer(.2, state.runtime.gate.release).start()
    start = monotonic()
    assert state.runtime.view('2026-10-05', 'datakit') == expected_view(state, '2026-10-05', 'datakit', 'bounded-name-postings')
    assert .15 < monotonic() - start < module.SLOT_WAIT_SECONDS
