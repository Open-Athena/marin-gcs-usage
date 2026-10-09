"""Exact subset parity against a larger completed paired L2 artifact.

This is artifact parity for every reference predicate, not independent source
acceptance of the candidate's remaining predicates. No database is queried.
"""

from hashlib import sha256
from json import dumps, loads
from pathlib import Path
from time import monotonic

from .hot_l1_catalog import _unique_object
from .hot_l2_pair_catalog import CUTOFF, _accept
from .hot_l2_pair_check import load


def _descriptor(path: Path, raw: bytes) -> dict:
    return {'path': str(path), 'sha256': sha256(raw).hexdigest(), 'bytes': len(raw)}


def _selected_cells(
    body: dict,
    prepared: dict,
    selected: set[str],
) -> dict[str, list[dict]]:
    by_id = dict(enumerate(prepared['patterns'], 1))
    result = {pattern: [] for pattern in selected}
    for cell in body['cells']:
        pattern = by_id[cell['predicate_id']]
        if pattern in selected:
            result[pattern].append({key: value for key, value in cell.items() if key != 'predicate_id'})
    return result


def compare(
    reference: Path,
    check: Path,
    candidate: Path,
    out: Path,
) -> dict:
    if out.exists() or out.is_symlink() or not out.parent.is_dir():
        raise ValueError('paired L2 comparison output must be new in an existing directory')
    start = monotonic()
    before, left, before_raw = load(reference, 'm')
    check_raw = check.read_bytes()
    proof = loads(check_raw, object_pairs_hook=_unique_object)
    _accept(before, left, before_raw, proof)
    after, right, after_raw = load(candidate, 'm')
    if (before.get('cutoff_scope') != CUTOFF or after.get('cutoff_scope') != CUTOFF
            or any(left[key] != right[key] for key in ('target', 'dates', 'dbs', 'rows', 'budget', 'buckets'))
            or before['frames'] != after['frames']):
        raise ValueError('paired L2 comparison requires identical source dates, budget and complete geometry')
    registries = [prepared['provenance']['queries'] for prepared in (left, right)]
    if any(registries[0][key] != registries[1][key] for key in ('sha256', 'bytes', 'header')):
        raise ValueError('paired L2 comparison requires the same complete query registry bytes/header')
    selected = set(left['patterns'])
    if not selected <= set(right['patterns']):
        raise ValueError('paired L2 candidate is missing reference predicates')
    roots = {row['pattern']: {key: value for key, value in row.items() if key != 'predicate_id'} for row in after['results']}
    for row in before['results']:
        if {key: value for key, value in row.items() if key != 'predicate_id'} != roots[row['pattern']]:
            raise AssertionError('paired L2 complete reference roots/buckets/Other disagree with candidate')
    left_cells = _selected_cells(before, left, selected)
    right_cells = _selected_cells(after, right, selected)
    if left_cells != right_cells:
        raise AssertionError('paired L2 complete retained reference cells/geometry disagree with candidate')
    result = {'schema': 'hot-l2-pair-compare-v1', 'complete': True, 'target': left['target'], 'dates': left['dates'],
              'budget': left['budget'], 'cutoff_scope': CUTOFF,
              'reference': {**_descriptor(reference, before_raw), 'check': _descriptor(check, check_raw)},
              'candidate': _descriptor(candidate, after_raw),
              'query_registry': {key: registries[0][key] for key in ('sha256', 'bytes', 'header')},
              'queries_compared': len(selected), 'cells_compared': len(before['cells']),
              'candidate_queries': len(right['patterns']), 'candidate_cells': len(after['cells']),
              'validation': 'complete paired reference predicate roots/buckets/Other and retained cells; predicate IDs remapped only',
              'reference_check_complete': True, 'full_catalog_source_oracle': False,
              'candidate_remaining_queries_independently_checked': False, 'compare_s': monotonic() - start}
    owned = False
    try:
        with out.open('x') as output:
            owned = True
            output.write(dumps(result) + '\n')
    except BaseException:
        if owned:
            out.unlink()
        raise
    return result
