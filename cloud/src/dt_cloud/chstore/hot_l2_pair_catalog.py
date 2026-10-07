"""Artifact-only bucket reads from an explicitly accepted paired L2 experiment.

The operator trusts the supplied artifacts and provenance files. A bound check
proves the complete covered control, not every query against independent source
truth. The paired cutoff/partition stays fixed when dates are reversed or equal.
"""

from hashlib import sha256
from json import loads
from math import isfinite
from pathlib import Path

from .hot_l1_catalog import CatalogRequest, SCOPE, _pattern, _unique_object
from .hot_l2_pair_check import load

CUTOFF = 'fixed per query and bucket root across all depth-2 children; not arbitrary-prefix refinement'
COVERED = 'complete ordinary recursive bucket/frame rollups on both dates'
SELECTED = 'complete scoped first-hit full-path frontier on both dates'


def _accept(
    body: dict,
    prepared: dict,
    raw: bytes,
    proof: dict,
) -> None:
    fields = {'schema', 'complete', 'target', 'dates', 'artifact', 'source_rows', 'prefix_proofs_checked',
              'covered_control', 'selected_cells', 'full_catalog_source_oracle', 'source_contract', 'limits', 'check_s'}
    if (not isinstance(proof, dict) or set(proof) != fields or proof['schema'] != 'hot-l2-pair-check-v1'
            or proof['complete'] is not True or proof['target'] != prepared['target'] or proof['dates'] != prepared['dates']
            or proof['source_rows'] != prepared['rows'] or not isinstance(proof['source_rows'], list)
            or any(type(n) is not int for n in proof['source_rows']) or proof['prefix_proofs_checked'] is not True
            or proof['full_catalog_source_oracle'] is not False or proof['source_contract'] != prepared['provenance']['source_contract']):
        raise ValueError('paired L2 catalog requires a complete matching check proof')
    if type(proof['check_s']) not in (int, float) or not isfinite(proof['check_s']) or proof['check_s'] < 0:
        raise ValueError('paired L2 catalog check duration is invalid')
    descriptor = proof['artifact']
    if (not isinstance(descriptor, dict) or set(descriptor) != {'path', 'sha256', 'bytes'}
            or not isinstance(descriptor['path'], str) or type(descriptor['bytes']) is not int
            or descriptor['bytes'] != len(raw) or descriptor['sha256'] != sha256(raw).hexdigest()):
        raise ValueError('paired L2 catalog check artifact bytes/hash mismatch')
    covered_id = prepared['patterns'].index('m') + 1
    expected = {'pattern': 'm', 'validation': COVERED, 'frames_checked': len(body['frames']),
                'heavy_cells_checked': sum(row['predicate_id'] == covered_id for row in body['cells']),
                'buckets_checked': len(prepared['buckets'])}
    covered = proof['covered_control']
    if (covered != expected or any(type(covered[key]) is not int for key in ('frames_checked', 'heavy_cells_checked', 'buckets_checked'))):
        raise ValueError('paired L2 catalog requires full covered-control acceptance')
    limits = proof['limits']
    if (not isinstance(limits, dict) or set(limits) != {'seconds', 'memory_gib', 'max_interval_span', 'max_selected_cells'}
            or any(type(value) is not int for value in limits.values()) or not 15 <= limits['seconds'] <= 30
            or limits['memory_gib'] != 2 or not 1 <= limits['max_interval_span'] <= 1_000_000
            or not 0 <= limits['max_selected_cells'] <= 4):
        raise ValueError('paired L2 catalog check limits are invalid')
    selected = proof['selected_cells']
    expected_queries = [(i, text) for i, text in enumerate(prepared['patterns'], 1) if text != 'm']
    if not isinstance(selected, list) or len(selected) != len(expected_queries):
        raise ValueError('paired L2 catalog selected-oracle declarations are incomplete')
    first_eligible = {}
    for cell in body['cells']:
        if cell['post'] - cell['pre'] + 1 <= limits['max_interval_span']:
            first_eligible.setdefault(cell['predicate_id'], cell)
    checked = 0
    for row, (qid, pattern) in zip(selected, expected_queries, strict=True):
        if (not isinstance(row, dict) or type(row.get('predicate_id')) is not int or row['predicate_id'] != qid
                or row.get('pattern') != pattern or type(row.get('checked')) is not bool):
            raise ValueError('paired L2 catalog selected-oracle declaration is invalid')
        cell = first_eligible.get(qid)
        if row['checked']:
            frame = row.get('frame_id')
            if (set(row) != {'predicate_id', 'pattern', 'checked', 'frame_id', 'interval_span', 'validation'} or type(frame) is not int or cell is None
                    or type(row['interval_span']) is not int or row['interval_span'] != cell['post'] - cell['pre'] + 1
                    or frame != cell['frame_id'] or checked == limits['max_selected_cells']
                    or row['interval_span'] > limits['max_interval_span'] or row['validation'] != SELECTED):
                raise ValueError('paired L2 catalog selected-oracle declaration is invalid')
            checked += 1
        elif (set(row) != {'predicate_id', 'pattern', 'checked', 'reason'}
              or row['reason'] != ('selected-cell budget exhausted' if checked == limits['max_selected_cells'] else 'no emitted cell within interval cap')
              or (checked != limits['max_selected_cells'] and cell is not None)):
            raise ValueError('paired L2 catalog selected-oracle declaration is invalid')
    if checked > limits['max_selected_cells']:
        raise ValueError('paired L2 catalog selected-oracle acceptance exceeds its budget')
    if body.get('cutoff_scope') != CUTOFF:
        raise ValueError('paired L2 catalog cutoff contract is unsupported')


def _weights(row: dict, side: int) -> dict:
    return {key: str(row[key][side]) for key in ('b', 'o')}


class HotL2PairCatalog:
    def __init__(self, artifact: Path, check: Path) -> None:
        body, prepared, raw = load(artifact, 'm')
        proof = loads(check.read_bytes(), object_pairs_hook=_unique_object)
        _accept(body, prepared, raw, proof)
        self.target = prepared['target']
        self.dates = tuple(prepared['dates'])
        self.patterns = tuple(prepared['patterns'])
        self._patterns = frozenset(self.patterns)
        self.paths = tuple(path for _, _, path in prepared['buckets'])
        self._budget = body['budget']
        self._proof = {'artifact_sha256': sha256(raw).hexdigest(), 'artifact_bytes': len(raw),
                       'prefix_proofs_checked': True, 'covered_control': dict(proof['covered_control']),
                       'full_catalog_source_oracle': False}
        self._selected = {row['pattern']: dict(row) for row in proof['selected_cells']}
        self._buckets = {}
        self._children = {}
        by_id = {i: pattern for i, pattern in enumerate(self.patterns, 1)}
        for result in body['results']:
            for bucket in result['buckets']:
                key = result['pattern'], bucket['path']
                self._buckets[key] = bucket
                self._children[key] = []
        for cell in body['cells']:
            bucket = self.paths[cell['bucket']]
            self._children[by_id[cell['predicate_id']], bucket].append(cell)

    @classmethod
    def load(cls, artifact: Path, check: Path) -> 'HotL2PairCatalog':
        return cls(artifact, check)

    def metadata(self) -> dict:
        return {'schema': 'hot-l2-pair-catalog-registry-v1', 'target': self.target, 'dates': list(self.dates),
                'patterns': len(self.patterns), 'buckets': len(self.paths), 'levels': 2,
                'cutoff_scope': CUTOFF, 'budget': self._budget, 'child_drill': False,
                'full_catalog_source_oracle': False}

    def _key(
        self,
        date: str,
        pattern: str,
        path: str = '',
    ) -> tuple[int, str]:
        try:
            normalized = _pattern(pattern)
        except ValueError as error:
            raise CatalogRequest(str(error)) from error
        if '\0' in normalized or normalized not in self._patterns or date not in self.dates:
            raise CatalogRequest('paired L2 pattern/date is not registered; no scan fallback')
        if path not in self.paths:
            raise CatalogRequest('paired L2 serves declared bucket roots only; no deeper drill or fallback')
        return self.dates.index(date), normalized

    def view(
        self,
        date: str,
        pattern: str,
        *,
        path: str = '',
    ) -> dict:
        side, normalized = self._key(date, pattern, path)
        bucket = self._buckets[normalized, path]
        validation = {**self._proof, 'covered_control': dict(self._proof['covered_control']),
                      'query_oracle': (dict(self._proof['covered_control']) if normalized == 'm' else dict(self._selected[normalized]))}
        return {'schema': 'hot-l2-pair-catalog-v1', 'target': self.target, 'date': date, 'pattern': normalized, 'path': path,
                'exact': True, 'incremental': False, 'levels': 2, 'scope': SCOPE,
                'source': 'registered accepted paired sparse artifact', 'validation': validation,
                'cutoff': {'scope': CUTOFF, 'dates': list(self.dates), 'budget': self._budget, 'threshold_bytes': str(bucket['threshold_bytes'])},
                'geometry': {'pre': bucket['pre'], 'post': bucket['post']}, 'root': _weights(bucket, side),
                'children': [{'path': row['path'], 'name': row['path'].rsplit('/', 1)[-1],
                              'pre': row['pre'], 'post': row['post'], **_weights(row, side), 'drill': False}
                             for row in self._children[normalized, path]],
                'other': {**_weights(bucket['other'], side), 'drill': False},
                'capabilities': {'bucket_roots_only': True, 'child_drill': False, 'filters': False}}

    def diff(
        self,
        before_date: str,
        after_date: str,
        pattern: str,
        *,
        path: str = '',
    ) -> dict:
        before = self.view(before_date, pattern, path=path)
        after = self.view(after_date, pattern, path=path)
        def paired(left: dict, right: dict) -> dict:
            return {'before': {key: left[key] for key in ('b', 'o')}, 'after': {key: right[key] for key in ('b', 'o')},
                    'delta': {key: str(int(right[key]) - int(left[key])) for key in ('b', 'o')}}
        return {'schema': 'hot-l2-pair-catalog-diff-v1', 'target': self.target, 'pattern': before['pattern'], 'path': path,
                'dates': [before_date, after_date], 'exact': True, 'incremental': False, 'levels': 2, 'scope': SCOPE,
                'source': before['source'], 'validation': before['validation'], 'cutoff': before['cutoff'], 'geometry': before['geometry'],
                'root': paired(before['root'], after['root']),
                'children': [{**{key: left[key] for key in ('path', 'name', 'pre', 'post')}, **paired(left, right), 'drill': False}
                             for left, right in zip(before['children'], after['children'], strict=True)],
                'other': {**paired(before['other'], after['other']), 'drill': False}, 'capabilities': before['capabilities']}
