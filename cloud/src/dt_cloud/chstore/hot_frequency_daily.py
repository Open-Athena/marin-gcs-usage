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
from .hot_frequency import census_weighted, normalize_patterns, validate_limits
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
