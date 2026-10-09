"""Accepted sparse bucket reads preserve the paired partition and oracle limits."""

from hashlib import sha256
from json import loads
from pathlib import Path

import pytest

from dt_cloud.chstore.hot_l1_catalog import CatalogRequest, SCOPE
from dt_cloud.chstore.hot_l2_pair_catalog import CUTOFF, COVERED, SELECTED, HotL2PairCatalog, _accept
from dt_cloud.chstore.hot_l2_pair_check import load
from dt_cloud.chstore.hot_l2_pair_stream import complete, prepare
from test_chhot_l1_batch_catalog import write
from test_chhot_l1_publish import prefix_proof
from test_chhot_l2_pair_check import inputs
from test_chhot_l2_pair_stream import native


def fixture(tmp_path: Path, *, scale: int = 1, selected: bool = False) -> tuple[Path, Path, dict]:
    path, previous, body = inputs(tmp_path)
    refs = tuple(Path(row['path']) for row in body['provenance']['references'])
    proofs = tuple(Path(row['path']) for row in body['provenance']['prefix_proofs'])
    for ref, proof in zip(refs, proofs, strict=True):
        source = loads(ref.read_bytes())
        source['source_validation']['rows'] = 13
        source['native']['nodes_read'] = 13
        for query in source['results']:
            query['root']['b'] *= scale
            for bucket in query['buckets']:
                bucket['b'] *= scale
            query['buckets'] += [{'pre': i, 'post': i, 'path': f'marin-{name}', 'b': 0, 'o': 0} for i, name in enumerate('cdef', 9)]
        if selected:
            source['results'][1].update(root=dict(source['results'][0]['root']), buckets=[dict(row) for row in source['results'][0]['buckets']])
        write(ref, source)
        prefix_proof(proof, ref)
    prepared = prepare('fleet', *previous['dates'], refs, proofs, Path(body['provenance']['queries']['path']), ('m', '.npy'), 4)
    output = native()
    output['rows_read'] = [13, 13]
    for root in output['roots']:
        for bucket in root['buckets']:
            bucket[0], bucket[2] = str(int(bucket[0]) * scale), str(int(bucket[2]) * scale)
        root['buckets'] += [['0', '0', '0', '0'] for _ in range(4)]
    for cell in output['cells']:
        cell['b'] = [str(int(n) * scale) for n in cell['b']]
    if selected:
        output['roots'][1]['buckets'] = [list(row) for row in output['roots'][0]['buckets']]
        output['cells'] = [copy for row in output['cells'] for copy in (row, {**row, 'predicate_id': 2})]
        output.update(emitted_cells=4, peak_active=[2, 2])
    body.update(complete(output, prepared, body['frames'], 10))
    body.update(provenance=prepared['provenance'], cutoff_scope=CUTOFF)
    write(path, body)
    raw = path.read_bytes()
    proof = {'schema': 'hot-l2-pair-check-v1', 'complete': True, 'target': 'fleet', 'dates': prepared['dates'],
             'artifact': {'path': str(path), 'sha256': sha256(raw).hexdigest(), 'bytes': len(raw)},
             'source_rows': [13, 13], 'prefix_proofs_checked': True,
             'covered_control': {'pattern': 'm', 'validation': COVERED, 'frames_checked': 4, 'heavy_cells_checked': 2, 'buckets_checked': 6},
             'selected_cells': [{'predicate_id': 2, 'pattern': '.npy', 'checked': False, 'reason': 'no emitted cell within interval cap'}],
             'full_catalog_source_oracle': False, 'source_contract': prepared['provenance']['source_contract'],
             'limits': {'seconds': 30, 'memory_gib': 2, 'max_interval_span': 1_000_000, 'max_selected_cells': 4}, 'check_s': .1}
    if selected:
        proof['selected_cells'] = [{'predicate_id': 2, 'pattern': '.npy', 'checked': True, 'frame_id': 1, 'interval_span': 2, 'validation': SELECTED}]
    check = write(tmp_path / 'check.json', proof)
    return path, check, proof


def expected_view(path: Path, date: str, pattern: str = 'm', bucket: str = 'marin-a') -> dict:
    raw = path.read_bytes()
    covered = {'pattern': 'm', 'validation': COVERED, 'frames_checked': 4, 'heavy_cells_checked': 2, 'buckets_checked': 6}
    before = date == '2026-10-04'
    is_m, is_a, is_b = pattern == 'm', bucket == 'marin-a', bucket == 'marin-b'
    total = {'b': '8' if before else '2', 'o': '3' if before else '1'} if is_m and is_a else {'b': '0', 'o': ('2' if before else '4') if is_m and is_b else '0'}
    lo, hi = {'marin-a': (1, 4), 'marin-b': (5, 8), 'marin-c': (9, 9), 'marin-d': (10, 10), 'marin-e': (11, 11), 'marin-f': (12, 12)}[bucket]
    oracle = covered if is_m else {'predicate_id': 2, 'pattern': '.npy', 'checked': False, 'reason': 'no emitted cell within interval cap'}
    return {'schema': 'hot-l2-pair-catalog-v1', 'target': 'fleet', 'date': date, 'pattern': pattern, 'path': bucket,
            'exact': True, 'incremental': False, 'levels': 2, 'scope': SCOPE,
            'source': 'registered accepted paired sparse artifact',
            'validation': {'artifact_sha256': sha256(raw).hexdigest(), 'artifact_bytes': len(raw), 'prefix_proofs_checked': True,
                           'covered_control': covered, 'full_catalog_source_oracle': False, 'query_oracle': oracle},
            'cutoff': {'scope': CUTOFF, 'dates': ['2026-10-04', '2026-10-05'], 'budget': 4, 'threshold_bytes': '2' if is_m and is_a else '1'},
            'geometry': {'pre': lo, 'post': hi}, 'root': total,
            'children': ([{'path': 'marin-a/x', 'name': 'x', 'pre': 2, 'post': 3, 'b': '6' if before else '0', 'o': '2' if before else '0', 'drill': False},
                          {'path': 'marin-a/y', 'name': 'y', 'pre': 4, 'post': 4, 'b': '2', 'o': '1', 'drill': False}] if is_m and is_a else []),
            'other': {'b': '0', 'o': total['o'] if is_b else '0', 'drill': False},
            'capabilities': {'bucket_roots_only': True, 'child_drill': False, 'filters': False}}


@pytest.mark.parametrize('date', ['2026-10-04', '2026-10-05'])
@pytest.mark.parametrize('bucket', ['marin-a', 'marin-b', 'marin-c', 'marin-d', 'marin-e', 'marin-f'])
def test_exact_views_include_every_declared_bucket_and_zero_byte_counts(tmp_path: Path, date: str, bucket: str) -> None:
    path, proof, _ = fixture(tmp_path)
    catalog = HotL2PairCatalog.load(path, proof)
    assert catalog.view(date, 'M', path=bucket) == expected_view(path, date, bucket=bucket)
    assert (catalog.target, catalog.dates, catalog.patterns, catalog.paths) == (
        'fleet', ('2026-10-04', '2026-10-05'), ('m', '.npy'), tuple(f'marin-{name}' for name in 'abcdef'))
    assert catalog.metadata() == {'schema': 'hot-l2-pair-catalog-registry-v1', 'target': 'fleet', 'dates': ['2026-10-04', '2026-10-05'],
                                  'patterns': 2, 'buckets': 6, 'levels': 2,
                                  'cutoff_scope': CUTOFF, 'budget': 4, 'child_drill': False, 'full_catalog_source_oracle': False}


def test_registered_zero_is_not_unknown_and_is_not_independently_oracle_verified(tmp_path: Path) -> None:
    path, proof, _ = fixture(tmp_path)
    catalog = HotL2PairCatalog.load(path, proof)
    assert catalog.view('2026-10-05', '.NPY', path='marin-a') == expected_view(path, '2026-10-05', '.npy')


def test_selected_cell_acceptance_remains_scoped_not_whole_query_truth(tmp_path: Path) -> None:
    path, proof, check = fixture(tmp_path, selected=True)
    catalog = HotL2PairCatalog.load(path, proof)
    view = catalog.view('2026-10-05', '.npy', path='marin-a')
    assert view['validation'] == {
        'artifact_sha256': sha256(path.read_bytes()).hexdigest(), 'artifact_bytes': path.stat().st_size,
        'prefix_proofs_checked': True, 'covered_control': check['covered_control'], 'full_catalog_source_oracle': False,
        'query_oracle': {'predicate_id': 2, 'pattern': '.npy', 'checked': True, 'frame_id': 1, 'interval_span': 2, 'validation': SELECTED}}
    assert view['root'] == {'b': '2', 'o': '1'}


def test_acceptance_cell_traversals_are_constant_not_per_predicate(tmp_path: Path) -> None:
    path, _, check = fixture(tmp_path, selected=True)
    body, prepared, raw = load(path, 'm')
    prepared['patterns'] += tuple(f'cold-{i}' for i in range(100))
    check['selected_cells'] += [{'predicate_id': i, 'pattern': pattern, 'checked': False,
                                'reason': 'no emitted cell within interval cap'}
                               for i, pattern in enumerate(prepared['patterns'][2:], 3)]

    class Cells(list):
        traversals = 0
        visited = 0

        def __iter__(self):
            self.traversals += 1
            for cell in super().__iter__():
                self.visited += 1
                yield cell

    cells = Cells(body['cells'])
    body['cells'] = cells
    _accept(body, prepared, raw, check)
    assert (cells.traversals, cells.visited) == (2, 8)


def test_wrong_cutoff_and_unproved_selected_frame_refuse(tmp_path: Path) -> None:
    path, proof, check = fixture(tmp_path, selected=True)
    check['selected_cells'][0]['frame_id'] = 3
    write(proof, check)
    with pytest.raises(ValueError) as caught:
        HotL2PairCatalog.load(path, proof)
    assert str(caught.value) == 'paired L2 catalog selected-oracle declaration is invalid'
    path, proof, check = fixture(tmp_path)
    body = loads(path.read_bytes())
    body['cutoff_scope'] = 'fleet-global'
    write(path, body)
    check['artifact'].update(sha256=sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
    write(proof, check)
    with pytest.raises(ValueError) as caught:
        HotL2PairCatalog.load(path, proof)
    assert str(caught.value) == 'paired L2 catalog cutoff contract is unsupported'


@pytest.mark.parametrize('before,after,delta', [('2026-10-04', '2026-10-05', '-6'), ('2026-10-05', '2026-10-04', '6'), ('2026-10-05', '2026-10-05', '0')])
def test_same_reversed_and_forward_dates_keep_exact_partition(tmp_path: Path, before: str, after: str, delta: str) -> None:
    path, proof, _ = fixture(tmp_path)
    catalog = HotL2PairCatalog.load(path, proof)
    view = expected_view(path, before)
    right = expected_view(path, after)
    def pair(left: dict, right: dict) -> dict:
        return {'before': {key: left[key] for key in ('b', 'o')}, 'after': {key: right[key] for key in ('b', 'o')},
                'delta': {key: str(int(right[key]) - int(left[key])) for key in ('b', 'o')}}
    expected = {key: view[key] for key in ('target', 'pattern', 'path', 'exact', 'incremental', 'levels', 'scope', 'source', 'validation', 'cutoff', 'geometry', 'capabilities')}
    expected.update(schema='hot-l2-pair-catalog-diff-v1', dates=[before, after], root=pair(view['root'], right['root']),
                    children=[{**{key: left[key] for key in ('path', 'name', 'pre', 'post')}, **pair(left, other), 'drill': False}
                              for left, other in zip(view['children'], right['children'], strict=True)],
                    other={**pair(view['other'], right['other']), 'drill': False})
    actual = catalog.diff(before, after, 'm', path='marin-a')
    assert actual == expected
    assert actual['root']['delta']['b'] == delta


def test_diff_zero_byte_object_growth_and_large_integer_precision(tmp_path: Path) -> None:
    path, proof, _ = fixture(tmp_path, scale=10_000_000_000_000_000)
    catalog = HotL2PairCatalog.load(path, proof)
    assert catalog.view('2026-10-04', 'm', path='marin-a')['root'] == {'b': '80000000000000000', 'o': '3'}
    assert catalog.diff('2026-10-04', '2026-10-05', 'm', path='marin-a')['root'] == {
        'before': {'b': '80000000000000000', 'o': '3'}, 'after': {'b': '20000000000000000', 'o': '1'}, 'delta': {'b': '-60000000000000000', 'o': '-2'}}
    assert catalog.diff('2026-10-04', '2026-10-05', 'm', path='marin-b')['other'] == {
        'before': {'b': '0', 'o': '2'}, 'after': {'b': '0', 'o': '4'}, 'delta': {'b': '0', 'o': '2'}, 'drill': False}


@pytest.mark.parametrize('date,pattern,bucket,message', [
    ('2026-10-06', 'm', 'marin-a', 'paired L2 pattern/date is not registered; no scan fallback'),
    ('2026-10-05', 'unknown', 'marin-a', 'paired L2 pattern/date is not registered; no scan fallback'),
    ('2026-10-05', 'm\0', 'marin-a', 'paired L2 pattern/date is not registered; no scan fallback'),
    ('2026-10-05', 'm', '', 'paired L2 serves declared bucket roots only; no deeper drill or fallback'),
    ('2026-10-05', 'm', 'marin-a/x', 'paired L2 serves declared bucket roots only; no deeper drill or fallback'),
])
def test_unknown_scope_is_refused_not_zero(tmp_path: Path, date: str, pattern: str, bucket: str, message: str) -> None:
    path, proof, _ = fixture(tmp_path)
    catalog = HotL2PairCatalog.load(path, proof)
    with pytest.raises(CatalogRequest) as caught:
        catalog.view(date, pattern, path=bucket)
    assert str(caught.value) == message


@pytest.mark.parametrize('change,message', [
    (lambda p: p.update(complete=False), 'paired L2 catalog requires a complete matching check proof'),
    (lambda p: p.update(dates=['2026-10-05', '2026-10-04']), 'paired L2 catalog requires a complete matching check proof'),
    (lambda p: p.update(source_rows=[12, 13]), 'paired L2 catalog requires a complete matching check proof'),
    (lambda p: p.update(prefix_proofs_checked=False), 'paired L2 catalog requires a complete matching check proof'),
    (lambda p: p.update(full_catalog_source_oracle=True), 'paired L2 catalog requires a complete matching check proof'),
    (lambda p: p['artifact'].update(sha256='0' * 64), 'paired L2 catalog check artifact bytes/hash mismatch'),
    (lambda p: p['artifact'].update(bytes=0), 'paired L2 catalog check artifact bytes/hash mismatch'),
    (lambda p: p['covered_control'].update(frames_checked=3), 'paired L2 catalog requires full covered-control acceptance'),
    (lambda p: p['covered_control'].update(heavy_cells_checked=1), 'paired L2 catalog requires full covered-control acceptance'),
    (lambda p: p.update(selected_cells=[]), 'paired L2 catalog selected-oracle declarations are incomplete'),
    (lambda p: p['selected_cells'][0].update(checked=True), 'paired L2 catalog selected-oracle declaration is invalid'),
    (lambda p: p['selected_cells'][0].update(reason='selected-cell budget exhausted'), 'paired L2 catalog selected-oracle declaration is invalid'),
    (lambda p: p.update(check_s=float('nan')), 'paired L2 catalog check duration is invalid'),
])
def test_incomplete_unbound_or_overstated_acceptance_refuses(tmp_path: Path, change, message: str) -> None:
    path, proof, body = fixture(tmp_path)
    change(body)
    write(proof, body)
    with pytest.raises(ValueError) as caught:
        HotL2PairCatalog.load(path, proof)
    assert str(caught.value) == message


def test_reads_are_pinned_once_and_responses_cannot_mutate_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, proof, _ = fixture(tmp_path)
    calls, original = [], Path.read_bytes
    def read(source: Path) -> bytes:
        calls.append(source.name)
        return original(source)
    monkeypatch.setattr(Path, 'read_bytes', read)
    catalog = HotL2PairCatalog.load(path, proof)
    assert calls == ['pair.json', '2026-10-04.json', '2026-10-05.json', '2026-10-04-proof.json', '2026-10-05-proof.json', 'queries.jsonl', 'check.json']
    view = catalog.view('2026-10-05', 'm', path='marin-a')
    view['validation']['covered_control']['frames_checked'] = 0
    view['children'][0]['b'] = '999'
    view['root']['b'] = '999'
    path.write_text('unavailable after load\n')
    proof.write_text('unavailable after load\n')
    restored = catalog.view('2026-10-05', 'm', path='marin-a')
    assert (restored['root'], restored['children'][0], restored['validation']['covered_control']) == (
        {'b': '2', 'o': '1'}, {'path': 'marin-a/x', 'name': 'x', 'pre': 2, 'post': 3, 'b': '0', 'o': '0', 'drill': False},
        {'pattern': 'm', 'validation': COVERED, 'frames_checked': 4, 'heavy_cells_checked': 2, 'buckets_checked': 6})
    assert calls == ['pair.json', '2026-10-04.json', '2026-10-05.json', '2026-10-04-proof.json', '2026-10-05-proof.json', 'queries.jsonl', 'check.json']
