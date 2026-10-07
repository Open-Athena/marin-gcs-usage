"""Complete original membership, fresh-source binding, no invented frequencies."""

from dataclasses import FrozenInstanceError
from hashlib import sha256
from json import dumps, loads
from pathlib import Path

import pytest

from dt_cloud.chstore import hot_registry_selection as module
from dt_cloud.chstore.hot_frequency_registry import PinnedExport, load_queries
from test_chhot_frequency_union import DATES, expected_header, ROWS, source, FREQUENCIES


def encoded(value: dict) -> bytes:
    return (dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


@pytest.fixture
def fixture(tmp_path: Path):
    sources = tuple(source(tmp_path / str(index), date, frequencies) for index, (date, frequencies)
                    in enumerate(zip(DATES, FREQUENCIES, strict=True)))
    header = expected_header(sources)
    registry = encoded(header) + b''.join(encoded(row) for row in ROWS) + encoded({'complete': True, 'patterns': len(ROWS)})
    source_body = {'schema': 'daily-scalar-source-v1', 'complete': True, 'logical_store': 'gcs_fleet', 'date': '2026-10-06',
                   'target': 'day_20261006', 'snapshot_db': 'day_20261006', 'prefix': '',
                   'source': {'schema': 'daily-scalar-input-v1', 'logical_store': 'gcs_fleet', 'date': '2026-10-06',
                              'identity': {'uri': 'gs://fixture/path-index.parquet', 'generation': 'immutable-fixture-generation'},
                              'bytes': 1234, 'sha256': 'f' * 64},
                   'source_rows': 15, 'selected_source_rows': 15, 'nodes': 9,
                   'root': {'path': '', 'pre': 0, 'post': 8, 'b': 12, 'o': 7},
                   'buckets': [{'path': 'a', 'pre': 1, 'post': 3}, {'path': 'b', 'pre': 4, 'post': 8}],
                   'validation': {'source_hash_checked': True, 'prefix_closed': True, 'interval_endpoints_checked': True, 'scalar_rollups_checked': True},
                   'limits': {'max_rows': 100}, 'stages': {'build_s': .1}}
    source_raw = encoded(source_body)
    document = module.envelope(registry, source_raw, logical_store='gcs_fleet')
    return registry, source_raw, document, header


def test_exact_envelope_preserves_original_target_qualification_and_new_source(fixture) -> None:
    registry, source_raw, document, header = fixture
    expected = {'schema': 'hot-registry-selection-v1', 'complete': True, 'logical_store': 'gcs_fleet',
                'registry': {'sha256': sha256(registry).hexdigest(), 'bytes': len(registry), 'patterns': 4,
                             'target': 'fixture', 'qualification_dates': ['2026-10-04', '2026-10-05']},
                'build_source': {'date': '2026-10-06', 'target': 'day_20261006', 'snapshot_db': 'day_20261006',
                                 'manifest_sha256': sha256(source_raw).hexdigest(), 'manifest_bytes': len(source_raw)}}
    assert document == expected
    result = module.validate(encoded(document), registry, source_raw)
    assert (result.logical_store, result.registry_target, result.qualification_dates, result.patterns,
            result.date, result.target, result.snapshot_db, result.nodes, result.buckets) == (
        'gcs_fleet', 'fixture', ('2026-10-04', '2026-10-05'), ('a', 'b', 'c', 'd'),
        '2026-10-06', 'day_20261006', 'day_20261006', 9, ((1, 3, 'a'), (4, 8, 'b')))
    assert result.metadata() == expected
    assert result.registry_header() == header
    assert result.registry_raw == registry
    assert result.source_manifest_raw == source_raw
    assert [loads(line)['direct_matching_paths'] for line in result.registry_raw.splitlines()[1:-1]] == [
        {'2026-10-04': 20, '2026-10-05': None}, {'2026-10-04': None, '2026-10-05': 30},
        {'2026-10-04': 12, '2026-10-05': 8}, {'2026-10-04': 50, '2026-10-05': 60}]
    with pytest.raises(FrozenInstanceError):
        result.date = '2026-10-07'


def test_explicit_files_are_pinned_once_and_returned_metadata_cannot_mutate_binding(fixture, tmp_path: Path) -> None:
    registry, source_raw, document, _ = fixture
    paths = tuple(tmp_path / name for name in ('selection.json', 'registry.jsonl', 'source.json'))
    for path, raw in zip(paths, (encoded(document), registry, source_raw), strict=True):
        path.write_bytes(raw)
    result = module.load(*paths)
    for path in paths:
        path.write_text('changed later\n')
    metadata = result.metadata()
    metadata['registry']['qualification_dates'].append('2026-10-06')
    result.source_manifest()['root']['b'] = 999
    assert result.metadata() == document
    assert result.source_manifest()['root'] == {'path': '', 'pre': 0, 'post': 8, 'b': 12, 'o': 7}


@pytest.mark.parametrize('side,message', [
    ('registry', 'registry selection original registry bytes/SHA256 differ from the pinned descriptor'),
    ('source', 'registry selection source manifest bytes/SHA256 differ from the pinned descriptor'),
])
def test_exact_bytes_not_only_semantically_equal_metadata_are_pinned(fixture, side, message: str) -> None:
    registry, source_raw, document, _ = fixture
    with pytest.raises(ValueError) as caught:
        module.validate(encoded(document), registry + (b' ' if side == 'registry' else b''), source_raw + (b' ' if side == 'source' else b''))
    assert str(caught.value) == message


@pytest.mark.parametrize('change', [
    lambda d: d['registry'].update(target='day_20261006'),
    lambda d: d['registry'].update(qualification_dates=['2026-10-06']),
    lambda d: d['registry'].update(patterns=3),
    lambda d: d['build_source'].update(date='2026-10-07'),
    lambda d: d['build_source'].update(snapshot_db='old_snapshot'),
])
def test_no_forged_selection_identity_dates_counts_or_source(fixture, change) -> None:
    registry, source_raw, document, _ = fixture
    change(document)
    with pytest.raises(ValueError) as caught:
        module.validate(encoded(document), registry, source_raw)
    assert str(caught.value) == 'registry selection qualification/count/source bindings differ from the original accepted bytes'


@pytest.mark.parametrize('change,message', [
    (lambda b: b.update(prefix='a/subtree'), 'registry selection requires a completed global daily-scalar-source-v1 manifest'),
    (lambda b: b.update(complete=False), 'registry selection requires a completed global daily-scalar-source-v1 manifest'),
    (lambda b: b['validation'].update(scalar_rollups_checked=1), 'registry selection requires all completed daily scalar validation declarations'),
    (lambda b: b['source'].update(date='2026-10-05'), 'registry selection daily input identity differs from its accepted source'),
    (lambda b: b.update(snapshot_db='old_snapshot'), 'registry selection daily source must use its own target as snapshot database'),
    (lambda b: b.update(nodes=True), 'registry selection daily source has invalid complete source/node counts'),
    (lambda b: b['buckets'][1].update(pre=5), 'registry selection daily buckets must completely partition their own global geometry'),
])
def test_incomplete_or_misbound_scalar_source_refused(fixture, change, message: str) -> None:
    registry, source_raw, _, _ = fixture
    body = loads(source_raw)
    change(body)
    with pytest.raises(ValueError) as caught:
        module.envelope(registry, encoded(body), logical_store='gcs_fleet')
    assert str(caught.value) == message


def test_truncated_registry_remains_refused_even_with_updated_hash_descriptor(fixture) -> None:
    registry, source_raw, document, _ = fixture
    truncated = b'\n'.join(registry.splitlines()[:-1]) + b'\n'
    document['registry'].update(sha256=sha256(truncated).hexdigest(), bytes=len(truncated))
    with pytest.raises(ValueError) as caught:
        module.validate(encoded(document), truncated, source_raw)
    assert str(caught.value) == 'hot query export lacks a valid exact-count completion footer'


def test_old_registry_api_does_not_silently_accept_fresh_date_or_target(fixture) -> None:
    registry = fixture[0]
    for target, date, message in [
        ('fixture', '2026-10-06', 'union batch date must be a source date and cannot use a single registry_date override'),
        ('day_20261006', '2026-10-04', 'union registry requires matching sorted dates and complete valid source provenance'),
    ]:
        with pytest.raises(ValueError) as caught:
            load_queries(PinnedExport(registry), target, date)
        assert str(caught.value) == message
