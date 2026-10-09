"""Private immutable dated L1 generations with selected full-source proofs.

Single-node acceptance prototype, not deployment or a full-catalog oracle.
Files and proof declarations are operator-trusted and must remain immutable.
No prior or interrupted generation is removed. MAX_SCANS is a local resource
guard, not an inventory/retention or serving-memory guarantee.
"""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from hashlib import sha256
from json import loads
from math import isfinite
from os import O_CREAT, O_EXCL, O_RDWR, O_WRONLY, fdopen, fsync, open as open_fd, replace
from pathlib import Path
from re import fullmatch
from types import MappingProxyType
from typing import Iterator, Mapping
from uuid import uuid4

from .daily_scalar import manifest_bytes
from .dated_hot_l1 import DatedHotL1Catalog, LIMIT as ARTIFACT_LIMIT
from .dated_hot_l1_check import VALIDATION as CHECK_VALIDATION
from .hot_l1_catalog import _unique_object
from .hot_l1_publish import _fsync_dir, _owned
from .hot_frequency_registry import iso_date
from .narrow import identifier

SCHEMA = 'dated-hot-l1-published-generation-v1'
MAX_SCANS = 64
MANIFEST_LIMIT = 256 << 10
PROOF_LIMIT = 64 << 10
VALIDATION = {'selected_full_source_checks': True, 'independent_full_catalog_source_oracle': False,
              'description': 'artifact-bound independent full-source scans for selected registered literals; not a full-catalog oracle'}


def _plain(path: Path) -> Path:
    path = path.absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        if part in ('.', '..'):
            raise ValueError('dated publication paths must be explicit without parent traversal')
        current /= part
        if current.is_symlink():
            raise ValueError('dated publication paths must not be symlinks')
    return path


def _root(path: Path, *, create: bool = False) -> Path:
    path = _plain(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError('dated publication root must be an explicit private directory')
    return path


def _read(path: Path, limit: int) -> bytes:
    path = _plain(path)
    if not path.is_file():
        raise FileNotFoundError('dated publication file is absent or not regular')
    with path.open('rb') as file:
        raw = file.read(limit + 1)
    if not 0 < len(raw) <= limit:
        raise ValueError('dated publication file exceeds its nonempty bounded read contract')
    return raw


def _json(raw: bytes) -> dict:
    body = loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(body, dict):
        raise ValueError('dated publication metadata must be an object')
    return body


def _scope(logical_store: str, paths: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(logical_store, str):
        raise ValueError('dated publication requires an explicit logical store')
    identifier(logical_store)
    if (not isinstance(paths, tuple) or not 1 <= len(paths) <= 6 or
            any(not isinstance(p, str) or not p or '/' in p or '\0' in p for p in paths) or len(set(paths)) != len(paths)):
        raise ValueError('dated publication requires one to six explicit unique complete bucket paths')
    for path in paths:
        path.encode('utf-8')
    return tuple(sorted(paths))


def _manifest(body: dict) -> dict:
    if (not isinstance(body, dict) or set(body) != {'schema', 'complete', 'generation', 'logical_store', 'bucket_paths', 'dates', 'artifacts', 'proofs', 'validation'} or
            body['schema'] != SCHEMA or body['complete'] is not True or not isinstance(body['generation'], str) or
            fullmatch('[a-f0-9]{32}', body['generation']) is None or not isinstance(body['bucket_paths'], list) or body['validation'] != VALIDATION or
            body['validation'].get('selected_full_source_checks') is not True or body['validation'].get('independent_full_catalog_source_oracle') is not False):
        raise ValueError('dated publication requires a complete distinct generation manifest')
    paths = _scope(body['logical_store'], tuple(body['bucket_paths']))
    dates = body['dates']
    if (list(paths) != body['bucket_paths'] or not isinstance(dates, list) or not 1 <= len(dates) <= MAX_SCANS or
            any(not iso_date(day) for day in dates) or sorted(set(dates)) != dates):
        raise ValueError('dated publication dates/scope must be complete, sorted and bounded')
    files = set()
    for role in ('artifacts', 'proofs'):
        rows = body[role]
        if not isinstance(rows, list) or len(rows) != len(dates):
            raise ValueError('dated publication requires exactly one artifact and proof per scan')
        actual_dates = []
        for row in rows:
            name = 'artifact' if role == 'artifacts' else 'proof'
            if (not isinstance(row, dict) or set(row) != {'date', 'file', 'sha256', 'bytes'} or not iso_date(row['date']) or
                    not isinstance(row['file'], str) or fullmatch('generations/' + body['generation'] + '/' + name + r'-[0-9]{4}\.json', row['file']) is None or
                    row['file'] in files or not isinstance(row['sha256'], str) or fullmatch('[a-f0-9]{64}', row['sha256']) is None or
                    type(row['bytes']) is not int or not 1 <= row['bytes'] <= (ARTIFACT_LIMIT if role == 'artifacts' else PROOF_LIMIT)):
                raise ValueError('dated publication descriptor has invalid ownership/date/hash/length')
            actual_dates.append(row['date'])
            files.add(row['file'])
        if actual_dates != dates:
            raise ValueError('dated publication descriptor dates must cover every scan exactly once')
    return body


def _proof(raw: bytes, catalog: DatedHotL1Catalog) -> None:
    proof = _json(raw)
    keys = {'schema', 'complete', 'logical_store', 'date', 'target', 'snapshot_db', 'artifact', 'selection', 'source_manifest',
            'source_nodes', 'selected_patterns', 'selected_patterns_checked', 'independent_full_catalog_source_oracle', 'check_s'}
    selection, source = catalog.selection, catalog.metadata()['source']
    if (set(proof) != keys or proof['schema'] != 'dated-hot-l1-check-v1' or proof['complete'] is not True or
            proof['independent_full_catalog_source_oracle'] is not False or type(proof['check_s']) not in (int, float) or
            not isfinite(proof['check_s']) or proof['check_s'] < 0):
        raise ValueError('dated publication requires complete truthful selected full-source proofs')
    artifact, manifest = proof['artifact'], proof['source_manifest']
    raw_source = selection.source_manifest_raw
    expected_manifest = {'sha256': sha256(raw_source).hexdigest(), 'bytes': len(raw_source),
                         'marker_sha256': sha256(raw_source[:-1]).hexdigest(), 'marker_bytes': len(raw_source) - 1}
    if (proof['logical_store'] != catalog.logical_store or proof['date'] != catalog.date or proof['target'] != selection.target or
            proof['snapshot_db'] != selection.snapshot_db or proof['selection'] != selection.metadata() or
            type(proof['source_nodes']) is not int or proof['source_nodes'] != selection.nodes or
            not isinstance(artifact, dict) or set(artifact) != {'sha256', 'bytes'} or type(artifact['bytes']) is not int or
            artifact != {'sha256': source['artifact_sha256'], 'bytes': source['artifact_bytes']} or
            not isinstance(manifest, dict) or set(manifest) != set(expected_manifest) or
            any(type(manifest[key]) is not int for key in ('bytes', 'marker_bytes')) or manifest != expected_manifest):
        raise ValueError('dated proof artifact/source/selection bindings differ from the copied catalog')
    checks, seen = proof['selected_patterns'], set()
    if (not isinstance(checks, list) or not 1 <= len(checks) <= 8 or type(proof['selected_patterns_checked']) is not int or
            proof['selected_patterns_checked'] != len(checks)):
        raise ValueError('dated publication requires one to eight unique registered selected checks')
    for check in checks:
        if (not isinstance(check, dict) or set(check) != {'pattern', 'validation', 'full_source_scan', 'buckets_checked'} or
                not isinstance(check['pattern'], str) or check['pattern'] not in selection.patterns or check['pattern'] in seen or
                check['validation'] != CHECK_VALIDATION or check['full_source_scan'] is not True or
                type(check['buckets_checked']) is not int or check['buckets_checked'] != len(selection.buckets)):
            raise ValueError('dated publication requires one to eight unique registered selected checks')
        seen.add(check['pattern'])


@dataclass(frozen=True)
class PublishedDatedL1:
    _manifest: dict
    catalogs: Mapping[str, DatedHotL1Catalog]

    @property
    def manifest(self) -> dict:
        return deepcopy(self._manifest)


def _verified(root: Path, row: dict, limit: int) -> bytes:
    raw = _read(_owned(root, row['file']), limit)
    if len(raw) != row['bytes'] or sha256(raw).hexdigest() != row['sha256']:
        raise ValueError('dated generation file length/SHA256 differs from its pinned manifest')
    return raw


def pin(root: Path) -> dict:
    return _manifest(_json(_read(_owned(_root(root), 'current.json'), MANIFEST_LIMIT)))


def load_pinned(root: Path, manifest: dict) -> PublishedDatedL1:
    root, manifest = _root(root), _manifest(deepcopy(manifest))
    immutable = _manifest(_json(_read(_owned(root, 'generations/' + manifest['generation'] + '/manifest.json'), MANIFEST_LIMIT)))
    if immutable != manifest:
        raise ValueError('dated pointer differs from its immutable generation manifest')
    catalogs = {}
    for artifact, proof in zip(manifest['artifacts'], manifest['proofs'], strict=True):
        catalog = DatedHotL1Catalog.from_bytes(_verified(root, artifact, ARTIFACT_LIMIT))
        if catalog.date != artifact['date'] or catalog.logical_store != manifest['logical_store'] or list(catalog.paths) != manifest['bucket_paths']:
            raise ValueError('dated artifact scan/store/complete path scope differs from publication')
        _proof(_verified(root, proof, PROOF_LIMIT), catalog)
        catalogs[catalog.date] = catalog
    return PublishedDatedL1(manifest, MappingProxyType(catalogs))


def load(root: Path) -> PublishedDatedL1:
    return load_pinned(root, pin(root))


@contextmanager
def _writer(root: Path) -> Iterator[None]:
    with fdopen(open_fd(_owned(root, '.publisher.lock'), O_RDWR | O_CREAT, 0o600), 'a+b') as lock:
        try:
            flock(lock, LOCK_EX | LOCK_NB)
        except BlockingIOError:
            raise ValueError('another dated publisher holds the local writer lock') from None
        try:
            yield
        finally:
            flock(lock, LOCK_UN)


def _write(path: Path, raw: bytes) -> None:
    with fdopen(open_fd(path, O_WRONLY | O_CREAT | O_EXCL, 0o600), 'wb') as file:
        file.write(raw)
        file.flush()
        fsync(file.fileno())


def _copy(path: Path, root: Path, relative: str, limit: int) -> tuple[dict, bytes]:
    path = _plain(path)
    if not path.is_file():
        raise ValueError('dated publication inputs must be explicit regular files')
    destination, digest, size = _owned(root, relative), sha256(), 0
    with path.open('rb') as source, fdopen(open_fd(destination, O_WRONLY | O_CREAT | O_EXCL, 0o600), 'wb') as output:
        while chunk := source.read(1 << 20):
            size += len(chunk)
            if size > limit:
                raise ValueError('dated publication copy exceeds its bounded input cap')
            output.write(chunk)
            digest.update(chunk)
        output.flush()
        fsync(output.fileno())
    raw = _read(destination, limit)
    if len(raw) != size or sha256(raw).hexdigest() != digest.hexdigest():
        raise ValueError('dated publication copied bytes changed before validation')
    return {'file': relative, 'sha256': digest.hexdigest(), 'bytes': size}, raw


def publish(
    artifacts: tuple[Path, ...],
    root: Path,
    *,
    proofs: tuple[Path, ...],
    logical_store: str,
    bucket_paths: tuple[str, ...],
) -> dict:
    paths = _scope(logical_store, bucket_paths)
    if (not isinstance(artifacts, tuple) or not isinstance(proofs, tuple) or not 1 <= len(artifacts) <= MAX_SCANS or len(proofs) != len(artifacts)):
        raise ValueError('dated publication requires one artifact and proof per scan, at most 64 scans')
    root = _root(root, create=True)
    with _writer(root):
        current = _owned(root, 'current.json')
        if current.exists():
            load_pinned(root, pin(root))
        generations = _owned(root, 'generations')
        generations.mkdir(mode=0o700, exist_ok=True)
        generation = uuid4().hex
        directory = generations / generation
        directory.mkdir(mode=0o700)
        accepted, declared = {}, {}
        for index, path in enumerate(artifacts):
            descriptor, raw = _copy(path, root, f'generations/{generation}/artifact-{index:04d}.json', ARTIFACT_LIMIT)
            catalog = DatedHotL1Catalog.from_bytes(raw)
            if catalog.date in accepted or catalog.logical_store != logical_store or catalog.paths != paths:
                raise ValueError('dated artifacts require unique scans and the explicit complete logical path scope')
            accepted[catalog.date] = catalog
            declared[catalog.date] = {**descriptor, 'date': catalog.date}
        checked = {}
        for index, path in enumerate(proofs):
            descriptor, raw = _copy(path, root, f'generations/{generation}/proof-{index:04d}.json', PROOF_LIMIT)
            day = _json(raw).get('date')
            if not isinstance(day, str) or day not in accepted or day in checked:
                raise ValueError('dated proofs must cover every copied artifact exactly once')
            _proof(raw, accepted[day])
            checked[day] = {**descriptor, 'date': day}
        dates = sorted(accepted)
        if set(checked) != set(accepted):
            raise ValueError('dated proofs must cover every copied artifact exactly once')
        manifest = _manifest({'schema': SCHEMA, 'complete': True, 'generation': generation, 'logical_store': logical_store,
                              'bucket_paths': list(paths), 'dates': dates, 'artifacts': [declared[day] for day in dates],
                              'proofs': [checked[day] for day in dates], 'validation': dict(VALIDATION)})
        raw = manifest_bytes(manifest)
        if len(raw) > MANIFEST_LIMIT:
            raise ValueError('dated publication generation manifest exceeds 256KiB')
        _write(directory / 'manifest.json', raw)
        _fsync_dir(directory)
        _fsync_dir(generations)
        _fsync_dir(root)
        staged = _owned(root, '.current-' + generation + '.json')
        _write(staged, raw)
        replace(staged, current)
        _fsync_dir(root)
        return manifest
