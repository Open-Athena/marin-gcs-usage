"""Exact hot qualification from one accepted global date-only scalar tree.

Physical basename staging sorts insert blocks; in-order grouping never needs
a full-vocabulary hash/window. No history, IDs, owner or coverage counts are
constructed. The original single-date export contract remains unchanged.
"""

from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from time import monotonic
from uuid import uuid4

from .client import Ch, lit
from .daily_scalar import manifest_bytes
from .hot_frequency import census_weighted, normalize_patterns, validate_limits, within
from .hot_registry_selection import DOCUMENT_LIMIT, _source
from .narrow import disk_reserve, identifier


def source_bytes(path: Path, target: str, date: str) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise ValueError('daily hot-frequency source must be an explicit regular manifest file')
    with path.open('rb') as source:
        raw = source.read(DOCUMENT_LIMIT + 1)
    source_body(raw, target, date)
    return raw


def source_body(raw: bytes, target: str, date: str) -> dict:
    body = _source(raw)
    if body['target'] != target or body['date'] != date or manifest_bytes(body) != raw:
        raise ValueError('daily hot-frequency requires matching canonical accepted source target/date')
    return body


def weighted_select(table: str, *, database: str | None = None) -> str:
    identifier(table)
    source = table if database is None else identifier(database) + '.' + table
    return f'SELECT l,count() AS c FROM {source} GROUP BY l SETTINGS optimize_aggregation_in_order=1'


class DailyCensusCh(Ch):
    """A nonrenewable census deadline and session-owned staging accounting."""

    def __init__(self, url: str, *, wall_seconds: int, staging_bytes: int, **settings: object) -> None:
        super().__init__(url, **settings)
        self.end, self.staging_bytes = monotonic() + wall_seconds, staging_bytes
        self.statement_seconds = float(self.settings['max_execution_time'])
        self.ids: list[str] = []
        self.peak_staging_bytes = 0

    def _open(self, sql: str, data=None, settings: dict | None = None):
        left = self.end - monotonic()
        if left <= 0:
            raise TimeoutError('daily hot-frequency total wall budget exhausted; no complete census')
        query = 'daily_hot_frequency_' + uuid4().hex
        self.ids.append(query)
        self.timeout = min(left, self.statement_seconds + 60)
        return super()._open(sql, data, {**(settings or {}), 'query_id': query,
                                       'max_execution_time': min(left, self.statement_seconds)})

    def checkpoint(self) -> None:
        disk_reserve(self, 'daily hot-frequency stage boundary', 20 << 30)
        if not self._tmp:
            return
        tables = self.json('SELECT name,total_bytes FROM system.tables WHERE is_temporary AND name IN (' + ','.join(map(lit, self._tmp)) + ') ORDER BY name')
        if ([row[0] for row in tables] != sorted(self._tmp) or
                any(type(row[1]) is not int or row[1] < 0 for row in tables)):
            raise RuntimeError('daily hot-frequency temporary table byte accounting is incomplete')
        size = sum(row[1] for row in tables)
        self.peak_staging_bytes = max(self.peak_staging_bytes, size)
        if size > self.staging_bytes:
            raise RuntimeError('daily hot-frequency temporary staging cap exceeded; no complete census')

    def check_wall(self) -> None:
        if self.end <= monotonic():
            raise TimeoutError('daily hot-frequency total wall budget exhausted; no complete census')

    def close(self) -> None:
        # Cleanup has its own bounded window; do not suppress failed drops.
        self.end, self.statement_seconds = monotonic() + 60, 10
        control = Ch(self.url, db=self.db, session=False, timeout=10, max_execution_time=10)
        failures = []
        try:
            if self.ids:
                owned = ','.join(map(lit, self.ids))
                try:
                    control.exec('KILL QUERY WHERE query_id IN (' + owned + ') SYNC', fmt=None)
                    if control.scalar('SELECT count() FROM system.processes WHERE query_id IN (' + owned + ')') != '0':
                        raise RuntimeError('owned queries are not quiescent')
                except (OSError, RuntimeError):
                    failures.append('owned query cleanup')
            for table in reversed(self._tmp):
                try:
                    self.exec(f'DROP TEMPORARY TABLE IF EXISTS {identifier(table)}', fmt=None)
                except (OSError, RuntimeError):
                    failures.append('owned temporary table cleanup')
            if failures:
                raise RuntimeError('daily hot-frequency owned cleanup could not be verified')
            self._tmp.clear()
        finally:
            control.close()


def census(
    ch: DailyCensusCh,
    target: str,
    date: str,
    raw: bytes,
    threshold: int,
    max_chars: int = 16,
    patterns: tuple[str, ...] = (),
    *,
    progress: Callable[[dict], None] | None = None,
    on_hot_table: Callable[[int, str], None] | None = None,
    thresholds: tuple[int, ...] = (),
    max_patterns: int = 500_000,
) -> dict:
    validate_limits(threshold, max_chars, thresholds, max_patterns)
    patterns = normalize_patterns(patterns)
    body = source_body(raw, target, date)

    def marker() -> None:
        found = ch.json(f"SELECT length(doc),if(length(doc)<={DOCUMENT_LIMIT},doc,'') FROM {target}.source_manifest LIMIT 2")
        if found != [[len(raw) - 1, raw.decode('utf-8')[:-1]]]:
            raise ValueError('daily hot-frequency source marker changed or differs from the pinned source')

    def report(row: dict) -> None:
        ch.checkpoint()
        if progress is not None:
            progress(row)

    def export(chars: int, table: str) -> None:
        ch.checkpoint()
        if on_hot_table is not None:
            on_hot_table(chars, table)

    marker()
    started, tag = monotonic(), uuid4().hex
    names, weighted = f'daily_frequency_names_{tag}', f'daily_frequency_weighted_{tag}'
    ch.checkpoint()
    if progress is not None:
        progress({'stage': 'daily-basename-staging', 'status': 'started'})
    ch.tmp(names, f"SELECT lowerUTF8(arrayElement(splitByChar('/',assumeNotNull(path)),-1)) AS l FROM {target}.nodes",
           disk=True, order_by='l', settings={'max_insert_threads': 1, 'min_insert_block_size_rows': 1 << 20,
                                             'min_insert_block_size_bytes': 64 << 20})
    ch.checkpoint()
    if progress is not None:
        progress({'stage': 'daily-basename-staging', 'status': 'complete', 'elapsed_s': monotonic() - started})
    if ch.scalar(f"SELECT countIf(NOT isValidUTF8(l) OR position(l,'\\0')>0) FROM {names}") != '0':
        raise ValueError('daily hot-frequency refuses invalid UTF-8 or NUL basename source names')
    if progress is not None:
        progress({'stage': 'daily-weighted-names', 'status': 'started'})
    ch.tmp(weighted, weighted_select(names), disk=True, order_by='l', settings={'optimize_aggregation_in_order': 1})
    result = census_weighted(ch, target, target, date, threshold, max_chars, patterns, weighted=weighted,
                             tag=tag, started=started, progress=report, on_hot_table=export,
                             thresholds=thresholds, max_patterns=max_patterns, expected_paths=body['nodes'])
    marker()
    result['source_provenance'] = {'kind': 'accepted global daily scalar nodes; no historical/name-ID ingestion',
                                   'logical_store': body['logical_store'], 'qualification_date': date,
                                   'source_manifest_sha256': sha256(raw).hexdigest(), 'source_manifest_bytes': len(raw),
                                   'snapshot_db': target, 'nodes': body['nodes'], 'source': body['source'],
                                   'validation': body['validation'], 'independent_frequency_source_oracle': False}
    return result


def native_census(
    ch: DailyCensusCh,
    target: str,
    date: str,
    raw: bytes,
    threshold: int,
    max_chars: int | None,
    binary: Path,
    patterns: tuple[str, ...] = (),
    *,
    threads: int = 8,
    progress: Callable[[dict], None] | None = None,
    thresholds: tuple[int, ...] = (),
    max_patterns: int = 500_000,
) -> tuple[dict, list[bytes]]:
    """The same qualification with the per-length passes in `native/hot_frequency.cpp`:
    ClickHouse groups the snapshot's lowercase basenames (one statement, no staged
    tables) and streams `(l, c)` as RowBinary into the binary, which validates the
    names (separators, NUL, UTF-8), counts, and writes the hot patterns. Returns the
    census body and the query export's pattern lines (`chars` then bytewise order).

    `max_chars` None is the complete length domain: every length until one has
    no hot pattern (that empty length is the census's last layer), so any
    unlisted literal, of any length, is below the threshold."""
    from json import loads
    from subprocess import PIPE, Popen
    from sys import stderr
    from threading import Thread

    cuts = validate_limits(threshold, max_chars, thresholds, max_patterns)
    patterns = normalize_patterns(patterns)
    body = source_body(raw, target, date)
    if not binary.is_file():
        raise ValueError('native hot-frequency binary is not a regular file')

    def marker() -> None:
        found = ch.json(f"SELECT length(doc),if(length(doc)<={DOCUMENT_LIMIT},doc,'') FROM {target}.source_manifest LIMIT 2")
        if found != [[len(raw) - 1, raw.decode('utf-8')[:-1]]]:
            raise ValueError('daily hot-frequency source marker changed or differs from the pinned source')

    marker()
    started = monotonic()
    stages: list[dict] = []
    proc = Popen([str(binary), str(threshold), str(max_chars or 0), str(threads), str(max_patterns)], stdin=PIPE, stdout=PIPE, stderr=PIPE)
    out: list[bytes] = []
    err: list[bytes] = []

    def drain_out() -> None:
        out.append(proc.stdout.read())

    def drain_err() -> None:
        for line in proc.stderr:
            err.append(line)
            if line.startswith(b'{'):
                stage = loads(line)
                stages.append(stage)
                if progress is not None:
                    progress({'engine': 'native', **stage})

    readers = [Thread(target=drain_out), Thread(target=drain_err)]
    for reader in readers:
        reader.start()
    try:
        sql = (f"SELECT lowerUTF8(arrayElement(splitByChar('/',assumeNotNull(path)),-1)) AS l,count() AS c "
               f"FROM {target}.nodes GROUP BY l")
        for chunk in ch.stream(sql, fmt='RowBinary', settings={'max_bytes_before_external_group_by': 4 << 30}):
            proc.stdin.write(chunk)
            ch.check_wall()
        proc.stdin.close()
        while proc.poll() is None:
            ch.check_wall()
            for reader in readers:
                reader.join(timeout=1)
    except BaseException:
        proc.kill()
        raise
    finally:
        for reader in readers:
            reader.join()
    if proc.wait() != 0:
        raise RuntimeError('native hot-frequency failed: ' + b''.join(err).decode(errors='replace').strip())
    lines = out[0].splitlines(keepends=True)
    header, footer, rows = loads(lines[0]), loads(lines[-1]), lines[1:-1]
    if header != {'schema': 'hot-frequency-queries-v1', 'engine': 'native', 'threshold_paths': threshold, 'max_chars': max_chars}:
        raise RuntimeError('native hot-frequency header disagrees with the request')
    if footer != {'complete': True, 'patterns': len(rows)}:
        raise RuntimeError('native hot-frequency output is incomplete')
    read = next(stage for stage in stages if stage['stage'] == 'read')
    if read['paths'] != body['nodes']:
        raise ValueError('daily hot-frequency weighted path count differs from its accepted source')
    marker()

    parsed = [loads(row) for row in rows]
    timing = {stage['chars']: stage['count_s'] + stage['assign_s'] for stage in stages if stage['stage'] == 'hot-substrings'}
    lengths, empty = [], False
    last = max_chars or max((row['chars'] for row in parsed), default=0) + 1
    for chars in range(1, last + 1):
        hot = [row for row in parsed if row['chars'] == chars]
        layer = {'chars': chars, 'hot_patterns': len(hot),
                 'hot_query_utf8_bytes': sum(len(row['pattern'].encode()) for row in hot),
                 'sum_hot_direct_matching_paths': sum(row['direct_matching_paths'] for row in hot),
                 'elapsed_s': timing.get(chars, 0.), 'pruned_by_empty_prefix': empty}
        if cuts:
            layer['threshold_counts'] = [{'threshold_paths': cut, 'hot_patterns': sum(row['direct_matching_paths'] >= cut for row in hot)} for cut in cuts]
        lengths.append(layer)
        empty = empty or not hot
    found = {row['pattern']: row['direct_matching_paths'] for row in parsed}
    result = {
        'schema': 'hot-frequency-v1', 'engine': 'native', 'target': target, 'snapshot_db': target, 'date': date,
        'scope': 'complete snapshot lowercase basename substrings; direct paths, not inherited coverage or occurrence windows',
        'threshold_paths': threshold, 'max_chars': max_chars, 'distinct_names': read['distinct_names'], 'paths': read['paths'],
        'weighted_names_s': read['elapsed_s'], 'lengths': lengths,
        'selected_patterns': [{'pattern': pattern, 'hot': pattern in found, 'direct_matching_paths': found.get(pattern)}
                              for pattern in patterns if within(len(pattern), max_chars)],
        'temporary_index': 'none: one streamed GROUP BY', 'persistent_index_created': False,
        'native_s': monotonic() - started,
    }
    if cuts:
        result['thresholds_paths'] = list(cuts)
    result['source_provenance'] = {'kind': 'accepted global daily scalar nodes; no historical/name-ID ingestion',
                                   'logical_store': body['logical_store'], 'qualification_date': date,
                                   'source_manifest_sha256': sha256(raw).hexdigest(), 'source_manifest_bytes': len(raw),
                                   'snapshot_db': target, 'nodes': body['nodes'], 'source': body['source'],
                                   'validation': body['validation'], 'independent_frequency_source_oracle': False}
    print(f'native hot-frequency: {len(rows)} patterns in {result["native_s"]:.1f}s', file=stderr)
    return result, rows
