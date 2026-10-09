"""Root differences align stable bucket paths, never snapshot-local numbers."""

from copy import deepcopy
from hashlib import sha256
from json import dumps, loads
from pathlib import Path

import pytest

from dt_cloud.chstore import hot_l1_publish as publication
from dt_cloud.chstore.hot_l1_batch_catalog import HotL1BatchCatalog
from dt_cloud.chstore.hot_l1_catalog import CatalogRequest, SCOPE
from dt_cloud.chstore.hot_frequency_registry import FREQUENCY_SEMANTICS, UNION_SCHEMA
from dt_cloud.chstore.path_aligned_l1_catalog import PathAlignedL1Catalog
from test_chhot_l1_batch_catalog import artifact
from test_chhot_l1_publish import prefix_proof

BEFORE, AFTER, QUALIFIED = '2026-10-04', '2026-10-06', '2026-10-01'


def body(day: str, target: str) -> dict:
    value = artifact(day, registry_date=QUALIFIED)
    value['target'] = value['queries']['header']['target'] = target
    value['snapshot_db'] = target + '_snapshot'
    # Different preorder widths AND sibling order. Identity is path spelling.
    rows = ([{'pre': 1, 'post': 4, 'path': 'a', 'b': 8, 'o': 3}, {'pre': 5, 'post': 8, 'path': 'b', 'b': 0, 'o': 2}]
            if day == BEFORE else [{'pre': 1, 'post': 2, 'path': 'b', 'b': 8, 'o': 4}, {'pre': 3, 'post': 11, 'path': 'a', 'b': 0, 'o': 1}])
    value['results'][0]['buckets'] = rows
    value['results'][0]['root'] = {'b': 8, 'o': 5}
    value['results'][1]['buckets'] = [{**row, 'b': 0, 'o': 0} for row in rows]
    return value


def publish(tmp_path: Path, value: dict, *, proofs: bool = True) -> tuple[Path, dict]:
    label = value['date']
    source, proof, root = tmp_path / (label + '.json'), tmp_path / (label + '.proof.json'), tmp_path / (label + '-published')
    source.write_text(dumps(value) + '\n')
    if proofs:
        prefix_proof(proof, source)
    manifest = publication.publish((source,), root, prefix_proofs=(proof,) if proofs else ())
    return root, manifest


@pytest.fixture
def accepted(tmp_path: Path):
    roots, manifests, bodies = [], [], [body(BEFORE, 'physical_old'), body(AFTER, 'physical_new')]
    for value in bodies:
        root, manifest = publish(tmp_path, value)
        roots.append(root)
        manifests.append(manifest)
    reader = PathAlignedL1Catalog.load(tuple(roots), logical_store='gcs_fleet', bucket_paths=('b', 'a'))
    return reader, tuple(roots), manifests, bodies


def expected_view(manifest: dict, day: str) -> dict:
    before = day == BEFORE
    return {'schema': 'path-aligned-l1-v1', 'logical_store': 'gcs_fleet', 'scan_date': day,
            'pattern': '.json', 'path': '', 'exact': True, 'incremental': False, 'levels': 1, 'scope': SCOPE,
            'root': {'b': 8, 'o': 5},
            'buckets': [{'path': 'a', 'b': 8 if before else 0, 'o': 3 if before else 1,
                         'geometry': {'pre': 1, 'post': 4} if before else {'pre': 3, 'post': 11}, 'drill': False},
                        {'path': 'b', 'b': 0 if before else 8, 'o': 2 if before else 4,
                         'geometry': {'pre': 5, 'post': 8} if before else {'pre': 1, 'post': 2}, 'drill': False}],
            'source': {'target': 'physical_old' if before else 'physical_new',
                       'snapshot_db': 'physical_old_snapshot' if before else 'physical_new_snapshot',
                       'generation': manifest['generation'], 'artifacts': manifest['artifacts'], 'source_prefix_proofs_checked': True},
            'registry': {'qualification_dates': [QUALIFIED], 'patterns': 2, 'threshold_paths': 10, 'max_chars': 7,
                         'selection_contract': 'membership on declared qualification dates; no current-scan frequency claim'},
            'validation': {'description': 'complete catalog structure; supplied trusted references checked, not an independent full-catalog scan',
                           'independently_scanned_entire_catalog': False,
                           'references': [{'path': 'json-reference.json', 'pattern': '.json', 'validation': 'complete independent full-path frontier scan'}]},
            'capabilities': {'bucket_drill': False, 'child_drill': False, 'fallback': False}}


def test_exact_views_keep_scan_qualification_store_and_local_geometry_distinct(accepted) -> None:
    reader, _, manifests, _ = accepted
    assert reader.dates == (BEFORE, AFTER)
    assert reader.paths == ('a', 'b')
    assert reader.view(BEFORE, '.JSON') == expected_view(manifests[0], BEFORE)
    assert reader.view(AFTER, '.json') == expected_view(manifests[1], AFTER)
    assert reader.metadata() == {'schema': 'path-aligned-l1-registry-v1', 'logical_store': 'gcs_fleet',
                                 'dates': [{'scan_date': day, 'source': expected_view(manifest, day)['source'],
                                            'registry': expected_view(manifest, day)['registry']} for day, manifest in zip((BEFORE, AFTER), manifests, strict=True)],
                                 'bucket_paths': ['a', 'b'], 'levels': 1, 'scope': SCOPE,
                                 'geometry': 'snapshot-local; stable bucket paths only', 'bucket_drill': False, 'fallback': False}


@pytest.mark.parametrize('reverse', [False, True])
def test_whole_diff_exact_zero_byte_objects_add_delete_and_offsetting(accepted, reverse: bool) -> None:
    reader, _, manifests, _ = accepted
    dates, proofs = ((AFTER, BEFORE), manifests[::-1]) if reverse else ((BEFORE, AFTER), manifests)
    before, after = (expected_view(proof, day) for proof, day in zip(proofs, dates, strict=True))
    sign = -1 if reverse else 1
    assert reader.diff(*dates, '.JSON') == {
        'schema': 'path-aligned-l1-diff-v1', 'logical_store': 'gcs_fleet', 'from_scan_date': dates[0], 'scan_date': dates[1],
        'pattern': '.json', 'path': '', 'exact': True, 'incremental': False, 'levels': 1, 'scope': SCOPE,
        'before': before, 'after': after, 'delta': {'b': 0, 'o': 0},
        'buckets': [{'path': 'a', 'before': {key: before['buckets'][0][key] for key in ('b', 'o', 'geometry')},
                     'after': {key: after['buckets'][0][key] for key in ('b', 'o', 'geometry')}, 'delta': {'b': -8 * sign, 'o': -2 * sign}, 'drill': False},
                    {'path': 'b', 'before': {key: before['buckets'][1][key] for key in ('b', 'o', 'geometry')},
                     'after': {key: after['buckets'][1][key] for key in ('b', 'o', 'geometry')}, 'delta': {'b': 8 * sign, 'o': 2 * sign}, 'drill': False}],
        'capabilities': {'bucket_drill': False, 'child_drill': False, 'fallback': False}}


def test_same_scan_and_registered_zero_are_not_unavailable(accepted) -> None:
    reader, _, _, _ = accepted
    result = reader.diff(AFTER, AFTER, '.npy')
    assert result['delta'] == {'b': 0, 'o': 0}
    assert result['before'] == result['after']
    assert result['before']['root'] == {'b': 0, 'o': 0}
    assert result['buckets'] == [
        {'path': 'a', 'before': {'b': 0, 'o': 0, 'geometry': {'pre': 3, 'post': 11}},
         'after': {'b': 0, 'o': 0, 'geometry': {'pre': 3, 'post': 11}}, 'delta': {'b': 0, 'o': 0}, 'drill': False},
        {'path': 'b', 'before': {'b': 0, 'o': 0, 'geometry': {'pre': 1, 'post': 2}},
         'after': {'b': 0, 'o': 0, 'geometry': {'pre': 1, 'post': 2}}, 'delta': {'b': 0, 'o': 0}, 'drill': False}]


@pytest.mark.parametrize('method,args,kwargs,message', [
    ('view', ('2026-10-07', '.json'), {}, 'path-aligned L1 scan is unavailable; no zero or scan fallback'),
    ('view', (AFTER, 'unknown'), {}, 'path-aligned L1 literal is unavailable on the requested scan; no zero or scan fallback'),
    ('view', (AFTER, '.json'), {'path': 'a'}, 'path-aligned L1 serves the global root only; no bucket or child drill'),
    ('diff', (BEFORE, AFTER, '.json'), {'path': 'a/child'}, 'path-aligned L1 serves the global root only; no bucket or child drill'),
    ('view', (AFTER, 'a/b'), {}, 'path-aligned L1 requires one valid nonempty NUL/slash-free literal of at most 512 characters'),
])
def test_refusal_never_fabricates_zero(accepted, method, args, kwargs, message: str) -> None:
    with pytest.raises(CatalogRequest) as caught:
        getattr(accepted[0], method)(*args, **kwargs)
    assert str(caught.value) == message


def test_missing_dated_predicate_refuses_entire_diff(tmp_path: Path) -> None:
    left, right = body(BEFORE, 'physical_old'), body(AFTER, 'physical_new')
    right['results'].pop()
    right['compiled_patterns'] = right['queries']['patterns'] = 1
    roots = tuple(publish(tmp_path, value)[0] for value in (left, right))
    reader = PathAlignedL1Catalog.load(roots, logical_store='gcs_fleet', bucket_paths=('a', 'b'))
    with pytest.raises(CatalogRequest) as caught:
        reader.diff(BEFORE, AFTER, '.npy')
    assert str(caught.value) == 'path-aligned L1 literal is unavailable on the requested scan; no zero or scan fallback'


def test_missing_bucket_scope_refuses_load_not_zero(tmp_path: Path) -> None:
    value = body(AFTER, 'physical_new')
    for row in value['results']:
        row['buckets'] = [row['buckets'][0]]
        row['root'] = {key: row['buckets'][0][key] for key in ('b', 'o')}
    root, _ = publish(tmp_path, value)
    with pytest.raises(ValueError) as caught:
        PathAlignedL1Catalog.load((root,), logical_store='gcs_fleet', bucket_paths=('a', 'b'))
    assert str(caught.value) == 'path-aligned L1 source bucket paths differ from the complete declared logical scope'


def test_dated_identity_and_all_proofs_remain_pinned_after_pointer_changes(accepted, monkeypatch: pytest.MonkeyPatch) -> None:
    reader, roots, manifests, _ = accepted
    for root in roots:
        (root / 'current.json').write_text('invalid later pointer\n')
    def forbidden(*args, **kwargs):
        raise AssertionError('read path must not access files')
    monkeypatch.setattr(Path, 'read_bytes', forbidden)
    monkeypatch.setattr(Path, 'open', forbidden)
    assert reader.view(AFTER, '.json') == expected_view(manifests[1], AFTER)


def test_no_unproven_source_generation(tmp_path: Path) -> None:
    root, _ = publish(tmp_path, body(AFTER, 'physical_new'), proofs=False)
    with pytest.raises(ValueError) as caught:
        PathAlignedL1Catalog.load((root,), logical_store='gcs_fleet', bucket_paths=('a', 'b'))
    assert str(caught.value) == 'path-aligned L1 requires every source generation to have bound dated prefix proofs'


def test_duplicate_date_generation_refused(accepted) -> None:
    _, roots, _, _ = accepted
    with pytest.raises(ValueError) as caught:
        PathAlignedL1Catalog.load((roots[0], roots[0]), logical_store='gcs_fleet', bucket_paths=('a', 'b'))
    assert str(caught.value) == 'path-aligned L1 source generations contain a duplicate scan date'


def test_artifact_corruption_refused(accepted) -> None:
    _, roots, manifests, _ = accepted
    descriptor = manifests[0]['artifacts'][0]
    (roots[0] / descriptor['file']).write_text('{}\n')
    with pytest.raises(ValueError) as caught:
        PathAlignedL1Catalog.load(roots, logical_store='gcs_fleet', bucket_paths=('a', 'b'))
    assert str(caught.value) == 'published artifact length/SHA256 disagrees with its pinned manifest'


def test_hash_valid_but_wrong_prefix_source_binding_refused(accepted) -> None:
    _, roots, manifests, _ = accepted
    manifest = manifests[0]
    descriptor = manifest['prefix_proofs'][0]
    proof_path = roots[0] / descriptor['file']
    proof = loads(proof_path.read_bytes())
    proof['snapshot_db'] = 'another_snapshot'
    raw = (dumps(proof) + '\n').encode()
    proof_path.write_bytes(raw)
    descriptor.update(sha256=sha256(raw).hexdigest(), bytes=len(raw))
    for path in (roots[0] / 'current.json', roots[0] / 'generations' / manifest['generation'] / 'manifest.json'):
        path.write_text(dumps(manifest) + '\n')
    with pytest.raises(ValueError) as caught:
        PathAlignedL1Catalog.load(roots, logical_store='gcs_fleet', bucket_paths=('a', 'b'))
    assert str(caught.value) == 'prefix proof source/artifact/native bindings disagree with the copied batch'


def test_union_qualification_dates_are_preserved_and_future_scan_not_forged(tmp_path: Path) -> None:
    value = body(AFTER, 'physical_new')
    header = {'schema': UNION_SCHEMA, 'target': 'physical_new', 'dates': [BEFORE, AFTER],
              'threshold_paths': 10, 'max_chars': 7, 'max_patterns': 500000, 'frequency_semantics': FREQUENCY_SEMANTICS,
              'sources': [{'date': day, 'snapshot_db': 'qualified_old_snapshot' if day == BEFORE else 'physical_new_snapshot',
                           'threshold_paths': 10, 'max_chars': 7, 'accepted_hot_pattern_cap': 500000,
                           'census': {'sha256': '1' * 64, 'bytes': 10},
                           'queries': {'sha256': '2' * 64, 'bytes': 20, 'patterns': 2}} for day in (BEFORE, AFTER)]}
    value['queries']['header'] = header
    value['queries']['registry_dates'] = [BEFORE, AFTER]
    root, _ = publish(tmp_path, value)
    reader = PathAlignedL1Catalog.load((root,), logical_store='gcs_fleet', bucket_paths=('a', 'b'))
    assert reader.view(AFTER, '.json')['registry'] == {
        'qualification_dates': [BEFORE, AFTER], 'patterns': 2, 'threshold_paths': 10, 'max_chars': 7,
        'selection_contract': 'membership on declared qualification dates; no current-scan frequency claim'}
    value['date'] = '2026-10-07'
    with pytest.raises(ValueError) as caught:
        HotL1BatchCatalog.from_bytes((dumps(value).encode(),))
    assert str(caught.value) == 'batch catalog union registry dates/snapshot declarations disagree'


@pytest.mark.parametrize('paths', [(), ('a', 'a'), ('a/b',), (None,), ('a', ['b'])])
def test_bad_logical_bucket_declarations_refused_before_loading(accepted, paths) -> None:
    with pytest.raises(ValueError) as caught:
        PathAlignedL1Catalog.load(accepted[1], logical_store='gcs_fleet', bucket_paths=paths)
    assert str(caught.value) == 'path-aligned L1 requires published sources and one to six explicit unique bucket paths'


def test_frozen_loader_is_unchanged_and_cannot_combine_different_numeric_domains(accepted) -> None:
    _, _, _, bodies = accepted
    # One source at a time is accepted; the existing union reader still refuses
    # physical target changes and has not gained a date-local numeric API.
    assert [HotL1BatchCatalog.from_bytes((dumps(value).encode(),)).target for value in bodies] == ['physical_old', 'physical_new']
    same_target = deepcopy(bodies[1])
    same_target['target'] = same_target['queries']['header']['target'] = 'physical_old'
    with pytest.raises(ValueError) as caught:
        HotL1BatchCatalog.from_bytes((dumps(bodies[0]).encode(), dumps(same_target).encode()))
    assert str(caught.value) == 'batch catalog bucket identities/bounds changed across results or dates'
