"""Independent complete bounded oracle over one supplied pinned path-index.

Arrow dataset filtering, Python owner collapse and UTF-8 segment tuple ordering
are independent of the producer's row-group selection/SQL/wire/DFS encoder.
This verifies the supplied path-index, not its object-inventory completeness.
Large global sources and over 4M selected owner/kind rows are refused, never
sampled. No tables are created.
"""

from datetime import date as Date
from hashlib import sha256
from json import dumps, loads
from pathlib import Path
from time import monotonic
from typing import Iterable, Iterator
from uuid import uuid4

from .client import Ch, lit
from .hot_l1_catalog import _unique_object
from .narrow import identifier

MAX_NODES = 1_000_000
MAX_SOURCE_ROWS = 4_000_000
MAX_MANIFEST = 64 << 10
MAX_LINE = 2 << 20
VALIDATION = 'complete independent bounded supplied-path-index oracle; not an independent object-store inventory'
FLAGS = {'source_hash_checked', 'prefix_closed', 'interval_endpoints_checked', 'scalar_rollups_checked'}


def _canonical(body: dict) -> bytes:
    return (dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def _integer(value: object, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _hash(path: Path) -> tuple[int, str]:
    size, digest = 0, sha256()
    with path.open('rb') as source:
        while chunk := source.read(8 << 20):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _manifest(path: Path, max_nodes: int) -> tuple[bytes, dict]:
    with path.open('rb') as source:
        raw = source.read(MAX_MANIFEST + 1)
    if not 0 < len(raw) <= MAX_MANIFEST:
        raise ValueError('daily scalar check manifest exceeds its bounded metadata contract')
    body = loads(raw, object_pairs_hook=_unique_object)
    if (not isinstance(body, dict) or body.get('schema') != 'daily-scalar-source-v1' or body.get('complete') is not True or
            not _integer(body.get('nodes'), 1) or body['nodes'] > max_nodes or
            not isinstance(body.get('validation'), dict) or set(body['validation']) != FLAGS or
            any(body['validation'][flag] is not True for flag in FLAGS)):
        raise ValueError('daily scalar check requires a complete accepted source with at most the requested node cap')
    if not _integer(body.get('selected_source_rows'), 1) or body['selected_source_rows'] > MAX_SOURCE_ROWS:
        raise ValueError('daily scalar check requires 1..4M selected source rows; no sample accepted')
    for key in ('target', 'snapshot_db', 'logical_store'):
        if not isinstance(body.get(key), str):
            raise ValueError('daily scalar check source identifiers must be strings')
        identifier(body[key])
    if body['target'] != body['snapshot_db']:
        raise ValueError('daily scalar check source target/snapshot database must agree')
    day = body.get('date')
    try:
        valid_date = isinstance(day, str) and Date.fromisoformat(day).isoformat() == day
    except ValueError:
        valid_date = False
    if not valid_date:
        raise ValueError('daily scalar check source date must be canonical ISO')
    prefix = body.get('prefix')
    if not isinstance(prefix, str) or prefix.strip('/') != prefix or '\0' in prefix:
        raise ValueError('daily scalar check requires a canonical complete subtree prefix')
    root, buckets = body.get('root'), body.get('buckets')
    if (not isinstance(root, dict) or set(root) != {'path', 'pre', 'post', 'b', 'o'} or root['path'] != prefix or
            any(not _integer(root[key]) for key in ('pre', 'post', 'b', 'o')) or
            root['pre'] != 0 or root['post'] != body['nodes'] - 1 or not isinstance(buckets, list) or
            any(not isinstance(row, dict) or set(row) != {'path', 'pre', 'post'} or not isinstance(row['path'], str) or
                not _integer(row['pre']) or not _integer(row['post']) for row in buckets)):
        raise ValueError('daily scalar check manifest root/bucket geometry must contain exact integer scalars')
    source = body.get('source')
    if (not isinstance(source, dict) or source.get('schema') != 'daily-scalar-input-v1' or
            source.get('logical_store') != body['logical_store'] or source.get('date') != body.get('date') or
            not _integer(source.get('bytes'), 1) or not isinstance(source.get('sha256'), str) or
            not _integer(body.get('source_rows'), 1) or not _integer(body.get('selected_source_rows'), 1)):
        raise ValueError('daily scalar check manifest requires its complete pinned input identity/counts')
    if raw != _canonical(body):
        raise ValueError('daily scalar check requires canonical pinned source-manifest bytes')
    return raw, body


def _expected(
    path: Path,
    manifest: dict,
    max_nodes: int,
) -> tuple[list[tuple], int, int]:
    import pyarrow.dataset as ds
    import pyarrow.parquet as pq

    prefix, totals, selected = manifest['prefix'], {}, 0
    footer_rows = pq.read_metadata(path).num_rows
    dataset = ds.dataset(path, format='parquet')
    required = ('depth', 'path', 'size', 'n_files', 'usr', 'kind')
    if not set(required) <= set(dataset.schema.names):
        raise ValueError('daily scalar check source lacks complete path-index scalar columns')
    predicate = None
    if prefix:
        field = ds.field('path')
        predicate = (field == prefix) | ((field >= prefix + '/') & (field < prefix + '0'))
    scanner = dataset.scanner(columns=list(required), filter=predicate, batch_size=65536, use_threads=False,
                              batch_readahead=1, fragment_readahead=1)
    for batch in scanner.to_batches():
        for row in batch.to_pylist():
            name = row['path']
            if not isinstance(name, str):
                raise ValueError('daily scalar check source path is not a nonnull string')
            if prefix and not (name == prefix or name.startswith(prefix + '/')):
                continue
            depth, b, o = row['depth'], row['size'], row['n_files']
            parts = name.split('/')
            if (not name or len(name.encode()) > 1 << 20 or
                    not _integer(depth, 1) or depth != len(parts) or depth > 254 or not _integer(b) or not _integer(o) or
                    row['kind'] not in ('dir', 'file') or (row['usr'] is not None and not isinstance(row['usr'], str))):
                raise ValueError('daily scalar check source contains invalid path/depth/owner/kind/scalar rows')
            if selected >= MAX_SOURCE_ROWS:
                raise ValueError('daily scalar check complete source exceeds its selected-row cap; no sample accepted')
            key = depth, name
            if key not in totals:
                if len(totals) >= max_nodes - (0 if prefix else 1):
                    raise ValueError('daily scalar check complete source exceeds its node cap; no sample accepted')
                totals[key] = [0, 0]
            totals[key][0] += b
            totals[key][1] += o
            if max(totals[key]) >= 1 << 64:
                raise ValueError('daily scalar check collapsed scalar exceeds UInt64')
            selected += 1
    if not prefix:
        totals[(0, '')] = [sum(pair[0] for (depth, _), pair in totals.items() if depth == 1),
                           sum(pair[1] for (depth, _), pair in totals.items() if depth == 1)]
    root_depth = len(prefix.split('/')) if prefix else 0
    if (root_depth, prefix) not in totals or not selected:
        raise ValueError('daily scalar check complete selected root is absent')
    ordered = sorted(totals, key=lambda key: tuple(part.encode('utf-8') for part in key[1].split('/')) if key[1] else ())
    rows, stack, children = [], [], {}
    for pre, key in enumerate(ordered):
        depth, name = key
        relative = depth - root_depth
        while len(stack) > relative:
            rows[stack.pop()][1] = pre - 1
        parent = (depth - 1, name.rsplit('/', 1)[0] if '/' in name else '')
        if (pre == 0 and key != (root_depth, prefix)) or len(stack) != relative or (pre and ordered[stack[-1]] != parent):
            raise ValueError('daily scalar check source is not a complete immediate-parent tree')
        b, o = totals[key]
        if pre:
            sums = children.setdefault(parent, [0, 0])
            sums[0] += b
            sums[1] += o
        rows.append([pre, pre, depth, name, b, o])
        stack.append(pre)
    while stack:
        rows[stack.pop()][1] = len(rows) - 1
    for key, pair in totals.items():
        child = children.get(key, (0, 0))
        own_b, own_o = pair[0] - child[0], pair[1] - child[1]
        if own_b < 0 or own_o < 0 or (own_b and not own_o) or max(pair) >= 1 << 64:
            raise ValueError('daily scalar check source has inconsistent recursive/own scalar totals')
    return [tuple(row) for row in rows], selected, footer_rows


def _rows(chunks: Iterable[bytes]) -> Iterator[object]:
    pending = b''
    for chunk in chunks:
        pieces = (pending + chunk).split(b'\n')
        pending = pieces.pop()
        for piece in pieces:
            if not piece or len(piece) > MAX_LINE:
                raise ValueError('daily scalar check CH stream has an invalid bounded JSON row')
            yield loads(piece, object_pairs_hook=_unique_object)
        if len(pending) > MAX_LINE:
            raise ValueError('daily scalar check CH stream has an invalid bounded JSON row')
    if pending:
        raise ValueError('daily scalar check CH stream ended with an incomplete JSON row')


def check(
    manifest_path: Path,
    local_parquet: Path,
    url: str,
    out: Path,
    *,
    max_nodes: int = MAX_NODES,
    seconds: int = 180,
) -> dict:
    if type(max_nodes) is not int or not 1 <= max_nodes <= MAX_NODES or type(seconds) is not int or not 1 <= seconds <= 600:
        raise ValueError('daily scalar check requires a node cap in 1..1M and seconds in 1..600')
    if out.exists() or out.is_symlink() or not out.parent.is_dir():
        raise ValueError('daily scalar check output must be fresh in an existing directory')
    if not local_parquet.is_file() or local_parquet.is_symlink():
        raise ValueError('daily scalar check input must be an explicit local regular parquet')
    start = monotonic()
    raw, manifest = _manifest(manifest_path, max_nodes)
    identity = manifest['source']['bytes'], manifest['source']['sha256']
    if _hash(local_parquet) != identity:
        raise ValueError('daily scalar check local input bytes/SHA256 differ from pinned manifest')
    expected, selected, footer_rows = _expected(local_parquet, manifest, max_nodes)
    root = expected[0]
    buckets = [] if manifest['prefix'] else [{'path': row[3], 'pre': row[0], 'post': row[1]} for row in expected if row[2] == 1]
    expected_root = {'path': root[3], 'pre': root[0], 'post': root[1], 'b': root[4], 'o': root[5]}
    if (len(expected) != manifest['nodes'] or selected != manifest['selected_source_rows'] or footer_rows != manifest['source_rows'] or
            manifest.get('root') != expected_root or manifest.get('buckets') != buckets):
        raise AssertionError('daily scalar check manifest disagrees with complete independently derived source rows/root/buckets')
    tag = 'daily_scalar_check_' + uuid4().hex
    ch = Ch(url, db=manifest['target'], timeout=seconds + 10, max_threads=1, max_memory_usage=1 << 30,
            max_execution_time=seconds, timeout_before_checking_execution_speed=0, timeout_overflow_mode='throw')
    stream, seen = None, 0
    try:
        docs = ch.json(f"SELECT doc FROM {manifest['target']}.source_manifest LIMIT 2", settings={'query_id': tag + '_manifest'})
        if (not isinstance(docs, list) or len(docs) != 1 or not isinstance(docs[0], list) or len(docs[0]) != 1 or
                not isinstance(docs[0][0], str) or _canonical(loads(docs[0][0], object_pairs_hook=_unique_object)) != raw):
            raise AssertionError('daily scalar check actual CH source manifest differs from pinned canonical bytes')
        stream = ch.stream(f"SELECT pre,post,depth,path,b,o FROM {manifest['target']}.nodes ORDER BY pre LIMIT {max_nodes + 1}",
                           fmt='JSONCompactEachRow', settings={'query_id': tag + '_nodes', 'output_format_json_quote_64bit_integers': 0}, chunk=1 << 20)
        for actual in _rows(stream):
            if (not isinstance(actual, list) or len(actual) != 6 or any(not _integer(actual[key]) for key in (0, 1, 2, 4, 5)) or
                    not isinstance(actual[3], str) or seen >= len(expected) or tuple(actual) != expected[seen]):
                raise AssertionError('daily scalar check final CH node differs from the complete independent path-index oracle')
            seen += 1
        if seen != len(expected):
            raise AssertionError('daily scalar check final CH node count differs from the complete independent path-index oracle')
        if _hash(local_parquet) != identity:
            raise ValueError('daily scalar check local pinned input changed during validation')
    except BaseException as error:
        if stream is not None:
            try:
                stream.close()
            except (OSError, RuntimeError):
                error.add_note('daily scalar check owned read stream could not be closed')
            stream = None
        ch.timeout = 2
        try:
            ch.exec(f'KILL QUERY WHERE query_id IN ({lit(tag + "_manifest")},{lit(tag + "_nodes")}) SYNC',
                    fmt=None, settings={'max_execution_time': 2})
        except (OSError, RuntimeError):
            error.add_note('daily scalar check owned read-query cleanup could not be verified')
        raise
    finally:
        if stream is not None:
            stream.close()
        ch.close()
    result = {'schema': 'daily-scalar-check-v1', 'complete': True, 'target': manifest['target'], 'date': manifest['date'],
              'logical_store': manifest['logical_store'], 'prefix': manifest['prefix'],
              'manifest_sha256': sha256(raw).hexdigest(), 'manifest_bytes': len(raw),
              'input_sha256': identity[1], 'input_bytes': identity[0], 'source_rows': footer_rows, 'selected_source_rows': selected,
              'nodes_checked': seen, 'max_nodes': max_nodes, 'elapsed_s': monotonic() - start,
              'max_selected_source_rows': MAX_SOURCE_ROWS,
              'validation': VALIDATION, 'sampling': False}
    owned = False
    try:
        with out.open('x') as output:
            owned = True
            output.write(dumps(result, allow_nan=False) + '\n')
    except BaseException:
        if owned:
            out.unlink()
        raise
    return result
