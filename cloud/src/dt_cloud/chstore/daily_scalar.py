"""Isolated date-only scalar construction from one pinned local v2 path sort.

This is not ingestion, history or publication. A completed marker accepts the
supplied path-store tree, not an independently verified object-store inventory.
Owned intermediate tables are retained on failure; budgets are checked at
stage boundaries in addition to statement memory/spill/time limits.
"""

from dataclasses import dataclass
from datetime import date as Date
from hashlib import sha256
from io import BytesIO
from itertools import chain
from json import dumps
from pathlib import Path
from re import fullmatch
from struct import Struct
from time import monotonic
from typing import BinaryIO, Callable, Iterable, Iterator
from uuid import uuid4

from .client import Ch, lit
from .narrow import identifier, tree_order_expr

U64 = (1 << 64) - 1
FIXED = Struct('<IBQQ')
ID_DEPTH = Struct('<IB')
INTERVAL = Struct('<III')
DOMAIN = Struct('<II')
TABLE_COLUMNS = {
    'raw': "path Nullable(String) CODEC(ZSTD(1)),usr Nullable(String),kind Nullable(String),depth Nullable(Int32),size Nullable(Int64),n_files Nullable(Int64)",
    'scalar': 'depth Int32,path String CODEC(ZSTD(1)),b Nullable(Int128),o Nullable(Int128)',
    'ordered': 'id UInt32 CODEC(Delta(4),ZSTD(1)),depth UInt8,path String CODEC(ZSTD(1)),b UInt64,o UInt64',
    'intervals': 'id UInt32 CODEC(Delta(4),ZSTD(1)),pre UInt32 CODEC(Delta(4),ZSTD(1)),post UInt32 CODEC(Delta(4),ZSTD(1))',
    'nodes': 'pre UInt32 CODEC(Delta(4),ZSTD(1)),post UInt32 CODEC(Delta(4),ZSTD(1)),depth UInt8,path String CODEC(ZSTD(1)),b UInt64,o UInt64',
    'dictionary': 'pre UInt32 CODEC(Delta(4),ZSTD(1)),post UInt32 CODEC(Delta(4),ZSTD(1)),depth UInt8,path String CODEC(ZSTD(1))',
    'tree_sorted': 'depth UInt8,path String CODEC(ZSTD(1)),b UInt64,o UInt64',
}


def manifest_bytes(body: dict) -> bytes:
    return (dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')


def ordered_select(target: str, *, order_plan: str = 'window') -> str:
    identifier(target)
    if order_plan == 'physical':
        return f'''SELECT toUInt32(rowNumberInAllBlocks()) AS id,depth,path,b,o
            FROM (SELECT depth,path,b,o FROM {target}.tree_sorted ORDER BY {tree_order_expr()})'''
    if order_plan != 'window':
        raise ValueError('daily scalar order plan must be window or physical')
    # A scalar block counter may be computed before a nested ORDER BY. The
    # window specifies the semantic rank, independent of optimizer evaluation
    # order and parallel source blocks. Its ROWS frame needs no future rows.
    return f'''SELECT toUInt32(row_number() OVER (ORDER BY {tree_order_expr()}
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)-1) AS id,
        assumeNotNull(toUInt8(depth)) AS depth,assumeNotNull(path) AS path,
        assumeNotNull(toUInt64(b)) AS b,assumeNotNull(toUInt64(o)) AS o FROM {target}.scalar'''


def wire_select(target: str) -> str:
    identifier(target)
    # RowBinary Nullable(T) carries an extra byte even for non-null values.
    # JSON parity cannot establish this fixed-width protocol's field types.
    return f'''SELECT assumeNotNull(toUInt32(id)),assumeNotNull(toUInt8(depth)),assumeNotNull(toUInt64(b)),assumeNotNull(toUInt64(o)),assumeNotNull(path) FROM {target}.ordered ORDER BY id'''


def scalar_select(target: str) -> str:
    identifier(target)
    # The validated depth is a function of path. Group only on the raw table's
    # leading sort expression so equal-path owner/kind slices can be reduced
    # in order, rather than retaining a fleet-sized final hash aggregation.
    return f'''SELECT toInt32(length(splitByChar('/',p))) AS depth,p AS path,b,o FROM (
        SELECT coalesce(path,'') AS p,sum(toInt128(size)) AS b,sum(toInt128(n_files)) AS o
        FROM {target}.raw GROUP BY p) SETTINGS optimize_aggregation_in_order=1'''


def stage_sql(target: str) -> dict[str, str]:
    """Shared exact mutation statements for fresh construction and adoption."""
    identifier(target)
    return {
        'upload': f"INSERT INTO {target}.raw SELECT * FROM input('path Nullable(String),usr Nullable(String),kind Nullable(String),depth Nullable(Int32),size Nullable(Int64),n_files Nullable(Int64)') FORMAT ArrowStream",
        'scalar': f"CREATE TABLE {target}.scalar ({TABLE_COLUMNS['scalar']}) ENGINE=MergeTree ORDER BY (depth,path) AS {scalar_select(target)}",
        'root': f"INSERT INTO {target}.scalar SELECT toInt32(0), '', sum(b),sum(o) FROM {target}.scalar WHERE depth=1",
        'failed_ordered': f"CREATE TABLE {target}.ordered ({TABLE_COLUMNS['ordered']}) ENGINE=MergeTree ORDER BY id AS {ordered_select(target)}",
    }


def physical_order_sql(target: str) -> tuple[str, str, str]:
    identifier(target)
    return (
        f"CREATE TABLE {target}.tree_sorted ({TABLE_COLUMNS['tree_sorted']}) ENGINE=MergeTree ORDER BY {tree_order_expr()}",
        f'''INSERT INTO {target}.tree_sorted SELECT assumeNotNull(toUInt8(depth)),assumeNotNull(path),
            assumeNotNull(toUInt64(b)),assumeNotNull(toUInt64(o)) FROM {target}.scalar''',
        f"CREATE TABLE {target}.ordered ({TABLE_COLUMNS['ordered']}) ENGINE=MergeTree ORDER BY id AS {ordered_select(target, order_plan='physical')}",
    )


def resume_evidence(
    request: Callable,
    target: str,
    descriptor: dict,
    source_rows: int,
    evidence: dict,
) -> dict:
    """Adopt only recorded raw/scalar construction, under operator authority.

    Query logs prove successful statement identities/counts, not the bytes of
    an ArrowStream file. The saved original descriptor is explicit provenance
    authority; this is not an independent raw-to-Parquet contents comparison.
    """
    if (not isinstance(evidence, dict) or set(evidence) != {'schema', 'target', 'prefix', 'source', 'queries'} or
            evidence['schema'] != 'daily-scalar-resume-v1' or evidence['target'] != target or evidence['prefix'] != '' or
            evidence['source'] != descriptor or not isinstance(evidence['queries'], dict) or
            set(evidence['queries']) != {'upload', 'scalar', 'root', 'failed_ordered'}):
        raise ValueError('daily scalar resume requires explicit original global source/query evidence')
    queries = evidence['queries']
    if (any(not isinstance(q, str) or not fullmatch('daily_scalar_[a-f0-9]{32}', q) for q in queries.values()) or len(set(queries.values())) != 4):
        raise ValueError('daily scalar resume requires four unique owned construction query IDs')
    if request('json', f'SELECT name FROM system.tables WHERE database={lit(target)} ORDER BY name') != [['raw'], ['scalar']]:
        raise ValueError('daily scalar resume requires only retained raw/scalar tables and no later marker')
    wanted_types = [['raw', name, typ] for name, typ in [('path', 'Nullable(String)'), ('usr', 'Nullable(String)'), ('kind', 'Nullable(String)'),
                    ('depth', 'Nullable(Int32)'), ('size', 'Nullable(Int64)'), ('n_files', 'Nullable(Int64)')]] + [
                    ['scalar', name, typ] for name, typ in [('depth', 'Int32'), ('path', 'String'), ('b', 'Nullable(Int128)'), ('o', 'Nullable(Int128)')]]
    actual_types = request('json', f'SELECT table,name,type FROM system.columns WHERE database={lit(target)} ORDER BY table,position')
    if actual_types != wanted_types:
        raise ValueError('daily scalar resume retained column types differ from construction')
    engines = request('json', f'SELECT name,engine,sorting_key FROM system.tables WHERE database={lit(target)} ORDER BY name')
    expected_engines = [['raw', 'MergeTree', "coalesce(path, ''), coalesce(usr, ''), coalesce(kind, '')"], ['scalar', 'MergeTree', 'depth, path']]
    if engines != expected_engines:
        raise ValueError('daily scalar resume retained engines/sorting keys differ from construction')
    codecs = request('json', f"SELECT table,name,compression_codec FROM system.columns WHERE database={lit(target)} AND name='path' ORDER BY table")
    if codecs != [['raw', 'path', 'CODEC(ZSTD(1))'], ['scalar', 'path', 'CODEC(ZSTD(1))']]:
        raise ValueError('daily scalar resume retained codecs differ from construction')
    active = request('scalar', lambda own: f"SELECT count() FROM system.processes WHERE query_id!={lit(own)} AND (current_database={lit(target)} OR position(query,{lit(target + '.')})>0 OR position(query,{lit('`' + target + '`.')})>0)")
    if active != '0':
        raise ValueError('daily scalar resume refuses an active query on the retained database')
    count = int(request('scalar', f'SELECT count() FROM {target}.scalar'))
    selected = ','.join(map(lit, queries.values()))
    rows = request('json', f'''SELECT query_id,toString(type),exception_code,query,written_rows,
        toUnixTimestamp64Micro(query_start_time_microseconds),toUnixTimestamp64Micro(event_time_microseconds)
        FROM system.query_log WHERE query_id IN ({selected}) AND type IN ('QueryFinish','ExceptionBeforeStart','ExceptionWhileProcessing')
        ORDER BY query_start_time_microseconds''')
    sql, expected_ids, previous_end = stage_sql(target), [queries[key] for key in ('upload', 'scalar', 'root', 'failed_ordered')], -1
    if len(rows) != 4 or [r[0] for r in rows if len(r) == 7] != expected_ids:
        raise ValueError('daily scalar resume lacks the exact chronological terminal construction records')
    for index, (key, row) in enumerate(zip(('upload', 'scalar', 'root', 'failed_ordered'), rows, strict=True)):
        qid, state, code, query, written, start, end = row
        if (not isinstance(query, str) or any(type(n) is not int or n < 0 for n in (code, written, start, end)) or
                start < previous_end or end < start or ' '.join(query.split()) != ' '.join(sql[key].split()) or
                (index < 3 and (state != 'QueryFinish' or code != 0 or written != (source_rows if key == 'upload' else count - 1 if key == 'scalar' else 1))) or
                (index == 3 and (state not in ('ExceptionBeforeStart', 'ExceptionWhileProcessing') or code != 241 or written != 0))):
            raise ValueError('daily scalar resume construction SQL/status/count/time differs from evidence')
        previous_end = end
    # Refuse successful or failed logical mutations other than the four owned
    # records. Broad DB references are conservative: even a copy out is refused.
    changes = request('json', f'''SELECT query_id FROM system.query_log
        WHERE toUnixTimestamp64Micro(query_start_time_microseconds)>={rows[0][5]}
        AND type IN ('QueryFinish','ExceptionBeforeStart','ExceptionWhileProcessing')
        AND query_kind IN ('Insert','Create','Alter','Drop','Truncate','Delete','Update','Rename','Restore')
        AND (has(databases,{lit(target)}) OR position(query,{lit(target + '.')})>0 OR position(query,{lit('`' + target + '`.')})>0)
        AND query_id NOT IN ({selected}) LIMIT 1''')
    if changes:
        raise ValueError('daily scalar resume refuses unrecorded logical mutations after construction')
    return {'schema': 'daily-scalar-resume-v1', 'source_descriptor_sha256': sha256(manifest_bytes(descriptor)).hexdigest(),
            'queries': dict(queries), 'reused_stages': ['upload', 'scalar', 'root'],
            'validation': 'recorded statements and fresh structural audits; operator-pinned original input, not independent raw/source equality'}


def audit_domain(chunks: Iterable[bytes], count: int) -> None:
    """Exact dense preorder/domain check in constant state, no hash/window."""
    seen, pending = 0, b''
    for chunk in chunks:
        data = pending + chunk
        end = len(data) // DOMAIN.size * DOMAIN.size
        for pre, post in DOMAIN.iter_unpack(data[:end]):
            if pre != seen or post < pre or post >= count or seen >= count:
                raise ValueError('daily scalar final preorder/endpoint domain is incomplete')
            seen += 1
        pending = data[end:]
    if pending or seen != count:
        raise ValueError('daily scalar final preorder/endpoint domain is incomplete')


def _positive(value: int, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f'{label} must be a positive integer')
    return value


def _descriptor(body: dict) -> dict:
    if not isinstance(body, dict) or set(body) != {'schema', 'logical_store', 'date', 'identity', 'bytes', 'sha256'} or body['schema'] != 'daily-scalar-input-v1':
        raise ValueError('daily scalar requires a complete pinned v2 input descriptor')
    identity = body['identity']
    if not isinstance(identity, dict) or set(identity) != {'uri', 'generation'}:
        raise ValueError('daily scalar input requires URI and immutable generation')
    for label, value in [('logical store', body['logical_store']), *identity.items()]:
        if not isinstance(value, str) or not value or '\0' in value or len(value) > 4096:
            raise ValueError(f'daily scalar input {label} must be a nonempty NUL-free string')
        value.encode('utf-8')
    identifier(body['logical_store'])
    value = body['date']
    if not isinstance(value, str) or not fullmatch(r'\d{4}-\d{2}-\d{2}', value) or Date.fromisoformat(value).isoformat() != value:
        raise ValueError('daily scalar input requires an ISO scan date')
    _positive(body['bytes'], 'source bytes')
    if not isinstance(body['sha256'], str) or not fullmatch('[0-9a-f]{64}', body['sha256']):
        raise ValueError('daily scalar input requires a lowercase SHA256')
    return {**body, 'identity': dict(identity)}


def _hash(file: BinaryIO) -> tuple[int, str]:
    file.seek(0)
    digest, size = sha256(), 0
    while chunk := file.read(8 << 20):
        digest.update(chunk)
        size += len(chunk)
    file.seek(0)
    return size, digest.hexdigest()


def _arrow(file: BinaryIO, prefix: str) -> Iterator[bytes]:
    """Bounded Arrow batches, retaining only the exact selected subtree."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.ipc as ipc
    import pyarrow.parquet as pq

    schema = pa.schema([('path', pa.string()), ('usr', pa.string()), ('kind', pa.string()),
                        ('depth', pa.int32()), ('size', pa.int64()), ('n_files', pa.int64())])
    sink = BytesIO()
    parquet = pq.ParquetFile(file)
    groups = None
    if prefix:
        # Exact subtree bounds, never sampled groups. Missing statistics retain
        # the group; the Arrow predicate below remains the final row filter.
        column = parquet.schema_arrow.names.index('path')
        lower, upper = prefix + '/', prefix + '0'
        groups = []
        for index in range(parquet.metadata.num_row_groups):
            stats = parquet.metadata.row_group(index).column(column).statistics
            if stats is None or not stats.has_min_max or not isinstance(stats.min, str) or not isinstance(stats.max, str):
                groups.append(index)
            elif stats.min <= prefix <= stats.max or (stats.max >= lower and stats.min < upper):
                groups.append(index)
    with ipc.new_stream(sink, schema) as writer:
        for batch in parquet.iter_batches(batch_size=65536, columns=schema.names, row_groups=groups):
            columns = [batch.column(name).cast(typ) for name, typ in zip(schema.names, schema.types, strict=True)]
            batch = pa.record_batch(columns, schema=schema)
            if prefix:
                keep = pc.or_(pc.equal(batch.column('path'), prefix), pc.starts_with(batch.column('path'), prefix + '/'))
                batch = batch.filter(keep)
            writer.write_batch(batch)
            yield sink.getvalue()
            sink.seek(0)
            sink.truncate()
    yield sink.getvalue()


def _records(chunks: Iterable[bytes]) -> Iterator[tuple[int, int, int, int, str]]:
    pending = b''
    for chunk in chunks:
        data, pos = pending + chunk, 0
        while len(data) - pos >= FIXED.size + 1:
            start = pos
            node, depth, b, o = FIXED.unpack_from(data, pos)
            pos += FIXED.size
            length, shift = 0, 0
            while pos < len(data):
                byte = data[pos]
                pos += 1
                if shift > 63 or (shift == 63 and byte > 1):
                    raise ValueError('daily scalar has an invalid RowBinary string length')
                length |= (byte & 127) << shift
                if not byte & 128:
                    break
                shift += 7
            else:
                pos = start
                break
            if length > 1 << 20:
                raise ValueError('daily scalar path exceeds 1MiB protocol limit')
            if pos + length > len(data):
                pos = start
                break
            path = data[pos:pos + length].decode('utf-8')
            pos += length
            yield node, depth, b, o, path
        pending = data[pos:]
    if pending:
        raise ValueError('daily scalar has a truncated RowBinary node')


@dataclass
class _Frame:
    path: str
    b: int
    o: int
    pre: int
    children_b: int = 0
    children_o: int = 0


def audited_preorder(
    chunks: Iterable[bytes],
    count: int,
    prefix: str,
) -> Iterator[bytes]:
    """Verify exact parent paths, order and additive own remainders in O(depth).

    Directory rollups may contain own objects, including zero-byte objects.
    Negative own remainders and bytes with no own objects are rejected.
    The resulting stream feeds the existing endpoint encoder, not Python arrays.
    """
    yield from _audited(chunks, count, prefix, intervals=False)


def audited_intervals(
    chunks: Iterable[bytes],
    count: int,
    prefix: str,
) -> Iterator[bytes]:
    """Emit endpoints directly from the same complete parent/rollup audit.

    One O(depth) stack supplies both subtree endpoints and own-contribution
    checks; no intermediate id/depth encoding or second DFS pass is needed.
    A failed stream never produces a completed source marker.
    """
    yield from _audited(chunks, count, prefix, intervals=True)


def _audited(
    chunks: Iterable[bytes],
    count: int,
    prefix: str,
    *,
    intervals: bool,
) -> Iterator[bytes]:
    stack, output, previous, seen = [], bytearray(), None, 0
    dp = prefix.count('/') + 1 if prefix else 0

    def close() -> None:
        node = stack.pop()
        b, o = node.b - node.children_b, node.o - node.children_o
        if b < 0 or o < 0 or (b > 0 and o == 0):
            raise ValueError('daily scalar has an invalid recursive rollup/own contribution')
        if stack:
            stack[-1].children_b += node.b
            stack[-1].children_o += node.o
        if intervals:
            output.extend(INTERVAL.pack(node.pre, node.pre, seen - 1))

    for node, depth, b, o, path in _records(chunks):
        key = path.encode('utf-8').replace(b'\0', b'\0\1').replace(b'/', b'\0\0')
        relative = depth - dp
        if node != seen or relative < 0 or relative >= 255 or seen >= count or (seen and relative == 0) or (previous is not None and key <= previous):
            raise ValueError('daily scalar preorder has invalid IDs/depth/order/count')
        if seen == 0 and (path != prefix or relative != 0):
            raise ValueError('daily scalar preorder must begin with the selected root')
        while len(stack) > relative:
            close()
        if len(stack) != relative or (seen and path.rsplit('/', 1)[0] != stack[-1].path and not (relative == 1 and '/' not in path and prefix == '')):
            raise ValueError('daily scalar is missing an immediate parent')
        if (0 if path == '' else path.count('/') + 1) != depth:
            raise ValueError('daily scalar path and depth disagree')
        stack.append(_Frame(path, b, o, node))
        if not intervals:
            output.extend(ID_DEPTH.pack(node, relative + 1))
        seen += 1
        previous = key
        if len(output) >= 1 << 20:
            yield bytes(output)
            output.clear()
    if seen != count:
        raise ValueError('daily scalar preorder count differs from the complete source')
    while stack:
        close()
    if output:
        yield bytes(output)


def build(
    ch: Ch,
    target: str,
    local_parquet: Path,
    descriptor: dict,
    *,
    prefix: str = '',
    max_nodes: int = 1_000_000,
    memory_bytes: int = 8 << 30,
    spill_bytes: int = 8 << 30,
    query_seconds: int = 1800,
    max_owned_bytes: int = 35 << 30,
    min_free_bytes: int = 20 << 30,
    progress: Callable[[str], None] | None = None,
    resume: dict | None = None,
    sort_spill_bytes: int | None = None,
    order_plan: str = 'window',
) -> dict:
    """Fresh construction or explicitly evidenced raw/scalar-only recovery."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    identifier(target)
    descriptor = _descriptor(descriptor)
    for label, value in [('node cap', max_nodes), ('memory bytes', memory_bytes), ('spill bytes', spill_bytes),
                         ('query seconds', query_seconds), ('owned bytes', max_owned_bytes), ('reserve bytes', min_free_bytes)]:
        _positive(value, label)
    if max_nodes >= 1 << 32:
        raise ValueError('daily scalar node cap must fit UInt32')
    if order_plan not in ('window', 'physical'):
        raise ValueError('daily scalar order plan must be window or physical')
    sort_threshold = min(256 << 20, memory_bytes // 4) if sort_spill_bytes is None else _positive(sort_spill_bytes, 'sort spill bytes')
    if sort_threshold > memory_bytes // 4:
        raise ValueError('daily scalar sort spill threshold must not exceed one quarter of its memory cap')
    if resume is not None and prefix != '':
        raise ValueError('daily scalar resume only supports the original complete global scope')
    if not isinstance(prefix, str) or prefix.strip('/') != prefix or '\0' in prefix:
        raise ValueError('daily scalar prefix must be a canonical complete subtree')
    prefix.encode('utf-8')
    if not local_parquet.is_file() or local_parquet.is_symlink():
        raise ValueError('daily scalar input must be an explicit local regular file')
    settings = {'max_threads': 2, 'max_insert_threads': 1, 'max_memory_usage': memory_bytes,
                'max_temporary_data_on_disk_size_for_query': spill_bytes,
                'max_bytes_before_external_sort': sort_threshold, 'max_bytes_ratio_before_external_sort': 0,
                'max_bytes_before_external_group_by': min(256 << 20, memory_bytes // 4), 'max_bytes_ratio_before_external_group_by': 0,
                'max_execution_time': query_seconds, 'timeout_before_checking_execution_speed': 0, 'timeout_overflow_mode': 'throw',
                'max_block_size': 8192, 'join_algorithm': 'full_sorting_merge', 'join_use_nulls': 0}
    client, query_ids, stages, readers = ch.fork(**settings), [], {}, []
    client.timeout = query_seconds + 60

    def report(name: str, status: str) -> None:
        # Library callers may stay silent; the CLI logs phase transitions to
        # stderr without exposing path names or usage weights.
        if progress is not None:
            progress(f'daily scalar {name}: {status}')

    def request(method: str, sql: str | Callable[[str], str], *args, **kwargs):
        query_id = 'daily_scalar_' + uuid4().hex
        query_ids.append(query_id)
        if callable(sql):
            sql = sql(query_id)
        per_query = {**(kwargs.pop('settings', None) or {}), 'query_id': query_id}
        if method == 'stream':
            reader = client.fork()
            readers.append(reader)
            return reader.stream(sql, *args, settings=per_query, **kwargs)
        return getattr(client, method)(sql, *args, settings=per_query, **kwargs)

    def guard(stage: str) -> None:
        available = int(request('scalar', 'SELECT min(free_space) FROM system.disks'))
        if available < min_free_bytes:
            raise ValueError(f'before {stage}: {available} free bytes < reserve {min_free_bytes}; partial build retained')
        owned = int(request('scalar', f'SELECT coalesce(sum(bytes_on_disk),0) FROM system.parts WHERE database={lit(target)}'))
        if owned > max_owned_bytes:
            raise ValueError(f'daily scalar owned database exceeds {max_owned_bytes}-byte stage budget; partial build retained')

    def stage(name: str, sql: str, *, settings: dict | None = None) -> None:
        guard(name)
        report(name, 'started')
        start = monotonic()
        request('exec', sql, settings=settings)
        stages[name] = round(monotonic() - start, 6)
        guard(name + '_complete')
        report(name, 'complete')

    try:
        with local_parquet.open('rb') as file:
            report('hash_before', 'started')
            if _hash(file) != (descriptor['bytes'], descriptor['sha256']):
                raise ValueError('daily scalar local source length/SHA256 differs from its descriptor')
            report('hash_before', 'complete')
            footer = pq.ParquetFile(file)
            required = {'path', 'usr', 'kind', 'depth', 'size', 'n_files'}
            if not required <= set(footer.schema_arrow.names) or footer.metadata.num_rows <= 0:
                raise ValueError('daily scalar requires a nonempty complete v2 path-sort schema')
            schema = footer.schema_arrow
            if (any(not (pa.types.is_string(schema.field(name).type) or pa.types.is_large_string(schema.field(name).type)) for name in ('path', 'kind')) or
                    any(not pa.types.is_integer(schema.field(name).type) for name in ('depth', 'size', 'n_files')) or
                    not (pa.types.is_string(schema.field('usr').type) or pa.types.is_large_string(schema.field('usr').type) or pa.types.is_null(schema.field('usr').type))):
                raise ValueError('daily scalar requires typed v2 path/owner/kind/integer scalar columns')
            source_rows = footer.metadata.num_rows
            sql = stage_sql(target)
            recovery = None
            if resume is None:
                if request('scalar', f'EXISTS DATABASE {target}') != '0':
                    raise ValueError('daily scalar refuses an existing database')
                guard('create_database')
                request('exec', f'CREATE DATABASE {target}')
                stage('raw', f"CREATE TABLE {target}.raw ({TABLE_COLUMNS['raw']}) ENGINE=MergeTree ORDER BY (coalesce(path,''),coalesce(usr,''),coalesce(kind,''))")
                report('upload', 'started')
                start = monotonic()
                request('insert', sql['upload'], _arrow(file, prefix))
                stages['upload'] = round(monotonic() - start, 6)
                guard('upload_complete')
                report('upload', 'complete')
            else:
                guard('resume_scalar')
                recovery = resume_evidence(request, target, descriptor, source_rows, resume)
                report('reused_scalar', 'complete')
            report('source_audit', 'started')
            audit = request('json', f"""SELECT count(),countIf(isNull(path) OR NOT isValidUTF8(coalesce(path,'')) OR path='' OR length(path)>1048576
                OR isNull(depth) OR depth<1 OR depth>254 OR depth!=length(splitByChar('/',coalesce(path,'')))
                OR isNull(size) OR size<0 OR isNull(n_files) OR n_files<0 OR isNull(kind) OR kind NOT IN ('file','dir')) FROM {target}.raw""")
            if len(audit) != 1 or len(audit[0]) != 2 or not audit[0][0] or audit[0][1]:
                raise ValueError('daily scalar source failed UTF-8/depth/nonnegative v2 audit')
            selected = int(audit[0][0])
            if not prefix and selected != source_rows:
                raise ValueError('daily scalar upload count differs from the pinned complete footer')
            duplicates = int(request('scalar', f"SELECT count() FROM (SELECT coalesce(path,'') AS p,coalesce(usr,'') AS owner,coalesce(kind,'') AS k FROM {target}.raw GROUP BY p,owner,k HAVING count()>1) SETTINGS optimize_aggregation_in_order=1"))
            if duplicates:
                raise ValueError('daily scalar source contains duplicate path/owner/kind slices')
            report('source_audit', 'complete')
            if recovery is None:
                stage('scalar', sql['scalar'])
            if int(request('scalar', f'SELECT count() FROM {target}.scalar WHERE b>{U64} OR o>{U64}')):
                raise ValueError('daily scalar rollup exceeds UInt64')
            if not prefix:
                if recovery is None:
                    request('exec', sql['root'])
                else:
                    root_sums = request('json', f"SELECT b,o FROM {target}.scalar WHERE path='' AND depth=0")
                    bucket_sums = request('json', f'SELECT sum(b),sum(o) FROM {target}.scalar WHERE depth=1')
                    if len(root_sums) != 1 or len(bucket_sums) != 1 or list(map(int, root_sums[0])) != list(map(int, bucket_sums[0])):
                        raise ValueError('daily scalar resume global root differs from complete bucket rollups')
                if int(request('scalar', f'SELECT count() FROM {target}.scalar WHERE b>{U64} OR o>{U64}')):
                    raise ValueError('daily scalar global rollup exceeds UInt64')
            count = int(request('scalar', f'SELECT count() FROM {target}.scalar'))
            if count > max_nodes:
                raise ValueError('daily scalar complete subtree exceeds its node cap')
            if not count or request('scalar', f'SELECT count() FROM {target}.scalar WHERE path={lit(prefix)}') != '1':
                raise ValueError('daily scalar complete selected root is absent')
            if order_plan == 'physical':
                create_tree, insert_tree, number_tree = physical_order_sql(target)
                stage('tree_sorted_table', create_tree)
                # MergeTree sorts insert blocks, never one fleet-wide window.
                # The statement's memory cap still refuses oversized blocks.
                stage('tree_sorted', insert_tree, settings={'min_insert_block_size_rows': 1 << 20,
                      'min_insert_block_size_bytes': 64 << 20, 'max_insert_threads': 1})
                if request('scalar', f'SELECT count() FROM {target}.tree_sorted') != str(count):
                    raise ValueError('daily scalar physical tree staging lost complete source rows')
                stage('ordered', number_tree, settings={'max_threads': 1, 'max_insert_threads': 1, 'max_block_size': 8192})
            else:
                stage('ordered', sql['failed_ordered'])
            stage('interval_table', f"CREATE TABLE {target}.intervals ({TABLE_COLUMNS['intervals']}) ENGINE=MergeTree ORDER BY id")
            report('intervals', 'started')
            start = monotonic()
            chunks = request('stream', wire_select(target), fmt='RowBinary')
            encoded = iter(audited_intervals(chunks, count, prefix))
            try:
                first = next(encoded)
                request('insert', f'INSERT INTO {target}.intervals FORMAT RowBinary', chain((first,), encoded))
            finally:
                chunks.close()
            stages['intervals'] = round(monotonic() - start, 6)
            guard('intervals_complete')
            report('intervals', 'complete')
            stage('nodes', f"""CREATE TABLE {target}.nodes ({TABLE_COLUMNS['nodes']}) ENGINE=MergeTree ORDER BY pre AS
                SELECT i.pre,i.post,s.depth,s.path,s.b,s.o FROM {target}.ordered s INNER JOIN {target}.intervals i ON s.id=i.id""")
            domain = request('stream', f'SELECT assumeNotNull(toUInt32(pre)),assumeNotNull(toUInt32(post)) FROM {target}.nodes ORDER BY pre', fmt='RowBinary')
            try:
                audit_domain(domain, count)
            finally:
                domain.close()
            # Every interval was encoded from the verified complete DFS stream;
            # require the numeric copy to preserve every generated endpoint.
            if request('scalar', f'SELECT count() FROM {target}.intervals WHERE id!=pre') != '0':
                raise ValueError('daily scalar interval IDs differ from the accepted dense preorder')
            if request('scalar', f'SELECT count() FROM {target}.nodes n INNER JOIN {target}.intervals i ON n.pre=i.id WHERE n.post!=i.post') != '0':
                raise ValueError('daily scalar final interval endpoints differ from the accepted encoding')
            stage('dictionary', f"CREATE TABLE {target}.dictionary ({TABLE_COLUMNS['dictionary']}) ENGINE=MergeTree ORDER BY pre AS SELECT pre,post,depth,path FROM {target}.nodes WHERE pre=0 OR depth=1")
            roots = request('json', f'SELECT path,pre,post,b,o FROM {target}.nodes WHERE pre=0')
            if len(roots) != 1 or roots[0][0:3] != [prefix, 0, count - 1]:
                raise ValueError('daily scalar global/selected root span is incomplete')
            root = dict(zip(('path', 'pre', 'post', 'b', 'o'), roots[0], strict=True))
            buckets = [] if prefix else request('json', f'SELECT path,pre,post FROM {target}.dictionary WHERE depth=1 ORDER BY pre LIMIT 7')
            if not prefix and (not 1 <= len(buckets) <= 6 or buckets[0][1] != 1 or buckets[-1][2] != count - 1 or any(a[2] + 1 != b[1] for a, b in zip(buckets, buckets[1:]))):
                raise ValueError('daily scalar complete bucket partition must contain one to six roots')
            report('hash_after', 'started')
            if _hash(file) != (descriptor['bytes'], descriptor['sha256']):
                raise ValueError('daily scalar pinned source changed during construction')
            report('hash_after', 'complete')
            body = {'schema': 'daily-scalar-source-v1', 'complete': True, 'logical_store': descriptor['logical_store'],
                    'date': descriptor['date'], 'target': target, 'snapshot_db': target, 'prefix': prefix, 'source': descriptor,
                    'source_rows': source_rows, 'selected_source_rows': selected, 'nodes': count, 'root': root,
                    'buckets': [dict(zip(('path', 'pre', 'post'), row, strict=True)) for row in buckets],
                    'validation': {'source_hash_checked': True, 'prefix_closed': True, 'interval_endpoints_checked': True, 'scalar_rollups_checked': True},
                    'limits': {'max_nodes': max_nodes, 'memory_bytes': memory_bytes, 'spill_bytes': spill_bytes, 'query_seconds': query_seconds,
                               'max_owned_bytes': max_owned_bytes, 'min_free_bytes': min_free_bytes, 'disk_checks': 'stage boundaries; not an in-flight database quota'},
                    'stages': stages}
            if recovery is not None:
                body['limits']['recovery'] = recovery
            if sort_spill_bytes is not None:
                body['limits']['sort_spill_bytes'] = sort_threshold
            if order_plan != 'window':
                body['limits']['order_plan'] = order_plan
            stage('source_marker', f'CREATE TABLE {target}.source_manifest (doc String) ENGINE=TinyLog')
            # Do not include the marker stage in its own serialized timings.
            body['stages'] = {name: seconds for name, seconds in stages.items() if name != 'source_marker'}
            guard('publish_source_marker')
            request('exec', f'INSERT INTO {target}.source_manifest VALUES ({lit(manifest_bytes(body).decode().rstrip())})')
            report('accepted_source_marker', 'complete')
            return body
    except BaseException as error:
        if query_ids:
            # Cancel only this build's dispatched statements, retaining partial
            # owned tables. Cancellation failure must not hide the original error.
            try:
                client.exec('KILL QUERY WHERE query_id IN (' + ','.join(map(lit, query_ids)) + ') SYNC', fmt=None)
            except (OSError, RuntimeError):
                error.add_note('daily scalar owned query cancellation could not be verified; partial build retained')
        raise
    finally:
        for reader in readers:
            reader.close()
        client.close()
