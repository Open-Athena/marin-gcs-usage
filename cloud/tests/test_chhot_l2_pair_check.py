"""Complete covered-control omission checks and bounded independent cell sums."""

from copy import deepcopy
from hashlib import sha256
from json import dumps, loads
from pathlib import Path
from os import environ
from subprocess import run
from sys import executable

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import hot_l2_pair_check as module
from dt_cloud.chstore.hot_l2_pair_stream import complete, prepare
from test_chhot_l1_batch_catalog import artifact, stream_artifact, write
from test_chhot_l1_publish import prefix_proof
from test_chhot_l2_pair_stream import declared, native


def inputs(tmp_path: Path) -> tuple[Path, dict, dict]:
    refs, proofs = [], []
    for day in ('2026-10-04', '2026-10-05'):
        body = artifact(day)
        source = stream_artifact()
        body.update({key: source[key] for key in ('schema', 'engine', 'source_query_id', 'native')})
        body['results'][0]['pattern'] = 'm'
        body['validation']['references'][0]['pattern'] = 'm'
        for query in body['results']:
            for bucket in query['buckets']:
                bucket['path'] = 'marin-' + bucket['path']
        ref = write(tmp_path / f'{day}.json', body)
        proof = tmp_path / f'{day}-proof.json'
        prefix_proof(proof, ref)
        refs.append(ref)
        proofs.append(proof)
    queries = tmp_path / 'queries.jsonl'
    queries.write_text('\n'.join(map(dumps, [body['queries']['header'],
        {'chars': 1, 'pattern': 'm', 'direct_matching_paths': 10},
        {'chars': 4, 'pattern': '.npy', 'direct_matching_paths': 12}, {'complete': True, 'patterns': 2}])) + '\n')
    prepared = prepare('fleet', '2026-10-04', '2026-10-05', tuple(refs), tuple(proofs), queries, ('m', '.npy'), 4)
    geometry = declared()
    for frame in geometry:
        frame['path'] = 'marin-' + frame['path']
    completed = complete(native(), prepared, geometry, 10)
    result = {'schema': 'hot-l2-pair-stream-v1', 'exact': True, 'incremental': False, 'levels': 2, 'scope': module.SCOPE,
              'target': 'fleet', 'dates': prepared['dates'], 'snapshot_dbs': prepared['dbs'], 'budget': 4,
              'query_subset': False, 'registered_predicates': 2, 'registered_frames': 4, **completed,
              'provenance': prepared['provenance'], 'persistent_index_created': False}
    return write(tmp_path / 'pair.json', result), prepared, result


class FakeCh:
    def __init__(self, url: str, **settings) -> None:
        self.url, self.settings = url, settings
        self.calls = []
        self.closed = False

    def json(self, sql: str) -> list[list]:
        self.calls.append(sql)
        before = 'snapshot_20261004' in sql
        if sql.startswith('SELECT count()'):
            return [[9]]
        if sql.startswith('SELECT pre,post,path,b,o'):
            return ([[1, 4, 'marin-a', 8, 3], [2, 3, 'marin-a/x', 6, 2], [4, 4, 'marin-a/y', 2, 1],
                     [5, 8, 'marin-b', 0, 2], [7, 8, 'marin-b/y', 0, 1]] if before else
                    [[1, 4, 'marin-a', 2, 1], [4, 4, 'marin-a/y', 2, 1], [5, 8, 'marin-b', 0, 4]])
        raise AssertionError(f'unexpected fixture SQL: {sql}')

    def close(self) -> None:
        self.closed = True


def test_oracle_selection_traverses_cells_once_for_a_large_registry() -> None:
    class CountedCells(list):
        traversals = 0
        visits = 0

        def __iter__(self):
            self.traversals += 1
            for cell in super().__iter__():
                self.visits += 1
                yield cell

    cells = CountedCells([{'predicate_id': 2, 'pre': 10, 'post': 11}])
    queries = [{'predicate_id': 1, 'pattern': 'm'}] + [
        {'predicate_id': qid, 'pattern': f'query-{qid}'} for qid in range(2, 103)
    ]
    ch = FakeCh('unused')
    actual = module.scoped_oracles(ch, {'results': queries, 'cells': cells}, 100, 0)
    assert actual == [
        {'predicate_id': qid, 'pattern': f'query-{qid}', 'checked': False, 'reason': 'selected-cell budget exhausted'}
        for qid in range(2, 103)
    ]
    assert (cells.traversals, cells.visits, ch.calls) == (1, 1, [])


def install(monkeypatch, result: dict) -> FakeCh:
    ch = FakeCh('fixture')
    monkeypatch.setattr(module, 'Ch', lambda url, **kw: (setattr(ch, 'settings', kw), ch)[1])
    monkeypatch.setattr(module, 'frames', lambda actual, prepared, maximum: deepcopy(result['frames']))
    monkeypatch.setattr(module, 'monotonic', lambda: 1.0)
    return ch


def test_complete_control_and_explicit_unchecked_query(tmp_path: Path, monkeypatch) -> None:
    path, prepared, body = inputs(tmp_path)
    ch = install(monkeypatch, body)
    out = tmp_path / 'check.json'
    result = module.check('fixture', path, out)
    raw = path.read_bytes()
    assert result == {
        'schema': 'hot-l2-pair-check-v1', 'complete': True, 'target': 'fleet', 'dates': prepared['dates'],
        'artifact': {'path': str(path), 'sha256': sha256(raw).hexdigest(), 'bytes': len(raw)},
        'source_rows': [9, 9], 'prefix_proofs_checked': True,
        'covered_control': {'pattern': 'm', 'validation': 'complete ordinary recursive bucket/frame rollups on both dates',
                            'frames_checked': 4, 'heavy_cells_checked': 2, 'buckets_checked': 2},
        'selected_cells': [{'predicate_id': 2, 'pattern': '.npy', 'checked': False, 'reason': 'no emitted cell within interval cap'}],
        'full_catalog_source_oracle': False, 'source_contract': prepared['provenance']['source_contract'],
        'limits': {'seconds': 30, 'memory_gib': 2, 'max_interval_span': 1_000_000, 'max_selected_cells': 4}, 'check_s': 0.0,
    }
    assert loads(out.read_bytes()) == result
    assert ch.closed is True
    assert ch.settings == {'db': 'fleet', 'timeout': 90, 'max_threads': 4, 'max_memory_usage': 2 << 30,
                           'max_execution_time': 30, 'timeout_before_checking_execution_speed': 0,
                           'timeout_overflow_mode': 'throw', 'max_temporary_data_on_disk_size_for_query': 1 << 30}
    assert ch.calls == [
        'SELECT count() FROM snapshot_20261004.nodes', 'SELECT count() FROM snapshot_20261005.nodes',
        'SELECT pre,post,path,b,o FROM snapshot_20261004.nodes WHERE pre IN (1,2,4,5,6,7) ORDER BY pre',
        'SELECT pre,post,path,b,o FROM snapshot_20261005.nodes WHERE pre IN (1,2,4,5,6,7) ORDER BY pre',
    ]


def test_all_provenance_files_and_artifact_are_read_once(tmp_path: Path, monkeypatch) -> None:
    path, _, _ = inputs(tmp_path)
    original = Path.read_bytes
    calls = []

    def read(source: Path) -> bytes:
        calls.append(source.name)
        return original(source)

    monkeypatch.setattr(Path, 'read_bytes', read)
    module.load(path, 'm')
    assert calls == ['pair.json', '2026-10-04.json', '2026-10-05.json', '2026-10-04-proof.json', '2026-10-05-proof.json', 'queries.jsonl']


def test_omitted_heavy_control_cell_refuses_even_with_rebalanced_other(tmp_path: Path, monkeypatch) -> None:
    path, _, body = inputs(tmp_path)
    body['cells'].pop(0)
    body['native']['emitted_cells'] = 1
    body['results'][0]['buckets'][0]['other'] = {'b': [6, 0], 'o': [2, 0]}
    write(path, body)
    ch = install(monkeypatch, body)
    with pytest.raises(AssertionError) as caught:
        module.check('fixture', path, tmp_path / 'check.json')
    assert str(caught.value) == 'paired L2 covered-ancestor complete cells/other disagree with ordinary rollups'
    assert ch.closed is True
    assert (tmp_path / 'check.json').exists() is False


@pytest.mark.parametrize('kind', ['existing', 'hash', 'partial', 'no-control', 'bad-frame'])
def test_refusal_before_ch(tmp_path: Path, monkeypatch, kind: str) -> None:
    path, _, body = inputs(tmp_path)
    out = tmp_path / 'check.json'
    if kind == 'existing':
        out.write_text('unchanged\n')
    elif kind == 'hash':
        body['provenance']['references'][0]['sha256'] = '0' * 64
    elif kind == 'partial':
        body['exact'] = False
    elif kind == 'no-control':
        body['results'][0]['pattern'] = '.json'
    else:
        body['frames'][0]['pre'] = True
    write(path, body)
    calls = []
    monkeypatch.setattr(module, 'Ch', lambda *a, **kw: calls.append((a, kw)))
    with pytest.raises((ValueError, RuntimeError)) as caught:
        module.check('fixture', path, out)
    assert str(caught.value) == {
        'existing': 'paired L2 check output must be new in an existing directory',
        'hash': 'paired L2 provenance bytes/hash mismatch',
        'partial': 'paired L2 check requires a completed paired experiment contract',
        'no-control': 'paired L2 check requires the registered covered-ancestor control m',
        'bad-frame': 'paired L2 requires bounded unsigned integers',
    }[kind]
    assert calls == []
    if kind == 'existing':
        assert out.read_text() == 'unchanged\n'
    else:
        assert out.exists() is False


@pytest.mark.parametrize('failure', ['geometry', 'count', 'point', 'query'])
def test_source_failures_close_and_leave_no_artifact(tmp_path: Path, monkeypatch, failure: str) -> None:
    path, _, body = inputs(tmp_path)
    ch = install(monkeypatch, body)
    if failure == 'geometry':
        monkeypatch.setattr(module, 'frames', lambda *args: [])
    else:
        original = ch.json

        def query(sql: str) -> list[list]:
            rows = original(sql)
            if failure == 'count' and sql.startswith('SELECT count()'):
                return [[8]]
            if sql.startswith('SELECT pre,post,path,b,o'):
                if failure == 'query':
                    raise RuntimeError('fixture timeout')
                if failure == 'point':
                    rows[0][2] = 'wrong'
            return rows

        ch.json = query
    with pytest.raises((ValueError, RuntimeError)) as caught:
        module.check('fixture', path, tmp_path / 'check.json')
    assert str(caught.value) == {
        'geometry': 'paired L2 check complete source frame geometry differs',
        'count': 'paired L2 check source row counts differ from bound references',
        'point': 'paired L2 ordinary point geometry is invalid',
        'query': 'fixture timeout',
    }[failure]
    assert ch.closed is True
    assert (tmp_path / 'check.json').exists() is False


def test_scoped_frontier_includes_matching_frame_even_when_external_parent_matches() -> None:
    body = {'snapshot_dbs': ['before', 'after'], 'results': [{'predicate_id': 1, 'pattern': 'm'}, {'predicate_id': 2, 'pattern': 'a%_'}],
            'cells': [{'predicate_id': 2, 'frame_id': 3, 'pre': 10, 'post': 15, 'b': [8, 2], 'o': [3, 1]}]}
    class Source:
        def __init__(self): self.calls = []
        def json(self, sql):
            self.calls.append(sql)
            return [['8', '3']] if 'before.nodes' in sql else [[2, 1]]
    source = Source()
    assert module.scoped_oracles(source, body, 6, 4) == [{'predicate_id': 2, 'pattern': 'a%_', 'checked': True, 'frame_id': 3,
        'interval_span': 6, 'validation': 'complete scoped first-hit full-path frontier on both dates'}]
    expected_tail = ("WHERE pre BETWEEN 10 AND 15 AND lowerUTF8(path) LIKE '%a\\\\%\\\\_%' "
                     "AND (pre = 10 OR NOT (lowerUTF8(if(position(path, '/') = 0, '', substring(path, 1, length(path) - position(reverse(path), '/')))) LIKE '%a\\\\%\\\\_%'))")
    assert source.calls == [f'SELECT sum(toUInt128(b)),sum(toUInt128(o)) FROM {db}.nodes ' + expected_tail for db in ('before', 'after')]


@pytest.mark.parametrize('cap,span,reason', [(0, 6, 'selected-cell budget exhausted'), (4, 5, 'no emitted cell within interval cap')])
def test_scoped_no_eligible_cell_never_runs_full_fleet_query(cap: int, span: int, reason: str) -> None:
    body = {'results': [{'predicate_id': 1, 'pattern': '.json'}], 'cells': [{'predicate_id': 1, 'pre': 10, 'post': 15}]}
    assert module.scoped_oracles(None, body, span, cap) == [{'predicate_id': 1, 'pattern': '.json', 'checked': False, 'reason': reason}]


def test_scoped_vector_mismatch_refuses() -> None:
    body = {'snapshot_dbs': ['before', 'after'], 'results': [{'predicate_id': 1, 'pattern': '.json'}],
            'cells': [{'predicate_id': 1, 'frame_id': 1, 'pre': 10, 'post': 10, 'b': [8, 2], 'o': [3, 1]}]}
    class Source:
        def json(self, sql): return [[0, 0]]
    with pytest.raises(AssertionError) as caught:
        module.scoped_oracles(Source(), body, 1, 4)
    assert str(caught.value) == 'paired L2 selected cell disagrees with independent full-path frontier'


@pytest.mark.parametrize('args,seconds,span,cells,url', [([], 30, 1_000_000, 4, 'http://localhost:8123'),
    (['-w', '15', '-s', '8', '-c', '0', '-U', 'http://fixture'], 15, 8, 0, 'http://fixture')])
def test_cli_forwards_exact_bounds_and_prints_no_private_usage(tmp_path: Path, monkeypatch, args, seconds, span, cells, url) -> None:
    from dt_cloud.cli import main
    calls = []
    def check(*pos, **kw):
        calls.append((pos, kw))
        return {'schema': 'hot-l2-pair-check-v1', 'dates': ['before', 'after'], 'complete': True,
                'covered_control': {'frames_checked': 4}, 'selected_cells': [{'checked': True}, {'checked': False}]}
    monkeypatch.setattr(module, 'check', check)
    artifact, out = tmp_path / 'pair.json', tmp_path / 'check.json'
    result = CliRunner().invoke(main, ['ch-hot-l2-pair-check', *args, '-o', str(out), str(artifact)])
    assert result.exit_code == 0, result.exception
    assert calls == [((url, artifact, out), {'seconds': seconds, 'max_span': span, 'max_cells': cells})]
    assert loads(result.output) == {'schema': 'hot-l2-pair-check-v1', 'dates': ['before', 'after'], 'complete': True,
                                  'covered_frames': 4, 'selected_cells_checked': 1, 'out': str(out)}


def test_module_cli_registers_the_same_checker_help_as_imported_cli() -> None:
    from dt_cloud.cli import main
    expected = CliRunner().invoke(main, ['ch-hot-l2-pair-check', '--help'], terminal_width=78)
    actual = run([executable, '-m', 'dt_cloud.cli', 'ch-hot-l2-pair-check', '--help'], capture_output=True, text=True,
                 env={**environ, 'COLUMNS': '80'}, check=False)
    assert expected.exit_code == actual.returncode == 0
    assert actual.stderr == ''
    normalize = lambda text: text.split('\n', 1)[1]
    assert normalize(actual.stdout) == normalize(expected.output)
