"""Private declaration fixtures; publication adds no independent source oracle."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from dt_cloud.chstore import dated_hot_l1_publish as module
from dt_cloud.chstore.daily_scalar import manifest_bytes
from dt_cloud.chstore.dated_hot_l1 import DatedHotL1Catalog
from dt_cloud.chstore.dated_hot_l1_check import VALIDATION as CHECK_VALIDATION
from dt_cloud.chstore.hot_registry_selection import envelope
from test_chdated_hot_l1 import prepared, produce  # noqa: F401
from test_chhot_registry_selection import fixture  # noqa: F401


def write(path: Path, raw: bytes) -> Path:
    path.write_bytes(raw)
    return path


def proof(raw: bytes) -> dict:
    catalog = DatedHotL1Catalog.from_bytes(raw)
    selected = catalog.selection
    source = selected.source_manifest_raw
    return {'schema': 'dated-hot-l1-check-v1', 'complete': True, 'logical_store': catalog.logical_store,
            'date': catalog.date, 'target': selected.target, 'snapshot_db': selected.snapshot_db,
            'artifact': {'sha256': sha256(raw).hexdigest(), 'bytes': len(raw)}, 'selection': selected.metadata(),
            'source_manifest': {'sha256': sha256(source).hexdigest(), 'bytes': len(source),
                                'marker_sha256': sha256(source[:-1]).hexdigest(), 'marker_bytes': len(source) - 1},
            'source_nodes': selected.nodes, 'selected_patterns': [{'pattern': 'a', 'validation': CHECK_VALIDATION,
             'full_source_scan': True, 'buckets_checked': 2}], 'selected_patterns_checked': 1,
            'independent_full_catalog_source_oracle': False, 'check_s': 1.}


@pytest.fixture
def pair(prepared, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    first, raw = produce(prepared)
    second = deepcopy(first)
    source = prepared.selection.source_manifest()
    source['date'] = source['source']['date'] = '2026-10-07'
    source['target'] = source['snapshot_db'] = 'day_20261007'
    source['buckets'] = [{'path': 'a', 'pre': 1, 'post': 5}, {'path': 'b', 'pre': 6, 'post': 8}]
    second.update(date='2026-10-07', target='day_20261007', snapshot_db='day_20261007', source_manifest_utf8=manifest_bytes(source).decode())
    second['selection'] = envelope(prepared.selection.registry_raw, manifest_bytes(source), logical_store='gcs_fleet')
    for result in second['results']:
        result['buckets'][0].update(pre=1, post=5)
        result['buckets'][1].update(pre=6, post=8)
    raws = (raw, manifest_bytes(second))
    artifacts = tuple(write(tmp_path / f'artifact-{index}.json', value) for index, value in enumerate(raws))
    proofs = tuple(write(tmp_path / f'proof-{index}.json', manifest_bytes(proof(value))) for index, value in enumerate(raws))
    ids = iter(['b' * 32, 'c' * 32, 'd' * 32, 'e' * 32])
    monkeypatch.setattr(module, 'uuid4', lambda: SimpleNamespace(hex=next(ids)))
    return SimpleNamespace(artifacts=artifacts, proofs=proofs, raws=raws, root=tmp_path / 'published',
                           bodies=(first, second), scope={'logical_store': 'gcs_fleet', 'bucket_paths': ('b', 'a')})


def publish(pair, *, both: bool = True) -> dict:
    return module.publish(pair.artifacts if both else pair.artifacts[:1], pair.root,
                          proofs=tuple(reversed(pair.proofs)) if both else pair.proofs[:1], **pair.scope)


def test_complete_generation_exact_copies_path_scope_local_bounds_and_private_modes(pair) -> None:
    manifest = publish(pair)
    expected = {'schema': module.SCHEMA, 'complete': True, 'generation': 'b' * 32, 'logical_store': 'gcs_fleet',
                'bucket_paths': ['a', 'b'], 'dates': ['2026-10-06', '2026-10-07'],
                'artifacts': [{'date': body['date'], 'file': f'generations/{"b" * 32}/artifact-{index:04d}.json',
                               'sha256': sha256(raw).hexdigest(), 'bytes': len(raw)} for index, (body, raw) in enumerate(zip(pair.bodies, pair.raws, strict=True))],
                'proofs': [{'date': body['date'], 'file': f'generations/{"b" * 32}/proof-{1 - index:04d}.json',
                            'sha256': sha256(pair.proofs[index].read_bytes()).hexdigest(), 'bytes': pair.proofs[index].stat().st_size}
                           for index, body in enumerate(pair.bodies)], 'validation': module.VALIDATION}
    assert (manifest, module.pin(pair.root)) == (expected, expected)
    loaded = module.load(pair.root)
    assert loaded.manifest == expected
    assert sorted(loaded.catalogs) == ['2026-10-06', '2026-10-07']
    assert [loaded.catalogs[day].view(day, 'b') for day in expected['dates']] == [DatedHotL1Catalog.from_bytes(raw).view(day, 'b') for day, raw in zip(expected['dates'], pair.raws, strict=True)]
    assert [(pair.root / row['file']).read_bytes() for row in manifest['artifacts']] == list(pair.raws)
    assert [(pair.root / row['file']).stat().st_mode & 0o777 for row in manifest['artifacts'] + manifest['proofs']] == [0o600] * 4
    assert (pair.root / 'current.json').stat().st_mode & 0o777 == 0o600
    assert (pair.root / 'current.json').read_bytes() == manifest_bytes(expected)
    with pytest.raises(TypeError):
        loaded.catalogs['bad'] = loaded.catalogs['2026-10-06']
    mutable = loaded.manifest
    mutable['dates'].append('changed')
    assert loaded.manifest == expected
    pair.artifacts[0].write_bytes(b'changed external source')
    assert module.load(pair.root).catalogs['2026-10-06'].view('2026-10-06', 'a') == DatedHotL1Catalog.from_bytes(pair.raws[0]).view('2026-10-06', 'a')


@pytest.mark.parametrize('after_replace', [False, True])
def test_failure_near_atomic_pointer_preserves_complete_old_or_new_and_pinned_readers(pair, monkeypatch: pytest.MonkeyPatch, after_replace: bool) -> None:
    old = publish(pair, both=False)
    original = module.replace

    def interrupted(source, destination):
        if after_replace:
            original(source, destination)
        raise OSError('fixture interrupted publication')

    monkeypatch.setattr(module, 'replace', interrupted)
    with pytest.raises(OSError) as caught:
        publish(pair)
    assert str(caught.value) == 'fixture interrupted publication'
    current = module.load(pair.root)
    assert current.manifest['generation'] == ('c' * 32 if after_replace else 'b' * 32)
    assert sorted(current.catalogs) == (['2026-10-06', '2026-10-07'] if after_replace else ['2026-10-06'])
    assert module.load_pinned(pair.root, old).catalogs['2026-10-06'].view('2026-10-06', 'b') == DatedHotL1Catalog.from_bytes(pair.raws[0]).view('2026-10-06', 'b')
    assert sorted(path.name for path in (pair.root / 'generations').iterdir()) == ['b' * 32, 'c' * 32]
    assert (pair.root / ('.current-' + 'c' * 32 + '.json')).exists() is (not after_replace)


def test_fsync_order_precedes_pointer_replace(pair, monkeypatch: pytest.MonkeyPatch) -> None:
    events, original_sync, original_dir, original_replace = [], module.fsync, module._fsync_dir, module.replace

    def sync(fd):
        events.append('file')
        original_sync(fd)

    def directory(path):
        events.append(('directory', path.relative_to(pair.root).as_posix()))
        original_dir(path)

    def replace(source, destination):
        events.append('replace')
        original_replace(source, destination)

    monkeypatch.setattr(module, 'fsync', sync)
    monkeypatch.setattr(module, '_fsync_dir', directory)
    monkeypatch.setattr(module, 'replace', replace)
    publish(pair)
    assert events == ['file'] * 5 + [('directory', 'generations/' + 'b' * 32), ('directory', 'generations'), ('directory', '.'), 'file', 'replace', ('directory', '.')]


@pytest.mark.parametrize('change,error', [
    (lambda p: p['artifact'].update(sha256='0' * 64), 'dated proof artifact/source/selection bindings differ from the copied catalog'),
    (lambda p: p['source_manifest'].update(marker_bytes=1), 'dated proof artifact/source/selection bindings differ from the copied catalog'),
    (lambda p: p['selection']['registry'].update(qualification_dates=['2026-10-06']), 'dated proof artifact/source/selection bindings differ from the copied catalog'),
    (lambda p: p.update(source_nodes=True), 'dated proof artifact/source/selection bindings differ from the copied catalog'),
    (lambda p: p.update(independent_full_catalog_source_oracle=True), 'dated publication requires complete truthful selected full-source proofs'),
    (lambda p: p.update(check_s=True), 'dated publication requires complete truthful selected full-source proofs'),
    (lambda p: p.update(selected_patterns=[]), 'dated publication requires one to eight unique registered selected checks'),
    (lambda p: (p['selected_patterns'].append(dict(p['selected_patterns'][0])), p.update(selected_patterns_checked=2)), 'dated publication requires one to eight unique registered selected checks'),
    (lambda p: p['selected_patterns'][0].update(pattern='unknown'), 'dated publication requires one to eight unique registered selected checks'),
    (lambda p: p['selected_patterns'][0].update(full_source_scan=False), 'dated publication requires one to eight unique registered selected checks'),
])
def test_bad_or_replayed_selected_proof_never_replaces_accepted_pointer(pair, change, error: str) -> None:
    old = publish(pair, both=False)
    malformed = proof(pair.raws[1])
    change(malformed)
    pair.proofs[1].write_bytes(manifest_bytes(malformed))
    with pytest.raises(ValueError) as caught:
        publish(pair)
    assert str(caught.value) == error
    assert module.pin(pair.root) == old
    assert sorted(path.name for path in (pair.root / 'generations').iterdir()) == ['b' * 32, 'c' * 32]


@pytest.mark.parametrize('kind', ['artifact', 'proof', 'generation-manifest', 'pointer'])
def test_hash_or_manifest_corruption_never_falls_back_and_blocks_next_publication(pair, kind: str) -> None:
    old = publish(pair, both=False)
    relative = old['artifacts'][0]['file'] if kind == 'artifact' else old['proofs'][0]['file'] if kind == 'proof' else f'generations/{"b" * 32}/manifest.json' if kind == 'generation-manifest' else 'current.json'
    (pair.root / relative).write_bytes(b'corrupt')
    with pytest.raises(ValueError):
        module.load(pair.root)
    with pytest.raises(ValueError):
        publish(pair)
    assert sorted(path.name for path in (pair.root / 'generations').iterdir()) == ['b' * 32]


def test_duplicate_scan_or_duplicate_proof_refused_without_pointer(pair) -> None:
    with pytest.raises(ValueError) as caught:
        module.publish((pair.artifacts[0], pair.artifacts[0]), pair.root, proofs=pair.proofs, **pair.scope)
    assert str(caught.value) == 'dated artifacts require unique scans and the explicit complete logical path scope'
    assert (pair.root / 'current.json').exists() is False


@pytest.mark.parametrize('kind', ['input', 'root', 'owned'])
def test_symlinks_refused_at_inputs_root_and_owned_generation(pair, kind: str) -> None:
    if kind == 'input':
        alias = pair.root.parent / 'input-alias.json'
        alias.symlink_to(pair.artifacts[0])
        pair.artifacts = (alias,)
        pair.proofs = pair.proofs[:1]
        action = lambda: publish(pair)
    elif kind == 'root':
        pair.root.mkdir()
        alias = pair.root.parent / 'root-alias'
        alias.symlink_to(pair.root, target_is_directory=True)
        pair.root = alias
        action = lambda: publish(pair)
    else:
        old = publish(pair, both=False)
        path = pair.root / old['artifacts'][0]['file']
        path.unlink()
        path.symlink_to(pair.artifacts[0])
        action = lambda: module.load(pair.root)
    with pytest.raises(ValueError) as caught:
        action()
    assert str(caught.value) == ('published generation paths must not be symlinks' if kind == 'owned' else 'dated publication paths must not be symlinks')


def test_manifest_proof_and_copy_caps_refuse_before_visibility(pair, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, 'PROOF_LIMIT', 10)
    with pytest.raises(ValueError) as caught:
        publish(pair)
    assert (str(caught.value), (pair.root / 'current.json').exists()) == ('dated publication copy exceeds its bounded input cap', False)


def test_verified_bytes_are_parsed_once_not_a_later_file_version(pair, monkeypatch: pytest.MonkeyPatch) -> None:
    accepted = publish(pair, both=False)
    artifact = pair.root / accepted['artifacts'][0]['file']
    original, reads = module._read, []

    def read(path, limit):
        raw = original(path, limit)
        reads.append(path.relative_to(pair.root).as_posix())
        if path == artifact:
            path.write_bytes(b'changed after the pinned read')
        return raw

    monkeypatch.setattr(module, '_read', read)
    catalog = module.load_pinned(pair.root, accepted).catalogs['2026-10-06']
    assert catalog.view('2026-10-06', 'b') == DatedHotL1Catalog.from_bytes(pair.raws[0]).view('2026-10-06', 'b')
    assert reads == [f'generations/{"b" * 32}/manifest.json', accepted['artifacts'][0]['file'], accepted['proofs'][0]['file']]
    with pytest.raises(ValueError) as caught:
        module.load_pinned(pair.root, accepted)
    assert str(caught.value) == 'dated generation file length/SHA256 differs from its pinned manifest'


def test_writer_lock_refuses_parallel_publication_before_creating_generation(pair) -> None:
    pair.root.mkdir()
    with module._writer(pair.root):
        with pytest.raises(ValueError) as caught:
            publish(pair)
    assert str(caught.value) == 'another dated publisher holds the local writer lock'
    assert sorted(path.name for path in pair.root.iterdir()) == ['.publisher.lock']
