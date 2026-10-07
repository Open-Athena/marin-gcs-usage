"""Root-only dated native L1 artifacts with unchanged registry provenance.

The accepted supplied scalar source and native kernel are trusted. Structural
checks and selected controls do not constitute a full-catalog source oracle.
This module does not publish generations, ingest history or provide fallback.
"""

from copy import deepcopy
from hashlib import sha256
from json import loads
from math import isfinite
from os import O_CREAT, O_EXCL, O_WRONLY, X_OK, access, fdopen, open as open_fd
from pathlib import Path
from re import fullmatch
from time import monotonic

from .client import Ch
from .daily_scalar import manifest_bytes
from .hot_l1_batch_catalog import _date, _literal
from .hot_l1_batch_stream import aggregate_source
from .hot_l1_catalog import CatalogRequest, SCOPE, _unique_object
from .hot_registry_selection import RegistrySelection, validate as validate_selection

SCHEMA = 'dated-hot-l1-native-v1'
LIMIT = 64 << 20
VALIDATION = {
    'description': 'complete registered native L1 over the bound audited scalar source; not an independent full-catalog source oracle',
    'source_prefix_proofs_checked': True,
    'independent_full_catalog_source_oracle': False,
}


def _integer(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f'dated L1 {field} must be a nonnegative integer')
    return value


def _weights(value: object, field: str) -> dict:
    if not isinstance(value, dict) or set(value) != {'b', 'o'}:
        raise ValueError(f'dated L1 {field} requires exact byte/object weights')
    return {key: _integer(value[key], field + '.' + key) for key in ('b', 'o')}


def _duration(value: object) -> bool:
    return type(value) in (int, float) and isfinite(value) and value >= 0


def _binary(path: Path) -> dict:
    if not path.is_file() or not access(path, X_OK):
        raise ValueError('dated L1 requires an explicit existing native executable')
    digest, size = sha256(), 0
    with path.open('rb') as file:
        while chunk := file.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    if not size:
        raise ValueError('dated L1 native executable must be nonempty')
    return {'bytes': size, 'sha256': digest.hexdigest()}


def _source_binding(ch: Ch, selection: RegistrySelection) -> None:
    doc = ch.scalar(f'SELECT doc FROM {selection.snapshot_db}.source_manifest')
    if not isinstance(doc, str):
        raise ValueError('dated L1 completed CH source manifest is absent')
    body = loads(doc, object_pairs_hook=_unique_object)
    if manifest_bytes(body) != selection.source_manifest_raw:
        raise ValueError('dated L1 actual CH source manifest differs from the pinned selection')


def _validated(body: dict) -> tuple[RegistrySelection, dict[str, dict]]:
    required = {'schema', 'complete', 'exact', 'incremental', 'levels', 'scope', 'logical_store', 'date', 'target', 'snapshot_db',
                'selection', 'registry_utf8', 'source_manifest_utf8', 'results', 'native', 'binary', 'source_query_id', 'stages', 'validation'}
    if (not isinstance(body, dict) or set(body) != required or body['schema'] != SCHEMA or body['complete'] is not True or
            body['exact'] is not True or body['incremental'] is not False or type(body['levels']) is not int or body['levels'] != 1 or body['scope'] != SCOPE):
        raise ValueError('dated L1 requires a complete exact root-only native artifact')
    if not isinstance(body['registry_utf8'], str) or not isinstance(body['source_manifest_utf8'], str) or not isinstance(body['selection'], dict):
        raise ValueError('dated L1 requires original registry/source bytes and a selection envelope')
    registry = body['registry_utf8'].encode('utf-8')
    source = body['source_manifest_utf8'].encode('utf-8')
    selection = validate_selection(manifest_bytes(body['selection']), registry, source)
    source_body = selection.source_manifest()
    if manifest_bytes(source_body) != source:
        raise ValueError('dated L1 requires canonical pinned daily source manifest bytes')
    if (body['logical_store'], body['date'], body['target'], body['snapshot_db']) != (
            selection.logical_store, selection.date, selection.target, selection.snapshot_db):
        raise ValueError('dated L1 logical store/date/source identity differs from the pinned selection')
    if body['validation'] != VALIDATION or any(type(body['validation'][key]) is not bool for key in ('source_prefix_proofs_checked', 'independent_full_catalog_source_oracle')):
        raise ValueError('dated L1 must retain its truthful source-validation limitations')
    binary = body['binary']
    if (not isinstance(binary, dict) or set(binary) != {'bytes', 'sha256'} or type(binary['bytes']) is not int or binary['bytes'] <= 0 or
            not isinstance(binary['sha256'], str) or fullmatch('[a-f0-9]{64}', binary['sha256']) is None):
        raise ValueError('dated L1 requires complete native binary byte/hash provenance')
    native = body['native']
    native_keys = {'schema', 'exact', 'incremental', 'levels', 'nodes_read', 'registered_predicates', 'peak_stack', 'peak_active', 'native_peak_rss_bytes'}
    if (not isinstance(native, dict) or set(native) != native_keys or native['schema'] != 'hot-l1-native-stream-v1' or
            native['exact'] is not True or native['incremental'] is not False or type(native['levels']) is not int or native['levels'] != 1):
        raise ValueError('dated L1 requires complete native process statistics without a duplicate matrix')
    for key in ('nodes_read', 'registered_predicates', 'peak_stack', 'peak_active', 'native_peak_rss_bytes'):
        _integer(native[key], 'native.' + key)
    if (native['nodes_read'] != selection.nodes or native['registered_predicates'] != len(selection.patterns) or
            native['peak_stack'] > selection.nodes or native['peak_active'] > len(selection.patterns)):
        raise ValueError('dated L1 native counts differ from the complete source/registry')
    if (not isinstance(body['source_query_id'], str) or fullmatch('hot_l1_stream_[a-f0-9]{32}', body['source_query_id']) is None or
            not isinstance(body['stages'], dict) or set(body['stages']) != {'before_source_binding_s', 'aggregate_s', 'after_source_binding_s', 'build_s'} or
            not all(_duration(value) for value in body['stages'].values())):
        raise ValueError('dated L1 requires valid owned query identity and stage timings')
    results = body['results']
    if not isinstance(results, list) or len(results) != len(selection.patterns):
        raise ValueError('dated L1 results must contain complete ordered registry membership')
    entries, global_root = {}, source_body['root']
    for predicate_id, (pattern, result) in enumerate(zip(selection.patterns, results, strict=True), 1):
        if (not isinstance(result, dict) or set(result) != {'predicate_id', 'pattern', 'root', 'buckets'} or
                type(result['predicate_id']) is not int or result['predicate_id'] != predicate_id or result['pattern'] != pattern):
            raise ValueError('dated L1 result IDs/literals differ from the original ordered registry')
        root = _weights(result['root'], 'root')
        buckets = result['buckets']
        if not isinstance(buckets, list) or len(buckets) != len(selection.buckets):
            raise ValueError('dated L1 result has incomplete source bucket coverage')
        sums = {'b': 0, 'o': 0}
        for row, (pre, post, path) in zip(buckets, selection.buckets, strict=True):
            if (not isinstance(row, dict) or set(row) != {'pre', 'post', 'path', 'b', 'o'} or
                    type(row['pre']) is not int or type(row['post']) is not int or (row['pre'], row['post'], row['path']) != (pre, post, path)):
                raise ValueError('dated L1 bucket bounds/paths differ from the accepted daily source')
            for key in ('b', 'o'):
                sums[key] += _integer(row[key], 'bucket.' + key)
        if root != sums or any(root[key] > global_root[key] for key in ('b', 'o')):
            raise ValueError('dated L1 root/bucket conservation exceeds the accepted complete source')
        entries[pattern] = result
    return selection, entries


def build(
    ch: Ch,
    selection: RegistrySelection,
    *,
    binary: Path,
    out: Path,
) -> dict:
    """Build one fresh private artifact; no completed output on source drift."""
    if out.exists() or out.is_symlink():
        raise ValueError('dated L1 refuses an existing output path')
    # Revalidate retained bytes rather than trusting a manually constructed
    # dataclass's convenient identity fields or mutable metadata dictionaries.
    selection = validate_selection(selection.selection_raw, selection.registry_raw, selection.source_manifest_raw)
    if manifest_bytes(selection.source_manifest()) != selection.source_manifest_raw:
        raise ValueError('dated L1 requires canonical pinned daily source manifest bytes')
    binary_identity, start = _binary(binary), monotonic()
    stage = monotonic()
    _source_binding(ch, selection)
    before_s = monotonic() - stage
    aggregate = aggregate_source(ch, selection.snapshot_db, selection.patterns, [list(row) for row in selection.buckets], selection.nodes, binary=binary)
    stage = monotonic()
    _source_binding(ch, selection)
    after_s = monotonic() - stage
    if _binary(binary) != binary_identity:
        raise ValueError('dated L1 native binary changed during construction')
    body = {'schema': SCHEMA, 'complete': True, 'exact': True, 'incremental': False, 'levels': 1, 'scope': SCOPE,
            'logical_store': selection.logical_store, 'date': selection.date, 'target': selection.target, 'snapshot_db': selection.snapshot_db,
            'selection': selection.metadata(), 'registry_utf8': selection.registry_raw.decode('utf-8'),
            'source_manifest_utf8': selection.source_manifest_raw.decode('utf-8'),
            'results': aggregate['results'], 'native': aggregate['native'], 'binary': binary_identity,
            'source_query_id': aggregate['source_query_id'],
            'stages': {'before_source_binding_s': round(before_s, 6), 'aggregate_s': aggregate['aggregate_s'],
                       'after_source_binding_s': round(after_s, 6), 'build_s': round(monotonic() - start, 6)},
            'validation': dict(VALIDATION)}
    _validated(body)
    raw = manifest_bytes(body)
    if len(raw) > LIMIT:
        raise ValueError('dated L1 private artifact exceeds its 64MiB cap')
    # An interrupted write remains invalid and is never reused/overwritten.
    with fdopen(open_fd(out, O_WRONLY | O_CREAT | O_EXCL, 0o600), 'wb') as file:
        file.write(raw)
    return body


class DatedHotL1Catalog:
    @classmethod
    def from_bytes(cls, raw: bytes) -> 'DatedHotL1Catalog':
        if not isinstance(raw, bytes) or not 0 < len(raw) <= LIMIT:
            raise ValueError('dated L1 private artifact must be nonempty bytes at most 64MiB')
        body = loads(raw, object_pairs_hook=_unique_object)
        selection, entries = _validated(body)
        reader = cls.__new__(cls)
        reader._body, reader._entries, reader.selection = body, entries, selection
        reader.target, reader.logical_store = selection.target, selection.logical_store
        reader.date, reader.dates = selection.date, (selection.date,)
        reader.paths = tuple(sorted(path for _, _, path in selection.buckets))
        reader._identity = {'artifact_sha256': sha256(raw).hexdigest(), 'artifact_bytes': len(raw),
                            'target': selection.target, 'snapshot_db': selection.snapshot_db,
                            'source_manifest_sha256': selection.metadata()['build_source']['manifest_sha256'],
                            'source_prefix_proofs_checked': True}
        return reader

    @classmethod
    def load(cls, path: Path) -> 'DatedHotL1Catalog':
        with path.open('rb') as file:
            return cls.from_bytes(file.read(LIMIT + 1))

    def metadata(self) -> dict:
        header = self.selection.registry_header()
        return {'schema': 'dated-hot-l1-registry-v1', 'logical_store': self.logical_store, 'scan_date': self.date,
                'source': deepcopy(self._identity), 'registry': {'qualification_dates': list(self.selection.qualification_dates),
                'target': self.selection.registry_target, 'patterns': len(self.selection.patterns), 'threshold_paths': header['threshold_paths'],
                'max_chars': header['max_chars'], 'selection_contract': 'membership on declared qualification dates; no current-scan frequency claim'},
                'bucket_paths': list(self.paths), 'levels': 1, 'scope': SCOPE,
                'validation': dict(VALIDATION), 'capabilities': {'bucket_drill': False, 'child_drill': False, 'fallback': False}}

    def view(
        self,
        date: str,
        pattern: str,
        *,
        path: str = '',
    ) -> dict:
        if path != '':
            raise CatalogRequest('dated L1 serves the global root only; no bucket or child drill')
        try:
            day, literal = _date(date), _literal(pattern)
        except (ValueError, UnicodeError):
            raise CatalogRequest('dated L1 requires a valid ISO date and one nonempty NUL/slash-free literal of at most 512 characters') from None
        if day != self.date or literal not in self._entries:
            raise CatalogRequest('dated L1 scan/literal is unavailable; no zero or scan fallback')
        entry = self._entries[literal]
        metadata = self.metadata()
        return {'schema': 'dated-hot-l1-v1', 'logical_store': self.logical_store, 'date': day, 'pattern': literal, 'path': '',
                'exact': True, 'incremental': False, 'levels': 1, 'scope': SCOPE, 'root': deepcopy(entry['root']),
                'buckets': deepcopy(entry['buckets']), 'source': metadata['source'], 'registry': metadata['registry'],
                'validation': metadata['validation'], 'capabilities': metadata['capabilities']}
