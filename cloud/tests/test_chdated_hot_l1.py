"""New dated scans retain old registry qualification and exact source bytes."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from os import environ
from struct import pack
from types import SimpleNamespace

import pytest

from dt_cloud.chstore import dated_hot_l1 as module
from dt_cloud.chstore.daily_scalar import manifest_bytes
from dt_cloud.chstore.hot_l1_catalog import CatalogRequest, SCOPE
from dt_cloud.chstore.hot_registry_selection import validate
from test_chhot_registry_selection import fixture  # noqa: F401


@pytest.fixture
def prepared(fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    registry, source, selection_body, _ = fixture
    selection = validate(manifest_bytes(selection_body), registry, source)
    binary = tmp_path / 'native'
    binary.write_bytes(b'pinned binary')
    binary.chmod(0o700)
    results = []
    for q, (pattern, weights) in enumerate(zip(selection.patterns, [((7, 3), (3, 1)), ((0, 0), (0, 2)), ((0, 0), (0, 0)), ((7, 3), (5, 4))], strict=True), 1):
        buckets = [{'pre': pre, 'post': post, 'path': path, 'b': b, 'o': o}
                   for (pre, post, path), (b, o) in zip(selection.buckets, weights, strict=True)]
        results.append({'predicate_id': q, 'pattern': pattern, 'root': {'b': sum(row['b'] for row in buckets), 'o': sum(row['o'] for row in buckets)}, 'buckets': buckets})
    native = {'schema': 'hot-l1-native-stream-v1', 'exact': True, 'incremental': False, 'levels': 1,
              'nodes_read': 9, 'registered_predicates': 4, 'peak_stack': 3, 'peak_active': 2, 'native_peak_rss_bytes': 12345}
    state = SimpleNamespace(selection=selection, binary=binary, out=tmp_path / 'dated.json', calls=[], docs=[source.decode(), source.decode()], results=results, native=native)

    def scalar(sql):
        state.calls.append(('scalar', sql))
        return state.docs.pop(0)

    state.ch = SimpleNamespace(scalar=scalar)

    def aggregate(ch, db, patterns, buckets, rows, *, binary):
        assert ch is state.ch
        state.calls.append(('aggregate', db, patterns, buckets, rows, binary))
        return {'results': deepcopy(state.results), 'native': dict(state.native), 'source_query_id': 'hot_l1_stream_' + 'a' * 32, 'aggregate_s': 0.}

    monkeypatch.setattr(module, 'aggregate_source', aggregate)
    monkeypatch.setattr(module, 'monotonic', lambda: 1.)
    return state


def produce(state) -> tuple[dict, bytes]:
    body = module.build(state.ch, state.selection, binary=state.binary, out=state.out)
    return body, state.out.read_bytes()


def expected_source(state, raw: bytes) -> dict:
    return {'artifact_sha256': sha256(raw).hexdigest(), 'artifact_bytes': len(raw), 'target': 'day_20261006', 'snapshot_db': 'day_20261006',
            'source_manifest_sha256': sha256(state.selection.source_manifest_raw).hexdigest(), 'source_prefix_proofs_checked': True}


REGISTRY = {'qualification_dates': ['2026-10-04', '2026-10-05'], 'target': 'fixture', 'patterns': 4, 'threshold_paths': 10, 'max_chars': 2,
            'selection_contract': 'membership on declared qualification dates; no current-scan frequency claim'}
CAPABILITIES = {'bucket_drill': False, 'child_drill': False, 'fallback': False}


def test_producer_checks_source_twice_and_preserves_complete_original_bytes(prepared) -> None:
    body, raw = produce(prepared)
    assert prepared.calls == [
        ('scalar', 'SELECT doc FROM day_20261006.source_manifest'),
        ('aggregate', 'day_20261006', ('a', 'b', 'c', 'd'), [[1, 3, 'a'], [4, 8, 'b']], 9, prepared.binary),
        ('scalar', 'SELECT doc FROM day_20261006.source_manifest'),
    ]
    assert body == {'schema': 'dated-hot-l1-native-v1', 'complete': True, 'exact': True, 'incremental': False, 'levels': 1, 'scope': SCOPE,
                    'logical_store': 'gcs_fleet', 'date': '2026-10-06', 'target': 'day_20261006', 'snapshot_db': 'day_20261006',
                    'selection': prepared.selection.metadata(), 'registry_utf8': prepared.selection.registry_raw.decode(),
                    'source_manifest_utf8': prepared.selection.source_manifest_raw.decode(), 'results': prepared.results, 'native': prepared.native,
                    'binary': {'bytes': 13, 'sha256': sha256(b'pinned binary').hexdigest()}, 'source_query_id': 'hot_l1_stream_' + 'a' * 32,
                    'stages': {'before_source_binding_s': 0., 'aggregate_s': 0., 'after_source_binding_s': 0., 'build_s': 0.},
                    'validation': module.VALIDATION}
    assert raw == manifest_bytes(body)
    assert prepared.out.stat().st_mode & 0o777 == 0o600


def test_exact_view_metadata_true_zero_counts_and_mutation_isolation(prepared) -> None:
    body, raw = produce(prepared)
    reader = module.DatedHotL1Catalog.load(prepared.out)
    assert reader.metadata() == {'schema': 'dated-hot-l1-registry-v1', 'logical_store': 'gcs_fleet', 'scan_date': '2026-10-06',
                                 'source': expected_source(prepared, raw), 'registry': REGISTRY, 'bucket_paths': ['a', 'b'], 'levels': 1,
                                 'scope': SCOPE, 'validation': module.VALIDATION, 'capabilities': CAPABILITIES}
    view = reader.view('2026-10-06', 'B')
    assert view == {'schema': 'dated-hot-l1-v1', 'logical_store': 'gcs_fleet', 'date': '2026-10-06', 'pattern': 'b', 'path': '',
                    'exact': True, 'incremental': False, 'levels': 1, 'scope': SCOPE, 'root': {'b': 0, 'o': 2},
                    'buckets': body['results'][1]['buckets'], 'source': expected_source(prepared, raw), 'registry': REGISTRY,
                    'validation': module.VALIDATION, 'capabilities': CAPABILITIES}
    view['buckets'][0]['b'] = 999
    view['registry']['qualification_dates'].append('2026-10-06')
    assert reader.view('2026-10-06', 'c')['root'] == {'b': 0, 'o': 0}
    assert reader.view('2026-10-06', 'b')['buckets'] == body['results'][1]['buckets']
    assert reader.metadata()['registry'] == REGISTRY


@pytest.mark.parametrize('change,error', [
    (lambda b: b.update(date='2026-10-05'), 'dated L1 logical store/date/source identity differs from the pinned selection'),
    (lambda b: b.update(registry_utf8=b['registry_utf8'] + ' '), 'registry selection original registry bytes/SHA256 differ from the pinned descriptor'),
    (lambda b: b['results'][0].update(predicate_id=True), 'dated L1 result IDs/literals differ from the original ordered registry'),
    (lambda b: b['results'].reverse(), 'dated L1 result IDs/literals differ from the original ordered registry'),
    (lambda b: b['results'].pop(), 'dated L1 results must contain complete ordered registry membership'),
    (lambda b: b['results'][0]['buckets'][0].update(pre=2), 'dated L1 bucket bounds/paths differ from the accepted daily source'),
    (lambda b: b['results'][0]['root'].update(b=11), 'dated L1 root/bucket conservation exceeds the accepted complete source'),
    (lambda b: b['results'][0]['buckets'][0].update(o=True), 'dated L1 bucket.o must be a nonnegative integer'),
    (lambda b: b['validation'].update(independent_full_catalog_source_oracle=True), 'dated L1 must retain its truthful source-validation limitations'),
    (lambda b: b['native'].update(nodes_read=8), 'dated L1 native counts differ from the complete source/registry'),
])
def test_artifact_refuses_malformed_complete_membership_and_forged_provenance(prepared, change, error: str) -> None:
    body, _ = produce(prepared)
    change(body)
    with pytest.raises(ValueError) as caught:
        module.DatedHotL1Catalog.from_bytes(manifest_bytes(body))
    assert str(caught.value) == error


def test_source_upper_bound_refuses_even_when_bucket_sum_is_consistent(prepared) -> None:
    body, _ = produce(prepared)
    body['results'][0]['buckets'][0]['b'] = 10
    body['results'][0]['root']['b'] = 13
    with pytest.raises(ValueError) as caught:
        module.DatedHotL1Catalog.from_bytes(manifest_bytes(body))
    assert str(caught.value) == 'dated L1 root/bucket conservation exceeds the accepted complete source'


@pytest.mark.parametrize('side', [0, 1])
def test_source_marker_change_before_or_after_native_leaves_no_artifact(prepared, side: int) -> None:
    source = prepared.selection.source_manifest()
    source['root']['b'] += 1
    prepared.docs[side] = manifest_bytes(source).decode()
    with pytest.raises(ValueError) as caught:
        produce(prepared)
    assert str(caught.value) == 'dated L1 actual CH source manifest differs from the pinned selection'
    assert prepared.out.exists() is False
    assert [call[0] for call in prepared.calls] == (['scalar'] if side == 0 else ['scalar', 'aggregate', 'scalar'])


def test_existing_output_refused_before_any_native_or_source_call(prepared) -> None:
    prepared.out.write_bytes(b'preserve')
    with pytest.raises(ValueError) as caught:
        produce(prepared)
    assert (str(caught.value), prepared.out.read_bytes(), prepared.calls) == ('dated L1 refuses an existing output path', b'preserve', [])


def test_artifact_cap_before_write_and_bounded_reader(prepared, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, 'LIMIT', 10)
    with pytest.raises(ValueError) as caught:
        produce(prepared)
    assert (str(caught.value), prepared.out.exists()) == ('dated L1 private artifact exceeds its 64MiB cap', False)
    with pytest.raises(ValueError) as caught:
        module.DatedHotL1Catalog.from_bytes(b'0' * 11)
    assert str(caught.value) == 'dated L1 private artifact must be nonempty bytes at most 64MiB'


@pytest.mark.parametrize('date,pattern,path,error', [
    ('2026-10-05', 'a', '', 'dated L1 scan/literal is unavailable; no zero or scan fallback'),
    ('2026-10-06', 'unknown', '', 'dated L1 scan/literal is unavailable; no zero or scan fallback'),
    ('2026-10-06', 'a', 'a', 'dated L1 serves the global root only; no bucket or child drill'),
    ('bad', 'a', '', 'dated L1 requires a valid ISO date and one nonempty NUL/slash-free literal of at most 512 characters'),
])
def test_unknown_queries_dates_and_drills_never_fall_back(prepared, date: str, pattern: str, path: str, error: str) -> None:
    _, raw = produce(prepared)
    reader = module.DatedHotL1Catalog.from_bytes(raw)
    with pytest.raises(CatalogRequest) as caught:
        reader.view(date, pattern, path=path)
    assert str(caught.value) == error


def test_real_native_protocol_results_parse_into_new_dated_reader_without_ch(prepared, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = environ.get('HL1_NATIVE_BINARY')
    if not binary:
        pytest.skip('HL1_NATIVE_BINARY is required for the real native dated payload fixture')
    from dt_cloud.chstore.hot_l1_batch_stream import _string, aggregate_source

    rows = [(0, 8, 12, 7, ''), (1, 3, 7, 3, 'a'), (2, 2, 2, 1, 'c'), (3, 3, 5, 1, 'd'),
            (4, 8, 5, 4, 'b'), (5, 5, 1, 1, 'e'), (6, 8, 4, 3, 'z'), (7, 7, 4, 1, 'c'), (8, 8, 0, 1, 'd')]
    wire = b''.join(pack('<QQQQ', pre, post, b, o) + _string(name) for pre, post, b, o, name in rows)
    calls = []

    class Reader:
        def stream(self, sql, *, fmt):
            calls.append(('stream', ' '.join(sql.split()), fmt))
            for start in range(0, len(wire), 7):
                yield wire[start:start + 7]

        def close(self):
            calls.append(('close',))

    prepared.ch.fork = lambda **kwargs: Reader()
    prepared.ch.timeout = 5
    monkeypatch.setattr(module, 'aggregate_source', aggregate_source)
    prepared.binary = Path(binary)
    body, raw = produce(prepared)
    reader = module.DatedHotL1Catalog.from_bytes(raw)
    assert [(row['predicate_id'], row['pattern'], row['root'], [(b['path'], b['b'], b['o']) for b in row['buckets']]) for row in body['results']] == [
        (1, 'a', {'b': 7, 'o': 3}, [('a', 7, 3), ('b', 0, 0)]),
        (2, 'b', {'b': 5, 'o': 4}, [('a', 0, 0), ('b', 5, 4)]),
        (3, 'c', {'b': 6, 'o': 2}, [('a', 2, 1), ('b', 4, 1)]),
        (4, 'd', {'b': 5, 'o': 2}, [('a', 5, 1), ('b', 0, 1)]),
    ]
    assert reader.view('2026-10-06', 'd')['root'] == {'b': 5, 'o': 2}
    from dt_cloud.chstore.hot_l1_batch_sql import NAME
    assert calls == [('stream', 'SELECT assumeNotNull(toUInt64(pre)),assumeNotNull(toUInt64(post)), '
                      'assumeNotNull(toUInt64(b)),assumeNotNull(toUInt64(o)),assumeNotNull(lowerUTF8(' + NAME + ')) '
                      'FROM day_20261006.nodes ORDER BY pre', 'RowBinary'), ('close',)]
