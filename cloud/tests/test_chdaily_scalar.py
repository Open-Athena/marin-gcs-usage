"""Date-only scalar construction: complete trees, never sampled prefixes."""

from hashlib import sha256
from json import loads
from pathlib import Path
from re import MULTILINE, findall, search
from struct import pack, iter_unpack
from types import SimpleNamespace
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud.chstore.client import Ch, ChError
from dt_cloud.chstore.daily_scalar import TABLE_COLUMNS, _descriptor, _records, audit_domain, audited_intervals, audited_preorder, build, manifest_bytes, ordered_select, physical_order_sql, resume_evidence, scalar_select, stage_sql, wire_select
from dt_cloud.chstore.narrow import preorder_intervals, tree_order_expr

from chserver import ch_url  # noqa: F401


def descriptor(path: Path) -> dict:
    data = path.read_bytes()
    return {'schema': 'daily-scalar-input-v1', 'date': '2026-10-06', 'logical_store': 'gcs',
            'identity': {'uri': 'gs://fixture/index/g/path-index.parquet', 'generation': 'g'},
            'bytes': len(data), 'sha256': sha256(data).hexdigest()}


def encoded(rows: list[tuple]) -> bytes:
    output = bytearray()
    for node, depth, b, o, path in rows:
        text = path.encode('utf-8')
        assert len(text) < 128
        output.extend(pack('<IBQQB', node, depth, b, o, len(text)) + text)
    return bytes(output)


ROWS = [(0, 0, 15, 5, ''), (1, 1, 15, 5, 'a'), (2, 2, 15, 4, 'a/run.json'),
        (3, 3, 5, 1, 'a/run.json/z'), (4, 3, 10, 1, 'a/run.json/Å😀'), (5, 2, 0, 1, 'a/run.json-b')]


def test_numbering_sql_requires_semantic_sorted_ordinal_not_expression_evaluation_order() -> None:
    assert ' '.join(ordered_select('fresh').split()) == (
        'SELECT toUInt32(row_number() OVER (ORDER BY ' + tree_order_expr() +
        ' ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)-1) AS id, assumeNotNull(toUInt8(depth)) AS depth,assumeNotNull(path) AS path, '
        'assumeNotNull(toUInt64(b)) AS b,assumeNotNull(toUInt64(o)) AS o FROM fresh.scalar'
    )


def test_wire_sql_requires_nonnullable_types_before_rowbinary_fixed_width_parser() -> None:
    assert wire_select('fresh') == (
        'SELECT assumeNotNull(toUInt32(id)),assumeNotNull(toUInt8(depth)),assumeNotNull(toUInt64(b)),'
        'assumeNotNull(toUInt64(o)),assumeNotNull(path) FROM fresh.ordered ORDER BY id'
    )


def test_scalar_sql_aggregates_by_the_leading_raw_sort_key_in_order() -> None:
    assert ' '.join(scalar_select('fresh').split()) == (
        "SELECT toInt32(length(splitByChar('/',p))) AS depth,p AS path,b,o FROM ( SELECT coalesce(path,'') AS p,"
        'sum(toInt128(size)) AS b,sum(toInt128(n_files)) AS o FROM fresh.raw GROUP BY p) SETTINGS optimize_aggregation_in_order=1'
    )


def test_fresh_table_schema_codecs_preserve_types_and_nullability_exactly() -> None:
    assert TABLE_COLUMNS == {
        'raw': "path Nullable(String) CODEC(ZSTD(1)),usr Nullable(String),kind Nullable(String),depth Nullable(Int32),size Nullable(Int64),n_files Nullable(Int64)",
        'scalar': 'depth Int32,path String CODEC(ZSTD(1)),b Nullable(Int128),o Nullable(Int128)',
        'ordered': 'id UInt32 CODEC(Delta(4),ZSTD(1)),depth UInt8,path String CODEC(ZSTD(1)),b UInt64,o UInt64',
        'intervals': 'id UInt32 CODEC(Delta(4),ZSTD(1)),pre UInt32 CODEC(Delta(4),ZSTD(1)),post UInt32 CODEC(Delta(4),ZSTD(1))',
        'nodes': 'pre UInt32 CODEC(Delta(4),ZSTD(1)),post UInt32 CODEC(Delta(4),ZSTD(1)),depth UInt8,path String CODEC(ZSTD(1)),b UInt64,o UInt64',
        'dictionary': 'pre UInt32 CODEC(Delta(4),ZSTD(1)),post UInt32 CODEC(Delta(4),ZSTD(1)),depth UInt8,path String CODEC(ZSTD(1))',
        'tree_sorted': 'depth UInt8,path String CODEC(ZSTD(1)),b UInt64,o UInt64',
    }


def test_physical_order_plan_uses_insert_block_sort_and_explicit_in_order_source() -> None:
    create, insert, number = physical_order_sql('fresh')
    assert create == 'CREATE TABLE fresh.tree_sorted (depth UInt8,path String CODEC(ZSTD(1)),b UInt64,o UInt64) ENGINE=MergeTree ORDER BY ' + tree_order_expr()
    assert ' '.join(insert.split()) == 'INSERT INTO fresh.tree_sorted SELECT assumeNotNull(toUInt8(depth)),assumeNotNull(path), assumeNotNull(toUInt64(b)),assumeNotNull(toUInt64(o)) FROM fresh.scalar'
    assert ' '.join(number.split()) == 'CREATE TABLE fresh.ordered (' + TABLE_COLUMNS['ordered'] + ') ENGINE=MergeTree ORDER BY id AS SELECT toUInt32(rowNumberInAllBlocks()) AS id,depth,path,b,o FROM (SELECT depth,path,b,o FROM fresh.tree_sorted ORDER BY ' + tree_order_expr() + ')'


def test_final_domain_stream_is_exact_across_arbitrary_chunk_boundaries() -> None:
    raw = b''.join(pack('<II', pre, post) for pre, post in [(0, 5), (1, 5), (2, 4), (3, 3), (4, 4), (5, 5)])
    assert audit_domain([raw[start:start + 3] for start in range(0, len(raw), 3)], 6) is None


@pytest.mark.parametrize('pairs,count,tail', [
    ([(0, 1), (0, 1)], 2, b''),
    ([(0, 2), (2, 2)], 3, b''),
    ([(0, 2), (1, 0)], 3, b''),
    ([(0, 3)], 3, b''),
    ([(0, 0)], 2, b''),
    ([(0, 1), (1, 1)], 1, b''),
    ([(0, 0)], 1, b'\x00'),
])
def test_final_domain_stream_refuses_gap_duplicate_bounds_count_and_truncation(pairs: list, count: int, tail: bytes) -> None:
    with pytest.raises(ValueError) as caught:
        audit_domain([b''.join(pack('<II', *pair) for pair in pairs) + tail], count)
    assert str(caught.value) == 'daily scalar final preorder/endpoint domain is incomplete'


def resume_fixture(tmp_path: Path) -> tuple:
    path = tmp_path / 'source'
    path.write_bytes(b'local')
    source = descriptor(path)
    ids = {key: 'daily_scalar_' + f'{index:032x}' for index, key in enumerate(('upload', 'scalar', 'root', 'failed_ordered'), 1)}
    evidence = {'schema': 'daily-scalar-resume-v1', 'target': 'fresh', 'prefix': '', 'source': source, 'queries': ids}
    sql = stage_sql('fresh')
    logs = [[ids[key], 'QueryFinish' if index < 3 else 'ExceptionBeforeStart', 0 if index < 3 else 241,
             sql[key] + '\n', [8, 6, 1, 0][index], 10 + index * 20, 20 + index * 20]
            for index, key in enumerate(ids)]
    replies = [
        [['raw'], ['scalar']],
        [['raw', name, typ] for name, typ in [('path', 'Nullable(String)'), ('usr', 'Nullable(String)'),
          ('kind', 'Nullable(String)'), ('depth', 'Nullable(Int32)'), ('size', 'Nullable(Int64)'), ('n_files', 'Nullable(Int64)')]] +
        [['scalar', name, typ] for name, typ in [('depth', 'Int32'), ('path', 'String'), ('b', 'Nullable(Int128)'), ('o', 'Nullable(Int128)')]],
        [['raw', 'MergeTree', "coalesce(path, ''), coalesce(usr, ''), coalesce(kind, '')"], ['scalar', 'MergeTree', 'depth, path']],
        [['raw', 'path', 'CODEC(ZSTD(1))'], ['scalar', 'path', 'CODEC(ZSTD(1))']],
        '0', '7', logs, [],
    ]
    calls = []

    def request(method: str, query) -> object:
        calls.append((method, query('own') if callable(query) else query))
        return replies[len(calls) - 1]

    return source, evidence, replies, calls, request


@pytest.mark.parametrize('failed_state', ['ExceptionBeforeStart', 'ExceptionWhileProcessing'])
def test_resume_accepts_only_exact_recorded_construction_and_preserves_authority_metadata(tmp_path: Path, failed_state: str) -> None:
    source, evidence, replies, calls, request = resume_fixture(tmp_path)
    replies[6][-1][1] = failed_state
    assert resume_evidence(request, 'fresh', source, 8, evidence) == {
        'schema': 'daily-scalar-resume-v1', 'source_descriptor_sha256': sha256(manifest_bytes(source)).hexdigest(),
        'queries': evidence['queries'], 'reused_stages': ['upload', 'scalar', 'root'],
        'validation': 'recorded statements and fresh structural audits; operator-pinned original input, not independent raw/source equality',
    }
    assert [method for method, _ in calls] == ['json', 'json', 'json', 'json', 'scalar', 'scalar', 'json', 'json']
    assert [' '.join(query.split()).split(' ', 1)[0] for _, query in calls] == ['SELECT'] * 8


@pytest.mark.parametrize('reply,value,error', [
    (0, [['raw'], ['scalar'], ['source_manifest']], 'daily scalar resume requires only retained raw/scalar tables and no later marker'),
    (1, [], 'daily scalar resume retained column types differ from construction'),
    (2, [['raw', 'Memory', ''], ['scalar', 'MergeTree', 'depth, path']], 'daily scalar resume retained engines/sorting keys differ from construction'),
    (3, [['raw', 'path', 'CODEC(LZ4)'], ['scalar', 'path', 'CODEC(ZSTD(1))']], 'daily scalar resume retained codecs differ from construction'),
    (4, '1', 'daily scalar resume refuses an active query on the retained database'),
    (6, [], 'daily scalar resume lacks the exact chronological terminal construction records'),
    (7, [['unrecorded_mutation']], 'daily scalar resume refuses unrecorded logical mutations after construction'),
])
def test_resume_guard_refuses_without_mutations(tmp_path: Path, reply: int, value: object, error: str) -> None:
    source, evidence, replies, calls, request = resume_fixture(tmp_path)
    replies[reply] = value
    with pytest.raises(ValueError) as caught:
        resume_evidence(request, 'fresh', source, 8, evidence)
    assert str(caught.value) == error
    assert [method for method, _ in calls] == ['json', 'json', 'json', 'json', 'scalar', 'scalar', 'json', 'json'][:reply + 1]


@pytest.mark.parametrize('column,value', [(1, 'QueryFinish'), (2, 242), (3, 'CREATE TABLE fresh.wrong (v UInt8) ENGINE=Memory'), (4, 1), (5, 0), (6, 69)])
def test_resume_failed_order_must_match_exact_sql_code_zero_writes_and_chronology(tmp_path: Path, column: int, value: object) -> None:
    source, evidence, replies, calls, request = resume_fixture(tmp_path)
    replies[6][-1][column] = value
    with pytest.raises(ValueError) as caught:
        resume_evidence(request, 'fresh', source, 8, evidence)
    assert str(caught.value) == 'daily scalar resume construction SQL/status/count/time differs from evidence'
    assert len(calls) == 7


def test_streamed_parent_rollup_audit_and_endpoint_encoder_exact_chunk_boundaries() -> None:
    raw = encoded(ROWS)
    chunks = [raw[start:start + 3] for start in range(0, len(raw), 3)]
    out = b''.join(preorder_intervals(audited_preorder(chunks, 6, ''), 6))
    assert sorted(iter_unpack('<III', out)) == [(0, 0, 5), (1, 1, 5), (2, 2, 4), (3, 3, 3), (4, 4, 4), (5, 5, 5)]


@pytest.mark.parametrize('step', [1, 3, 21, 65536])
@pytest.mark.parametrize('rows,prefix,endpoints', [
    (ROWS, '', [(3, 3, 3), (4, 4, 4), (2, 2, 4), (5, 5, 5), (1, 1, 5), (0, 0, 5)]),
    ([(0, 2, 15, 4, 'a/run.json'), (1, 3, 5, 1, 'a/run.json/z'), (2, 3, 10, 1, 'a/run.json/Å😀')],
     'a/run.json', [(1, 1, 1), (2, 2, 2), (0, 0, 2)]),
    ([(0, 0, 0, 0, '')], '', [(0, 0, 0)]),
    ([(0, 0, 0, 3, ''), (1, 1, 0, 2, 'a'), (2, 2, 0, 1, 'a/x'), (3, 1, 0, 1, 'b')],
     '', [(2, 2, 2), (1, 1, 2), (3, 3, 3), (0, 0, 3)]),
])
def test_fused_interval_emitter_exact_legacy_bytes_and_endpoints(rows: list, prefix: str, endpoints: list, step: int) -> None:
    raw = encoded(rows)
    chunks = [raw[start:start + step] for start in range(0, len(raw), step)]
    legacy = b''.join(preorder_intervals(audited_preorder(chunks, len(rows), prefix), len(rows)))
    fused = b''.join(audited_intervals(chunks, len(rows), prefix))
    assert fused == legacy == b''.join(pack('<III', *row) for row in endpoints)


@pytest.mark.parametrize('rows,count,error', [
    ([ROWS[0], (1, 2, 15, 5, 'a/missing')], 2, 'daily scalar is missing an immediate parent'),
    ([ROWS[0], ROWS[1], (2, 2, 15, 4, 'b/run')], 3, 'daily scalar is missing an immediate parent'),
    ([(0, 0, 0, 0, ''), (1, 1, 1, 1, 'a')], 2, 'daily scalar has an invalid recursive rollup/own contribution'),
    ([(0, 0, 5, 0, '')], 1, 'daily scalar has an invalid recursive rollup/own contribution'),
    ([ROWS[0], (2, 1, 15, 5, 'a')], 2, 'daily scalar preorder has invalid IDs/depth/order/count'),
    (ROWS[:2], 3, 'daily scalar preorder count differs from the complete source'),
    ([ROWS[0], ROWS[1], (2, 2, 0, 1, 'a/run.json-b'), ROWS[2]], 4, 'daily scalar preorder has invalid IDs/depth/order/count'),
])
def test_invalid_complete_preorder_refuses(rows: list[tuple], count: int, error: str) -> None:
    errors = []
    for fused in (False, True):
        chunks = [encoded(rows)]
        output = audited_intervals(chunks, count, '') if fused else preorder_intervals(audited_preorder(chunks, count, ''), count)
        with pytest.raises(ValueError) as caught:
            b''.join(output)
        errors.append(str(caught.value))
    assert errors == [error, error]


def test_truncated_source_never_accepts_partial() -> None:
    errors = []
    for fused in (False, True):
        chunks = [encoded(ROWS)[:-1]]
        output = audited_intervals(chunks, 6, '') if fused else preorder_intervals(audited_preorder(chunks, 6, ''), 6)
        with pytest.raises(ValueError) as caught:
            b''.join(output)
        errors.append(str(caught.value))
    assert errors == ['daily scalar has a truncated RowBinary node'] * 2


def test_descriptor_and_manifest_bytes_are_precise(tmp_path: Path) -> None:
    path = tmp_path / 'source'
    path.write_bytes(b'local')
    body = descriptor(path)
    assert _descriptor(body) == body
    assert manifest_bytes({'z': 'Å', 'a': True}) == b'{"a":true,"z":"\xc3\x85"}\n'


@pytest.mark.parametrize('kwargs,error', [
    ({'max_nodes': True}, 'node cap must be a positive integer'),
    ({'max_nodes': 1 << 32}, 'daily scalar node cap must fit UInt32'),
    ({'min_free_bytes': 0}, 'reserve bytes must be a positive integer'),
    ({'prefix': '/a'}, 'daily scalar prefix must be a canonical complete subtree'),
    ({'order_plan': 'implicit'}, 'daily scalar order plan must be window or physical'),
    ({'sort_spill_bytes': True}, 'sort spill bytes must be a positive integer'),
    ({'sort_spill_bytes': (2 << 30) + 1}, 'daily scalar sort spill threshold must not exceed one quarter of its memory cap'),
    ({'resume': {}, 'prefix': 'a'}, 'daily scalar resume only supports the original complete global scope'),
])
def test_invalid_budgets_refuse_before_server_io(tmp_path: Path, kwargs: dict, error: str) -> None:
    path = tmp_path / 'source'
    path.write_bytes(b'local')
    with pytest.raises(ValueError) as caught:
        build(SimpleNamespace(), 'fresh', path, descriptor(path), **kwargs)
    assert str(caught.value) == error


def test_source_hash_refuses_before_any_database_write(tmp_path: Path) -> None:
    path = tmp_path / 'source'
    path.write_bytes(b'local')
    calls = []
    client = SimpleNamespace(close=lambda: calls.append('close'))
    source = {**descriptor(path), 'sha256': '0' * 64}
    with pytest.raises(ValueError) as caught:
        build(SimpleNamespace(fork=lambda **settings: client), 'fresh', path, source)
    assert (str(caught.value), calls) == ('daily scalar local source length/SHA256 differs from its descriptor', ['close'])


def write_fixture(path: Path, mutation: str = '') -> None:
    # Two owners, plus own zero-byte objects on the matching directory path.
    rows = [('a', 'u', 'dir', 1, 15, 4), ('a', None, 'dir', 1, 0, 1),
            ('a/run.json', 'u', 'dir', 2, 15, 3), ('a/run.json', 'u', 'file', 2, 0, 1),
            ('a/run.json/Å😀', 'u', 'file', 3, 10, 1), ('a/run.json/z', 'u', 'file', 3, 5, 1),
            ('a/run.json-b', None, 'file', 2, 0, 1), ('b', None, 'file', 1, 0, 1)]
    if mutation == 'missing_parent':
        rows = [row for row in rows if row[0] != 'a/run.json']
    elif mutation == 'negative':
        rows[-1] = (*rows[-1][:4], -1, 1)
    elif mutation == 'duplicate':
        rows.append(rows[-1])
    elif mutation == 'rollup':
        rows[2] = (*rows[2][:4], 14, 3)
    elif mutation == 'depth':
        rows[-1] = (*rows[-1][:3], 2, *rows[-1][4:])
    columns = dict(zip(('path', 'usr', 'kind', 'depth', 'size', 'n_files'), zip(*rows), strict=True))
    pq.write_table(pa.table(columns), path, row_group_size=2)


def test_local_arrow_complete_prefix_exact_slices_and_rowgroup_pruning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pyarrow.ipc as ipc
    from io import BytesIO
    from dt_cloud.chstore.daily_scalar import _arrow

    path = tmp_path / 'path-index.parquet'
    write_fixture(path)
    original, groups = pq.ParquetFile.iter_batches, []

    def batches(self, *args, **kwargs):
        groups.append(kwargs['row_groups'])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pq.ParquetFile, 'iter_batches', batches)
    with path.open('rb') as file:
        data = b''.join(_arrow(file, 'a/run.json'))
    table = ipc.open_stream(BytesIO(data)).read_all()
    assert table.to_pylist() == [
        {'path': 'a/run.json', 'usr': 'u', 'kind': 'dir', 'depth': 2, 'size': 15, 'n_files': 3},
        {'path': 'a/run.json', 'usr': 'u', 'kind': 'file', 'depth': 2, 'size': 0, 'n_files': 1},
        {'path': 'a/run.json/Å😀', 'usr': 'u', 'kind': 'file', 'depth': 3, 'size': 10, 'n_files': 1},
        {'path': 'a/run.json/z', 'usr': 'u', 'kind': 'file', 'depth': 3, 'size': 5, 'n_files': 1},
    ]
    assert groups == [[1, 2, 3]]


@pytest.fixture
def fresh(ch_url: str) -> tuple[Ch, str]:
    ch = Ch(ch_url)
    target = 'daily_fixture_' + uuid4().hex[:12]
    yield ch, target
    ch.exec(f'DROP DATABASE IF EXISTS {target} SYNC')
    ch.close()


def test_native_scalar_collapse_exact_counts_and_in_order_aggregation_pipeline(fresh: tuple) -> None:
    ch, target = fresh
    ch.exec(f'CREATE DATABASE {target}')
    ch.exec(f"CREATE TABLE {target}.raw (path Nullable(String),usr Nullable(String),kind Nullable(String),depth Nullable(Int32),size Nullable(Int64),n_files Nullable(Int64)) ENGINE=MergeTree ORDER BY (coalesce(path,''),coalesce(usr,''),coalesce(kind,''))")
    ch.exec(f"INSERT INTO {target}.raw VALUES ('a',NULL,'dir',1,0,1),('a','u','dir',1,15,4),('a/run.json','u','dir',2,15,3),('a/run.json','u','file',2,0,1)")
    rows = ch.json(scalar_select(target) + ' ', settings={'max_threads': 2, 'max_block_size': 2})
    assert sorted([[depth, path, int(b), int(o)] for depth, path, b, o in rows]) == [[1, 'a', 15, 5], [2, 'a/run.json', 15, 4]]
    plan = ch.exec('EXPLAIN PIPELINE compact=0 ' + scalar_select(target), settings={'max_threads': 2})
    assert sorted(set(findall(r'^\s*([A-Za-z]*Aggregat[A-Za-z]*Transform)\b', plan, flags=MULTILINE))) == [
        'AggregatingInOrderTransform', 'FinalizeAggregatedTransform',
    ]


def test_native_semantic_ordinal_exact_for_reversed_input_and_two_row_blocks(fresh: tuple) -> None:
    ch, target = fresh
    ch.exec(f'CREATE DATABASE {target}')
    ch.exec(f'CREATE TABLE {target}.scalar (depth UInt8,path String,b UInt64,o UInt64) ENGINE=MergeTree ORDER BY (depth,path)')
    from dt_cloud.chstore.client import lit
    values = ','.join(f'({depth},{lit(path)},{b},{o})' for _, depth, b, o, path in reversed(ROWS))
    ch.exec(f'INSERT INTO {target}.scalar VALUES {values}')
    rows = ch.json(ordered_select(target) + ' ORDER BY id', settings={'max_threads': 2, 'max_block_size': 2})
    assert rows == [[node, depth, path, b, o] for node, depth, b, o, path in ROWS]


def test_native_physical_order_has_exact_ids_across_parts_blocks_and_no_global_sort_window(fresh: tuple) -> None:
    from dt_cloud.chstore.client import lit
    ch, target = fresh
    ch.exec(f'CREATE DATABASE {target}')
    ch.exec(physical_order_sql(target)[0])
    for start in range(0, len(ROWS), 2):
        values = ','.join(f'({depth},{lit(path)},{b},{o})' for _, depth, b, o, path in reversed(ROWS[start:start + 2]))
        ch.exec(f'INSERT INTO {target}.tree_sorted VALUES {values}')
    select = ordered_select(target, order_plan='physical')
    rows = ch.json(select, settings={'max_threads': 1, 'max_block_size': 2})
    assert rows == [[node, depth, path, b, o] for node, depth, b, o, path in ROWS]
    plan = ch.exec('EXPLAIN PIPELINE compact=0 ' + select, settings={'max_threads': 1, 'max_block_size': 2})
    assert sorted(set(findall(r'MergeTreeSelect\(pool: ([A-Za-z]+), algorithm: ([A-Za-z]+)\)', plan))) == [('ReadPoolInOrder', 'InOrder')], plan
    processors = findall(r'\b([A-Za-z]*(?:Sort|Window)[A-Za-z]*Transform)\b', plan)
    assert sorted(set(p for p in processors if p != 'MergingSortedTransform')) == [], plan
    # PIPELINE prints consumers above inputs. The counter's outer Expression
    # must follow the in-order source/part merge in the execution direction.
    expressions = findall(r'^\s*(ExpressionTransform)\b', plan, flags=MULTILINE)
    assert bool(expressions) is True, plan


def test_native_ordered_table_types_and_exact_binary_wire_match_json(fresh: tuple) -> None:
    ch, target = fresh
    ch.exec(f'CREATE DATABASE {target}')
    # Mirror aggregation's nullable arithmetic, which JSON conceals but
    # RowBinary prefixes with null flags unless the wire explicitly unwraps it.
    ch.exec(f'CREATE TABLE {target}.scalar (depth Int32,path String,b Nullable(Int128),o Nullable(Int128)) ENGINE=MergeTree ORDER BY (depth,path)')
    from dt_cloud.chstore.client import lit
    values = ','.join(f'({depth},{lit(path)},{b},{o})' for _, depth, b, o, path in reversed(ROWS))
    ch.exec(f'INSERT INTO {target}.scalar VALUES {values}')
    ch.exec(f'CREATE TABLE {target}.ordered ENGINE=MergeTree ORDER BY id AS {ordered_select(target)}')
    assert ch.json(f'SELECT toTypeName(id),toTypeName(depth),toTypeName(b),toTypeName(o),toTypeName(path) FROM {target}.ordered LIMIT 1') == [
        ['UInt32', 'UInt8', 'UInt64', 'UInt64', 'String'],
    ]
    chunks = ch.stream(wire_select(target), fmt='RowBinary', settings={'max_block_size': 2})
    assert list(_records(chunks)) == ROWS


@pytest.mark.parametrize('order_plan', ['window', 'physical'])
def test_native_date_only_scalar_exact_body_and_no_rich_history_tables(fresh: tuple, tmp_path: Path, order_plan: str) -> None:
    ch, target = fresh
    path = tmp_path / 'path-index.parquet'
    write_fixture(path)
    progress = []
    body = build(ch, target, path, descriptor(path), min_free_bytes=1, progress=progress.append, order_plan=order_plan)
    assert progress == [
        'daily scalar hash_before: started', 'daily scalar hash_before: complete',
        'daily scalar raw: started', 'daily scalar raw: complete',
        'daily scalar upload: started', 'daily scalar upload: complete',
        'daily scalar source_audit: started', 'daily scalar source_audit: complete',
        'daily scalar scalar: started', 'daily scalar scalar: complete',
        *(['daily scalar tree_sorted_table: started', 'daily scalar tree_sorted_table: complete',
           'daily scalar tree_sorted: started', 'daily scalar tree_sorted: complete'] if order_plan == 'physical' else []),
        'daily scalar ordered: started', 'daily scalar ordered: complete',
        'daily scalar interval_table: started', 'daily scalar interval_table: complete',
        'daily scalar intervals: started', 'daily scalar intervals: complete',
        'daily scalar nodes: started', 'daily scalar nodes: complete',
        'daily scalar dictionary: started', 'daily scalar dictionary: complete',
        'daily scalar hash_after: started', 'daily scalar hash_after: complete',
        'daily scalar source_marker: started', 'daily scalar source_marker: complete',
        'daily scalar accepted_source_marker: complete',
    ]
    assert {key: value for key, value in body.items() if key not in ('stages', 'limits')} == {
        'schema': 'daily-scalar-source-v1', 'complete': True, 'logical_store': 'gcs', 'date': '2026-10-06',
        'target': target, 'snapshot_db': target, 'prefix': '', 'source': descriptor(path), 'source_rows': 8,
        'selected_source_rows': 8, 'nodes': 7, 'root': {'path': '', 'pre': 0, 'post': 6, 'b': 15, 'o': 6},
        'buckets': [{'path': 'a', 'pre': 1, 'post': 5}, {'path': 'b', 'pre': 6, 'post': 6}],
        'validation': {'source_hash_checked': True, 'prefix_closed': True, 'interval_endpoints_checked': True, 'scalar_rollups_checked': True},
    }
    assert ch.json(f'SELECT pre,post,depth,path,b,o FROM {target}.nodes ORDER BY pre') == [
        [0, 6, 0, '', 15, 6], [1, 5, 1, 'a', 15, 5], [2, 4, 2, 'a/run.json', 15, 4],
        [3, 3, 3, 'a/run.json/z', 5, 1], [4, 4, 3, 'a/run.json/Å😀', 10, 1], [5, 5, 2, 'a/run.json-b', 0, 1],
        [6, 6, 1, 'b', 0, 1],
    ]
    assert loads(ch.scalar(f'SELECT doc FROM {target}.source_manifest')) == body
    assert ch.rows(f'SHOW TABLES FROM {target}') == [[name] for name in ['dictionary', 'intervals', 'nodes', 'ordered', 'raw', 'scalar', 'source_manifest'] + (['tree_sorted'] if order_plan == 'physical' else [])]
    assert ch.rows(f"SELECT table,name,type,compression_codec FROM system.columns WHERE database='{target}' AND name IN ('path','id','pre','post') ORDER BY table,position") == [
        ['dictionary', 'pre', 'UInt32', 'CODEC(Delta(4), ZSTD(1))'], ['dictionary', 'post', 'UInt32', 'CODEC(Delta(4), ZSTD(1))'], ['dictionary', 'path', 'String', 'CODEC(ZSTD(1))'],
        ['intervals', 'id', 'UInt32', 'CODEC(Delta(4), ZSTD(1))'], ['intervals', 'pre', 'UInt32', 'CODEC(Delta(4), ZSTD(1))'], ['intervals', 'post', 'UInt32', 'CODEC(Delta(4), ZSTD(1))'],
        ['nodes', 'pre', 'UInt32', 'CODEC(Delta(4), ZSTD(1))'], ['nodes', 'post', 'UInt32', 'CODEC(Delta(4), ZSTD(1))'], ['nodes', 'path', 'String', 'CODEC(ZSTD(1))'],
        ['ordered', 'id', 'UInt32', 'CODEC(Delta(4), ZSTD(1))'], ['ordered', 'path', 'String', 'CODEC(ZSTD(1))'],
        ['raw', 'path', 'Nullable(String)', 'CODEC(ZSTD(1))'], ['scalar', 'path', 'String', 'CODEC(ZSTD(1))'],
    ] + ([['tree_sorted', 'path', 'String', 'CODEC(ZSTD(1))']] if order_plan == 'physical' else [])
    assert ch.rows(f"SELECT name,type FROM system.columns WHERE database='{target}' AND table='scalar' ORDER BY position") == [
        ['depth', 'Int32'], ['path', 'String'], ['b', 'Nullable(Int128)'], ['o', 'Nullable(Int128)'],
    ]


def test_native_resume_exact_querylog_guard_and_physical_continuation(fresh: tuple, tmp_path: Path) -> None:
    from dt_cloud.chstore.daily_scalar import _arrow

    ch, target = fresh
    path = tmp_path / 'path-index.parquet'
    write_fixture(path)
    source = descriptor(path)
    sql = stage_sql(target)
    ids = {key: 'daily_scalar_' + uuid4().hex for key in sql}
    evidence = {'schema': 'daily-scalar-resume-v1', 'target': target, 'prefix': '', 'source': source, 'queries': ids}
    ch.exec(f'CREATE DATABASE {target}')
    ch.exec(f"CREATE TABLE {target}.raw ({TABLE_COLUMNS['raw']}) ENGINE=MergeTree ORDER BY (coalesce(path,''),coalesce(usr,''),coalesce(kind,''))")
    with path.open('rb') as file:
        ch.insert(sql['upload'], _arrow(file, ''), settings={'query_id': ids['upload']})
    ch.exec(sql['scalar'], settings={'query_id': ids['scalar']})
    ch.exec(sql['root'], settings={'query_id': ids['root']})
    # The exact same construction SQL fails under a query-scoped low memory
    # limit. Do not fabricate log rows or remove a possibly created table.
    with pytest.raises(ChError) as caught:
        ch.exec(sql['failed_ordered'], settings={'query_id': ids['failed_ordered'], 'max_memory_usage': 1})
    code = search(r'Code: (\d+)\.', caught.value.text)
    assert (code.group(1) if code else None) == '241', caught.value.text
    assert ch.scalar(f'EXISTS TABLE {target}.ordered') == '0'
    ch.exec('SYSTEM FLUSH LOGS')
    body = build(ch, target, path, source, resume=evidence, order_plan='physical', sort_spill_bytes=1 << 30, min_free_bytes=1)
    assert body['limits']['recovery'] == {
        'schema': 'daily-scalar-resume-v1', 'source_descriptor_sha256': sha256(manifest_bytes(source)).hexdigest(),
        'queries': ids, 'reused_stages': ['upload', 'scalar', 'root'],
        'validation': 'recorded statements and fresh structural audits; operator-pinned original input, not independent raw/source equality',
    }
    assert sorted(body['stages']) == ['dictionary', 'interval_table', 'intervals', 'nodes', 'ordered', 'tree_sorted', 'tree_sorted_table']
    assert (body['limits']['order_plan'], body['limits']['sort_spill_bytes'], body['nodes'], body['root'], body['buckets']) == (
        'physical', 1 << 30, 7, {'path': '', 'pre': 0, 'post': 6, 'b': 15, 'o': 6},
        [{'path': 'a', 'pre': 1, 'post': 5}, {'path': 'b', 'pre': 6, 'post': 6}],
    )
    assert ch.json(f'SELECT pre,post,depth,path,b,o FROM {target}.nodes ORDER BY pre') == [
        [0, 6, 0, '', 15, 6], [1, 5, 1, 'a', 15, 5], [2, 4, 2, 'a/run.json', 15, 4],
        [3, 3, 3, 'a/run.json/z', 5, 1], [4, 4, 3, 'a/run.json/Å😀', 10, 1], [5, 5, 2, 'a/run.json-b', 0, 1],
        [6, 6, 1, 'b', 0, 1],
    ]
    assert loads(ch.scalar(f'SELECT doc FROM {target}.source_manifest')) == body


def test_native_complete_prefix_is_not_sample_and_preserves_directory_own_objects(fresh: tuple, tmp_path: Path) -> None:
    ch, target = fresh
    path = tmp_path / 'path-index.parquet'
    write_fixture(path)
    body = build(ch, target, path, descriptor(path), prefix='a/run.json', max_nodes=3, min_free_bytes=1)
    assert (body['prefix'], body['selected_source_rows'], body['nodes'], body['root'], body['buckets']) == (
        'a/run.json', 4, 3, {'path': 'a/run.json', 'pre': 0, 'post': 2, 'b': 15, 'o': 4}, [],
    )


@pytest.mark.parametrize('mutation,error', [
    ('negative', 'daily scalar source failed UTF-8/depth/nonnegative v2 audit'),
    ('duplicate', 'daily scalar source contains duplicate path/owner/kind slices'),
    ('missing_parent', 'daily scalar is missing an immediate parent'),
    ('rollup', 'daily scalar has an invalid recursive rollup/own contribution'),
    ('depth', 'daily scalar source failed UTF-8/depth/nonnegative v2 audit'),
])
def test_native_malformed_source_retains_owned_partial_but_no_completed_marker(fresh: tuple, tmp_path: Path, mutation: str, error: str) -> None:
    ch, target = fresh
    path = tmp_path / 'path-index.parquet'
    write_fixture(path, mutation)
    with pytest.raises(ValueError) as caught:
        build(ch, target, path, descriptor(path), min_free_bytes=1)
    assert str(caught.value) == error
    assert ch.scalar(f'EXISTS DATABASE {target}') == '1'
    assert ch.scalar(f'EXISTS TABLE {target}.source_manifest') == '0'


def test_native_existing_database_never_changes(fresh: tuple, tmp_path: Path) -> None:
    ch, target = fresh
    ch.exec(f'CREATE DATABASE {target}')
    ch.exec(f'CREATE TABLE {target}.sentinel (value UInt8) ENGINE=TinyLog')
    ch.exec(f'INSERT INTO {target}.sentinel VALUES (7)')
    path = tmp_path / 'path-index.parquet'
    write_fixture(path)
    with pytest.raises(ValueError) as caught:
        build(ch, target, path, descriptor(path), min_free_bytes=1)
    assert str(caught.value) == 'daily scalar refuses an existing database'
    assert (ch.rows(f'SHOW TABLES FROM {target}'), ch.rows(f'SELECT * FROM {target}.sentinel')) == ([['sentinel']], [['7']])


@pytest.mark.parametrize('limits,error', [
    ({'max_nodes': 6}, 'daily scalar complete subtree exceeds its node cap'),
    ({'max_owned_bytes': 1}, 'daily scalar owned database exceeds 1-byte stage budget; partial build retained'),
])
def test_native_complete_scope_budget_failure_never_has_marker(fresh: tuple, tmp_path: Path, limits: dict, error: str) -> None:
    ch, target = fresh
    path = tmp_path / 'path-index.parquet'
    write_fixture(path)
    with pytest.raises(ValueError) as caught:
        build(ch, target, path, descriptor(path), min_free_bytes=1, **limits)
    assert str(caught.value) == error
    assert ch.scalar(f'EXISTS TABLE {target}.source_manifest') == '0'
