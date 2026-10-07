"""Dated catalog stitching; old-only bodies and compute budgets stay unchanged.

An explicit logical-store/bucket binding is operator authority. Daily numeric
geometry is snapshot-local; mixed comparisons align only bucket paths. New
scans never trigger cold discovery, and no new bucket detail is advertised.
"""

from copy import deepcopy
from re import fullmatch
from threading import BoundedSemaphore
from types import MappingProxyType
from typing import TYPE_CHECKING

from .hot_l1_batch_catalog import _date, _literal
from .hot_l1_catalog import CatalogRequest, SCOPE
from .name_summary import SummaryBusy, SummaryUnavailable

if TYPE_CHECKING:
    from .dated_hot_l1_publish import PublishedDatedL1
    from .name_summary import NameSummaryRuntime

CAPABILITIES = {'bucket_drill': False, 'child_drill': False, 'fallback': False}


def _weights(value: object) -> dict:
    if (not isinstance(value, dict) or set(value) != {'b', 'o'} or
            any(type(v) is not int or v < 0 for v in value.values())):
        raise SummaryUnavailable('dated name summary returned invalid exact weights')
    return dict(value)


class DatedNameSummaryRuntime:
    def __init__(
        self,
        legacy: 'NameSummaryRuntime',
        daily: 'PublishedDatedL1',
        *,
        logical_store: str,
        bucket_paths: tuple[str, ...],
    ) -> None:
        if (not isinstance(logical_store, str) or not fullmatch('[a-z][a-z0-9_]*', logical_store) or
                not isinstance(bucket_paths, tuple) or not 1 <= len(bucket_paths) <= 6 or
                any(not isinstance(p, str) or not p or '/' in p or '\0' in p for p in bucket_paths) or
                len(set(bucket_paths)) != len(bucket_paths)):
            raise ValueError('dated name summary requires explicit complete logical store/bucket paths')
        self.logical_store, self.bucket_paths = logical_store, tuple(sorted(bucket_paths))
        manifest, catalogs = daily.manifest, dict(daily.catalogs)
        old_dates = tuple(day for day, _ in legacy.binding.dates)
        if (not old_dates or len(set(old_dates)) != len(old_dates) or not catalogs or
                manifest.get('schema') != 'dated-hot-l1-published-generation-v1' or manifest.get('complete') is not True or
                not isinstance(manifest.get('generation'), str) or not fullmatch('[a-f0-9]{32}', manifest['generation']) or
                manifest.get('logical_store') != logical_store or manifest.get('bucket_paths') != list(self.bucket_paths) or
                manifest.get('dates') != sorted(catalogs) or len(set(manifest['dates'])) != len(manifest['dates']) or
                set(old_dates).intersection(catalogs)):
            raise ValueError('dated name summary source dates/logical scope conflict')
        old_paths = tuple(path for _, _, path in legacy.binding.buckets)
        if len(old_paths) != len(set(old_paths)) or tuple(sorted(old_paths)) != self.bucket_paths:
            raise ValueError('dated name summary frozen bucket set differs from declared logical scope')
        new_patterns, source_metadata = {}, {}
        for day, catalog in catalogs.items():
            if (catalog.date != day or catalog.logical_store != logical_store or catalog.paths != self.bucket_paths):
                raise ValueError('dated name summary daily bucket/store/date differs from declared logical scope')
            _date(day)
            new_patterns[day] = frozenset(catalog.selection.patterns)
            source_metadata[day] = catalog.metadata()
        old_registry = {row['date']: row for row in legacy.catalog.metadata()['dates']}
        if set(old_registry) != set(old_dates):
            raise ValueError('dated name summary frozen registry dates differ from accepted source')
        for row in old_registry.values():
            qualification = row.get('registry_dates', [row.get('registry_date')])
            if not isinstance(qualification, list) or not qualification or len(set(qualification)) != len(qualification):
                raise ValueError('dated name summary requires original registry qualification dates')
            for day in qualification:
                _date(day)
        self.legacy, self.daily = legacy, MappingProxyType(catalogs)
        self.old_dates, self.new_patterns = old_dates, MappingProxyType(new_patterns)
        self.dates = tuple(sorted((*old_dates, *catalogs)))
        self._manifest, self._source_metadata, self._old_registry = deepcopy(manifest), source_metadata, old_registry
        self.catalog_gate = BoundedSemaphore(2)

    def metadata(self) -> dict:
        rows = []
        for day in self.dates:
            if day in self.daily:
                metadata = self._source_metadata[day]
                rows.append({'date': day, 'plans': ['catalog'], 'kind': 'daily-scalar-source-v1',
                             'registry': deepcopy(metadata['registry']), 'source': deepcopy(metadata['source']),
                             'generation': self._manifest['generation']})
            else:
                registry = self._old_registry[day]
                qualification = registry.get('registry_dates', [registry.get('registry_date')])
                rows.append({'date': day, 'plans': ['catalog', 'bounded-name-postings'], 'kind': 'frozen-history',
                             'registry': {'qualification_dates': list(qualification), 'target': self.legacy.binding.target, 'patterns': registry['patterns'],
                                          'selection_contract': 'membership on declared qualification dates; no current-scan frequency claim'}})
        return {'schema': 'dated-name-summary-registry-v1', 'logical_store': self.logical_store,
                'bucket_paths': list(self.bucket_paths), 'dates': rows, 'levels': 1, 'scope': SCOPE,
                'daily_catalog_slots': 2, 'legacy': self.legacy.metadata(), 'capabilities': dict(CAPABILITIES)}

    def _envelope(self, body: dict, day: str, pattern: str, *, daily: bool) -> dict:
        if (body.get('date') != day or body.get('pattern') != pattern or body.get('path') != '' or
                body.get('exact') is not True or body.get('incremental') is not False or type(body.get('levels')) is not int or body.get('levels') != 1 or body.get('scope') != SCOPE or
                body.get('schema') != ('dated-hot-l1-v1' if daily else 'name-summary-v1') or (daily and body.get('logical_store') != self.logical_store)):
            raise SummaryUnavailable('dated name summary returned mismatched exact scope')
        root, rows = _weights(body.get('root')), body.get('buckets')
        if not isinstance(rows, list) or len(rows) != len(self.bucket_paths):
            raise SummaryUnavailable('dated name summary returned incomplete logical buckets')
        by_path = {}
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {'path', 'pre', 'post', 'b', 'o'} or row['path'] not in self.bucket_paths or row['path'] in by_path or
                    type(row['pre']) is not int or type(row['post']) is not int or row['pre'] < 1 or row['post'] < row['pre']):
                raise SummaryUnavailable('dated name summary returned invalid bucket identity/geometry')
            _weights({key: row[key] for key in ('b', 'o')})
            by_path[row['path']] = dict(row)
        ordered = sorted(by_path.values(), key=lambda r: r['pre'])
        if ordered[0]['pre'] != 1 or any(a['post'] + 1 != b['pre'] for a, b in zip(ordered, ordered[1:])):
            raise SummaryUnavailable('dated name summary returned incomplete bucket geometry')
        if root != {key: sum(r[key] for r in rows) for key in ('b', 'o')}:
            raise SummaryUnavailable('dated name summary bucket/root weights do not conserve')
        result = deepcopy(body)
        if daily:
            if not isinstance(body.get('source'), dict):
                raise SummaryUnavailable('dated name summary returned invalid source identity')
            identity = {**deepcopy(body['source']), 'kind': 'daily-scalar-source-v1', 'generation': self._manifest['generation']}
            result.update(plan='catalog', target=identity['target'], source='published dated precomputed batch artifact')
        else:
            if not isinstance(body.get('source_identity'), dict):
                raise SummaryUnavailable('dated name summary returned invalid source identity')
            identity = {**deepcopy(body['source_identity']), 'kind': 'frozen-history'}
        result.update(schema='dated-name-summary-v1', logical_store=self.logical_store, source_identity=identity,
                      root=root, buckets=[by_path[path] for path in self.bucket_paths], capabilities=dict(CAPABILITIES))
        return result

    def _request(self, dates: tuple[str, ...], pattern: str, path: str) -> dict:
        if path != '':
            raise CatalogRequest('dated name summary serves the global root only; no drill fallback')
        try:
            dates, pattern = tuple(_date(day) for day in dates), _literal(pattern)
        except (ValueError, UnicodeError):
            raise CatalogRequest('dated name summary requires valid ISO scans and one UTF-8 NUL/slash-free literal') from None
        if any(day not in self.dates for day in dates):
            raise CatalogRequest('dated name summary scan is unavailable; no scan fallback')
        if len(dates) == 2 and dates[0] >= dates[1]:
            raise CatalogRequest('dated name summary baseline must precede the selected scan')
        new_dates = [day for day in dates if day in self.daily]
        if any(pattern not in self.new_patterns[day] for day in new_dates):
            raise CatalogRequest('dated name summary new scan/literal is not registered; no cold fallback')
        if not self.catalog_gate.acquire(blocking=False):
            raise SummaryBusy('dated name summary daily catalog slots busy; retry shortly')
        try:
            sides = {day: self._envelope(self.daily[day].view(day, pattern), day, pattern, daily=True) for day in new_dates}
        finally:
            self.catalog_gate.release()
        # Never hold new catalog slots while legacy cold discovery owns its
        # existing combined compute budget and independently admitted lane.
        for day in dates:
            if day not in sides:
                sides[day] = self._envelope(self.legacy.view(day, pattern), day, pattern, daily=False)
        if len(dates) == 1:
            return sides[dates[0]]
        before, after = (sides[day] for day in dates)
        delta = lambda a, b: {key: b[key] - a[key] for key in ('b', 'o')}
        fields = ('pre', 'post', 'b', 'o')
        return {'schema': 'dated-name-summary-diff-v1', 'logical_store': self.logical_store,
                'from': dates[0], 'date': dates[1], 'pattern': pattern, 'path': '', 'exact': True,
                'incremental': False, 'levels': 1, 'scope': SCOPE, 'before': before, 'after': after,
                'delta': delta(before['root'], after['root']), 'capabilities': dict(CAPABILITIES),
                'buckets': [{'path': path, 'before': {key: a[key] for key in fields}, 'after': {key: b[key] for key in fields},
                             'delta': delta(a, b)} for path, a, b in zip(self.bucket_paths, before['buckets'], after['buckets'], strict=True)]}

    def view(self, date: str, pattern: str, *, path: str = '') -> dict:
        if date in self.old_dates:
            return self.legacy.view(date, pattern, path=path)
        return self._request((date,), pattern, path)

    def diff(self, before: str, after: str, pattern: str, *, path: str = '') -> dict:
        if before in self.old_dates and after in self.old_dates:
            return self.legacy.diff(before, after, pattern, path=path)
        return self._request((before, after), pattern, path)
