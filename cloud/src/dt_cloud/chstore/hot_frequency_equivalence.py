"""Date-bound substring equivalence sizing from one complete union registry.

For each date, Q containing P proves Hit(Q) subset Hit(P). Equal exact finite
cardinalities then prove equal sets, hence equal coverage aggregates in every
subtree. Null counts never prove equality; future scans require a fresh proof.
This report does not change an accepted registry or authorize kernel aliases.
"""

from hashlib import sha256
from json import dumps, loads
from pathlib import Path
from re import fullmatch

from .hot_frequency_registry import PinnedExport, UNION_SCHEMA, load_queries
from .hot_l1_catalog import _unique_object


def report(
    queries: Path,
    out: Path,
    *,
    expected_sha256: str | None = None,
) -> dict:
    if out.exists() or out.is_symlink() or not out.parent.is_dir():
        raise ValueError('equivalence report output must be new in an existing directory')
    if expected_sha256 is not None and (not isinstance(expected_sha256, str) or fullmatch('[a-f0-9]{64}', expected_sha256) is None):
        raise ValueError('equivalence expected SHA256 must be 64 lowercase hexadecimal characters')
    raw = queries.read_bytes()
    digest = sha256(raw).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError('equivalence registry SHA256 differs from the pinned expectation')
    lines = raw.splitlines()
    header = loads(lines[0], object_pairs_hook=_unique_object) if lines else None
    if not isinstance(header, dict) or header.get('schema') != UNION_SCHEMA:
        raise ValueError('equivalence requires one completed dated-union query export')
    dates = header.get('dates')
    if not isinstance(dates, list) or not dates:
        raise ValueError('equivalence requires explicit registry dates')
    header, patterns = load_queries(PinnedExport(raw), header.get('target'), dates[-1])
    rows = [loads(line, object_pairs_hook=_unique_object) for line in lines[1:-1]]
    vectors = {row['pattern']: tuple(row['direct_matching_paths'][day] for day in dates) for row in rows}
    parent = {pattern: pattern for pattern in patterns}
    rank = dict.fromkeys(patterns, 0)
    def find(pattern: str) -> str:
        while parent[pattern] != pattern:
            parent[pattern] = parent[parent[pattern]]
            pattern = parent[pattern]
        return pattern
    edges = []
    for longer in patterns:
        vector = vectors[longer]
        if any(value is None for value in vector):
            continue
        shorter_candidates = {longer[start:end] for start in range(len(longer)) for end in range(start + 1, len(longer) + 1)
                              if end - start < len(longer)}
        for shorter in sorted(shorter_candidates, key=lambda text: (len(text), text)):
            if vectors.get(shorter) != vector:
                continue
            left, right = find(shorter), find(longer)
            if left == right:
                continue
            if rank[left] < rank[right]:
                left, right = right, left
            parent[right] = left
            if rank[left] == rank[right]:
                rank[left] += 1
            edges.append({'shorter': shorter, 'longer': longer, 'direct_matching_paths': dict(zip(dates, vector, strict=True))})
    groups = {}
    for pattern in patterns:
        groups.setdefault(find(pattern), []).append(pattern)
    classes = []
    for members in groups.values():
        representative = min(members, key=lambda text: (-len(text), text))
        classes.append({'representative': representative, 'members': sorted(members, key=lambda text: (len(text), text)),
                        'direct_matching_paths': dict(zip(dates, vectors[representative], strict=True))})
    classes.sort(key=lambda row: (len(row['representative']), row['representative']))
    work = []
    for side, day in enumerate(dates):
        known = sum(vector[side] for vector in vectors.values() if vector[side] is not None)
        representative_known = sum(row['direct_matching_paths'][day] for row in classes if row['direct_matching_paths'][day] is not None)
        unknown = sum(vector[side] is None for vector in vectors.values())
        minimum = header['sources'][side]['threshold_paths']
        unknown_upper = unknown * (minimum - 1)
        removed = known - representative_known
        work.append({'date': day, 'known_predicate_frequency_sum': known, 'representative_known_frequency_sum': representative_known,
                     'removed_known_frequency_sum': removed, 'unknown_predicates': unknown,
                     'unknown_source_minimum': minimum, 'unknown_frequency_sum_upper_bound': unknown_upper,
                     'removed_unknown_frequency_sum': 0,
                     'removed_fraction_bounds': {
                         'lower': {'numerator': removed, 'denominator': known + unknown_upper} if known + unknown_upper else None,
                         'upper': {'numerator': removed, 'denominator': known} if known else None}})
    result = {'schema': 'hot-frequency-equivalence-report-v1', 'complete': True, 'target': header['target'], 'dates': dates,
              'queries': {'path': str(queries), 'sha256': digest, 'bytes': len(raw), 'header': header},
              'patterns': len(patterns), 'classes': len(classes), 'aliases_removed': len(patterns) - len(classes),
              'nontrivial_classes': sum(len(row['members']) > 1 for row in classes), 'proof_edges': len(edges),
              'representative_policy': 'longest Unicode literal; ascending lexical tie break',
              'proof_basis': 'proper literal containment plus equal non-null exact direct-path cardinalities on every registry date; transitive closure',
              'valid_only_for_registry_dates': True, 'accepted_for_kernel': False, 'independent_path_set_validation': False,
              'persistent_index_created': False, 'direct_hit_work': work,
              'work_metric': 'sum of direct name-hit outputs across predicates, not unique paths, coverage bytes or a runtime promise',
              'equivalence_classes': classes, 'edges': edges}
    owned = False
    try:
        with out.open('x') as output:
            owned = True
            output.write(dumps(result, ensure_ascii=False) + '\n')
    except BaseException:
        if owned:
            out.unlink()
        raise
    return result
