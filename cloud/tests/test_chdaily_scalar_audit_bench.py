from hashlib import sha256
from json import loads
from pathlib import Path
from struct import pack
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import client, daily_scalar_audit_bench as module
from dt_cloud.chstore.daily_scalar import manifest_bytes, wire_select
from dt_cloud.cli import main
from test_chdaily_scalar import ROWS, descriptor, encoded, fresh, write_fixture  # noqa: F401
from chserver import ch_url  # noqa: F401


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    body = {'schema': 'daily-scalar-source-v1', 'complete': True, 'date': '2026-10-06', 'logical_store': 'gcs',
            'target': 'fixture', 'snapshot_db': 'fixture', 'prefix': '', 'nodes': 6,
            'source_rows': 7, 'selected_source_rows': 7, 'root': {'path': '', 'pre': 0, 'post': 5, 'b': 15, 'o': 5},
            'buckets': [{'path': 'a', 'pre': 1, 'post': 5}], 'limits': {}, 'stages': {},
            'source': {'schema': 'daily-scalar-input-v1', 'logical_store': 'gcs', 'date': '2026-10-06',
                       'identity': {'uri': 'gs://fixture/path-index.parquet', 'generation': 'g'}, 'bytes': 1, 'sha256': '1' * 64},
            'validation': dict.fromkeys(('source_hash_checked', 'prefix_closed', 'interval_endpoints_checked', 'scalar_rollups_checked'), True)}
    raw = manifest_bytes(body)
    path = tmp_path / 'manifest.json'
    path.write_bytes(raw)
    events, seq, streams = [], [], []
    monkeypatch.setattr(module, 'monotonic', lambda: 0)

    def uuid():
        seq.append(len(seq) + 1)
        return SimpleNamespace(hex=f'{len(seq):032x}')

    monkeypatch.setattr(module, 'uuid4', uuid)

    class Fake:
        timeout = 120

        def fork(self, **settings):
            events.append(('fork', settings))
            return Fake()

        def json(self, sql, *, settings):
            events.append(('json', sql, settings))
            return [[len(raw) - 1, raw.decode()[:-1]]]

        def stream(self, sql, *, fmt, settings):
            events.append(('stream', sql, fmt, settings))
            stream = iter_chunks()
            streams.append(stream)
            return stream

        def scalar(self, sql, *, settings):
            events.append(('scalar', sql, settings))
            return '0'

        def exec(self, sql, *, fmt):
            events.append(('exec', sql, fmt))

        def close(self):
            events.append(('close',))

    def iter_chunks():
        try:
            data = encoded(ROWS)
            for pos in range(0, len(data), 3):
                yield data[pos:pos + 3]
        finally:
            events.append(('stream_closed',))

    return SimpleNamespace(body=body, raw=raw, path=path, ch=Fake(), events=events, cls=Fake)


def test_complete_alternating_trials_exact_hashes_limits_and_read_only_calls(source) -> None:
    expected = b''.join(pack('<III', *row) for row in [(3, 3, 3), (4, 4, 4), (2, 2, 4), (5, 5, 5), (1, 1, 5), (0, 0, 5)])
    result = module.bench(source.ch, source.path, trials=2, seconds=30)
    assert result == {
        'schema': 'daily-scalar-audit-bench-v1', 'target': 'fixture', 'date': '2026-10-06', 'nodes': 6,
        'source_manifest_bytes': len(source.raw), 'source_manifest_sha256': sha256(source.raw).hexdigest(),
        'complete_endpoint_agreement': True, 'independent_source_oracle': False,
        'trials': [{'trial': trial, 'plan': plan, 'seconds': 0, 'endpoint_bytes': 72, 'endpoint_sha256': sha256(expected).hexdigest()}
                   for trial, plan in [(1, 'legacy'), (1, 'fused'), (2, 'fused'), (2, 'legacy')]],
        'limits': {'max_nodes': 1000000, 'trials': 2, 'total_seconds': 30, 'query_memory_bytes': 1 << 30,
                   'query_spill_bytes': 256 << 20, 'cleanup_seconds': 10},
        'cache_state': 'uncontrolled; alternating order, not cold-cache or serving latency',
    }
    assert [event[0] for event in source.events] == [
        'fork', 'json', 'fork', 'stream', 'stream_closed', 'fork', 'stream', 'stream_closed',
        'fork', 'stream', 'stream_closed', 'fork', 'stream', 'stream_closed', 'json', 'scalar',
        'close', 'close', 'close', 'close', 'close',
    ]
    assert [(event[1], event[2]) for event in source.events if event[0] == 'stream'] == [(wire_select('fixture'), 'RowBinary')] * 4
    assert source.events[0] == ('fork', {'max_threads': 1, 'max_memory_usage': 1 << 30,
                                        'max_temporary_data_on_disk_size_for_query': 256 << 20,
                                        'timeout_before_checking_execution_speed': 0, 'timeout_overflow_mode': 'throw'})


@pytest.mark.parametrize('kwargs', [{'trials': 0}, {'trials': 4}, {'trials': True}, {'seconds': 301}, {'seconds': True}])
def test_invalid_budget_refuses_before_source_or_server(source, kwargs: dict) -> None:
    with pytest.raises(ValueError) as caught:
        module.bench(source.ch, source.path, **kwargs)
    assert str(caught.value) == 'daily scalar audit benchmark requires 1..3 trials and 1..300 total seconds'
    assert source.events == []


def test_overcap_source_is_not_sampled(source) -> None:
    source.body['nodes'] = 1000001
    source.path.write_bytes(manifest_bytes(source.body))
    with pytest.raises(ValueError) as caught:
        module.bench(source.ch, source.path)
    assert str(caught.value) == 'daily scalar check requires a complete accepted source with at most the requested node cap'
    assert source.events == []


def test_changed_marker_refuses_and_cancels_only_owned_ids(source, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source.cls, 'json', lambda *args, **kwargs: [])
    with pytest.raises(ValueError) as caught:
        module.bench(source.ch, source.path)
    assert str(caught.value) == 'daily scalar audit benchmark source marker differs from its pinned manifest'
    assert source.events[1:] == [('exec', "KILL QUERY WHERE query_id IN ('daily_scalar_audit_00000000000000000000000000000001') SYNC", None), ('close',)]


def test_disagreeing_fused_hash_fails_without_success_result(source, monkeypatch: pytest.MonkeyPatch) -> None:
    def wrong(chunks, count, prefix):
        yield b'\0' * (12 * count)

    monkeypatch.setattr(module, 'audited_intervals', wrong)
    with pytest.raises(AssertionError) as caught:
        module.bench(source.ch, source.path)
    assert str(caught.value) == 'daily scalar audit benchmark complete endpoint identities disagree'
    assert source.events[-4:] == [
        ('exec', "KILL QUERY WHERE query_id IN ('daily_scalar_audit_00000000000000000000000000000001','daily_scalar_audit_00000000000000000000000000000002','daily_scalar_audit_00000000000000000000000000000003') SYNC", None),
        ('close',), ('close',), ('close',),
    ]


def test_changed_marker_after_complete_pair_invalidates_benchmark(source, monkeypatch: pytest.MonkeyPatch) -> None:
    original, markers = source.cls.json, []

    def changed(self, *args, **kwargs):
        markers.append(True)
        return original(self, *args, **kwargs) if len(markers) == 1 else []

    monkeypatch.setattr(source.cls, 'json', changed)
    with pytest.raises(ValueError) as caught:
        module.bench(source.ch, source.path)
    assert str(caught.value) == 'daily scalar audit benchmark source marker differs from its pinned manifest'
    assert [event[0] for event in source.events] == [
        'fork', 'json', 'fork', 'stream', 'stream_closed', 'fork', 'stream', 'stream_closed',
        'exec', 'close', 'close', 'close',
    ]


def test_total_deadline_failure_closes_stream_and_cancels_owned_queries(source, monkeypatch: pytest.MonkeyPatch) -> None:
    times = iter([0] * 6 + [121])
    monkeypatch.setattr(module, 'monotonic', lambda: next(times))
    with pytest.raises(TimeoutError) as caught:
        module.bench(source.ch, source.path)
    assert str(caught.value) == 'daily scalar audit benchmark exceeded its total wall budget'
    assert [event[0] for event in source.events] == ['fork', 'json', 'fork', 'stream', 'stream_closed', 'exec', 'close', 'close']
    assert source.events[-3] == ('exec', "KILL QUERY WHERE query_id IN ('daily_scalar_audit_00000000000000000000000000000001','daily_scalar_audit_00000000000000000000000000000002') SYNC", None)


def test_native_complete_source_wire_marker_hash_and_query_cleanup(fresh: tuple, tmp_path: Path) -> None:
    from dt_cloud.chstore.daily_scalar import build

    ch, target = fresh
    parquet, manifest = tmp_path / 'input.parquet', tmp_path / 'source.json'
    write_fixture(parquet)
    body = build(ch, target, parquet, descriptor(parquet), min_free_bytes=1)
    manifest.write_bytes(manifest_bytes(body))
    result = module.bench(ch, manifest, trials=2, seconds=30)
    expected = b''.join(pack('<III', *row) for row in [(3, 3, 3), (4, 4, 4), (2, 2, 4), (5, 5, 5), (1, 1, 5), (6, 6, 6), (0, 0, 6)])
    assert [(row['trial'], row['plan'], row['endpoint_bytes'], row['endpoint_sha256']) for row in result['trials']] == [
        (trial, plan, 84, sha256(expected).hexdigest()) for trial, plan in [(1, 'legacy'), (1, 'fused'), (2, 'fused'), (2, 'legacy')]
    ]
    assert (result['nodes'], result['complete_endpoint_agreement'], result['independent_source_oracle']) == (7, True, False)
    assert ch.scalar(f'SELECT doc FROM {target}.source_manifest') == manifest.read_text()[:-1]


def test_cli_exact_forwarding_primary_json_and_failure_no_stdout(source, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(client, 'Ch', lambda url: calls.append(('client', url)) or source.ch)
    monkeypatch.setattr(module, 'bench', lambda ch, manifest, **limits: calls.append(('bench', ch is source.ch, manifest, limits)) or {'complete_endpoint_agreement': True})
    result = CliRunner().invoke(main, ['ch-daily-scalar-audit-bench', str(source.path), '-s', '30', '-t', '2', '-U', 'http://fixture:8123'])
    assert (result.exit_code, result.exception, loads(result.stdout), result.stderr) == (0, None, {'complete_endpoint_agreement': True}, '')
    assert calls == [('client', 'http://fixture:8123'), ('bench', True, source.path, {'trials': 2, 'seconds': 30})]
    assert source.events == [('close',)]

    def fail(*args, **kwargs):
        raise ValueError('refused fixture')

    monkeypatch.setattr(module, 'bench', fail)
    result = CliRunner().invoke(main, ['ch-daily-scalar-audit-bench', str(source.path)])
    assert (result.exit_code, type(result.exception), str(result.exception), result.stdout, result.stderr) == (1, ValueError, 'refused fixture', '', '')
