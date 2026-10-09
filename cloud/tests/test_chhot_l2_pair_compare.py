"""Full retained subset parity is independent of predicate ID or file paths."""

from copy import deepcopy
from hashlib import sha256
from json import dumps, loads
from pathlib import Path

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import hot_l2_pair_compare as module
from dt_cloud.chstore.hot_l2_pair_stream import complete, prepare
from test_chhot_l1_batch_catalog import write
from test_chhot_l1_publish import prefix_proof
from test_chhot_l2_pair_catalog import fixture


def inputs(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    reference, check, proof = fixture(tmp_path)
    original = loads(reference.read_bytes())
    refs = tuple(Path(row['path']) for row in original['provenance']['references'])
    proofs = tuple(Path(row['path']) for row in original['provenance']['prefix_proofs'])
    queries = Path(original['provenance']['queries']['path'])
    records = [loads(line) for line in queries.read_text().splitlines()]
    records[-1:] = [{'chars': 4, 'pattern': '.txt', 'direct_matching_paths': 10}, {'complete': True, 'patterns': 3}]
    queries.write_text('\n'.join(map(dumps, records)) + '\n')
    copied_queries = tmp_path / 'same-registry-new-path.jsonl'
    copied_queries.write_bytes(queries.read_bytes())
    for ref, prefix in zip(refs, proofs, strict=True):
        body = loads(ref.read_bytes())
        body.update(compiled_patterns=3)
        body['native']['registered_predicates'] = 3
        body['queries']['patterns'] = 3
        body['results'].append({**deepcopy(body['results'][1]), 'predicate_id': 3, 'pattern': '.txt'})
        write(ref, body)
        prefix_proof(prefix, ref)
    def build(patterns: tuple[str, ...], registry: Path) -> dict:
        prepared = prepare('fleet', *original['dates'], refs, proofs, registry, patterns, 4)
        native = {**original['native'], 'registered_predicates': len(patterns),
                  'roots': [{'predicate_id': i, 'buckets': [list(map(str, row)) for row in buckets]}
                            for i, buckets in enumerate(prepared['expected'], 1)],
                  'cells': [{'predicate_id': patterns.index('m') + 1, 'frame_id': row['frame_id'],
                             'b': list(map(str, row['b'])), 'o': list(map(str, row['o']))} for row in original['cells']]}
        return {**original, 'query_subset': prepared['query_subset'], 'registered_predicates': len(patterns),
                'provenance': prepared['provenance'], **complete(native, prepared, original['frames'], 10)}
    left = build(('m', '.npy'), queries)
    right = build(('.npy', '.txt', 'm'), copied_queries)
    write(reference, left)
    candidate = write(tmp_path / 'candidate.json', right)
    proof['artifact'].update(bytes=reference.stat().st_size, sha256=sha256(reference.read_bytes()).hexdigest())
    write(check, proof)
    return reference, check, candidate, right


def descriptor(path: Path) -> dict:
    raw = path.read_bytes()
    return {'path': str(path), 'sha256': sha256(raw).hexdigest(), 'bytes': len(raw)}


def test_complete_subset_exact_parity_remaps_only_predicate_ids_and_ignores_registry_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reference, check, candidate, right = inputs(tmp_path)
    monkeypatch.setattr(module, 'monotonic', lambda: 1.)
    out = tmp_path / 'parity.json'
    registry = right['provenance']['queries']
    expected = {'schema': 'hot-l2-pair-compare-v1', 'complete': True, 'target': 'fleet', 'dates': ['2026-10-04', '2026-10-05'],
                'budget': 4, 'cutoff_scope': module.CUTOFF, 'reference': {**descriptor(reference), 'check': descriptor(check)},
                'candidate': descriptor(candidate), 'query_registry': {key: registry[key] for key in ('sha256', 'bytes', 'header')},
                'queries_compared': 2, 'cells_compared': 2, 'candidate_queries': 3, 'candidate_cells': 2,
                'validation': 'complete paired reference predicate roots/buckets/Other and retained cells; predicate IDs remapped only',
                'reference_check_complete': True, 'full_catalog_source_oracle': False,
                'candidate_remaining_queries_independently_checked': False, 'compare_s': 0.}
    assert module.compare(reference, check, candidate, out) == expected
    assert loads(out.read_bytes()) == expected


def test_missing_or_changed_cell_refuses_whole_partition_not_sample(tmp_path: Path) -> None:
    reference, check, candidate, body = inputs(tmp_path)
    body['cells'][0]['b'][0] = 5
    body['cells'][1]['b'][0] = 3
    write(candidate, body)
    out = tmp_path / 'failed.json'
    with pytest.raises(AssertionError) as caught:
        module.compare(reference, check, candidate, out)
    assert str(caught.value) == 'paired L2 complete retained reference cells/geometry disagree with candidate'
    assert out.exists() is False


def test_omitted_cell_with_rebalanced_other_refuses(tmp_path: Path) -> None:
    reference, check, candidate, body = inputs(tmp_path)
    body['cells'].pop(0)
    body['native']['emitted_cells'] = 1
    body['results'][2]['buckets'][0]['other'] = {'b': [6, 0], 'o': [2, 0]}
    write(candidate, body)
    out = tmp_path / 'failed.json'
    with pytest.raises(AssertionError) as caught:
        module.compare(reference, check, candidate, out)
    assert str(caught.value) == 'paired L2 complete reference roots/buckets/Other disagree with candidate'
    assert out.exists() is False


def test_reference_must_have_its_own_bound_complete_acceptance(tmp_path: Path) -> None:
    reference, check, candidate, _ = inputs(tmp_path)
    proof = loads(check.read_bytes())
    proof['artifact']['sha256'] = '0' * 64
    write(check, proof)
    with pytest.raises(ValueError) as caught:
        module.compare(reference, check, candidate, tmp_path / 'failed.json')
    assert str(caught.value) == 'paired L2 catalog check artifact bytes/hash mismatch'


@pytest.mark.parametrize('changed', ['budget', 'registry', 'source', 'geometry', 'missing'])
def test_identity_mismatches_refuse_before_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str) -> None:
    reference, check, candidate, _ = inputs(tmp_path)
    original = module.load
    def load(path, covered):
        body, prepared, raw = original(path, covered)
        if path == candidate:
            if changed == 'budget':
                prepared['budget'] = 8
            elif changed == 'registry':
                prepared['provenance']['queries']['sha256'] = '0' * 64
            elif changed == 'source':
                prepared['rows'][0] = 12
            elif changed == 'geometry':
                body['frames'][0]['path'] = 'other/x'
            else:
                prepared['patterns'] = ('.txt', 'm')
        return body, prepared, raw
    monkeypatch.setattr(module, 'load', load)
    out = tmp_path / 'failed.json'
    with pytest.raises(ValueError) as caught:
        module.compare(reference, check, candidate, out)
    assert str(caught.value) == {
        'budget': 'paired L2 comparison requires identical source dates, budget and complete geometry',
        'source': 'paired L2 comparison requires identical source dates, budget and complete geometry',
        'geometry': 'paired L2 comparison requires identical source dates, budget and complete geometry',
        'registry': 'paired L2 comparison requires the same complete query registry bytes/header',
        'missing': 'paired L2 candidate is missing reference predicates',
    }[changed]
    assert out.exists() is False


def test_output_is_never_overwritten_and_preflight_does_no_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / 'out.json'
    out.write_text('keep\n')
    calls = []
    monkeypatch.setattr(module, 'load', lambda *args: calls.append(args))
    with pytest.raises(ValueError) as caught:
        module.compare(tmp_path / 'ref', tmp_path / 'check', tmp_path / 'candidate', out)
    assert str(caught.value) == 'paired L2 comparison output must be new in an existing directory'
    assert (calls, out.read_text()) == ([], 'keep\n')


def test_each_cell_list_is_traversed_once_not_once_per_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reference, check, candidate, _ = inputs(tmp_path)
    original, cells = module.load, []
    class Cells(list):
        traversals = 0
        def __iter__(self):
            self.traversals += 1
            yield from super().__iter__()
    def load(path, covered):
        body, prepared, raw = original(path, covered)
        counted = Cells(body['cells'])
        body['cells'] = counted
        cells.append(counted)
        return body, prepared, raw
    monkeypatch.setattr(module, 'load', load)
    monkeypatch.setattr(module, '_accept', lambda *args: None)
    module.compare(reference, check, candidate, tmp_path / 'out.json')
    assert [body.traversals for body in cells] == [1, 1]


def test_cli_forwards_paths_and_emits_only_compact_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud.cli import main
    calls = []
    paths = tuple(tmp_path / name for name in ('reference', 'check', 'candidate', 'out'))
    body = {'schema': 'hot-l2-pair-compare-v1', 'complete': True, 'queries_compared': 5, 'cells_compared': 95,
            'candidate_queries': 100, 'candidate_cells': 500, 'compare_s': .1, 'private': {'storage': 'not output'}}
    def compare(*args):
        calls.append(args)
        return body
    monkeypatch.setattr(module, 'compare', compare)
    result = CliRunner().invoke(main, ['ch-hot-l2-pair-compare', *map(str, paths[:3]), '-o', str(paths[3])])
    assert (result.exit_code, result.stderr) == (0, '')
    assert loads(result.stdout) == {**{key: body[key] for key in ('schema', 'complete', 'queries_compared', 'cells_compared', 'candidate_queries', 'candidate_cells', 'compare_s')}, 'out': str(paths[3])}
    assert calls == [paths]
