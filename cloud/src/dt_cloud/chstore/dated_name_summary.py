"""Dated catalog stitching; old-only bodies and compute budgets stay unchanged.

An explicit logical-store/bucket binding is operator authority. Daily numeric
geometry is snapshot-local; mixed comparisons align only bucket paths. A new
scan answers an unregistered literal only when it carries a completed name
index bound to its catalog's exact source (`daily_name_index`): bounded cold
discovery over that scan's own postings, in the legacy lane's one cold slot
and compute budget. Without one, an unregistered literal is refused. No new
bucket detail is advertised.

A consolidated name index (`mega_names.binding`: one store's name-sorted
postings over every scan it holds) answers in the same lane instead: an
unregistered literal on a daily scan it covers (its bucket geometry must equal
the catalog's), and any literal on a scan with neither a frozen nor a daily
catalog (`consolidated-store-v1`).
"""

from copy import deepcopy
from re import fullmatch
from threading import BoundedSemaphore
from types import MappingProxyType
from typing import TYPE_CHECKING

from . import mega_names
from .hot_l1 import build
from .hot_l1_batch_catalog import _date, _literal
from .hot_l1_catalog import CatalogRequest, SCOPE
from .name_summary import CAPS, SummaryBusy, SummaryUnavailable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .dated_hot_l1_publish import PublishedDatedL1
    from .name_summary import NameSummaryRuntime

CAPABILITIES = {'bucket_drill': False, 'child_drill': False, 'fallback': False}
COLD_SOURCE = "bounded dated name postings over the scan's own name index; directory rollups are atomic"
MEGA_SOURCE = 'bounded name postings over the consolidated store; directory rollups are atomic'
MEGA_VALIDATION = {'description': "bounded exact first-hit coverage over the consolidated store's name index; no per-request source oracle",
                   'source_prefix_proofs_checked': True, 'independent_full_catalog_source_oracle': False}
COLD_VALIDATION = {'description': "bounded exact first-hit coverage over the scan's own name index; no per-request source oracle",
                   'source_prefix_proofs_checked': True, 'independent_full_catalog_source_oracle': False}


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
        cold: 'Mapping[str, dict] | None' = None,
        mega: dict | None = None,
    ) -> None:
        """`cold`: new scan date → its `daily_name_index.load` manifest. `mega`: a `mega_names.binding`."""
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
        cold = dict(cold or {})
        for day, index in cold.items():
            source = source_metadata.get(day, {}).get('source', {})
            if (day not in catalogs or not isinstance(index, dict) or index.get('schema') != 'daily-name-index-v1' or
                    index.get('complete') is not True or index.get('date') != day or index.get('logical_store') != logical_store or
                    index.get('target') != source.get('snapshot_db') or index.get('source_manifest_sha256') != source.get('source_manifest_sha256') or
                    sorted(row.get('path') for row in index.get('buckets', [])) != list(self.bucket_paths)):
                raise ValueError('dated name summary cold name index differs from its scan catalog source')
        self.cold = MappingProxyType({day: deepcopy(index) for day, index in cold.items()})
        self.mega, self.geometry = self._mega(mega, catalogs), MappingProxyType({})
        if self.mega:
            self.geometry = MappingProxyType({day: tuple(tuple(row) for row in rows) for day, rows in self.mega['geometry'].items()})
        self.mega_dates = tuple(sorted(day for day in self.geometry if day not in catalogs and day not in old_dates))
        self.legacy, self.daily = legacy, MappingProxyType(catalogs)
        self.old_dates, self.new_patterns = old_dates, MappingProxyType(new_patterns)
        self.dates = tuple(sorted((*old_dates, *catalogs, *self.mega_dates)))
        self._manifest, self._source_metadata, self._old_registry = deepcopy(manifest), source_metadata, old_registry
        self.catalog_gate = BoundedSemaphore(2)

    def _mega(self, mega: dict | None, catalogs: dict) -> dict | None:
        if mega is None:
            return None
        if (not isinstance(mega, dict) or mega.get('schema') != 'mega-name-binding-v1' or
                any(not isinstance(mega.get(key), str) or not fullmatch('[a-z_][a-z0-9_]*', mega[key]) for key in ('target', 'postings')) or
                not isinstance(mega.get('geometry'), dict) or not mega['geometry']):
            raise ValueError('dated name summary consolidated binding is invalid')
        _date(mega.get('through'))
        for day, rows in mega['geometry'].items():
            _date(day)
            if (day > mega['through'] or not isinstance(rows, list) or sorted(row[2] for row in rows) != list(self.bucket_paths) or
                    rows[0][0] != 1 or any(type(v) is not int for row in rows for v in row[:2]) or
                    any(row[1] < row[0] for row in rows) or any(a[1] + 1 != b[0] for a, b in zip(rows, rows[1:]))):
                raise ValueError('dated name summary consolidated geometry is incomplete')
            selection = getattr(catalogs.get(day), 'selection', None)
            if selection is not None and sorted(map(tuple, rows)) != sorted(map(tuple, selection.buckets)):
                raise ValueError('dated name summary consolidated geometry differs from its scan catalog')
        return deepcopy(mega)

    def metadata(self) -> dict:
        rows = []
        for day in self.dates:
            if day in self.mega_dates:
                rows.append({'date': day, 'plans': ['bounded-name-postings'], 'kind': 'consolidated-store-v1',
                             'source': {key: self.mega[key] for key in ('target', 'postings', 'through')}})
            elif day in self.daily:
                metadata = self._source_metadata[day]
                row = {'date': day, 'plans': ['catalog'], 'kind': 'daily-scalar-source-v1',
                       'registry': deepcopy(metadata['registry']), 'source': deepcopy(metadata['source']),
                       'generation': self._manifest['generation']}
                if day in self.cold or day in self.geometry:
                    row['plans'].append('bounded-name-postings')
                rows.append(row)
            else:
                registry = self._old_registry[day]
                qualification = registry.get('registry_dates', [registry.get('registry_date')])
                rows.append({'date': day, 'plans': ['catalog', 'bounded-name-postings'], 'kind': 'frozen-history',
                             'registry': {'qualification_dates': list(qualification), 'target': self.legacy.binding.target, 'patterns': registry['patterns'],
                                          'selection_contract': 'membership on declared qualification dates; no current-scan frequency claim'}})
        return {'schema': 'dated-name-summary-registry-v1', 'logical_store': self.logical_store,
                'bucket_paths': list(self.bucket_paths), 'dates': rows, 'levels': 1, 'scope': SCOPE,
                'daily_catalog_slots': 2, 'legacy': self.legacy.metadata(), 'capabilities': dict(CAPABILITIES)}

    def _envelope(self, body: dict, day: str, pattern: str, *, daily: bool, cold: str | None = None, consolidated: bool = False) -> dict:
        """`cold`: the bounded plan's source description; `consolidated`: a scan only the consolidated store holds."""
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
        if consolidated:
            identity = {'kind': 'consolidated-store-v1', **{key: self.mega[key] for key in ('target', 'postings', 'through')}}
            result.update(plan='bounded-name-postings', target=identity['target'], source=cold)
        elif daily:
            if not isinstance(body.get('source'), dict):
                raise SummaryUnavailable('dated name summary returned invalid source identity')
            identity = {**deepcopy(body['source']), 'kind': 'daily-scalar-source-v1', 'generation': self._manifest['generation']}
            if cold:
                result.update(plan='bounded-name-postings', target=identity['target'], source=cold)
            else:
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
        cold = [day for day in new_dates if pattern not in self.new_patterns[day]]
        if any(day not in self.cold and day not in self.geometry for day in cold):
            raise CatalogRequest('dated name summary new scan/literal is not registered; no cold fallback')
        sides = {}
        if len(cold) < len(new_dates):
            if not self.catalog_gate.acquire(blocking=False):
                raise SummaryBusy('dated name summary daily catalog slots busy; retry shortly')
            try:
                sides = {day: self._envelope(self.daily[day].view(day, pattern), day, pattern, daily=True) for day in new_dates if day not in cold}
            finally:
                self.catalog_gate.release()
        cold += [day for day in dates if day in self.mega_dates]
        if cold:
            sides.update(self._cold(cold, pattern))
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

    def _cold(self, days: list[str], pattern: str) -> dict:
        """Bounded discovery over the consolidated store (when it holds every
        day) or each new scan's own name index, in the legacy lane's one cold
        slot and one total compute budget."""
        if all(day in self.geometry for day in days):
            return self._consolidated(days, pattern)
        if any(day not in self.cold for day in days):
            raise CatalogRequest('dated name summary cannot answer these scans from one name index; no partial result')

        def compute(source, checkpoint) -> dict:
            raw = {}
            for day in days:
                checkpoint()
                raw[day] = build(source, self.cold[day]['target'], day, pattern, daily=True, **CAPS)
            checkpoint()
            return raw

        raw = self.legacy.bounded(self.cold[days[0]]['target'], compute)
        sides = {}
        for day in days:
            body, metadata = raw[day], self.daily[day].metadata()
            if body.get('snapshot_db') != metadata['source']['snapshot_db']:
                raise SummaryUnavailable('dated name summary cold source changed; no partial result')
            # The catalog body's shape and pinned scan identity; only the plan,
            # its source description and validation differ.
            envelope = {'schema': 'dated-hot-l1-v1', 'logical_store': self.logical_store, 'date': body.get('date'),
                        'pattern': body.get('pattern'), 'path': '', 'exact': body.get('exact'), 'incremental': body.get('incremental'),
                        'levels': 1, 'scope': body.get('scope'), 'root': body.get('root'),
                        'buckets': [{key: row.get(key) for key in ('path', 'pre', 'post', 'b', 'o')} for row in body.get('buckets', [])],
                        'source': metadata['source'], 'registry': metadata['registry'], 'validation': dict(COLD_VALIDATION),
                        'capabilities': dict(CAPABILITIES)}
            sides[day] = self._envelope(envelope, day, pattern, daily=True, cold=COLD_SOURCE)
        return sides

    def _consolidated(self, days: list[str], pattern: str) -> dict:
        def compute(source, checkpoint) -> dict:
            raw = {}
            for day in days:
                checkpoint()
                raw[day] = mega_names.answer(source, day, pattern, postings=self.mega['postings'], max_names=CAPS['max_names'])
            checkpoint()
            return raw

        raw = self.legacy.bounded(self.mega['target'], compute)
        sides = {}
        for day in days:
            body, geometry = raw[day], {path: (pre, post) for pre, post, path in self.geometry[day]}
            if body.get('schema') != 'mega-name-totals-v1' or sorted(row.get('path') for row in body.get('buckets', [])) != sorted(geometry):
                raise SummaryUnavailable('dated name summary consolidated answer differs from its bound geometry; no partial result')
            envelope = {'schema': 'dated-hot-l1-v1', 'logical_store': self.logical_store, 'date': body.get('date'),
                        'pattern': body.get('pattern'), 'path': '', 'exact': body.get('exact'), 'incremental': False,
                        'levels': 1, 'scope': SCOPE, 'root': body.get('root'),
                        'buckets': [{'path': row['path'], 'pre': geometry[row['path']][0], 'post': geometry[row['path']][1], 'b': row.get('b'), 'o': row.get('o')}
                                    for row in body['buckets']],
                        'validation': dict(MEGA_VALIDATION), 'capabilities': dict(CAPABILITIES)}
            if day in self.daily:
                metadata = self.daily[day].metadata()
                envelope.update(source=metadata['source'], registry=metadata['registry'])
            sides[day] = self._envelope(envelope, day, pattern, daily=True, cold=MEGA_SOURCE, consolidated=day not in self.daily)
        return sides

    def view(self, date: str, pattern: str, *, path: str = '') -> dict:
        if date in self.old_dates:
            return self.legacy.view(date, pattern, path=path)
        return self._request((date,), pattern, path)

    def diff(self, before: str, after: str, pattern: str, *, path: str = '') -> dict:
        if before in self.old_dates and after in self.old_dates:
            return self.legacy.diff(before, after, pattern, path=path)
        return self._request((before, after), pattern, path)
