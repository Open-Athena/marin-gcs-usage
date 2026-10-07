"""Pin existing union membership separately from a fresh dated scalar source.

The original registry bytes are validated on an actual qualification date,
never rewritten to the new target/date. Source proof declarations are trusted
completed daily_scalar output, not an independent object-listing oracle. An
explicit logical-store binding is operator authority, not inferred from the
registry's old physical target. This module neither builds nor publishes.
"""

from dataclasses import dataclass
from hashlib import sha256
from json import loads
from pathlib import Path
from re import fullmatch

from .hot_frequency_registry import PinnedExport, UNION_SCHEMA, iso_date, load_queries
from .hot_l1_catalog import _unique_object
from .narrow import identifier

SCHEMA = 'hot-registry-selection-v1'
SOURCE_SCHEMA = 'daily-scalar-source-v1'
VALIDATION = {'source_hash_checked': True, 'prefix_closed': True,
              'interval_endpoints_checked': True, 'scalar_rollups_checked': True}
REGISTRY_LIMIT = 64 << 20
DOCUMENT_LIMIT = 64 << 10


def _number(value: object, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _hash(value: object) -> bool:
    return isinstance(value, str) and fullmatch('[a-f0-9]{64}', value) is not None


def _identifier(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError('registry selection source/store identifiers must be strings')
    return identifier(value)


def _json(raw: bytes, limit: int) -> dict:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= limit:
        raise ValueError('registry selection requires bounded nonempty immutable byte inputs')
    value = loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError('registry selection metadata must be an object')
    return value


def _registry(raw: bytes) -> tuple[dict, tuple[str, ...]]:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= REGISTRY_LIMIT:
        raise ValueError('registry selection union export must be nonempty immutable bytes at most 64 MiB')
    header = loads(raw.partition(b'\n')[0], object_pairs_hook=_unique_object)
    if (not isinstance(header, dict) or header.get('schema') != UNION_SCHEMA or
            not isinstance(header.get('dates'), list) or not header['dates']):
        raise ValueError('registry selection requires a complete original dated-union export')
    # This date is genuinely in the original registry. It says nothing about
    # the requested build date and must never be replaced by that date.
    header, patterns = load_queries(PinnedExport(raw), header.get('target'), header['dates'][0])
    if not patterns:
        raise ValueError('registry selection requires nonempty complete union membership')
    return header, patterns


def _source(raw: bytes) -> dict:
    body = _json(raw, DOCUMENT_LIMIT)
    required = {'schema', 'complete', 'logical_store', 'date', 'target', 'snapshot_db', 'prefix', 'source',
                'source_rows', 'selected_source_rows', 'nodes', 'root', 'buckets', 'validation', 'limits', 'stages'}
    if (set(body) != required or body.get('schema') != SOURCE_SCHEMA or body.get('complete') is not True or
            body.get('prefix') != '' or not iso_date(body.get('date'))):
        raise ValueError('registry selection requires a completed global daily-scalar-source-v1 manifest')
    for field in ('logical_store', 'target', 'snapshot_db'):
        _identifier(body[field])
    if body['target'] != body['snapshot_db']:
        raise ValueError('registry selection daily source must use its own target as snapshot database')
    validation = body['validation']
    if (not isinstance(validation, dict) or set(validation) != set(VALIDATION) or
            any(validation[key] is not True for key in VALIDATION)):
        raise ValueError('registry selection requires all completed daily scalar validation declarations')
    source = body['source']
    if (not isinstance(source, dict) or set(source) != {'schema', 'logical_store', 'date', 'identity', 'bytes', 'sha256'} or
            source['schema'] != 'daily-scalar-input-v1' or source['logical_store'] != body['logical_store'] or
            source['date'] != body['date'] or not _number(source['bytes'], 1) or not _hash(source['sha256'])):
        raise ValueError('registry selection daily input identity differs from its accepted source')
    identity = source['identity']
    if (not isinstance(identity, dict) or set(identity) != {'uri', 'generation'} or
            any(not isinstance(identity[key], str) or not identity[key] for key in identity)):
        raise ValueError('registry selection daily input requires an explicit URI and immutable generation')
    if (not _number(body['source_rows'], 1) or not _number(body['selected_source_rows'], 1) or
            body['selected_source_rows'] > body['source_rows'] or not _number(body['nodes'], 2)):
        raise ValueError('registry selection daily source has invalid complete source/node counts')
    root, buckets = body['root'], body['buckets']
    if (not isinstance(root, dict) or set(root) != {'path', 'pre', 'post', 'b', 'o'} or root['path'] != '' or
            any(not _number(root[key]) for key in ('pre', 'post', 'b', 'o')) or
            root['pre'] != 0 or root['post'] != body['nodes'] - 1 or not isinstance(buckets, list) or not 1 <= len(buckets) <= 6):
        raise ValueError('registry selection daily source requires complete global root geometry')
    previous, paths = 0, set()
    for bucket in buckets:
        if (not isinstance(bucket, dict) or set(bucket) != {'path', 'pre', 'post'} or
                not isinstance(bucket['path'], str) or not bucket['path'] or '/' in bucket['path'] or '\0' in bucket['path'] or
                bucket['path'] in paths or not _number(bucket['pre'], 1) or not _number(bucket['post'], 1) or
                bucket['pre'] != previous + 1 or bucket['post'] < bucket['pre']):
            raise ValueError('registry selection daily buckets must completely partition their own global geometry')
        bucket['path'].encode('utf-8')
        previous = bucket['post']
        paths.add(bucket['path'])
    if previous != root['post'] or not isinstance(body['limits'], dict) or not isinstance(body['stages'], dict):
        raise ValueError('registry selection daily buckets must completely partition their own global geometry')
    return body


def envelope(
    registry_raw: bytes,
    source_manifest_raw: bytes,
    *,
    logical_store: str,
) -> dict:
    """Pure descriptor creation from complete validated bytes, without writes."""
    _identifier(logical_store)
    header, patterns = _registry(registry_raw)
    source = _source(source_manifest_raw)
    return _envelope(registry_raw, source_manifest_raw, logical_store, header, patterns, source)


def _envelope(
    registry_raw: bytes,
    source_manifest_raw: bytes,
    logical_store: str,
    header: dict,
    patterns: tuple[str, ...],
    source: dict,
) -> dict:
    if source['logical_store'] != logical_store:
        raise ValueError('registry selection logical store differs from the accepted daily source')
    return {'schema': SCHEMA, 'complete': True, 'logical_store': logical_store,
            'registry': {'sha256': sha256(registry_raw).hexdigest(), 'bytes': len(registry_raw), 'patterns': len(patterns),
                         'target': header['target'], 'qualification_dates': header['dates']},
            'build_source': {'date': source['date'], 'target': source['target'], 'snapshot_db': source['snapshot_db'],
                             'manifest_sha256': sha256(source_manifest_raw).hexdigest(), 'manifest_bytes': len(source_manifest_raw)}}


@dataclass(frozen=True)
class RegistrySelection:
    selection_raw: bytes
    registry_raw: bytes
    source_manifest_raw: bytes
    logical_store: str
    registry_target: str
    qualification_dates: tuple[str, ...]
    patterns: tuple[str, ...]
    date: str
    target: str
    snapshot_db: str
    nodes: int
    buckets: tuple[tuple[int, int, str], ...]

    def metadata(self) -> dict:
        return _json(self.selection_raw, DOCUMENT_LIMIT)

    def registry_header(self) -> dict:
        return loads(self.registry_raw.partition(b'\n')[0], object_pairs_hook=_unique_object)

    def source_manifest(self) -> dict:
        return _json(self.source_manifest_raw, DOCUMENT_LIMIT)


def validate(
    selection_raw: bytes,
    registry_raw: bytes,
    source_manifest_raw: bytes,
) -> RegistrySelection:
    selection = _json(selection_raw, DOCUMENT_LIMIT)
    if (set(selection) != {'schema', 'complete', 'logical_store', 'registry', 'build_source'} or
            selection.get('schema') != SCHEMA or selection.get('complete') is not True):
        raise ValueError('registry selection requires the explicit complete selection envelope')
    registry, source = selection['registry'], selection['build_source']
    if (not isinstance(registry, dict) or set(registry) != {'sha256', 'bytes', 'patterns', 'target', 'qualification_dates'} or
            not _hash(registry['sha256']) or not _number(registry['bytes'], 1) or not _number(registry['patterns'], 1) or
            not isinstance(source, dict) or set(source) != {'date', 'target', 'snapshot_db', 'manifest_sha256', 'manifest_bytes'} or
            not _hash(source['manifest_sha256']) or not _number(source['manifest_bytes'], 1)):
        raise ValueError('registry selection requires complete registry and source hash/count descriptors')
    if not isinstance(registry_raw, bytes) or len(registry_raw) != registry['bytes'] or sha256(registry_raw).hexdigest() != registry['sha256']:
        raise ValueError('registry selection original registry bytes/SHA256 differ from the pinned descriptor')
    if not isinstance(source_manifest_raw, bytes) or len(source_manifest_raw) != source['manifest_bytes'] or sha256(source_manifest_raw).hexdigest() != source['manifest_sha256']:
        raise ValueError('registry selection source manifest bytes/SHA256 differ from the pinned descriptor')
    _identifier(selection['logical_store'])
    header, patterns = _registry(registry_raw)
    body = _source(source_manifest_raw)
    expected = _envelope(registry_raw, source_manifest_raw, selection['logical_store'], header, patterns, body)
    if selection != expected:
        raise ValueError('registry selection qualification/count/source bindings differ from the original accepted bytes')
    return RegistrySelection(selection_raw, registry_raw, source_manifest_raw, selection['logical_store'], header['target'],
                             tuple(header['dates']), patterns, body['date'], body['target'], body['snapshot_db'], body['nodes'],
                             tuple((row['pre'], row['post'], row['path']) for row in body['buckets']))


def load(
    selection_path: Path,
    registry_path: Path,
    source_manifest_path: Path,
) -> RegistrySelection:
    """Read each explicit file once and retain only its pinned immutable bytes."""
    def bounded(path: Path, limit: int) -> bytes:
        with path.open('rb') as source:
            return source.read(limit + 1)
    return validate(bounded(selection_path, DOCUMENT_LIMIT), bounded(registry_path, REGISTRY_LIMIT),
                    bounded(source_manifest_path, DOCUMENT_LIMIT))
