"""Paired depth-2 adapters require complete controls, exact roots and bounded IO."""

from io import BytesIO
from json import dumps, loads
from os import dup, environ, fdopen
from pathlib import Path
from struct import pack
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import hot_l2_pair_stream as module
from test_chhot_l1_batch_catalog import artifact, stream_artifact, write
from test_chhot_l1_publish import prefix_proof


def prepared() -> dict:
    return {'target': 'fleet', 'dates': ['2026-10-04', '2026-10-05'], 'dbs': ['snapshot_20261004', 'snapshot_20261005'],
            'rows': [9, 9], 'patterns': ('.json', '.npy'), 'budget': 4, 'query_subset': False,
            'buckets': [(1, 4, 'a'), (5, 8, 'b')], 'thresholds': [[2, 1], [1, 1]],
            'expected': [[[8, 3, 2, 1], [0, 2, 0, 4]], [[0, 0, 0, 0], [0, 0, 0, 0]]], 'provenance': {}}


def declared() -> list[dict]:
    return [{'frame_id': 1, 'pre': 2, 'post': 3, 'path': 'a/x', 'bucket': 0},
            {'frame_id': 2, 'pre': 4, 'post': 4, 'path': 'a/y', 'bucket': 0},
            {'frame_id': 3, 'pre': 6, 'post': 6, 'path': 'b/x', 'bucket': 1},
            {'frame_id': 4, 'pre': 7, 'post': 8, 'path': 'b/y', 'bucket': 1}]


def native() -> dict:
    return {'schema': 'hot-l2-native-pair-v1', 'exact': True, 'incremental': False, 'levels': 2,
            'rows_read': [9, 9], 'registered_predicates': 2, 'registered_frames': 4,
            'peak_stack': [3, 3], 'peak_active': [1, 1], 'max_cells': 10, 'emitted_cells': 2, 'native_peak_rss_bytes': 1024,
            'roots': [{'predicate_id': 1, 'buckets': [['8', '3', '2', '1'], ['0', '2', '0', '4']]},
                      {'predicate_id': 2, 'buckets': [['0', '0', '0', '0'], ['0', '0', '0', '0']]}],
            'cells': [{'predicate_id': 1, 'frame_id': 1, 'b': ['6', '0'], 'o': ['2', '0']},
                      {'predicate_id': 1, 'frame_id': 2, 'b': ['2', '2'], 'o': ['1', '1']}]}


def test_complete_exact_deleted_offsetting_and_zero_byte_remainder() -> None:
    body = native()
    assert module.complete(body, prepared(), declared(), 10) == {
        'results': [
            {'predicate_id': 1, 'pattern': '.json', 'root': {'b': [8, 2], 'o': [5, 5]}, 'buckets': [
                {'pre': 1, 'post': 4, 'path': 'a', 'threshold_bytes': 2, 'b': [8, 2], 'o': [3, 1], 'other': {'b': [0, 0], 'o': [0, 0]}},
                {'pre': 5, 'post': 8, 'path': 'b', 'threshold_bytes': 1, 'b': [0, 0], 'o': [2, 4], 'other': {'b': [0, 0], 'o': [2, 4]}},
            ]},
            {'predicate_id': 2, 'pattern': '.npy', 'root': {'b': [0, 0], 'o': [0, 0]}, 'buckets': [
                {'pre': 1, 'post': 4, 'path': 'a', 'threshold_bytes': 1, 'b': [0, 0], 'o': [0, 0], 'other': {'b': [0, 0], 'o': [0, 0]}},
                {'pre': 5, 'post': 8, 'path': 'b', 'threshold_bytes': 1, 'b': [0, 0], 'o': [0, 0], 'other': {'b': [0, 0], 'o': [0, 0]}},
            ]},
        ], 'frames': declared(),
        'cells': [{'predicate_id': 1, **declared()[0], 'b': [6, 0], 'o': [2, 0]},
                  {'predicate_id': 1, **declared()[1], 'b': [2, 2], 'o': [1, 1]}],
        'native': {key: value for key, value in body.items() if key not in ('roots', 'cells')},
    }


@pytest.mark.parametrize('change,message', [
    (lambda b: b.update(rows_read=[8, 9]), 'paired L2 native source/catalog/control counts disagree'),
    (lambda b: b['roots'][0]['buckets'][0].__setitem__(0, '7'), 'paired L2 root totals disagree with the accepted paired references'),
    (lambda b: b['roots'][0]['buckets'][0].__setitem__(0, '08'), 'paired L2 native weights must be canonical decimal strings'),
    (lambda b: b['cells'].reverse(), 'paired L2 native cells are unknown, duplicate or unordered'),
    (lambda b: b['cells'][0].update(frame_id=5), 'paired L2 native cells are unknown, duplicate or unordered'),
    (lambda b: b['cells'][0].update(b=['1', '0']), 'paired L2 native emitted a cell below its bucket threshold'),
    (lambda b: b['cells'][0].update(o=['4', '0']), 'paired L2 cell partition exceeds its exact bucket totals'),
    (lambda b: b.update(emitted_cells=3), 'paired L2 native returned incomplete roots or cells'),
    (lambda b: b.update(extra=True), 'paired L2 native returned an unsupported contract'),
])
def test_native_partial_or_inconsistent_output_refuses(change, message: str) -> None:
    body = native()
    change(body)
    with pytest.raises(RuntimeError) as caught:
        module.complete(body, prepared(), declared(), 10)
    assert str(caught.value) == message


def test_wire_controls_include_each_bucket_threshold_and_utf8() -> None:
    p = prepared()
    p['patterns'] = ('å', '.npy')
    expected = (b'HL2PAIR1' + pack('<QQIIB', 9, 9, 2, 4, 2) + pack('<QQQQ', 1, 4, 5, 8)
                + b''.join(pack('<QQI', row['pre'], row['post'], row['bucket']) for row in declared())
                + b'\x02\xc3\xa5' + pack('<QQ', 2, 1) + b'\x04.npy' + pack('<QQ', 1, 1))
    assert module.control(p, declared()) == expected


def inputs(tmp_path: Path) -> dict:
    refs = []
    for day in ('2026-10-04', '2026-10-05'):
        body = artifact(day)
        source = stream_artifact()
        body.update({key: source[key] for key in ('schema', 'engine', 'source_query_id', 'native')})
        refs.append(write(tmp_path / f'{day}.json', body))
    refs = tuple(refs)
    proofs = tuple(tmp_path / f'proof-{i}.json' for i in range(2))
    for proof, ref in zip(proofs, refs, strict=True):
        prefix_proof(proof, ref)
    query = tmp_path / 'queries.jsonl'
    query.write_text('\n'.join(map(dumps, [artifact()['queries']['header'],
        {'chars': 5, 'pattern': '.json', 'direct_matching_paths': 10},
        {'chars': 4, 'pattern': '.npy', 'direct_matching_paths': 12}, {'complete': True, 'patterns': 2}])) + '\n')
    return {'target': 'fleet', 'before': '2026-10-04', 'after': '2026-10-05', 'references': refs,
            'proofs': proofs, 'queries': query, 'patterns': ('.JSON', '.npy'), 'budget': 4}


def test_prepare_binds_complete_registry_artifacts_and_dated_proofs(tmp_path: Path) -> None:
    actual = module.prepare(**inputs(tmp_path))
    provenance = actual.pop('provenance')
    expected = prepared()
    expected.pop('provenance')
    assert actual == expected
    assert [(r['bytes'], len(r['sha256'])) for r in provenance['references']] == [(path.stat().st_size, 64) for path in (tmp_path / '2026-10-04.json', tmp_path / '2026-10-05.json')]
    assert loads((tmp_path / 'queries.jsonl').read_text().splitlines()[0]) == provenance['queries']['header']


@pytest.mark.parametrize('change,message', [
    (lambda p: p.update(references=p['references'][:1]), 'paired L2 needs two ordered dates, two complete references and two dated prefix proofs'),
    (lambda p: p.update(proofs=p['proofs'][:1]), 'paired L2 needs two ordered dates, two complete references and two dated prefix proofs'),
    (lambda p: p.update(patterns=('.json', '.JSON')), 'paired L2 selected literals must be unique and registered on both artifacts'),
    (lambda p: p.update(patterns=('unknown',)), 'paired L2 selected literals must be unique and registered on both artifacts'),
    (lambda p: p.update(patterns=()), 'select explicit literals or explicitly request all registered predicates'),
])
def test_prepare_refuses_partial_or_invented_identity(tmp_path: Path, change, message: str) -> None:
    args = inputs(tmp_path)
    change(args)
    with pytest.raises(ValueError) as caught:
        module.prepare(**args)
    assert str(caught.value) == message


def test_prepare_requires_native_references_and_completed_registry(tmp_path: Path) -> None:
    args = inputs(tmp_path)
    write(args['references'][0], artifact('2026-10-04'))
    with pytest.raises(ValueError) as caught:
        module.prepare(**args)
    assert str(caught.value) == 'paired L2 requires completed native L1 references'
    args = inputs(tmp_path)
    args['queries'].write_text('\n'.join(args['queries'].read_text().splitlines()[:-1]) + '\n')
    with pytest.raises(ValueError) as caught:
        module.prepare(**args)
    assert str(caught.value) == 'hot query export lacks a valid exact-count completion footer'


def test_frames_are_complete_union_partition_not_only_present_date_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(module, '_buckets', lambda ch, target: prepared()['buckets'])

    class Ch:
        def scalar(self, sql):
            calls.append(sql)
            return dumps({'prefix': '', 'dates': prepared()['dates'], 'dbs': prepared()['dbs']})

        def json(self, sql):
            calls.append(sql)
            return [[r['pre'], r['post'], r['path']] for r in declared()]

    assert module.frames(Ch(), prepared(), 4) == declared()
    assert calls == ['SELECT doc FROM fleet.history_manifest', 'SELECT toUInt64(pre),toUInt64(post),path FROM fleet.dictionary WHERE depth=2 ORDER BY pre LIMIT 5']


@pytest.mark.parametrize('rows,cap,message', [
    ([[2, 3, 'a/x'], [6, 8, 'b/x']], 4, 'paired L2 frames do not completely partition bucket descendants'),
    ([[2, 4, 'a/x/z'], [6, 8, 'b/x']], 4, 'paired L2 frames do not completely partition bucket descendants'),
    ([[2, 4, 'a/x'], [6, 8, 'b/x']], 1, 'paired L2 complete frame count exceeds its guard'),
])
def test_frames_refuse_gaps_wrong_depth_and_guard(monkeypatch: pytest.MonkeyPatch, rows: list, cap: int, message: str) -> None:
    monkeypatch.setattr(module, '_buckets', lambda ch, target: prepared()['buckets'])
    ch = SimpleNamespace(scalar=lambda sql: dumps({'prefix': '', 'dates': prepared()['dates'], 'dbs': prepared()['dbs']}), json=lambda sql: rows)
    with pytest.raises(ValueError) as caught:
        module.frames(ch, prepared(), cap)
    assert str(caught.value) == message


def test_partial_pipe_writes_are_completed_and_zero_write_refuses() -> None:
    class Sink:
        data = bytearray()

        def write(self, data):
            self.data.extend(data[:2])
            return min(2, len(data))

    sink = Sink()
    module._write_all(sink, b'abcdefg')
    assert bytes(sink.data) == b'abcdefg'
    with pytest.raises(BrokenPipeError) as caught:
        module._write_all(SimpleNamespace(write=lambda data: 0), b'x')
    assert str(caught.value) == 'paired L2 consumer stopped accepting input'


@pytest.mark.parametrize('failure', [None, 'stdout', 'source', 'exit'])
@pytest.mark.parametrize('native_source', [False, True])
def test_stream_raw_pumps_and_owned_error_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None, native_source: bool) -> None:
    calls, readers, children, cancellations = [], [], [], []

    class Reader:
        def __init__(self, side):
            self.side, self.closed = side, False

        def stream(self, sql, fmt):
            calls.append((self.side, ' '.join(sql.split()), fmt))
            if failure == 'source' and self.side == 0:
                raise RuntimeError('fixture source failure')
            yield b'left' if self.side == 0 else b'right'

        def close(self):
            self.closed = True

    class Ch:
        def fork(self, query_id):
            assert native_source is False
            reader = Reader(len(readers))
            readers.append(reader)
            return reader

    source_calls = []
    source_client = tmp_path / 'clickhouse'

    def native_reader(ch, binary, query_id, wall_seconds):
        source_calls.append((binary, query_id, wall_seconds))
        reader = Reader(len(readers))
        readers.append(reader)
        return reader

    from dt_cloud.chstore import hot_l2_native_source
    monkeypatch.setattr(hot_l2_native_source, 'NativeSource', native_reader)

    class Child:
        def __init__(self, argv, **kwargs):
            self.argv, self.kwargs = argv, kwargs
            self.reads = [fdopen(dup(fd), 'rb') for fd in kwargs['pass_fds']]
            self.stdin, self.stdout, self.stderr = BytesIO(), BytesIO(dumps(native()).encode()), BytesIO(b'bounded diagnostic')
            self.returncode = 7 if failure == 'exit' else 0
            children.append(self)

        def poll(self):
            return self.returncode

    monkeypatch.setattr(module, 'Popen', Child)
    monkeypatch.setattr(module, '_cancel', lambda ch, ids: cancellations.append(ids))
    monkeypatch.setattr(module, 'uuid4', lambda: SimpleNamespace(hex='a' * 32))
    options = dict(binary=tmp_path / 'native', max_cells=10, max_output_bytes=1 if failure == 'stdout' else 4096, wall_seconds=10)
    if native_source:
        options['source_client'] = source_client
    ids = ['hot_l2_pair_' + 'a' * 32 + suffix for suffix in ('_before', '_after')]
    if failure:
        with pytest.raises(RuntimeError) as caught:
            module.stream(Ch(), prepared(), declared(), **options)
        assert str(caught.value) == {'stdout': 'paired L2 native stdout exceeded its byte guard', 'source': 'fixture source failure', 'exit': 'paired L2 native engine exited with status 7'}[failure]
        assert cancellations == [ids]
    else:
        actual, query_ids, elapsed = module.stream(Ch(), prepared(), declared(), **options)
        assert actual == native()
        assert query_ids == ids
        assert elapsed >= 0
        assert cancellations == []
        assert sorted(calls) == [(side, ' '.join(f'SELECT assumeNotNull(toUInt64(pre)),assumeNotNull(toUInt64(post)), assumeNotNull(toUInt64(b)),assumeNotNull(toUInt64(o)),assumeNotNull(lowerUTF8({module.NAME})) FROM snapshot_2026100{4 + side}.nodes ORDER BY pre'.split()), 'RowBinary') for side in range(2)]
    assert [r.closed for r in readers] == [True, True]
    assert source_calls == ([(source_client, query_id, 10) for query_id in ids] if native_source else [])
    child = children[0]
    chunks = [reader.read() for reader in child.reads]
    for reader in child.reads:
        reader.close()
    if not failure:
        assert chunks == [b'left', b'right']
    assert [child.stdin.closed, child.stdout.closed, child.stderr.closed] == [True, True, True]
    assert child.argv == [str((tmp_path / 'native').resolve()), '--left-fd', str(child.kwargs['pass_fds'][0]), '--right-fd', str(child.kwargs['pass_fds'][1]), '--max-cells', '10']


def test_second_reader_creation_failure_closes_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    closed = []
    def fork(query_id):
        if closed:
            raise RuntimeError('second reader failed')
        closed.append(False)
        return SimpleNamespace(close=lambda: closed.__setitem__(0, True))
    with pytest.raises(RuntimeError) as caught:
        module.stream(SimpleNamespace(fork=fork), prepared(), declared(), binary=tmp_path / 'native', max_cells=10, max_output_bytes=4096, wall_seconds=10)
    assert str(caught.value) == 'second reader failed'
    assert closed == [True]


def test_wall_watchdog_terminates_child_and_cancels_only_owned_queries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls, clock = [], iter(range(20))

    class Child:
        returncode = None

        def __init__(self, *args, **kwargs):
            self.stdin, self.stdout, self.stderr = BytesIO(), BytesIO(b'{}'), BytesIO()

        def poll(self):
            return self.returncode

        def terminate(self):
            calls.append('terminate')
            self.returncode = -15

        def wait(self, timeout):
            calls.append(('wait', timeout))
            return self.returncode

    monkeypatch.setattr(module, 'Popen', Child)
    monkeypatch.setattr(module, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(module, 'uuid4', lambda: SimpleNamespace(hex='b' * 32))
    monkeypatch.setattr(module, '_cancel', lambda ch, ids: calls.append(('cancel', ids)))
    reader = SimpleNamespace(stream=lambda *args, **kwargs: iter(()), close=lambda: calls.append('close-reader'))
    # Empty iterators are deliberately closeable: match the real streaming API.
    def source(*args, **kwargs):
        yield from ()
    reader.stream = source
    with pytest.raises(TimeoutError) as caught:
        module.stream(SimpleNamespace(fork=lambda **kwargs: reader), prepared(), declared(), binary=tmp_path / 'native', max_cells=10, max_output_bytes=4096, wall_seconds=1)
    assert str(caught.value) == 'paired L2 experiment exceeded its wall deadline'
    assert calls == ['terminate', ('wait', 5), ('cancel', ['hot_l2_pair_' + 'b' * 32 + suffix for suffix in ('_before', '_after')]), 'close-reader', 'close-reader']


@pytest.mark.parametrize('fail', [False, True])
@pytest.mark.parametrize('native_source', [False, True])
def test_bench_settings_private_artifact_profile_and_finally_close(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail: bool, native_source: bool) -> None:
    calls = []

    class Ch:
        def __init__(self, *args, **kwargs):
            calls.append(('open', args, kwargs))
            self.url, self.db, self.headers, self.settings = args[0], kwargs['db'], {}, {}

        def exec(self, sql):
            calls.append(('exec', sql))

        def json(self, sql):
            calls.append(('profile', sql))
            return [['source', 200, 9, 300, 400]]

        def close(self):
            calls.append(('close',))

    def stream(ch, p, d, **kwargs):
        calls.append(('stream', {key: value for key, value in kwargs.items() if key != 'progress'}))
        if fail:
            raise RuntimeError('fixture stream failed')
        return native(), ['before-id', 'after-id'], .25

    monkeypatch.setattr(module, 'Ch', Ch)
    monkeypatch.setattr(module, 'prepare', lambda *a, **k: prepared())
    monkeypatch.setattr(module, 'frames', lambda *a: declared())
    monkeypatch.setattr(module, 'stream', stream)
    monkeypatch.setattr(module, 'uuid4', lambda: SimpleNamespace(hex='c' * 32))
    monkeypatch.setattr(module, 'monotonic', lambda: 1.)
    binary, out = tmp_path / 'native', tmp_path / 'out.json'
    binary.touch()
    binary.chmod(0o700)
    url = 'http://localhost:8123' if native_source else 'url'
    source_options = {'source_client': binary} if native_source else {}
    args = (url, 'fleet', '2026-10-04', '2026-10-05', (), (), tmp_path / 'queries', out)
    if fail:
        with pytest.raises(RuntimeError) as caught:
            module.bench(*args, binary=binary, max_cells=10, **source_options)
        assert str(caught.value) == 'fixture stream failed'
        assert out.exists() is False
    else:
        body = module.bench(*args, binary=binary, max_cells=10, **source_options)
        assert loads(out.read_text()) == body
        assert body['timings'] == {'reference_load_s': 0., 'stream_s': .25, 'build_s': 0.}
        assert body['profile'] == [['source', 200, 9, 300, 400]]
        assert body['source_query_ids'] == ['before-id', 'after-id']
        assert body.get('source_transport') == ({'protocol': 'native-tcp', 'client_binary': str(binary.resolve()),
            'host': '127.0.0.1', 'port': 9000, 'idle_timeout_seconds': 4560,
            'client_config': 'explicit verified empty XML; no ambient credentials'} if native_source else None)
        assert {key: body[key] for key in ('scope', 'exact', 'incremental', 'persistent_index_created', 'cutoff_scope')} == {
            'scope': module.SCOPE, 'exact': True, 'incremental': False, 'persistent_index_created': False,
            'cutoff_scope': 'fixed per query and bucket root across all depth-2 children; not arbitrary-prefix refinement'}
    expected = [('open', (url,), {'db': 'fleet', 'timeout': 3660, 'max_threads': 4, 'max_memory_usage': 8 << 30,
                    'max_execution_time': 3600, 'timeout_before_checking_execution_speed': 0, 'timeout_overflow_mode': 'throw',
                    'max_bytes_before_external_sort': 256 << 20, 'max_bytes_ratio_before_external_sort': 0,
                    'max_temporary_data_on_disk_size_for_query': 8 << 30, 'log_comment': 'hot_l2_pair_bench_' + 'c' * 32}),
                ('stream', {'binary': binary, 'max_cells': 10, 'max_output_bytes': 512 << 20, 'wall_seconds': 4500, **source_options})]
    if not fail:
        expected += [('exec', 'SYSTEM FLUSH LOGS'), ('profile', "SELECT query_id,query_duration_ms,read_rows,read_bytes,memory_usage FROM system.query_log WHERE log_comment='hot_l2_pair_bench_" + 'c' * 32 + "' AND type='QueryFinish' ORDER BY event_time_microseconds")]
    assert calls == [*expected, ('close',)]


def test_existing_artifact_refuses_before_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(module, 'Ch', lambda *a, **k: calls.append((a, k)))
    binary, out = tmp_path / 'native', tmp_path / 'out'
    binary.touch()
    binary.chmod(0o700)
    out.write_text('keep\n')
    with pytest.raises(ValueError) as caught:
        module.bench('url', 'fleet', '2026-10-04', '2026-10-05', (), (), tmp_path / 'queries', out, binary=binary)
    assert str(caught.value) == 'paired L2 output must be new in an existing directory'
    assert (calls, out.read_text()) == ([], 'keep\n')


@pytest.mark.parametrize('kwargs', [{'memory_gib': 9}, {'spill_gib': 0}, {'seconds': 3601}, {'wall_seconds': 4501}, {'max_cells': 10_000_001}, {'max_output_bytes': 513 << 20}])
def test_resource_bounds_refuse_before_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kwargs: dict) -> None:
    calls = []
    monkeypatch.setattr(module, 'Ch', lambda *a, **k: calls.append((a, k)))
    with pytest.raises(ValueError) as caught:
        module.bench('url', 'fleet', '2026-10-04', '2026-10-05', (), (), tmp_path / 'queries', tmp_path / 'out', binary=tmp_path / 'native', **kwargs)
    assert str(caught.value) == 'paired L2 resource/shape limits are outside their bounded ranges'
    assert calls == []


@pytest.mark.parametrize('native_source', [False, True])
def test_cli_compact_summary_and_exact_forwarding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, native_source: bool) -> None:
    from dt_cloud.cli import main
    calls = []
    body = {'schema': 'hot-l2-pair-stream-v1', 'dates': ['2026-10-04', '2026-10-05'], 'registered_predicates': 1,
            'registered_frames': 4, 'query_subset': True, 'timings': {'stream_s': .2}, 'cells': [{'private': 'not printed'}]}
    def bench(*args, **kwargs):
        calls.append((args, kwargs))
        return body
    monkeypatch.setattr(module, 'bench', bench)
    paths = [tmp_path / name for name in ('native', 'queries', 'ref0', 'ref1', 'proof0', 'proof1')]
    for path in paths:
        path.touch()
    out = tmp_path / 'out'
    extra = ['-C', str(paths[0])] if native_source else []
    result = CliRunner().invoke(main, ['ch-hot-l2-pair-bench', 'fleet', '-d', '2026-10-05', '-D', '2026-10-04', '-b', str(paths[0]), '-q', str(paths[1]), '-r', str(paths[2]), '-r', str(paths[3]), '-p', str(paths[4]), '-p', str(paths[5]), '-n', '.json', '-o', str(out), *extra])
    assert (result.exit_code, result.stderr) == (0, '')
    assert loads(result.stdout) == {**{k: body[k] for k in ('schema', 'dates', 'registered_predicates', 'registered_frames', 'query_subset', 'timings')}, 'out': str(out), 'cells': 1}
    assert calls == [(('http://localhost:8123', 'fleet', '2026-10-04', '2026-10-05', (paths[2], paths[3]), (paths[4], paths[5]), paths[1], out),
                      {'binary': paths[0], 'patterns': ('.json',), 'all_registered': False, 'budget': 64, 'memory_gib': 8, 'spill_gib': 8, 'seconds': 3600, 'wall_seconds': 4500, 'max_frames': 500_000, 'max_cells': 10_000_000, 'max_output_bytes': 512 << 20,
                       **({'source_client': paths[0]} if native_source else {})})]


def test_real_native_subprocess_two_rowbinary_pipes_and_complete_partition(monkeypatch: pytest.MonkeyPatch) -> None:
    binary = environ.get('HL2_NATIVE_BINARY')
    if not binary:
        pytest.skip('paired adapter integration requires explicit HL2_NATIVE_BINARY; never compiles locally')
    assert Path(binary).is_file() is True
    from dt_cloud.chstore.hot_l1_batch import Node
    from test_chhot_l2_native_stream import records

    p, d = prepared(), declared()
    p['patterns'] = ('å', '.npy')
    d[1]['path'], d[3]['path'] = 'a/å', 'b/å'
    left = [Node(0, 8, '', 8, 5), Node(1, 4, 'a', 8, 3), Node(2, 3, 'x', 6, 2),
            Node(3, 3, 'å', 6, 2), Node(4, 4, 'å', 2, 1), Node(5, 8, 'b', 0, 2),
            Node(6, 6, 'x', 0, 0), Node(7, 8, 'å', 0, 2), Node(8, 8, 'plain', 0, 2)]
    right = [Node(0, 8, '', 2, 5), Node(1, 4, 'a', 2, 1), Node(2, 3, 'x', 0, 0),
             Node(3, 3, 'å', 0, 0), Node(4, 4, 'å', 2, 1), Node(5, 8, 'b', 0, 4),
             Node(6, 6, 'x', 0, 0), Node(7, 8, 'å', 0, 4), Node(8, 8, 'plain', 0, 4)]
    calls, readers, cancellations = [], [], []

    class Reader:
        def __init__(self, side: int) -> None:
            self.side, self.closed = side, False

        def stream(self, sql: str, fmt: str):
            calls.append((self.side, ' '.join(sql.split()), fmt))
            raw = records((left, right)[self.side])
            # Tiny chunks deliberately split fixed fields, lengths and UTF-8.
            for offset in range(0, len(raw), 7):
                yield raw[offset:offset + 7]

        def close(self) -> None:
            self.closed = True

    class Ch:
        def fork(self, query_id: str) -> Reader:
            reader = Reader(len(readers))
            readers.append(reader)
            return reader

    monkeypatch.setattr(module, 'uuid4', lambda: SimpleNamespace(hex='d' * 32))
    monkeypatch.setattr(module, '_cancel', lambda ch, ids: cancellations.append(ids))
    actual, ids, elapsed = module.stream(Ch(), p, d, binary=Path(binary), max_cells=10, max_output_bytes=4096, wall_seconds=10)
    rss = actual['native_peak_rss_bytes']
    assert (type(rss), rss > 0) == (int, True)
    expected_native = native()
    if actual['schema'] == 'hot-l2-native-pair-v2':
        from test_chhot_l2_native_stream import matcher_counts
        expected_native.update(schema='hot-l2-native-pair-v2', **matcher_counts(left, right))
    assert actual == {**expected_native, 'native_peak_rss_bytes': rss}
    completed = module.complete(actual, p, d, 10)
    assert completed == {
        'results': [
            {'predicate_id': 1, 'pattern': 'å', 'root': {'b': [8, 2], 'o': [5, 5]}, 'buckets': [
                {'pre': 1, 'post': 4, 'path': 'a', 'threshold_bytes': 2, 'b': [8, 2], 'o': [3, 1], 'other': {'b': [0, 0], 'o': [0, 0]}},
                {'pre': 5, 'post': 8, 'path': 'b', 'threshold_bytes': 1, 'b': [0, 0], 'o': [2, 4], 'other': {'b': [0, 0], 'o': [2, 4]}},
            ]},
            {'predicate_id': 2, 'pattern': '.npy', 'root': {'b': [0, 0], 'o': [0, 0]}, 'buckets': [
                {'pre': 1, 'post': 4, 'path': 'a', 'threshold_bytes': 1, 'b': [0, 0], 'o': [0, 0], 'other': {'b': [0, 0], 'o': [0, 0]}},
                {'pre': 5, 'post': 8, 'path': 'b', 'threshold_bytes': 1, 'b': [0, 0], 'o': [0, 0], 'other': {'b': [0, 0], 'o': [0, 0]}},
            ]},
        ], 'frames': d,
        'cells': [{'predicate_id': 1, **d[0], 'b': [6, 0], 'o': [2, 0]},
                  {'predicate_id': 1, **d[1], 'b': [2, 2], 'o': [1, 1]}],
        'native': {key: value for key, value in actual.items() if key not in ('roots', 'cells')},
    }
    assert ids == ['hot_l2_pair_' + 'd' * 32 + suffix for suffix in ('_before', '_after')]
    assert (type(elapsed), elapsed >= 0) == (float, True)
    assert cancellations == []
    assert [reader.closed for reader in readers] == [True, True]
    assert sorted(calls) == [(side, ' '.join(f'SELECT assumeNotNull(toUInt64(pre)),assumeNotNull(toUInt64(post)), assumeNotNull(toUInt64(b)),assumeNotNull(toUInt64(o)),assumeNotNull(lowerUTF8({module.NAME})) FROM snapshot_2026100{4 + side}.nodes ORDER BY pre'.split()), 'RowBinary') for side in range(2)]


def test_v2_matcher_metadata_is_validated_and_preserved_without_changing_v1_results() -> None:
    before = native()
    after = {**before, 'schema': 'hot-l2-native-pair-v2', 'matcher_scans': 8, 'cache_hits': 8}
    v1 = module.complete(before, prepared(), declared(), 10)
    v2 = module.complete(after, prepared(), declared(), 10)
    assert v2 == {**v1, 'native': {**v1['native'], 'schema': 'hot-l2-native-pair-v2', 'matcher_scans': 8, 'cache_hits': 8}}


@pytest.mark.parametrize('change,message,error', [
    (lambda b: b.pop('matcher_scans'), 'paired L2 native returned an unsupported contract', RuntimeError),
    (lambda b: b.update(extra=True), 'paired L2 native returned an unsupported contract', RuntimeError),
    (lambda b: b.update(schema='hot-l2-native-pair-v1'), 'paired L2 native returned an unsupported contract', RuntimeError),
    (lambda b: b.update(schema='hot-l2-native-pair-v3'), 'paired L2 native returned an unsupported contract', RuntimeError),
    (lambda b: b.update(matcher_scans=True), 'paired L2 requires bounded unsigned integers', ValueError),
    (lambda b: b.update(cache_hits=-1), 'paired L2 requires bounded unsigned integers', ValueError),
    (lambda b: b.update(cache_hits=1 << 64), 'paired L2 requires bounded unsigned integers', ValueError),
    (lambda b: b.update(cache_hits=7), 'paired L2 matcher counters disagree with complete nonroot source rows', RuntimeError),
    (lambda b: b.update(matcher_scans=0, cache_hits=16), 'paired L2 matcher counters disagree with complete nonroot source rows', RuntimeError),
])
def test_v2_missing_unsupported_or_invalid_matcher_statistics_refuse(change, message: str, error: type[Exception]) -> None:
    body = {**native(), 'schema': 'hot-l2-native-pair-v2', 'matcher_scans': 8, 'cache_hits': 8}
    change(body)
    with pytest.raises(error) as caught:
        module.complete(body, prepared(), declared(), 10)
    assert str(caught.value) == message
