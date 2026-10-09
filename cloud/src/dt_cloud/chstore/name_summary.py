"""Opt-in stitched frozen-root summaries; no route or canonical-reader change.

Published artifacts and the separately audited frozen source stay immutable.
Startup compares their identities/geometry, not all billion source values.
One cold worker owns its CH session until verified cleanup; an unverified
cleanup quarantines the lane until a later re-verification succeeds. A response deadline does not imply that CH can
interrupt every analysis phase instantly. No source oracle runs per request.
"""

from dataclasses import dataclass
from datetime import date as Date
from hashlib import sha256
from json import loads
from os import cpu_count, environ
from pathlib import Path
from sys import stderr
from threading import BoundedSemaphore, Event, Lock, Thread
from time import monotonic
from typing import Callable
from uuid import uuid4

from .client import Ch, lit
from .coarse import CoarseRequest
from .hot_l1 import _buckets, build
from .hot_l1_batch_catalog import HotL1BatchCatalog, _entry, _literal
from .hot_l1_catalog import CatalogRequest, SCOPE, _unique_object
from .hot_l1_publish import load_pinned, pin
from .narrow import identifier

COMPUTE_SECONDS = 5.0
# ClickHouse threads per cold statement: the one cold slot may use the node,
# up to 16 (`NAME_SUMMARY_COLD_THREADS` overrides).
COLD_THREADS = int(environ.get('NAME_SUMMARY_COLD_THREADS') or min(16, cpu_count() or 4))
CLEANUP_SECONDS = 2.0
# The client's socket outlives each statement's own `max_execution_time` by this much, so a statement that runs out
# (or that the watchdog kills) ends with ClickHouse's error over the open connection: a known outcome, verified
# cleanup, no quarantine. Only a server that doesn't answer at all leaves the transport uncertain.
TRANSPORT_GRACE_SECONDS = 1.0
# A request waits this long (within its own deadline) for the one cold slot, e.g. while the previous request's owned
# work is being cancelled and verified, before reporting it busy.
SLOT_WAIT_SECONDS = 1.0
# A finished query can linger in `system.processes` while it finalizes; quiescence polls this long before refusing.
QUIESCENCE_SECONDS = 1.0
# A quarantined lane re-verifies after every owned query's own
# `max_execution_time` has run out (a late-dispatched one carries it too), then
# at twice and three times that; failing all three, it stays quarantined.
RECOVERY_SECONDS = COMPUTE_SECONDS + CLEANUP_SECONDS
RECOVERY_ATTEMPTS = 3
CAPS = {'max_names': 200_000, 'max_postings': 100_000, 'max_roots': 100_000}


class SummaryUnavailable(RuntimeError):
    """A controlled refusal, never a zero or partial match result."""


class SummaryBusy(SummaryUnavailable):
    pass


class SummaryDeadline(SummaryUnavailable):
    pass


@dataclass(frozen=True)
class SourceBinding:
    target: str
    dates: tuple[tuple[str, str], ...]
    buckets: tuple[tuple[int, int, str], ...]
    generation: str
    manifest_sha256: str


def bind(ch: Ch, catalog: HotL1BatchCatalog, published: dict, target: str) -> SourceBinding:
    """Small startup reads only; load_pinned already checked artifact/proofs."""
    identifier(target)
    if catalog.target != target or published['metadata'] != catalog.metadata():
        raise ValueError('name summary published target/metadata differs from selected source')
    if published.get('source_prefix_validation', {}).get('checked') is not True:
        raise ValueError('name summary requires published artifact-bound prefix proofs')
    raw = ch.scalar(f'SELECT doc FROM {target}.history_manifest')
    manifest = loads(raw, object_pairs_hook=_unique_object)
    dates = tuple((row['date'], row['snapshot_db']) for row in catalog.metadata()['dates'])
    if (manifest.get('prefix') != '' or manifest.get('dates') != [day for day, _ in dates] or
            manifest.get('dbs') != [db for _, db in dates]):
        raise ValueError('name summary frozen source dates/databases differ from published catalog')
    bounds = tuple(tuple(row) for row in _buckets(ch, target))
    for day, _ in dates:
        patterns = catalog.registered_patterns(day)
        if not patterns:
            raise ValueError('name summary needs a published bucket-geometry declaration')
        view = catalog.view(day, patterns[0])
        expected = tuple(sorted((row['pre'], row['post'], row['path']) for row in view['buckets']))
        if bounds != expected:
            raise ValueError('name summary frozen bucket bounds differ from published catalog')
    return SourceBinding(target, dates, bounds, published['generation'], sha256(raw.encode()).hexdigest())


class DeadlineCh(Ch):
    """Existing HTTP/session client with owned IDs and a nonrenewable deadline."""

    def __init__(self, url: str, target: str, deadline: float, stopped: Event, request_id: str) -> None:
        super().__init__(url, db=target, timeout=COMPUTE_SECONDS, max_threads=COLD_THREADS, max_memory_usage=4 << 30,
                         max_execution_time=COMPUTE_SECONDS, timeout_before_checking_execution_speed=0,
                         timeout_overflow_mode='throw', max_bytes_before_external_sort=256 << 20,
                         max_bytes_ratio_before_external_sort=0, max_bytes_before_external_group_by=256 << 20,
                         max_bytes_ratio_before_external_group_by=0, max_temporary_data_on_disk_size_for_query=4 << 30,
                         log_comment=request_id)
        self.settings['session_timeout'] = '60'
        self.deadline, self.stopped, self.request_id = deadline, stopped, request_id
        self.ids, self.ids_lock, self.cleanup_deadline = [], Lock(), None
        self.transport_uncertain = False

    def _open(self, sql: str, data=None, settings: dict | None = None):
        with self.ids_lock:
            limit = self.deadline if self.cleanup_deadline is None else self.cleanup_deadline
            remaining = limit - monotonic()
            if remaining <= 0 or (self.cleanup_deadline is None and self.stopped.is_set()):
                raise SummaryDeadline('name summary exceeded its total compute deadline; no partial result')
            query = f'{self.request_id}_{len(self.ids) + 1:04d}'
            self.ids.append(query)
            self.timeout = remaining + TRANSPORT_GRACE_SECONDS
        selected = {**(settings or {}), 'query_id': query, 'max_execution_time': remaining,
                    'timeout_before_checking_execution_speed': 0, 'timeout_overflow_mode': 'throw'}
        try:
            return super()._open(sql, data, selected)
        except OSError:
            self.transport_uncertain = True
            raise

    def exec(self, sql: str, *, fmt: str | None = 'TSV', settings: dict | None = None) -> str:
        try:
            return super().exec(sql, fmt=fmt, settings=settings)
        except OSError:
            # A closed client socket + a transient empty process list cannot
            # establish that a queued HTTP dispatch will never start later.
            self.transport_uncertain = True
            raise

    def owned_ids(self) -> tuple[str, ...]:
        with self.ids_lock:
            return tuple(self.ids)

    def cleanup(self) -> None:
        """Do not call Ch.close: it suppresses failed temporary-table drops."""
        self.cleanup_deadline = monotonic() + CLEANUP_SECONDS
        failures = []
        try:
            for table in reversed(self._tmp):
                try:
                    self.exec(f'DROP TEMPORARY TABLE IF EXISTS {identifier(table)}', fmt=None)
                except BaseException:
                    failures.append('owned temporary-table cleanup failed')
            if failures:
                raise SummaryUnavailable('name summary cleanup could not be verified; cold lane quarantined')
            self._tmp.clear()
        finally:
            self.cleanup_deadline = None


def cancel_owned(source: DeadlineCh, *, cancel: bool) -> None:
    """Only exact owned query IDs; independently bounded control requests."""
    ids = source.owned_ids()
    if not ids:
        return
    selected = ','.join(lit(query) for query in ids)
    control = Ch(source.url, db=source.db, session=False, timeout=1,
                 max_execution_time=1, timeout_before_checking_execution_speed=0, timeout_overflow_mode='throw')
    try:
        if cancel:
            control.exec(f'KILL QUERY WHERE query_id IN ({selected}) SYNC', fmt=None)
        deadline = monotonic() + QUIESCENCE_SECONDS
        while control.scalar(f'SELECT count() FROM system.processes WHERE query_id IN ({selected})') != '0':
            if monotonic() >= deadline:
                raise SummaryUnavailable('name summary query quiescence could not be verified')
            Event().wait(.02)
    finally:
        control.close()


def _view(binding: SourceBinding, body: dict, plan: str, date: str, pattern: str) -> dict:
    entry = _entry(body, binding.target, body['date'], 512)
    if (body.get('target') != binding.target or body.get('date') != date or body.get('pattern') != pattern or
            body.get('scope') != SCOPE or body.get('exact') is not True or
            body.get('incremental') is not False or body['date'] not in dict(binding.dates) or
            tuple(sorted((row.pre, row.post, row.path) for row in entry.buckets)) != binding.buckets):
        raise SummaryUnavailable('name summary returned an inconsistent frozen partition; no partial result')
    result = {'schema': 'name-summary-v1', 'target': binding.target, 'date': body['date'], 'pattern': entry.pattern,
              'path': '', 'exact': True, 'incremental': False, 'levels': 1, 'scope': SCOPE,
              'plan': plan, 'root': entry.root, 'buckets': [row.body() for row in sorted(entry.buckets, key=lambda row: row.pre)],
              'source': 'registered precomputed batch artifact' if plan == 'catalog' else 'bounded dated name postings; directory rollups are atomic',
              'validation': {'source_prefix_proofs_checked': True, 'independent_query_source_oracle': False,
                             'description': 'pinned catalog validation' if plan == 'catalog' else 'bounded exact first-hit coverage; no per-request source oracle'},
              'source_identity': {'generation': binding.generation, 'snapshot_db': dict(binding.dates)[body['date']],
                                  'history_manifest_sha256': binding.manifest_sha256}}
    if plan == 'catalog':
        result['validation']['catalog'] = body['validation']
    else:
        if body.get('snapshot_db') != dict(binding.dates)[body['date']]:
            raise SummaryUnavailable('name summary source database changed; no partial result')
        result.update({key: body[key] for key in ('work_bounds', 'direct_matching_rows', 'stages')})
    return result


def _diff(before: dict, after: dict) -> dict:
    weights = lambda a, b: {key: b[key] - a[key] for key in ('b', 'o')}
    return {'schema': 'name-summary-diff-v1', 'target': after['target'], 'pattern': after['pattern'], 'path': '',
            'exact': True, 'incremental': False, 'levels': 1, 'scope': SCOPE, 'before': before, 'after': after,
            'delta': weights(before['root'], after['root']),
            'buckets': [{'pre': a['pre'], 'post': a['post'], 'path': a['path'],
                         'before': {key: a[key] for key in ('b', 'o')}, 'after': {key: b[key] for key in ('b', 'o')},
                         'delta': weights(a, b)} for a, b in zip(before['buckets'], after['buckets'], strict=True)]}


class NameSummaryRuntime:
    def __init__(self, catalog: HotL1BatchCatalog, binding: SourceBinding, url: str) -> None:
        if catalog.target != binding.target:
            raise ValueError('name summary catalog/source target mismatch')
        self.catalog, self.binding, self.url = catalog, binding, url
        self.registered = {day: frozenset(catalog.registered_patterns(day)) for day, _ in binding.dates}
        self.gate, self.hot_gate, self.quarantined = BoundedSemaphore(1), BoundedSemaphore(2), False
        self.recovery_seconds = RECOVERY_SECONDS

    def _recover(self, source: 'DeadlineCh | None') -> None:
        """Reopen a quarantined slot once its owned work is verifiably gone: kill
        and check the exact owned query IDs, drop the owned temporary tables."""
        for attempt in range(1, RECOVERY_ATTEMPTS + 1):
            Event().wait(self.recovery_seconds * attempt)
            try:
                if source is not None:
                    cancel_owned(source, cancel=True)
                    source.cleanup()
                    cancel_owned(source, cancel=False)
            except BaseException:
                continue
            print(f'name summary: cold lane recovered (attempt {attempt})', file=stderr)
            self.quarantined = False
            self.gate.release()
            return
        print('name summary: cold lane recovery failed; quarantined until restart', file=stderr)

    @property
    def target(self) -> str:
        return self.binding.target

    @classmethod
    def load(
        cls,
        generation_root: Path,
        url: str,
        *,
        target: str,
        catalog: HotL1BatchCatalog | None = None,
        published: dict | None = None,
    ) -> 'NameSummaryRuntime':
        """Reuse only the exact catalog/manifest pair already load_pinned-verified."""
        if (catalog is None) != (published is None):
            raise ValueError('name summary catalog reuse requires its exact verified pinned manifest')
        if catalog is None:
            published = pin(generation_root)
            catalog = load_pinned(generation_root, published)
        ch = Ch(url, db=target, timeout=2, max_execution_time=2, timeout_before_checking_execution_speed=0, timeout_overflow_mode='throw')
        try:
            binding = bind(ch, catalog, published, target)
        finally:
            ch.close()
        return cls(catalog, binding, url)

    def metadata(self) -> dict:
        return {'schema': 'name-summary-registry-v1', 'target': self.binding.target,
                'dates': [day for day, _ in self.binding.dates], 'levels': 1, 'scope': SCOPE,
                'catalog_patterns': {day: len(patterns) for day, patterns in self.registered.items()},
                'source_prefix_proofs_checked': True, 'cold_slots': 1, 'catalog_slots': 2,
                'compute_seconds': COMPUTE_SECONDS, 'work_bounds': dict(CAPS), 'cold_quarantined': self.quarantined}

    def view(self, date: str, pattern: str, *, path: str = '') -> dict:
        return self._request((date,), pattern, path)

    def diff(self, before: str, after: str, pattern: str, *, path: str = '') -> dict:
        return self._request((before, after), pattern, path)

    def _request(self, dates: tuple[str, ...], pattern: str, path: str) -> dict:
        if path != '':
            raise CatalogRequest('name summary serves the global root only; no drill fallback')
        try:
            pattern = _literal(pattern)
            if any(not isinstance(day, str) or Date.fromisoformat(day).isoformat() != day for day in dates):
                raise ValueError('invalid date')
        except (ValueError, UnicodeError):
            raise CatalogRequest('name summary requires valid ISO scans and one UTF-8 NUL/slash-free literal of at most 512 characters') from None
        if any(day not in self.registered for day in dates):
            raise CatalogRequest('name summary scan is outside the pinned frozen source; no fallback')
        if len(dates) == 2 and dates[0] >= dates[1]:
            raise CatalogRequest('name summary baseline must precede the selected scan')
        hot = {}
        if any(pattern in self.registered[day] for day in dates):
            if not self.hot_gate.acquire(blocking=False):
                raise SummaryBusy('name summary catalog serving slots busy; retry shortly')
            try:
                hot = {day: _view(self.binding, self.catalog.view(day, pattern), 'catalog', day, pattern) for day in dates if pattern in self.registered[day]}
            finally:
                self.hot_gate.release()
        if len(hot) == len(dates):
            sides = [hot[day] for day in dates]
            return sides[0] if len(sides) == 1 else _diff(*sides)

        def compute(source: DeadlineCh, checkpoint: Callable[[], None]) -> dict:
            sides = []
            for day in dates:
                checkpoint()
                sides.append(hot[day] if day in hot else _view(self.binding, build(source, self.binding.target, day, pattern, **CAPS), 'bounded-name-postings', day, pattern))
            checkpoint()
            return sides[0] if len(sides) == 1 else _diff(*sides)

        return self.bounded(self.binding.target, compute)

    def bounded(self, target: str, compute: Callable[[DeadlineCh, Callable[[], None]], object]) -> object:
        """Run `compute` in the one cold slot under the total compute deadline,
        with owned-query cancellation and verified cleanup (or quarantine).
        `compute(source, checkpoint)`; `checkpoint()` raises once the deadline passed."""
        if self.quarantined:
            raise SummaryBusy('name summary cold lane is quarantined; catalog reads remain available')
        deadline = monotonic() + COMPUTE_SECONDS
        if not self.gate.acquire(timeout=min(SLOT_WAIT_SECONDS, COMPUTE_SECONDS / 2)):
            raise SummaryBusy('name summary cold serving slot busy; retry shortly')
        if self.quarantined:
            self.gate.release()
            raise SummaryBusy('name summary cold lane is quarantined; catalog reads remain available')
        stopped, finished, done = Event(), Event(), Event()
        outcome = []

        def checkpoint() -> None:
            if stopped.is_set() or monotonic() >= deadline:
                raise SummaryDeadline('name summary exceeded its total compute deadline; no partial result')

        def run() -> None:
            source, body, error, clean, timer = None, None, None, False, None
            try:
                source = DeadlineCh(self.url, target, deadline, stopped, 'name_summary_' + uuid4().hex)

                def watchdog() -> None:
                    if not finished.wait(max(0, deadline - monotonic())):
                        stopped.set()
                        try:
                            cancel_owned(source, cancel=True)
                        except BaseException:
                            # Final cleanup re-cancels/verifies after dispatch stopped,
                            # closing the deadline-vs-request-dispatch race.
                            pass

                timer = Thread(target=watchdog, name='name-summary-deadline', daemon=True)
                timer.start()
                body = compute(source, checkpoint)
            except BaseException as failure:
                stopped.set()
                print(f'name summary: cold computation failed: {type(failure).__name__}: {str(failure)[:300]}', file=stderr)
                error = failure if isinstance(failure, SummaryUnavailable) else SummaryUnavailable('bounded name summary unavailable or over budget; no partial result')
            finally:
                finished.set()
                timer_stopped = True
                if timer is not None:
                    try:
                        timer.join(timeout=CLEANUP_SECONDS)
                        timer_stopped = not timer.is_alive()
                    except BaseException:
                        timer_stopped = False
                try:
                    if source is not None:
                        cancel_owned(source, cancel=stopped.is_set())
                        source.cleanup()
                        cancel_owned(source, cancel=False)
                    clean = timer_stopped and not getattr(source, 'transport_uncertain', False)
                except BaseException:
                    clean = False
                if not clean:
                    error = SummaryUnavailable('name summary cleanup could not be verified; cold lane quarantined')
                if clean:
                    self.gate.release()
                else:
                    self.quarantined = True
                    Thread(target=self._recover, args=(source,), name='name-summary-recover', daemon=True).start()
                outcome.append(error if error is not None else body)
                done.set()

        worker = Thread(target=run, name='name-summary-cold', daemon=True)
        try:
            worker.start()
        except BaseException:
            self.gate.release()
            raise SummaryUnavailable('name summary worker unavailable; no partial result') from None
        if not done.wait(max(0, deadline - monotonic())) or monotonic() >= deadline:
            stopped.set()
            raise SummaryDeadline('name summary exceeded its total compute deadline; no partial result')
        selected = outcome[0]
        if isinstance(selected, BaseException):
            raise selected
        return selected
