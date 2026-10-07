"""Synthetic accepted-reader stand-ins; no producer or native kernel oracle."""

from copy import deepcopy
from hashlib import sha256
from json import dumps, loads
from os import environ
from types import SimpleNamespace
from uuid import uuid4

import pytest

from dt_cloud.chstore import dated_hot_l1_check as module
from dt_cloud.chstore.client import Ch, lit

DB = 'day_fixture'
PATTERNS = ('alfa', '.npy', 'å', 'zarr.json', 'none', '%', '_', '\\')
NODES = [
    [0, 8, 0, '', 13, 5], [1, 5, 1, 'alfa', 10, 4], [2, 4, 2, 'alfa/Å-match', 7, 3],
    [3, 3, 3, 'alfa/Å-match/file.npy', 5, 1], [4, 4, 3, 'alfa/Å-match/zero', 0, 1],
    [5, 5, 2, 'alfa/root.npy', 3, 1], [6, 8, 1, 'beta', 3, 1], [7, 8, 2, 'beta/other', 3, 1],
    [8, 8, 3, 'beta/other/zarr.json', 3, 1],
]
BUCKETS = [{'path': 'alfa', 'pre': 1, 'post': 5}, {'path': 'beta', 'pre': 6, 'post': 8}]
DICTIONARY = [['', 0, 8, 0], ['alfa', 1, 5, 1], ['beta', 6, 8, 1]]


def encoded(body: dict) -> bytes:
    return (dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def own_oracle(pattern: str) -> list[list]:
    # This scalar toy tree has objects at a covered directory, including a
    # zero-byte descendant. Summing recursive first hits must retain them.
    sums = {}
    for _, _, _, path, b, o in NODES:
        parent = path.rsplit('/', 1)[0] if '/' in path else ''
        if pattern in path.lower() and pattern not in parent.lower():
            bucket = path.split('/', 1)[0]
            pair = sums.setdefault(bucket, [0, 0])
            pair[0] += b; pair[1] += o
    return [[path, str(b), str(o)] for path, (b, o) in sorted(sums.items())]


def frontier_sql(pattern: str) -> str:
    literal = "'" + pattern.replace('\\', '\\\\').replace("'", "\\'") + "'"
    return (f'WITH lowerUTF8({literal}) AS literal '
            f"SELECT arrayElement(splitByChar('/',path),1) AS bucket,sum(toUInt128(b)),sum(toUInt128(o)) FROM {DB}.nodes "
            "WHERE position(lowerUTF8(path),literal)>0 AND position(lowerUTF8(if(position(path, '/') = 0, '', substring(path, 1, length(path) - position(reverse(path), '/')))),literal)=0 GROUP BY bucket ORDER BY bucket")


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch):
    raw = encoded({'complete': True, 'prefix': '', 'snapshot_db': DB, 'target': DB, 'date': '2026-10-06', 'nodes': 9,
                   'root': {'path': '', 'pre': 0, 'post': 8, 'b': 13, 'o': 5}, 'buckets': BUCKETS})
    selection_body = {'registry': {'sha256': 'b' * 64, 'bytes': 123, 'qualification_dates': ['2026-10-04', '2026-10-05']},
                      'build_source': {'manifest_sha256': sha256(raw).hexdigest(), 'manifest_bytes': len(raw)}}
    selection = SimpleNamespace(snapshot_db=DB, target=DB, nodes=9, source_manifest_raw=raw, patterns=PATTERNS,
                                buckets=tuple((r['pre'], r['post'], r['path']) for r in BUCKETS), metadata=lambda: deepcopy(selection_body))
    views = {}
    for p in PATTERNS:
        hits = {path: (int(b), int(o)) for path, b, o in own_oracle(p)}
        buckets = [{**r, 'b': hits.get(r['path'], (0, 0))[0], 'o': hits.get(r['path'], (0, 0))[1]} for r in BUCKETS]
        views[p] = {'root': {key: sum(r[key] for r in buckets) for key in ('b', 'o')}, 'buckets': buckets}
    catalog = SimpleNamespace(selection=selection, date='2026-10-06', logical_store='gcs_fleet',
                              view=lambda day, p: deepcopy(views[p]), metadata=lambda: {'source': {'artifact_sha256': 'c' * 64, 'artifact_bytes': 456}})
    result = SimpleNamespace(catalog=catalog, views=views, raw=raw, sql=[], results=None, controls=[], cleanup_ok=True, control_closed=False)
    ch = SimpleNamespace(url='http://fixture', db='default', settings={'max_execution_time': '600', 'max_memory_usage': str(2 << 30)}, timeout=660)

    def json(sql, *, settings):
        result.sql.append((sql, settings))
        value = result.results.pop(0)
        if isinstance(value, BaseException):
            raise value
        return deepcopy(value)

    ch.json = json
    result.ch = ch
    class Control:
        def __init__(self, *args, **kwargs):
            result.controls.append(('open', args, kwargs))
        def exec(self, sql, *, fmt):
            result.controls.append(('exec', sql, fmt))
        def scalar(self, sql):
            result.controls.append(('scalar', sql))
            return '0' if result.cleanup_ok else '1'
        def close(self):
            result.control_closed = True
    monkeypatch.setattr(module, 'Ch', Control)
    monkeypatch.setattr(module, 'uuid4', lambda: SimpleNamespace(hex='a' * 32))
    monkeypatch.setattr(module, 'monotonic', lambda: 1.)
    return result


def responses(state, patterns: tuple[str, ...]) -> list:
    return [[[state.raw.decode().removesuffix('\n')]], [[9, 0, 8, 0]], DICTIONARY, [['', 0, 8, 13, 5]],
            *[own_oracle(pattern) for pattern in patterns], [[state.raw.decode().removesuffix('\n')]]]


def test_exact_full_source_sql_and_selected_proof_preserve_scope_and_no_weights(state) -> None:
    state.results = responses(state, PATTERNS)
    proof = module.check(state.ch, state.catalog, PATTERNS)
    assert proof == {'schema': 'dated-hot-l1-check-v1', 'complete': True, 'logical_store': 'gcs_fleet', 'date': '2026-10-06', 'target': DB, 'snapshot_db': DB,
                     'artifact': {'sha256': 'c' * 64, 'bytes': 456}, 'selection': state.catalog.selection.metadata(),
                     'source_manifest': {'sha256': sha256(state.raw).hexdigest(), 'bytes': len(state.raw),
                                         'marker_sha256': sha256(state.raw[:-1]).hexdigest(), 'marker_bytes': len(state.raw) - 1},
                     'source_nodes': 9, 'selected_patterns': [{'pattern': p, 'validation': 'complete independent full-path first-hit source scan', 'full_source_scan': True, 'buckets_checked': 2} for p in PATTERNS],
                     'selected_patterns_checked': 8, 'independent_full_catalog_source_oracle': False, 'check_s': 0.}
    expected_sql = [f'SELECT doc FROM {DB}.source_manifest',
                    f'''SELECT count(),min(pre),max(pre),
            countIf(isNull(path) OR NOT isValidUTF8(path) OR isNull(pre) OR isNull(post) OR isNull(b) OR isNull(o)
                OR pre < 0 OR post < pre OR post >= 9 OR b < 0 OR o < 0)
            FROM {DB}.nodes''', f'SELECT path,pre,post,depth FROM {DB}.dictionary ORDER BY pre',
                    f'SELECT path,pre,post,b,o FROM {DB}.nodes WHERE pre=0', *[frontier_sql(p) for p in PATTERNS], f'SELECT doc FROM {DB}.source_manifest']
    assert state.sql == [(sql, {'query_id': f'dated_hot_l1_check_{"a" * 32}_{i:04d}', 'timeout_overflow_mode': 'throw', 'timeout_before_checking_execution_speed': 0}) for i, sql in enumerate(expected_sql, 1)]
    assert (state.controls, state.results, state.ch.settings) == ([], [], {'max_execution_time': '600', 'max_memory_usage': str(2 << 30)})


@pytest.mark.parametrize('patterns', [(), ('a',), ('alfa', 'alfa'), ('',), ('alfa/x',), ('\0',), tuple('abcdefghi'), ['alfa']])
def test_invalid_or_unregistered_patterns_refuse_before_source_queries(state, patterns) -> None:
    with pytest.raises(ValueError) as caught:
        module.check(state.ch, state.catalog, patterns)
    assert (str(caught.value), state.sql, state.controls) == ('dated L1 check requires one to eight unique registered literals', [], [])


@pytest.mark.parametrize('setting,value', [('max_execution_time', '0'), ('max_execution_time', 'nan'), ('max_memory_usage', '0'), ('max_memory_usage', 'bad')])
def test_requires_bounded_caller_resources_before_sql(state, setting: str, value: str) -> None:
    state.ch.settings[setting] = value
    with pytest.raises(ValueError) as caught:
        module.check(state.ch, state.catalog, ('alfa',))
    assert (str(caught.value), state.sql, state.controls) == ('dated L1 check requires finite positive caller memory/time budgets', [], [])


@pytest.mark.parametrize('stage', ['marker before', 'marker after', 'multiple markers', 'domain', 'missing bucket', 'source root', 'unknown hit', 'duplicate hit', 'bool weight', 'selected mismatch', 'timeout'])
def test_failure_refuses_proof_and_cancels_only_own_exact_ids(state, stage: str) -> None:
    state.results = responses(state, ('.npy',))
    if stage == 'marker before': state.results[0] = [['changed']]
    if stage == 'marker after': state.results[-1] = [['changed']]
    if stage == 'multiple markers': state.results[0].append(state.results[0][0])
    if stage == 'domain': state.results[1] = [[8, 0, 8, 0]]
    if stage == 'missing bucket': state.results[2] = DICTIONARY[:-1]
    if stage == 'source root': state.results[3][0][3] = 999
    if stage == 'unknown hit': state.results[4] = [['outsider', 0, 0]]
    if stage == 'duplicate hit': state.results[4].append(state.results[4][0])
    if stage == 'bool weight': state.results[4][0][1] = True
    if stage == 'selected mismatch': state.results[4][0][1] = '7'
    if stage == 'timeout': state.results[4] = TimeoutError('fixture timeout')
    errors = {
        'marker before': 'dated L1 check actual source marker differs from the pinned selection',
        'marker after': 'dated L1 check actual source marker differs from the pinned selection',
        'multiple markers': 'dated L1 check requires one completed source marker',
        'domain': 'dated L1 check complete source count/domain differs from selection',
        'missing bucket': 'dated L1 check actual root/bucket dictionary differs from selection',
        'source root': 'dated L1 check actual source root differs from manifest',
        'unknown hit': 'dated L1 check frontier returned invalid or undeclared buckets',
        'duplicate hit': 'dated L1 check frontier returned invalid or undeclared buckets',
        'bool weight': 'dated L1 check source returned an invalid unsigned scalar',
        'selected mismatch': 'dated L1 selected root/buckets disagree with independent full-source first-hit scan',
        'timeout': 'fixture timeout',
    }
    with pytest.raises((ValueError, AssertionError, TimeoutError)) as caught:
        module.check(state.ch, state.catalog, ('.npy',))
    assert str(caught.value) == errors[stage]
    ids = ','.join(f"'dated_hot_l1_check_{'a' * 32}_{i:04d}'" for i in range(1, len(state.sql) + 1))
    assert state.controls == [('open', ('http://fixture',), {'db': 'default', 'session': False, 'timeout': 2, 'max_threads': 1,
                              'max_memory_usage': 64 << 20, 'max_execution_time': 2, 'timeout_before_checking_execution_speed': 0, 'timeout_overflow_mode': 'throw'}),
                              ('exec', f'KILL QUERY WHERE query_id IN ({ids}) SYNC', None), ('scalar', f'SELECT count() FROM system.processes WHERE query_id IN ({ids})')]
    assert state.control_closed is True


def test_canonical_literal_percent_underscore_and_backslash_not_like_patterns(state) -> None:
    state.results = responses(state, ('%', '_', '\\'))
    proof = module.check(state.ch, state.catalog, ('%', '_', '\\'))
    assert proof['selected_patterns'] == [{'pattern': p, 'validation': 'complete independent full-path first-hit source scan', 'full_source_scan': True, 'buckets_checked': 2} for p in ('%', '_', '\\')]
    assert [sql for sql, _ in state.sql[4:-1]] == [frontier_sql(p) for p in ('%', '_', '\\')]


def test_missing_source_identity_never_reclassified_as_true_zero(state) -> None:
    state.results = responses(state, ('none',)); state.results[2] = DICTIONARY[:-1]
    with pytest.raises(ValueError) as caught:
        module.check(state.ch, state.catalog, ('none',))
    assert str(caught.value) == 'dated L1 check actual root/bucket dictionary differs from selection'
    assert len(state.sql) == 3


def test_oracle_compares_all_buckets_not_just_total_root(state) -> None:
    state.results = responses(state, ('.npy',)); state.results[4] = [['beta', 8, 2]]
    with pytest.raises(AssertionError) as caught:
        module.check(state.ch, state.catalog, ('.npy',))
    assert str(caught.value) == 'dated L1 selected root/buckets disagree with independent full-source first-hit scan'


def test_cleanup_quiescence_failure_raises_not_accepted_proof(state) -> None:
    state.results = responses(state, ('alfa',)); state.results[4] = TimeoutError('fixture timeout'); state.cleanup_ok = False
    with pytest.raises(RuntimeError) as caught:
        module.check(state.ch, state.catalog, ('alfa',))
    assert (str(caught.value), state.control_closed) == ('dated L1 check could not verify owned-query cleanup', True)


@pytest.mark.skipif(not environ.get('CLICKHOUSE_URL'), reason='requires an explicit ClickHouse fixture server')
def test_tiny_clickhouse_fullpath_oracle_has_directory_own_and_zero_byte_objects(state, monkeypatch: pytest.MonkeyPatch) -> None:
    # Native SQL grammar/Unicode semantics only; still no producer/kernel
    # oracle or catalog construction. Main owns running this remote fixture.
    database = 'dated_check_fixture_' + uuid4().hex
    ch = Ch(environ['CLICKHOUSE_URL'], timeout=30, max_execution_time=15, max_memory_usage=128 << 20, max_threads=1)
    source = loads(state.raw)
    source.update(target=database, snapshot_db=database)
    raw = encoded(source)
    selection = state.catalog.selection
    selection.target = selection.snapshot_db = database
    selection.source_manifest_raw = raw
    old_metadata = selection.metadata()
    old_metadata['build_source'] = {'manifest_sha256': sha256(raw).hexdigest(), 'manifest_bytes': len(raw)}
    selection.metadata = lambda: deepcopy(old_metadata)
    monkeypatch.setattr(module, 'Ch', Ch)
    ch.exec(f'CREATE DATABASE {database}', fmt=None)
    try:
        ch.exec(f'CREATE TABLE {database}.nodes (pre UInt32,post UInt32,depth UInt8,path String,b UInt64,o UInt64) ENGINE=Memory', fmt=None)
        values = ','.join(f'({pre},{post},{depth},{lit(path)},{b},{o})' for pre, post, depth, path, b, o in NODES)
        ch.exec(f'INSERT INTO {database}.nodes VALUES {values}', fmt=None)
        ch.exec(f'CREATE TABLE {database}.dictionary (path String,pre UInt32,post UInt32,depth UInt8) ENGINE=Memory', fmt=None)
        values = ','.join(f'({lit(path)},{pre},{post},{depth})' for path, pre, post, depth in DICTIONARY)
        ch.exec(f'INSERT INTO {database}.dictionary VALUES {values}', fmt=None)
        ch.exec(f'CREATE TABLE {database}.source_manifest (doc String) ENGINE=Memory', fmt=None)
        doc = raw.decode().removesuffix('\n')
        ch.exec(f'INSERT INTO {database}.source_manifest VALUES ({lit(doc)})', fmt=None)
        proof = module.check(ch, state.catalog, PATTERNS)
        assert (proof['complete'], proof['source_nodes'], proof['selected_patterns_checked'], proof['independent_full_catalog_source_oracle']) == (True, 9, 8, False)
        assert proof['selected_patterns'] == [{'pattern': p, 'validation': 'complete independent full-path first-hit source scan', 'full_source_scan': True, 'buckets_checked': 2} for p in PATTERNS]
    finally:
        ch.exec(f'DROP DATABASE {database}', fmt=None)
        ch.close()
