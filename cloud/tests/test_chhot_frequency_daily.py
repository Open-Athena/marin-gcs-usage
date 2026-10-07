from collections import Counter
from hashlib import sha256
from os import environ
from json import dumps, loads
from pathlib import Path
from re import MULTILINE, findall
from types import SimpleNamespace

from click.testing import CliRunner
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud.chstore import client, hot_frequency_bench, hot_frequency_daily as module, hot_frequency_report
from dt_cloud.chstore.daily_scalar import build, manifest_bytes
from dt_cloud.chstore.hot_frequency_registry import load_queries
from dt_cloud.cli import main
from test_chdaily_scalar import descriptor, fresh  # noqa: F401
from chserver import ch_url  # noqa: F401


def accepted(target: str = 'fresh', date: str = '2026-10-06') -> dict:
    return {'schema': 'daily-scalar-source-v1', 'complete': True, 'logical_store': 'gcs', 'date': date,
            'target': target, 'snapshot_db': target, 'prefix': '', 'source_rows': 2, 'selected_source_rows': 2,
            'nodes': 3, 'root': {'path': '', 'pre': 0, 'post': 2, 'b': 0, 'o': 1},
            'buckets': [{'path': 'a', 'pre': 1, 'post': 2}], 'limits': {}, 'stages': {},
            'validation': dict.fromkeys(('source_hash_checked', 'prefix_closed', 'interval_endpoints_checked', 'scalar_rollups_checked'), True),
            'source': {'schema': 'daily-scalar-input-v1', 'logical_store': 'gcs', 'date': date,
                       'identity': {'uri': 'gs://fixture/input.parquet', 'generation': 'g'}, 'bytes': 1, 'sha256': 'a' * 64}}


def test_weighted_source_sql_has_in_order_group_without_vocabulary_sort_or_name_ids() -> None:
    assert module.weighted_select('ordered_names') == 'SELECT l,count() AS c FROM ordered_names GROUP BY l SETTINGS optimize_aggregation_in_order=1'
    assert module.weighted_select('by_basename', database='dated_source') == 'SELECT l,count() AS c FROM dated_source.by_basename GROUP BY l SETTINGS optimize_aggregation_in_order=1'


@pytest.mark.parametrize('case,error', [
    ('date', 'daily hot-frequency requires matching canonical accepted source target/date'),
    ('target', 'daily hot-frequency requires matching canonical accepted source target/date'),
    ('format', 'daily hot-frequency requires matching canonical accepted source target/date'),
    ('prefix', 'registry selection requires a completed global daily-scalar-source-v1 manifest'),
    ('incomplete', 'registry selection requires a completed global daily-scalar-source-v1 manifest'),
    ('proof', 'registry selection requires all completed daily scalar validation declarations'),
])
def test_source_binding_refuses_before_any_staging(case: str, error: str) -> None:
    body = accepted()
    if case == 'date':
        body['date'] = body['source']['date'] = '2026-10-05'
    elif case == 'target':
        body['target'] = body['snapshot_db'] = 'other'
    elif case == 'prefix':
        body['prefix'] = 'a'
    elif case == 'incomplete':
        body['complete'] = False
    elif case == 'proof':
        body['validation']['prefix_closed'] = False
    raw = dumps(body).encode() if case == 'format' else manifest_bytes(body)
    with pytest.raises(ValueError) as caught:
        module.census(None, 'fresh', '2026-10-06', raw, 2)
    assert str(caught.value) == error


def test_adapter_exact_staging_provenance_and_marker_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    body, calls, progress = accepted(), [], []
    raw = manifest_bytes(body)
    monkeypatch.setattr(module, 'uuid4', lambda: SimpleNamespace(hex='fixture'))
    monkeypatch.setattr(module, 'monotonic', lambda: 0)

    class Fake:
        def json(self, sql):
            calls.append(('marker', sql))
            return [[len(raw) - 1, raw.decode()[:-1]]]

        def tmp(self, name, sql, **kwargs):
            calls.append(('tmp', name, ' '.join(sql.split()), kwargs))

        def checkpoint(self):
            calls.append(('checkpoint',))

        def scalar(self, sql):
            calls.append(('scalar', sql))
            return '0'

    def enumerate(ch, target, database, date, threshold, maximum, patterns, **kwargs):
        calls.append(('enumerate', target, database, date, threshold, maximum, patterns,
                      {key: value for key, value in kwargs.items() if key not in ('progress', 'on_hot_table')}))
        kwargs['progress']({'stage': 'weighted-names'})
        kwargs['on_hot_table'](1, 'hot_layer')
        return {'schema': 'hot-frequency-v1'}

    monkeypatch.setattr(module, 'census_weighted', enumerate)
    output = []
    result = module.census(Fake(), 'fresh', '2026-10-06', raw, 2, 16, ('AA',), progress=progress.append,
                           on_hot_table=lambda *args: output.append(args))
    assert result == {'schema': 'hot-frequency-v1', 'source_provenance': {
        'kind': 'accepted global daily scalar nodes; no historical/name-ID ingestion', 'logical_store': 'gcs',
        'qualification_date': '2026-10-06', 'source_manifest_sha256': sha256(raw).hexdigest(), 'source_manifest_bytes': len(raw),
        'snapshot_db': 'fresh', 'nodes': 3, 'source': body['source'], 'validation': body['validation'],
        'independent_frequency_source_oracle': False,
    }}
    marker = ('marker', 'SELECT length(doc),if(length(doc)<=65536,doc,\'\') FROM fresh.source_manifest LIMIT 2')
    assert calls == [marker, ('checkpoint',),
        ('tmp', 'daily_frequency_names_fixture', "SELECT lowerUTF8(arrayElement(splitByChar('/',assumeNotNull(path)),-1)) AS l FROM fresh.nodes",
         {'disk': True, 'order_by': 'l', 'settings': {'max_insert_threads': 1, 'min_insert_block_size_rows': 1 << 20, 'min_insert_block_size_bytes': 64 << 20}}),
        ('checkpoint',), ('scalar', "SELECT countIf(NOT isValidUTF8(l) OR position(l,'\\0')>0) FROM daily_frequency_names_fixture"),
        ('tmp', 'daily_frequency_weighted_fixture', module.weighted_select('daily_frequency_names_fixture'),
         {'disk': True, 'order_by': 'l', 'settings': {'optimize_aggregation_in_order': 1}}),
        ('enumerate', 'fresh', 'fresh', '2026-10-06', 2, 16, ('aa',),
         {'weighted': 'daily_frequency_weighted_fixture', 'tag': 'fixture', 'started': 0, 'thresholds': (), 'max_patterns': 500000, 'expected_paths': 3}),
        ('checkpoint',), ('checkpoint',), marker,
    ]
    assert progress == [{'stage': 'daily-basename-staging', 'status': 'started'},
                        {'stage': 'daily-basename-staging', 'status': 'complete', 'elapsed_s': 0},
                        {'stage': 'daily-weighted-names', 'status': 'started'}, {'stage': 'weighted-names'}]
    assert output == [(1, 'hot_layer')]


def test_nonrenewable_deadline_clamps_every_owned_statement(monkeypatch: pytest.MonkeyPatch) -> None:
    now, calls = [0], []
    monkeypatch.setattr(module, 'monotonic', lambda: now[0])
    monkeypatch.setattr(module, 'uuid4', lambda: SimpleNamespace(hex='owned'))
    monkeypatch.setattr(client.Ch, '_open', lambda self, sql, data, settings: calls.append((sql, settings, self.timeout)))
    ch = module.DailyCensusCh('http://fixture', wall_seconds=10, staging_bytes=100, max_execution_time=600)
    now[0] = 3
    ch._open('SELECT 1')
    assert calls == [('SELECT 1', {'query_id': 'daily_hot_frequency_owned', 'max_execution_time': 7}, 7)]
    assert ch.ids == ['daily_hot_frequency_owned']
    now[0] = 11
    with pytest.raises(TimeoutError) as caught:
        ch._open('SELECT 2')
    assert str(caught.value) == 'daily hot-frequency total wall budget exhausted; no complete census'
    assert len(calls) == 1


@pytest.mark.parametrize('tables,error', [
    ([['owned', 101]], 'daily hot-frequency temporary staging cap exceeded; no complete census'),
    ([['owned', None]], 'daily hot-frequency temporary table byte accounting is incomplete'),
    ([], 'daily hot-frequency temporary table byte accounting is incomplete'),
])
def test_staging_accounting_refuses_missing_or_overbudget_bytes(monkeypatch: pytest.MonkeyPatch, tables: list, error: str) -> None:
    monkeypatch.setattr(module, 'disk_reserve', lambda *args: None)
    ch = module.DailyCensusCh('http://fixture', wall_seconds=10, staging_bytes=100, max_execution_time=1)
    ch._tmp = ['owned']
    monkeypatch.setattr(ch, 'json', lambda sql: tables)
    with pytest.raises(RuntimeError) as caught:
        ch.checkpoint()
    assert str(caught.value) == error


def test_cleanup_cancels_exact_ids_attempts_all_owned_drops_and_refuses_unverified_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = module.DailyCensusCh('http://fixture', db='fresh', wall_seconds=10, staging_bytes=100, max_execution_time=1)
    ch.ids, ch._tmp, calls = ['daily_hot_frequency_owned'], ['older', 'newer'], []

    class Control:
        def __init__(self, url, **kwargs):
            calls.append(('control', url, kwargs))

        def exec(self, sql, *, fmt):
            calls.append(('cancel', sql, fmt))

        def scalar(self, sql):
            calls.append(('quiescence', sql))
            return '0'

        def close(self):
            calls.append(('control-close',))

    def drop(sql, *, fmt):
        calls.append(('drop', sql, fmt))
        if sql == 'DROP TEMPORARY TABLE IF EXISTS newer':
            raise RuntimeError('failed drop')

    monkeypatch.setattr(module, 'Ch', Control)
    monkeypatch.setattr(ch, 'exec', drop)
    with pytest.raises(RuntimeError) as caught:
        ch.close()
    assert str(caught.value) == 'daily hot-frequency owned cleanup could not be verified'
    assert calls == [
        ('control', 'http://fixture', {'db': 'fresh', 'session': False, 'timeout': 10, 'max_execution_time': 10}),
        ('cancel', "KILL QUERY WHERE query_id IN ('daily_hot_frequency_owned') SYNC", None),
        ('quiescence', "SELECT count() FROM system.processes WHERE query_id IN ('daily_hot_frequency_owned')"),
        ('drop', 'DROP TEMPORARY TABLE IF EXISTS newer', None), ('drop', 'DROP TEMPORARY TABLE IF EXISTS older', None),
        ('control-close',),
    ]
    assert ch._tmp == ['older', 'newer']


@pytest.mark.parametrize('change_after', [False, True])
def test_marker_failure_before_or_after_enumeration_never_returns_census(monkeypatch: pytest.MonkeyPatch, change_after: bool) -> None:
    raw, calls = manifest_bytes(accepted()), []

    class Fake:
        def json(self, sql):
            calls.append('marker')
            return [[len(raw) - 1, raw.decode()[:-1]]] if change_after and calls == ['marker'] else []

        def checkpoint(self):
            calls.append('checkpoint')

        def tmp(self, *args, **kwargs):
            calls.append('tmp')

        def scalar(self, *args):
            calls.append('name-check')
            return '0'

    def enumerate(*args, **kwargs):
        calls.append('enumerate')
        return {}

    monkeypatch.setattr(module, 'census_weighted', enumerate)
    with pytest.raises(ValueError) as caught:
        module.census(Fake(), 'fresh', '2026-10-06', raw, 2)
    assert str(caught.value) == 'daily hot-frequency source marker changed or differs from the pinned source'
    assert calls == (['marker', 'checkpoint', 'tmp', 'checkpoint', 'name-check', 'tmp', 'enumerate', 'marker'] if change_after else ['marker'])


PATHS = {
    '2026-10-05': ['a', 'a/one', 'a/one/AAAA', 'a/two', 'a/two/aaaa', 'a/zarr.json', 'a/zarr.json/aaaa', 'a/Å😀',
                  'a/datakit', 'a/datakit/x', 'a/datakit/y'],
    '2026-10-06': ['a', 'a/one', 'a/one/AAAA', 'a/new', 'a/new/zarr.json', 'a/zarr.json', 'a/Å😀', 'a/Å😀/Å😀',
                  'a/datakit', 'a/datakit/x', 'a/datakit/y'],
}


def write_tree(path: Path, day: str) -> None:
    paths = PATHS[day]
    # Independent own-object fixture: every path has an object, directories
    # can have zero-byte own objects, and recursive totals include descendants.
    own = {name: (0 if name.endswith(('json', 'kit')) else 1, 1) for name in paths}
    rows = []
    for name in paths:
        covered = [value for child, value in own.items() if child == name or child.startswith(name + '/')]
        rows.append((name, 'u', 'dir' if any(child.startswith(name + '/') for child in paths) else 'file',
                     name.count('/') + 1, sum(value[0] for value in covered), sum(value[1] for value in covered)))
    columns = dict(zip(('path', 'usr', 'kind', 'depth', 'size', 'n_files'), zip(*reversed(rows)), strict=True))
    pq.write_table(pa.table(columns), path, row_group_size=2)


@pytest.mark.parametrize('engine', ['ch', 'native'])
@pytest.mark.parametrize('day', list(PATHS))
def test_native_actual_date_hot_qualification_full_export_and_in_order_pipeline(fresh: tuple, tmp_path: Path, day: str, engine: str) -> None:
    # `native`: the per-length passes in `native/hot_frequency.cpp` (`HF_NATIVE_BINARY`, built
    # like `HL1_NATIVE_BINARY`) must export exactly what ClickHouse's own passes do.
    native = environ.get('HF_NATIVE_BINARY') if engine == 'native' else None
    if engine == 'native' and not native:
        pytest.skip('HF_NATIVE_BINARY not set')
    ch, target = fresh
    parquet, source, census_out, export = [tmp_path / name for name in ('input.parquet', 'source.json', 'census.json', 'queries.jsonl')]
    write_tree(parquet, day)
    pinned = descriptor(parquet)
    pinned['date'] = day
    body = build(ch, target, parquet, pinned, min_free_bytes=1)
    source.write_bytes(manifest_bytes(body))
    result = hot_frequency_bench.bench(ch.url, target, day, 2, 16, census_out, daily_source=source,
                                     queries_out=export, patterns=('aa', 'zarr', 'Å😀', 'datakit'), seconds=30, wall_seconds=120,
                                     **({'native': Path(native)} if native else {}))
    weighted = Counter(name.rsplit('/', 1)[-1].lower() for name in ['', *PATHS[day]])
    expected = {}
    for chars in range(1, 17):
        counts = Counter()
        for name, weight in weighted.items():
            counts.update({name[pos:pos + chars]: weight for pos in range(max(0, len(name) - chars + 1))})
        expected.update({(chars, gram): count for gram, count in counts.items() if count >= 2})
    rows = [loads(line) for line in export.read_text().splitlines()]
    assert rows == [{'schema': 'hot-frequency-queries-v1', 'target': target, 'date': day, 'threshold_paths': 2, 'max_chars': 16},
                    *[{'chars': chars, 'pattern': gram, 'direct_matching_paths': value} for (chars, gram), value in sorted(expected.items())],
                    {'complete': True, 'patterns': len(expected)}]
    header, literals = load_queries(export, target, day, allow_union=False)
    assert (header['date'], len(literals), result['paths'], result['source_provenance']['qualification_date'],
            result['source_provenance']['owned_cleanup_verified']) == (day, len(expected), len(PATHS[day]) + 1, day, True)
    assert result['selected_patterns'] == [
        {'pattern': pattern, 'hot': (len(pattern), pattern) in expected, 'direct_matching_paths': expected.get((len(pattern), pattern))}
        for pattern in ('aa', 'zarr', 'å😀', 'datakit')]
    assert census_out.stat().st_mode & 0o777 == export.stat().st_mode & 0o777 == 0o600
    assert ch.scalar(f'SELECT doc FROM {target}.source_manifest') == source.read_text()[:-1]
    if native:
        return

    probe = module.DailyCensusCh(ch.url, db=target, wall_seconds=30, staging_bytes=1 << 30, max_execution_time=10, max_threads=4)
    try:
        probe.tmp('basename_ordered_probe', f"SELECT lowerUTF8(arrayElement(splitByChar('/',path),-1)) AS l FROM {target}.nodes", disk=True, order_by='l')
        select = module.weighted_select('basename_ordered_probe')
        assert probe.json(select) == [[name, count] for name, count in sorted(weighted.items())]
        plan = probe.exec('EXPLAIN PIPELINE compact=0 ' + select)
        assert sorted(set(findall(r'^\s*([A-Za-z]*Aggregat[A-Za-z]*Transform)\b', plan, flags=MULTILINE))) == [
            'AggregatingInOrderTransform', 'FinalizeAggregatedTransform'], plan
        assert sorted(set(findall(r'MergeTreeSelect\(pool: ([A-Za-z]+), algorithm: ([A-Za-z]+)\)', plan))) == [('ReadPoolInOrder', 'InOrder')], plan
        processors = findall(r'\b([A-Za-z]*(?:Sort|Window)[A-Za-z]*Transform)\b', plan)
        assert sorted(set(name for name in processors if name != 'MergingSortedTransform')) == [], plan
        probe.checkpoint()
    finally:
        probe.close()


@pytest.mark.parametrize('day', list(PATHS))
def test_native_complete_length_domain(fresh: tuple, tmp_path: Path, day: str) -> None:
    # `max_chars` None: every length until one has no hot pattern, so a miss of
    # any length is below the threshold. The census layers run through that
    # first empty length, and the export is the brute force over all lengths.
    native = environ.get('HF_NATIVE_BINARY')
    if not native:
        pytest.skip('HF_NATIVE_BINARY not set')
    ch, target = fresh
    parquet, source, census_out, export = [tmp_path / name for name in ('input.parquet', 'source.json', 'census.json', 'queries.jsonl')]
    write_tree(parquet, day)
    pinned = descriptor(parquet)
    pinned['date'] = day
    source.write_bytes(manifest_bytes(build(ch, target, parquet, pinned, min_free_bytes=1)))
    weighted = Counter(name.rsplit('/', 1)[-1].lower() for name in ['', *PATHS[day]])
    expected = {}
    for chars in range(1, max(map(len, weighted)) + 1):
        counts = Counter()
        for name, weight in weighted.items():
            counts.update({name[pos:pos + chars]: weight for pos in range(max(0, len(name) - chars + 1))})
        expected.update({(chars, gram): count for gram, count in counts.items() if count >= 2})
    longest, top = max(expected)
    result = hot_frequency_bench.bench(ch.url, target, day, 2, None, census_out, daily_source=source, queries_out=export,
                                     patterns=(top, 'x' * 20), seconds=30, wall_seconds=120, native=Path(native))
    rows = [loads(line) for line in export.read_text().splitlines()]
    assert rows == [{'schema': 'hot-frequency-queries-v1', 'target': target, 'date': day, 'threshold_paths': 2, 'max_chars': None},
                    *[{'chars': chars, 'pattern': gram, 'direct_matching_paths': value} for (chars, gram), value in sorted(expected.items())],
                    {'complete': True, 'patterns': len(expected)}]
    assert ([(layer['chars'], layer['hot_patterns']) for layer in result['lengths']] ==
            [*[(chars, sum(k == chars for k, _ in expected)) for chars in range(1, longest + 1)], (longest + 1, 0)])
    assert (result['max_chars'], result['selected_patterns']) == (None, [
        {'pattern': top, 'hot': True, 'direct_matching_paths': expected[longest, top]},
        {'pattern': 'x' * 20, 'hot': False, 'direct_matching_paths': None},
    ])
    header, literals = load_queries(export, target, day, allow_union=False)
    assert (header['max_chars'], len(literals)) == (None, len(expected))
    report = hot_frequency_report.report(census_out, export, thresholds=(2,), lengths=(None,), patterns=(top, 'x' * 40))
    assert (report['grid'], report['selected_literals']) == (
        [{'threshold_paths': 2, 'max_chars': None, 'literals': len(expected),
          'literal_utf8_bytes': sum(len(gram.encode()) for _, gram in expected),
          'export_records_raw_bytes': sum(len(line.encode()) + 1 for line in export.read_text().splitlines()[1:-1])}],
        [{'pattern': top, 'enumerated': True, 'registered': True, 'direct_matching_paths': expected[longest, top], 'cold_upper_bound_exclusive': None},
         {'pattern': 'x' * 40, 'enumerated': True, 'registered': False, 'direct_matching_paths': None, 'cold_upper_bound_exclusive': 2}])


def test_complete_length_domain_needs_the_native_engine(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r'^the complete length domain needs the native engine \(`-e`\)$'):
        hot_frequency_bench.bench('http://fixture.invalid:8123', 'fresh', '2026-10-06', 2, None, tmp_path / 'census.json',
                                  daily_source=tmp_path / 'source.json')


def test_cli_daily_source_exact_forwarding_without_changing_frozen_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(hot_frequency_bench, 'bench', lambda *args, **kwargs: calls.append((args, kwargs)) or {'complete': True})
    result = CliRunner().invoke(main, ['ch-hot-frequency-census', 'daily_target', '-d', '2026-10-06', '-t', '100000',
        '-k', '16', '-f', str(tmp_path / 'source.json'), '-o', str(tmp_path / 'census.json'), '-q', str(tmp_path / 'queries.jsonl'),
        '-b', '16', '-v', '3600', '-s', '16'])
    assert (result.exit_code, result.exception, loads(result.stdout), result.stderr) == (0, None, {'complete': True}, '')
    assert calls == [(('http://localhost:8123', 'daily_target', '2026-10-06', 100000, 16, tmp_path / 'census.json'),
                     {'memory_gib': 8, 'seconds': 600, 'spill_gib': 16, 'pids': (), 'patterns': (),
                      'queries_out': tmp_path / 'queries.jsonl', 'thresholds': (), 'max_patterns': 500000,
                      'daily_source': tmp_path / 'source.json', 'wall_seconds': 3600, 'staging_gib': 16, 'native': None})]
