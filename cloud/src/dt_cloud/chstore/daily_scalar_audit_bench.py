"""Bounded read-only comparison of legacy and fused complete-tree audits."""

from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Iterable, Iterator
from uuid import uuid4

from .client import Ch, lit
from .daily_scalar import audited_intervals, audited_preorder, wire_select
from .daily_scalar_check import MAX_MANIFEST, MAX_NODES, _manifest
from .narrow import preorder_intervals


def bench(
    ch: Ch,
    manifest: Path,
    *,
    trials: int = 1,
    seconds: int = 120,
) -> dict:
    if type(trials) is not int or not 1 <= trials <= 3 or type(seconds) is not int or not 1 <= seconds <= 300:
        raise ValueError('daily scalar audit benchmark requires 1..3 trials and 1..300 total seconds')
    raw, body = _manifest(manifest, MAX_NODES)
    target, count, prefix = body['target'], body['nodes'], body['prefix']
    end = monotonic() + seconds
    client = ch.fork(max_threads=1, max_memory_usage=1 << 30,
                     max_temporary_data_on_disk_size_for_query=256 << 20,
                     timeout_before_checking_execution_speed=0, timeout_overflow_mode='throw')
    ids, readers, rows, expected = [], [], [], None

    def remaining() -> float:
        left = end - monotonic()
        if left <= 0:
            raise TimeoutError('daily scalar audit benchmark exceeded its total wall budget')
        return left

    def settings() -> dict:
        query = 'daily_scalar_audit_' + uuid4().hex
        ids.append(query)
        return {'query_id': query, 'max_execution_time': remaining()}

    def checked_chunks(chunks: Iterable[bytes]) -> Iterator[bytes]:
        for chunk in chunks:
            remaining()
            yield chunk

    def marker() -> None:
        client.timeout = remaining()
        found = client.json(f"SELECT length(doc),if(length(doc)<={MAX_MANIFEST},doc,'') FROM {target}.source_manifest LIMIT 2", settings=settings())
        if found != [[len(raw) - 1, raw.decode('utf-8')[:-1]]]:
            raise ValueError('daily scalar audit benchmark source marker differs from its pinned manifest')

    try:
        marker()
        for trial in range(trials):
            for plan in (('legacy', 'fused') if trial % 2 == 0 else ('fused', 'legacy')):
                reader = client.fork()
                readers.append(reader)
                reader.timeout = remaining()
                started, digest, size = monotonic(), sha256(), 0
                chunks = reader.stream(wire_select(target), fmt='RowBinary', settings=settings())
                checked = checked_chunks(chunks)
                output = (audited_intervals(checked, count, prefix) if plan == 'fused' else
                          preorder_intervals(audited_preorder(checked, count, prefix), count))
                try:
                    for chunk in output:
                        remaining()
                        size += len(chunk)
                        if size > count * 12:
                            raise AssertionError('daily scalar audit benchmark emitted excess endpoints')
                        digest.update(chunk)
                finally:
                    output.close()
                    chunks.close()
                identity = size, digest.hexdigest()
                if size != count * 12 or (expected is not None and identity != expected):
                    raise AssertionError('daily scalar audit benchmark complete endpoint identities disagree')
                expected = identity
                rows.append({'trial': trial + 1, 'plan': plan, 'seconds': monotonic() - started,
                             'endpoint_bytes': size, 'endpoint_sha256': identity[1]})
        marker()
        owned = ','.join(map(lit, ids))
        if client.scalar(f'SELECT count() FROM system.processes WHERE query_id IN ({owned})', settings=settings()) != '0':
            raise RuntimeError('daily scalar audit benchmark owned source queries are not quiescent')
        remaining()
        return {'schema': 'daily-scalar-audit-bench-v1', 'target': target, 'date': body['date'], 'nodes': count,
                'source_manifest_bytes': len(raw), 'source_manifest_sha256': sha256(raw).hexdigest(),
                'complete_endpoint_agreement': True, 'independent_source_oracle': False, 'trials': rows,
                'limits': {'max_nodes': MAX_NODES, 'trials': trials, 'total_seconds': seconds,
                           'query_memory_bytes': 1 << 30, 'query_spill_bytes': 256 << 20, 'cleanup_seconds': 10},
                'cache_state': 'uncontrolled; alternating order, not cold-cache or serving latency'}
    except BaseException as error:
        if ids:
            try:
                client.timeout = 10
                client.exec('KILL QUERY WHERE query_id IN (' + ','.join(map(lit, ids)) + ') SYNC', fmt=None)
            except (OSError, RuntimeError) as cleanup:
                error.add_note('daily scalar audit benchmark owned cancellation could not be verified: ' + type(cleanup).__name__)
        raise
    finally:
        for reader in readers:
            reader.close()
        client.close()
