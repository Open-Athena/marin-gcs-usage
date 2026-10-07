"""Offline paired sparse depth-2 experiment over immutable accepted snapshots.

Thresholds are fixed PER BUCKET, not per fleet or arbitrary drill directory.
Two RowBinary streams go directly to native FDs; Python never decodes nodes.
Trusted L1 references and artifact-bound dated prefix proofs are prerequisites.
This module does not publish a catalog, install a fallback or change serving.
"""

from collections.abc import Callable
from hashlib import sha256
from json import dumps, loads
from os import X_OK, access, close, fdopen, pipe
from pathlib import Path
from queue import Queue
from struct import pack
from subprocess import PIPE, Popen, TimeoutExpired
from sys import stderr
from threading import Event, Thread
from time import monotonic
from typing import BinaryIO
from uuid import uuid4

from .client import Ch, lit
from .hot_frequency_registry import PinnedExport, UNION_SCHEMA, load_queries
from .hot_l1 import _buckets
from .hot_l1_batch_catalog import HotL1BatchCatalog
from .hot_l1_batch_sql import NAME, SCOPE
from .hot_l1_batch_stream import _string
from .hot_l1_catalog import _unique_object
from .hot_l1_publish import _prefix_proofs
from .narrow import identifier

MAX_FRAMES = 500_000
MAX_CELLS = 10_000_000
MAX_OUTPUT = 512 << 20


def _uint(value: object, bits: int = 64) -> int:
    if type(value) is not int or not 0 <= value < 1 << bits:
        raise ValueError("paired L2 requires bounded unsigned integers")
    return value


def _weight(value: object) -> int:
    if not isinstance(value, str) or len(value) > 39 or not value.isascii() or not value.isdecimal() or (len(value) > 1 and value[0] == '0'):
        raise RuntimeError("paired L2 native weights must be canonical decimal strings")
    return _uint(int(value), 128)


def prepare(
    target: str,
    before: str,
    after: str,
    references: tuple[Path, ...],
    proofs: tuple[Path, ...],
    queries: Path,
    patterns: tuple[str, ...],
    budget: int,
    *,
    all_registered: bool = False,
) -> dict:
    identifier(target)
    if len(references) != 2 or len(proofs) != 2 or before >= after:
        raise ValueError("paired L2 needs two ordered dates, two complete references and two dated prefix proofs")
    if type(budget) is not int or not 1 <= budget <= 4096:
        raise ValueError("paired L2 child budget must be in 1..4096")
    if bool(patterns) == bool(all_registered):
        raise ValueError("select explicit literals or explicitly request all registered predicates")
    blobs = [path.read_bytes() for path in references]
    catalog = HotL1BatchCatalog.from_bytes(blobs)
    bodies = [loads(blob, object_pairs_hook=_unique_object) for blob in blobs]
    if any(body['schema'] != 'hot-l1-batch-stream-v1' for body in bodies):
        raise ValueError("paired L2 requires completed native L1 references")
    by_date = {body['date']: body for body in bodies}
    if catalog.target != target or set(by_date) != {before, after}:
        raise ValueError("paired L2 references must cover the requested target and dates exactly")
    proof_blobs = [path.read_bytes() for path in proofs]
    if _prefix_proofs(proof_blobs, blobs).get('checked') is not True:
        raise ValueError("paired L2 needs complete artifact-bound prefix proofs")
    raw = queries.read_bytes()
    raw_header = loads(raw.splitlines()[0], object_pairs_hook=_unique_object)
    registry_date = None if raw_header.get('schema') == UNION_SCHEMA else raw_header.get('date')
    header, registered = load_queries(PinnedExport(raw), target, after, registry_date=registry_date)
    if not registered or len(registered) > 500_000:
        raise ValueError("paired L2 registry must contain 1..500K completed predicates")
    for day in (before, after):
        if by_date[day]['queries']['header'] != header or catalog.registered_patterns(day) != registered:
            raise ValueError("paired L2 references must use the exact supplied complete query registry")
    if any(not isinstance(p, str) or not p or '/' in p or '\0' in p for p in patterns):
        raise ValueError("paired L2 literals must be nonempty NUL/slash-free names")
    selected = registered if all_registered else tuple(p.lower() for p in patterns)
    registered_set = set(registered)
    if len(set(selected)) != len(selected) or any(p not in registered_set for p in selected):
        raise ValueError("paired L2 selected literals must be unique and registered on both artifacts")
    views = [[catalog.view(day, p) for p in selected] for day in (before, after)]
    bounds = sorted([(r['pre'], r['post'], r['path']) for r in views[0][0]['buckets']])
    thresholds = []
    expected = []
    for q in range(len(selected)):
        paired, cuts = [], []
        maps = [{r['path']: r for r in views[side][q]['buckets']} for side in range(2)]
        for lo, hi, path in bounds:
            rows = [m[path] for m in maps]
            paired.append([rows[0]['b'], rows[0]['o'], rows[1]['b'], rows[1]['o']])
            cuts.append(_uint(max(1, (max(rows[0]['b'], rows[1]['b']) + budget - 1) // budget)))
        thresholds.append(cuts)
        expected.append(paired)
    return {'target': target, 'dates': [before, after], 'patterns': selected, 'budget': budget,
            'dbs': [identifier(by_date[day]['snapshot_db']) for day in (before, after)],
            'rows': [_uint(by_date[day]['source_validation']['rows']) for day in (before, after)],
            'buckets': bounds, 'thresholds': thresholds, 'expected': expected, 'query_subset': len(selected) != len(registered),
            'provenance': {'references': [{'path': str(path), 'sha256': sha256(blob).hexdigest(), 'bytes': len(blob)} for path, blob in zip(references, blobs, strict=True)],
                           'prefix_proofs': [{'path': str(path), 'sha256': sha256(blob).hexdigest(), 'bytes': len(blob)} for path, blob in zip(proofs, proof_blobs, strict=True)],
                           'queries': {'path': str(queries), 'sha256': sha256(raw).hexdigest(), 'bytes': len(raw), 'header': header},
                           'source_contract': 'immutable accepted frozen snapshots and separately audited union geometry; not independent raw-listing truth'}}


def frames(ch: Ch, prepared: dict, max_frames: int) -> list[dict]:
    target = prepared['target']
    manifest = loads(ch.scalar(f"SELECT doc FROM {target}.history_manifest"))
    if manifest['prefix'] != '' or any(day not in manifest['dates'] for day in prepared['dates']) or [manifest['dbs'][manifest['dates'].index(day)] for day in prepared['dates']] != prepared['dbs']:
        raise ValueError("paired L2 source identities differ from the accepted snapshots")
    if [tuple(row) for row in _buckets(ch, target)] != prepared['buckets']:
        raise ValueError("paired L2 union bucket geometry differs from accepted references")
    rows = ch.json(f"SELECT toUInt64(pre),toUInt64(post),path FROM {target}.dictionary WHERE depth=2 ORDER BY pre LIMIT {max_frames + 1}")
    if len(rows) > max_frames:
        raise ValueError("paired L2 complete frame count exceeds its guard")
    result, index = [], 0
    for bucket, (lo, hi, name) in enumerate(prepared['buckets']):
        cursor = lo + 1
        while index < len(rows) and rows[index][0] <= hi:
            pre, post, path = rows[index]
            _uint(pre)
            _uint(post)
            if not isinstance(path, str) or '\0' in path or path.partition('/')[0] != name or not path.partition('/')[2] or '/' in path.partition('/')[2] or pre != cursor or post < pre or post > hi:
                raise ValueError("paired L2 frames do not completely partition bucket descendants")
            path.encode('utf-8')
            result.append({'frame_id': len(result) + 1, 'pre': pre, 'post': post, 'path': path, 'bucket': bucket})
            cursor, index = post + 1, index + 1
        if cursor != hi + 1:
            raise ValueError("paired L2 frames do not completely partition bucket descendants")
    if index != len(rows):
        raise ValueError("paired L2 returned frames outside the complete bucket partition")
    return result


def control(prepared: dict, declared: list[dict]) -> bytes:
    data = bytearray(b'HL2PAIR1' + pack('<QQIIB', *prepared['rows'], len(prepared['patterns']), len(declared), len(prepared['buckets'])))
    for lo, hi, _ in prepared['buckets']:
        data.extend(pack('<QQ', lo, hi))
    for row in declared:
        data.extend(pack('<QQI', row['pre'], row['post'], row['bucket']))
    for text, thresholds in zip(prepared['patterns'], prepared['thresholds'], strict=True):
        data.extend(_string(text))
        data.extend(pack('<' + 'Q' * len(thresholds), *thresholds))
    return bytes(data)


def _cancel(ch: Ch, ids: list[str]) -> None:
    killer = Ch(ch.url, db=ch.db, session=False, timeout=10, max_execution_time=10, timeout_overflow_mode='throw')
    try:
        for query in ids:
            killer.exec(f"KILL QUERY WHERE query_id={lit(query)} SYNC", fmt=None)
    finally:
        killer.close()


def _write_all(sink: BinaryIO, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        count = sink.write(remaining)
        if count is None or count <= 0:
            raise BrokenPipeError("paired L2 consumer stopped accepting input")
        remaining = remaining[count:]


def stream(
    ch: Ch,
    prepared: dict,
    declared: list[dict],
    *,
    binary: Path,
    max_cells: int,
    max_output_bytes: int,
    wall_seconds: int,
    progress: Callable[[dict], None] | None = None,
    source_client: Path | None = None,
) -> tuple[dict, list[str], float]:
    ids = ['hot_l2_pair_' + uuid4().hex + suffix for suffix in ('_before', '_after')]
    readers = []
    stop, failures = Event(), Queue()
    threads, handles, fd_set, child = [], [], set(), None
    output, error_tail, byte_counts = bytearray(), bytearray(), [0, 0]
    start = monotonic()

    def worker(fn: Callable[[], None]) -> None:
        try:
            fn()
        except BaseException as error:
            if not stop.is_set():
                failures.put(error)
                stop.set()

    def launch(name: str, fn: Callable[[], None]) -> None:
        thread = Thread(target=worker, args=(fn,), name=name, daemon=True)
        threads.append(thread)
        thread.start()

    def write_control() -> None:
        try:
            _write_all(child.stdin, control(prepared, declared))
        finally:
            child.stdin.close()

    def pump(side: int, sink) -> None:
        source = None
        try:
            db = prepared['dbs'][side]
            source = readers[side].stream(f"""SELECT assumeNotNull(toUInt64(pre)),assumeNotNull(toUInt64(post)),
                assumeNotNull(toUInt64(b)),assumeNotNull(toUInt64(o)),assumeNotNull(lowerUTF8({NAME})) FROM {db}.nodes ORDER BY pre""", fmt='RowBinary')
            for chunk in source:
                if stop.is_set():
                    break
                _write_all(sink, chunk)
                byte_counts[side] += len(chunk)
        finally:
            if source is not None:
                source.close()
            sink.close()

    def drain_stdout() -> None:
        while chunk := child.stdout.read(65536):
            if len(output) + len(chunk) > max_output_bytes:
                raise RuntimeError("paired L2 native stdout exceeded its byte guard")
            output.extend(chunk)

    def drain_stderr() -> None:
        while chunk := child.stderr.read(4096):
            error_tail.extend(chunk)
            del error_tail[:-65536]

    try:
        for query in ids:
            if source_client is None:
                readers.append(ch.fork(query_id=query))
            else:
                from .hot_l2_native_source import NativeSource

                readers.append(NativeSource(ch, source_client, query, wall_seconds))
        pairs = []
        for _ in range(2):
            pair = pipe()
            pairs.append(pair)
            fd_set.update(pair)
        child = Popen([str(binary.resolve()), '--left-fd', str(pairs[0][0]), '--right-fd', str(pairs[1][0]), '--max-cells', str(max_cells)],
                      stdin=PIPE, stdout=PIPE, stderr=PIPE, pass_fds=tuple(pair[0] for pair in pairs), bufsize=0)
        for read_fd, write_fd in pairs:
            close(read_fd)
            fd_set.remove(read_fd)
            handles.append(fdopen(write_fd, 'wb', buffering=0))
            fd_set.remove(write_fd)
        launch('hot-l2-stdout', drain_stdout)
        launch('hot-l2-stderr', drain_stderr)
        launch('hot-l2-control', write_control)
        for side, sink in enumerate(handles):
            launch(f'hot-l2-source-{side}', lambda side=side, sink=sink: pump(side, sink))
        next_progress = start + 30
        while child.poll() is None or any(thread.is_alive() for thread in threads):
            if not failures.empty():
                raise failures.get_nowait()
            if child.poll() not in (None, 0):
                raise RuntimeError(f"paired L2 native engine exited with status {child.returncode}")
            now = monotonic()
            if now - start >= wall_seconds:
                raise TimeoutError("paired L2 experiment exceeded its wall deadline")
            if progress is not None and now >= next_progress:
                progress({'stage': 'paired-stream', 'elapsed_s': round(now - start, 3), 'source_bytes': list(byte_counts)})
                next_progress = now + 30
            stop.wait(.05)
        if not failures.empty():
            raise failures.get_nowait()
        if child.returncode != 0:
            raise RuntimeError(f"paired L2 native engine exited with status {child.returncode}")
        native = loads(output, object_pairs_hook=_unique_object)
        return native, ids, monotonic() - start
    except BaseException as original:
        stop.set()
        cleanup = []
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except TimeoutExpired:
                child.kill()
                try:
                    child.wait(timeout=5)
                except BaseException as error:
                    cleanup.append(error)
        if threads:
            try:
                _cancel(ch, ids)
            except BaseException as error:
                cleanup.append(error)
        for thread in threads:
            thread.join(timeout=2)
        if any(thread.is_alive() for thread in threads):
            cleanup.append(RuntimeError("paired L2 owned workers did not stop within bounded cleanup"))
        if cleanup:
            raise BaseExceptionGroup('paired L2 failed and cleanup was incomplete', [original, *cleanup])
        raise
    finally:
        stop.set()
        for fd in fd_set:
            close(fd)
        for handle in handles:
            if not handle.closed:
                handle.close()
        if child is not None:
            for handle in (child.stdin, child.stdout, child.stderr):
                if handle is not None and not handle.closed:
                    handle.close()
        for reader in readers:
            reader.close()


def complete(native: dict, prepared: dict, declared: list[dict], max_cells: int) -> dict:
    qcount, fcount, bcount = len(prepared['patterns']), len(declared), len(prepared['buckets'])
    fields = {'schema', 'exact', 'incremental', 'levels', 'rows_read', 'registered_predicates', 'registered_frames',
              'peak_stack', 'peak_active', 'max_cells', 'emitted_cells', 'native_peak_rss_bytes', 'roots', 'cells'}
    schema = native.get('schema') if isinstance(native, dict) else None
    if schema == 'hot-l2-native-pair-v2':
        fields.update(('matcher_scans', 'cache_hits'))
    if not isinstance(native, dict) or set(native) != fields or schema not in ('hot-l2-native-pair-v1', 'hot-l2-native-pair-v2') or native['exact'] is not True or native['incremental'] is not False or type(native['levels']) is not int or native['levels'] != 2:
        raise RuntimeError("paired L2 native returned an unsupported contract")
    if native['rows_read'] != prepared['rows'] or native['registered_predicates'] != qcount or native['registered_frames'] != fcount or native['max_cells'] != max_cells:
        raise RuntimeError("paired L2 native source/catalog/control counts disagree")
    for field in ('registered_predicates', 'registered_frames', 'max_cells', 'emitted_cells', 'native_peak_rss_bytes'):
        _uint(native[field])
    for field, limits in (('rows_read', prepared['rows']), ('peak_stack', prepared['rows']), ('peak_active', [qcount, qcount])):
        if not isinstance(native[field], list) or len(native[field]) != 2 or any(_uint(n) > limit for n, limit in zip(native[field], limits, strict=True)):
            raise RuntimeError("paired L2 native returned invalid process statistics")
    if schema == 'hot-l2-native-pair-v2':
        scans, hits = _uint(native['matcher_scans']), _uint(native['cache_hits'])
        nonroots = sum(native['rows_read']) - 2
        if scans + hits != nonroots or (nonroots > 0 and scans == 0):
            raise RuntimeError("paired L2 matcher counters disagree with complete nonroot source rows")
    if any(n < 1 for n in native['peak_stack']) or native['emitted_cells'] > max_cells:
        raise RuntimeError("paired L2 native exceeded its declared guards")
    if not isinstance(native['roots'], list) or len(native['roots']) != qcount or not isinstance(native['cells'], list) or len(native['cells']) != native['emitted_cells']:
        raise RuntimeError("paired L2 native returned incomplete roots or cells")
    for q, row in enumerate(native['roots']):
        if not isinstance(row, dict) or set(row) != {'predicate_id', 'buckets'} or type(row['predicate_id']) is not int or row['predicate_id'] != q + 1 or not isinstance(row['buckets'], list) or len(row['buckets']) != bcount:
            raise RuntimeError("paired L2 native returned invalid root identities")
        actual = [[_weight(n) for n in pair] for pair in row['buckets'] if isinstance(pair, list) and len(pair) == 4]
        if actual != prepared['expected'][q]:
            raise RuntimeError("paired L2 root totals disagree with the accepted paired references")
    remaining = [[list(pair) for pair in buckets] for buckets in prepared['expected']]
    cells, previous = [], (0, 0)
    for row in native['cells']:
        if not isinstance(row, dict) or set(row) != {'predicate_id', 'frame_id', 'b', 'o'}:
            raise RuntimeError("paired L2 native returned an invalid cell")
        q, frame = _uint(row['predicate_id']), _uint(row['frame_id'])
        if not 1 <= q <= qcount or not 1 <= frame <= fcount or (frame, q) <= previous or any(not isinstance(row[key], list) or len(row[key]) != 2 for key in ('b', 'o')):
            raise RuntimeError("paired L2 native cells are unknown, duplicate or unordered")
        values = [_weight(row['b'][0]), _weight(row['o'][0]), _weight(row['b'][1]), _weight(row['o'][1])]
        geometry = declared[frame - 1]
        bucket = geometry['bucket']
        if max(values[0], values[2]) < prepared['thresholds'][q - 1][bucket]:
            raise RuntimeError("paired L2 native emitted a cell below its bucket threshold")
        remaining[q - 1][bucket] = [a - b for a, b in zip(remaining[q - 1][bucket], values, strict=True)]
        if any(n < 0 for n in remaining[q - 1][bucket]):
            raise RuntimeError("paired L2 cell partition exceeds its exact bucket totals")
        cells.append({'predicate_id': q, **geometry, 'b': [values[0], values[2]], 'o': [values[1], values[3]]})
        previous = frame, q
    results = []
    for q, pattern in enumerate(prepared['patterns'], 1):
        buckets = []
        for i, (lo, hi, path) in enumerate(prepared['buckets']):
            pair, other = prepared['expected'][q - 1][i], remaining[q - 1][i]
            buckets.append({'pre': lo, 'post': hi, 'path': path, 'threshold_bytes': prepared['thresholds'][q - 1][i],
                            'b': [pair[0], pair[2]], 'o': [pair[1], pair[3]], 'other': {'b': [other[0], other[2]], 'o': [other[1], other[3]]}})
        results.append({'predicate_id': q, 'pattern': pattern, 'root': {'b': [sum(r['b'][side] for r in buckets) for side in range(2)],
                       'o': [sum(r['o'][side] for r in buckets) for side in range(2)]}, 'buckets': buckets})
    return {'results': results, 'frames': declared, 'cells': cells, 'native': {k: v for k, v in native.items() if k not in ('roots', 'cells')}}


def bench(
    url: str,
    target: str,
    before: str,
    after: str,
    references: tuple[Path, ...],
    proofs: tuple[Path, ...],
    queries: Path,
    out: Path,
    *,
    binary: Path,
    patterns: tuple[str, ...] = (),
    all_registered: bool = False,
    budget: int = 64,
    memory_gib: int = 8,
    spill_gib: int = 8,
    seconds: int = 3600,
    wall_seconds: int = 4500,
    max_frames: int = MAX_FRAMES,
    max_cells: int = MAX_CELLS,
    max_output_bytes: int = MAX_OUTPUT,
    source_client: Path | None = None,
) -> dict:
    limits = {'memory_gib': memory_gib, 'spill_gib': spill_gib, 'source_seconds': seconds, 'wall_seconds': wall_seconds,
              'source_threads_each': 4, 'sources': 2, 'max_frames': max_frames, 'max_cells': max_cells, 'max_output_bytes': max_output_bytes}
    for value, hi in ((memory_gib, 8), (spill_gib, 8), (seconds, 3600), (wall_seconds, 4500), (max_frames, MAX_FRAMES), (max_cells, MAX_CELLS), (max_output_bytes, MAX_OUTPUT)):
        if type(value) is not int or not 1 <= value <= hi:
            raise ValueError("paired L2 resource/shape limits are outside their bounded ranges")
    if not binary.is_file() or not access(binary, X_OK):
        raise ValueError("paired L2 requires an explicit existing executable")
    if out.exists() or out.is_symlink() or not out.parent.is_dir():
        raise ValueError("paired L2 output must be new in an existing directory")
    if source_client is not None:
        from .hot_l2_native_source import validate_endpoint

        # Preflight before references/CH requests, without reading credentials.
        validate_endpoint(url.rstrip('/'), source_client, wall_seconds)
    start = monotonic()
    prepared = prepare(target, before, after, references, proofs, queries, patterns, budget, all_registered=all_registered)
    reference_s = monotonic() - start
    tag = 'hot_l2_pair_bench_' + uuid4().hex
    ch = Ch(url, db=target, timeout=seconds + 60, max_threads=4, max_memory_usage=memory_gib << 30,
            max_execution_time=seconds, timeout_before_checking_execution_speed=0, timeout_overflow_mode='throw',
            max_bytes_before_external_sort=256 << 20, max_bytes_ratio_before_external_sort=0,
            max_temporary_data_on_disk_size_for_query=spill_gib << 30, log_comment=tag)
    try:
        if source_client is not None:
            from .hot_l2_native_source import validate

            validate(ch, source_client, wall_seconds)
        declared = frames(ch, prepared, max_frames)
        native, ids, stream_s = stream(ch, prepared, declared, binary=binary, max_cells=max_cells,
                                      max_output_bytes=max_output_bytes, wall_seconds=wall_seconds,
                                      progress=lambda stage: print(dumps(stage), file=stderr),
                                      **({'source_client': source_client} if source_client is not None else {}))
        result = {'schema': 'hot-l2-pair-stream-v1', 'exact': True, 'incremental': False, 'levels': 2, 'scope': SCOPE,
                  'target': target, 'dates': prepared['dates'], 'snapshot_dbs': prepared['dbs'], 'budget': budget,
                  'cutoff_scope': 'fixed per query and bucket root across all depth-2 children; not arbitrary-prefix refinement',
                  'query_subset': prepared['query_subset'], 'registered_predicates': len(prepared['patterns']),
                  'registered_frames': len(declared), **complete(native, prepared, declared, max_cells),
                  'provenance': prepared['provenance'], 'source_query_ids': ids, 'limits': limits,
                  'timings': {'reference_load_s': reference_s, 'stream_s': stream_s, 'build_s': monotonic() - start},
                  'validation': 'complete streamed counts/order and paired L1 reference roots; L2 cell completeness requires independent fixtures/oracle acceptance',
                  'persistent_index_created': False, 'cache_state': 'uncontrolled; offline experiment, not serving latency'}
        if source_client is not None:
            result['source_transport'] = {'protocol': 'native-tcp', 'client_binary': str(source_client.resolve()),
                                          'host': '127.0.0.1', 'port': 9000, 'idle_timeout_seconds': wall_seconds + 60,
                                          'client_config': 'explicit verified empty XML; no ambient credentials'}
        ch.exec('SYSTEM FLUSH LOGS')
        result['profile'] = ch.json(f"SELECT query_id,query_duration_ms,read_rows,read_bytes,memory_usage FROM system.query_log WHERE log_comment={lit(tag)} AND type='QueryFinish' ORDER BY event_time_microseconds")
        with out.open('x') as output:
            output.write(dumps(result) + '\n')
        return result
    finally:
        ch.close()
