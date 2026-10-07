"""Independent literal subtree oracles, including owner/kind and zero objects."""

from copy import deepcopy
from hashlib import sha256
from json import dumps, loads
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud.chstore import daily_scalar_check as module

PREFIX = 'a/run.json'
INPUT = [
    ('a', 'u', 'dir', 1, 21, 5), ('a', None, 'dir', 1, 0, 1),
    (PREFIX, 'u', 'dir', 2, 12, 2), (PREFIX, None, 'file', 2, 0, 1),
    (PREFIX + '/x', 'u', 'file', 3, 7, 1), (PREFIX + '/Å😀', 'u', 'file', 3, 5, 1),
    (PREFIX + '-b', None, 'file', 2, 0, 1), (PREFIX + '0', 'u', 'dir', 2, 9, 1),
    (PREFIX + '0/sneak', 'u', 'file', 3, 9, 1), ('a/zero', None, 'file', 2, 0, 1), ('b', None, 'file', 1, 0, 1),
]
GLOBAL = [(0, 9, 0, '', 21, 7), (1, 8, 1, 'a', 21, 6), (2, 4, 2, PREFIX, 12, 3),
          (3, 3, 3, PREFIX + '/x', 7, 1), (4, 4, 3, PREFIX + '/Å😀', 5, 1),
          (5, 5, 2, PREFIX + '-b', 0, 1), (6, 7, 2, PREFIX + '0', 9, 1),
          (7, 7, 3, PREFIX + '0/sneak', 9, 1), (8, 8, 2, 'a/zero', 0, 1), (9, 9, 1, 'b', 0, 1)]
SUBTREE = [(0, 2, 2, PREFIX, 12, 3), (1, 1, 3, PREFIX + '/x', 7, 1), (2, 2, 3, PREFIX + '/Å😀', 5, 1)]


def canonical(body: dict) -> bytes:
    return (dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / 'source.parquet'
    # Root/children and owner slices lie in different unsorted row groups.
    columns = dict(zip(('path', 'usr', 'kind', 'depth', 'size', 'n_files'), zip(*reversed(INPUT)), strict=True))
    pq.write_table(pa.table(columns), source, row_group_size=2)
    raw = source.read_bytes()
    body = {'schema': 'daily-scalar-source-v1', 'complete': True, 'logical_store': 'gcs', 'date': '2026-10-06',
            'target': 'fresh', 'snapshot_db': 'fresh', 'prefix': PREFIX,
            'source': {'schema': 'daily-scalar-input-v1', 'logical_store': 'gcs', 'date': '2026-10-06',
                       'identity': {'uri': 'gs://fixture/path-index.parquet', 'generation': 'g'}, 'bytes': len(raw), 'sha256': sha256(raw).hexdigest()},
            'source_rows': 11, 'selected_source_rows': 4, 'nodes': 3,
            'root': {'path': PREFIX, 'pre': 0, 'post': 2, 'b': 12, 'o': 3}, 'buckets': [],
            'validation': {'source_hash_checked': True, 'prefix_closed': True, 'interval_endpoints_checked': True, 'scalar_rollups_checked': True},
            'limits': {'max_nodes': 1000000}, 'stages': {'build_s': .1}}
    state = SimpleNamespace(source=source, body=body, manifest=tmp_path / 'manifest.json', out=tmp_path / 'check.json',
                            actual=[list(row) for row in SUBTREE], calls=[], doc=None, after=None)
    state.manifest.write_bytes(canonical(body))
    class Client:
        timeout = 190
        def __init__(self, *args, **kwargs):
            state.calls.append(('client', args, kwargs))
        def json(self, sql, *, settings):
            state.calls.append(('manifest', sql, settings))
            return [[dumps(state.doc or state.body)]]
        def stream(self, sql, *, fmt, settings, chunk):
            state.calls.append(('nodes', sql, fmt, settings, chunk))
            try:
                data = b''.join((dumps(row, ensure_ascii=False) + '\n').encode() for row in state.actual)
                for pos in range(0, len(data), 7):
                    yield data[pos:pos + 7]
                if state.after:
                    state.after()
            finally:
                state.calls.append(('stream-close',))
        def exec(self, sql, *, fmt, settings):
            state.calls.append(('cancel', sql, fmt, settings))
        def close(self):
            state.calls.append(('close',))
    monkeypatch.setattr(module, 'Ch', Client)
    monkeypatch.setattr(module, 'uuid4', lambda: SimpleNamespace(hex='1' * 32))
    monkeypatch.setattr(module, 'monotonic', lambda: 1.)
    return state


def run(fixture, **kwargs) -> dict:
    return module.check(fixture.manifest, fixture.source, 'http://fixture.invalid:8123', fixture.out, **kwargs)


def test_complete_independent_arrow_owner_collapse_and_unicode_tuple_dfs(fixture) -> None:
    assert module._expected(fixture.source, fixture.body, 1000000) == (SUBTREE, 4, 11)
    global_body = {**fixture.body, 'prefix': ''}
    assert module._expected(fixture.source, global_body, 1000000) == (GLOBAL, 11, 11)


@pytest.mark.parametrize('global_scope', [False, True])
def test_whole_source_exact_report_and_bounded_native_read(fixture, global_scope: bool) -> None:
    if global_scope:
        fixture.body.update(prefix='', nodes=10, selected_source_rows=11,
                            root={'path': '', 'pre': 0, 'post': 9, 'b': 21, 'o': 7},
                            buckets=[{'path': 'a', 'pre': 1, 'post': 8}, {'path': 'b', 'pre': 9, 'post': 9}])
        fixture.manifest.write_bytes(canonical(fixture.body))
        fixture.actual = [list(row) for row in GLOBAL]
    raw = fixture.manifest.read_bytes()
    result = run(fixture, max_nodes=20)
    expected = {'schema': 'daily-scalar-check-v1', 'complete': True, 'target': 'fresh', 'date': '2026-10-06',
                'logical_store': 'gcs', 'prefix': '' if global_scope else PREFIX,
                'manifest_sha256': sha256(raw).hexdigest(), 'manifest_bytes': len(raw),
                'input_sha256': fixture.body['source']['sha256'], 'input_bytes': fixture.body['source']['bytes'],
                'source_rows': 11, 'selected_source_rows': 11 if global_scope else 4,
                'nodes_checked': 10 if global_scope else 3, 'max_nodes': 20, 'elapsed_s': 0., 'max_selected_source_rows': 4000000,
                'validation': 'complete independent bounded supplied-path-index oracle; not an independent object-store inventory', 'sampling': False}
    assert result == expected
    assert loads(fixture.out.read_bytes()) == expected
    tag = 'daily_scalar_check_' + '1' * 32
    assert fixture.calls == [
        ('client', ('http://fixture.invalid:8123',), {'db': 'fresh', 'timeout': 190, 'max_threads': 1, 'max_memory_usage': 1 << 30,
                                                   'max_execution_time': 180, 'timeout_before_checking_execution_speed': 0, 'timeout_overflow_mode': 'throw'}),
        ('manifest', 'SELECT doc FROM fresh.source_manifest LIMIT 2', {'query_id': tag + '_manifest'}),
        ('nodes', 'SELECT pre,post,depth,path,b,o FROM fresh.nodes ORDER BY pre LIMIT 21', 'JSONCompactEachRow',
         {'query_id': tag + '_nodes', 'output_format_json_quote_64bit_integers': 0}, 1 << 20),
        ('stream-close',), ('close',)]


@pytest.mark.parametrize('change', [
    lambda rows: rows[0].__setitem__(1, 1),
    lambda rows: rows[1].__setitem__(0, 2),
    lambda rows: rows[1].__setitem__(2, 2),
    lambda rows: rows[2].__setitem__(3, PREFIX + '/different'),
    lambda rows: (rows[1].__setitem__(4, 6), rows[2].__setitem__(4, 6)),
    lambda rows: rows[0].__setitem__(5, 2),
    lambda rows: rows[0].__setitem__(0, 0.0),
    lambda rows: rows.append(rows[-1]),
])
def test_complete_node_corruption_and_offsetting_changes_not_only_root_totals(fixture, change) -> None:
    change(fixture.actual)
    with pytest.raises(AssertionError) as caught:
        run(fixture)
    assert str(caught.value) == 'daily scalar check final CH node differs from the complete independent path-index oracle'
    assert [row[0] for row in fixture.calls] == ['client', 'manifest', 'nodes', 'stream-close', 'cancel', 'close']
    assert fixture.out.exists() is False


def test_missing_final_row_refused_and_owned_reader_cancelled(fixture) -> None:
    fixture.actual.pop()
    with pytest.raises(AssertionError) as caught:
        run(fixture)
    assert str(caught.value) == 'daily scalar check final CH node count differs from the complete independent path-index oracle'
    tag = 'daily_scalar_check_' + '1' * 32
    assert fixture.calls[-2:] == [('cancel', f"KILL QUERY WHERE query_id IN ('{tag}_manifest','{tag}_nodes') SYNC", None, {'max_execution_time': 2}), ('close',)]


def test_independent_dataset_detects_producer_rowgroup_omission_before_ch(fixture) -> None:
    fixture.body.update(nodes=2, selected_source_rows=3, root={'path': PREFIX, 'pre': 0, 'post': 1, 'b': 12, 'o': 3})
    fixture.manifest.write_bytes(canonical(fixture.body))
    with pytest.raises(AssertionError) as caught:
        run(fixture)
    assert str(caught.value) == 'daily scalar check manifest disagrees with complete independently derived source rows/root/buckets'
    assert fixture.calls == []


def test_actual_ch_manifest_full_body_mismatch_refused(fixture) -> None:
    fixture.doc = deepcopy(fixture.body)
    fixture.doc['stages']['build_s'] = .2
    with pytest.raises(AssertionError) as caught:
        run(fixture)
    assert str(caught.value) == 'daily scalar check actual CH source manifest differs from pinned canonical bytes'
    assert [row[0] for row in fixture.calls] == ['client', 'manifest', 'cancel', 'close']


def test_input_corruption_before_and_after_is_refused(fixture) -> None:
    fixture.after = lambda: fixture.source.write_bytes(fixture.source.read_bytes() + b'x')
    with pytest.raises(ValueError) as caught:
        run(fixture)
    assert str(caught.value) == 'daily scalar check local pinned input changed during validation'
    assert fixture.out.exists() is False
    fixture.calls.clear()
    with pytest.raises(ValueError) as caught:
        run(fixture)
    assert str(caught.value) == 'daily scalar check local input bytes/SHA256 differ from pinned manifest'
    assert fixture.calls == []


def test_global_manifest_above_cap_refused_before_hash_or_ch(fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture.body.update(prefix='', nodes=1000001)
    fixture.manifest.write_bytes(canonical(fixture.body))
    monkeypatch.setattr(module, '_hash', lambda *args: pytest.fail('oversized manifest must refuse before hash'))
    with pytest.raises(ValueError) as caught:
        run(fixture)
    assert str(caught.value) == 'daily scalar check requires a complete accepted source with at most the requested node cap'
    assert fixture.calls == []


def test_actual_unique_path_cap_refused_before_large_accumulation(fixture) -> None:
    fixture.body['nodes'] = 2
    fixture.body['root']['post'] = 1
    fixture.manifest.write_bytes(canonical(fixture.body))
    with pytest.raises(ValueError) as caught:
        run(fixture, max_nodes=2)
    assert str(caught.value) == 'daily scalar check complete source exceeds its node cap; no sample accepted'
    assert fixture.calls == []


def test_existing_output_never_overwritten(fixture) -> None:
    fixture.out.write_text('keep\n')
    with pytest.raises(ValueError) as caught:
        run(fixture)
    assert str(caught.value) == 'daily scalar check output must be fresh in an existing directory'
    assert fixture.out.read_text() == 'keep\n'
    assert fixture.calls == []


@pytest.mark.parametrize('kwargs', [{'max_nodes': True}, {'max_nodes': 1000001}, {'seconds': 0}])
def test_invalid_limits_refuse_before_reading_source_or_ch(fixture, kwargs) -> None:
    with pytest.raises(ValueError) as caught:
        run(fixture, **kwargs)
    assert str(caught.value) == 'daily scalar check requires a node cap in 1..1M and seconds in 1..600'
    assert fixture.calls == []


def test_producer_helpers_are_not_used_for_independent_acceptance(fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud.chstore import daily_scalar
    def forbidden(*args, **kwargs):
        raise AssertionError('producer helper reached independent checker')
    for name in ('_arrow', '_records', 'audited_preorder', 'audited_intervals', 'ordered_select', 'wire_select'):
        monkeypatch.setattr(daily_scalar, name, forbidden)
    result = run(fixture)
    assert (result['complete'], result['nodes_checked'], result['sampling']) == (True, 3, False)


def test_utf8_component_tuple_dfs_keeps_empty_and_literal_nul_components(tmp_path: Path) -> None:
    rows = [('bucket', None, 'dir', 1, 8, 3), ('bucket/a', None, 'dir', 2, 8, 3),
            ('bucket/a/', None, 'dir', 3, 3, 1), ('bucket/a//b', None, 'file', 4, 3, 1),
            ('bucket/a/\0z', None, 'file', 3, 5, 1), ('bucket/a/z', None, 'file', 3, 0, 1)]
    path = tmp_path / 'component-paths.parquet'
    columns = dict(zip(('path', 'usr', 'kind', 'depth', 'size', 'n_files'), zip(*reversed(rows)), strict=True))
    pq.write_table(pa.table(columns), path, row_group_size=2)
    assert module._expected(path, {'prefix': ''}, 10) == ([
        (0, 6, 0, '', 8, 3), (1, 6, 1, 'bucket', 8, 3), (2, 6, 2, 'bucket/a', 8, 3),
        (3, 4, 3, 'bucket/a/', 3, 1), (4, 4, 4, 'bucket/a//b', 3, 1),
        (5, 5, 3, 'bucket/a/\0z', 5, 1), (6, 6, 3, 'bucket/a/z', 0, 1),
    ], 6, 6)
    assert module._expected(path, {'prefix': 'bucket/a//b'}, 10) == ([(0, 0, 4, 'bucket/a//b', 3, 1)], 1, 6)


def test_internal_empty_prefix_is_accepted_by_manifest_contract(fixture) -> None:
    fixture.body['prefix'] = 'bucket/a//b'
    fixture.body['root']['path'] = 'bucket/a//b'
    fixture.manifest.write_bytes(canonical(fixture.body))
    assert module._manifest(fixture.manifest, 1000000) == (canonical(fixture.body), fixture.body)


def test_selected_row_cap_refuses_before_hash_or_ch(fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture.body['selected_source_rows'] = 4000001
    fixture.manifest.write_bytes(canonical(fixture.body))
    monkeypatch.setattr(module, '_hash', lambda *args: pytest.fail('selected row cap must precede input hash'))
    with pytest.raises(ValueError) as caught:
        run(fixture)
    assert str(caught.value) == 'daily scalar check requires 1..4M selected source rows; no sample accepted'
    assert fixture.calls == []


def test_actual_selected_owner_slices_have_cap_independent_of_unique_node_count(fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    # Lower this fixed gate in the tiny fixture: two owner/kind slices share
    # the root, so the third selected row is not a third unique tree node.
    monkeypatch.setattr(module, 'MAX_SOURCE_ROWS', 2)
    with pytest.raises(ValueError) as caught:
        module._expected(fixture.source, fixture.body, 1000000)
    assert str(caught.value) == 'daily scalar check complete source exceeds its selected-row cap; no sample accepted'
