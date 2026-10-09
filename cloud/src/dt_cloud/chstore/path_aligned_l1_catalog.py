"""Root-only composition of separately pinned, accepted dated L1 catalogs.

Bucket paths identify the logical store; preorder numbers identify only one
dated physical source. The caller explicitly declares that store/bucket scope:
neither a shared spelling nor a SHA proves that two sources represent it.
Loading requires existing artifact-bound prefix proofs and accepted catalog
validation. No database, new artifact format, publisher or daily build exists
here. In particular the current union loader still rejects a future scan not
among its qualification dates; this adapter never rewrites that provenance.
"""

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from .hot_l1_batch_catalog import HotL1BatchCatalog, _date, _identity, _literal
from .hot_l1_catalog import CatalogRequest, SCOPE
from .hot_l1_publish import load_pinned, pin


@dataclass(frozen=True)
class _DatedSource:
    catalog: HotL1BatchCatalog
    manifest: dict
    date: str

    def identity(self) -> dict:
        snapshot = self.catalog._snapshots[self.date]
        return {'target': snapshot.target, 'snapshot_db': snapshot.snapshot_db,
                'generation': self.manifest['generation'], 'artifacts': deepcopy(self.manifest['artifacts']),
                'source_prefix_proofs_checked': True}

    def registry(self) -> dict:
        snapshot = self.catalog._snapshots[self.date]
        return {'qualification_dates': list(snapshot.registry_dates or (snapshot.registry_date,)),
                'patterns': len(snapshot.entries), 'threshold_paths': snapshot.threshold_paths, 'max_chars': snapshot.max_chars,
                'selection_contract': 'membership on declared qualification dates; no current-scan frequency claim'}


class PathAlignedL1Catalog:
    """Path-aligned additive roots, not aligned numeric ranges or L2 geometry."""

    @classmethod
    def load(
        cls,
        roots: tuple[Path, ...],
        *,
        logical_store: str,
        bucket_paths: tuple[str, ...],
    ) -> 'PathAlignedL1Catalog':
        _identity(logical_store)
        if (not roots or not 1 <= len(bucket_paths) <= 6 or
                any(not isinstance(path, str) or not path or '/' in path or '\0' in path for path in bucket_paths) or
                len(set(bucket_paths)) != len(bucket_paths)):
            raise ValueError('path-aligned L1 requires published sources and one to six explicit unique bucket paths')
        for path in bucket_paths:
            path.encode('utf-8')
        sources = {}
        declared = frozenset(bucket_paths)
        for root in roots:
            manifest = pin(root)
            catalog = load_pinned(root, manifest)
            if manifest.get('source_prefix_validation', {}).get('checked') is not True:
                raise ValueError('path-aligned L1 requires every source generation to have bound dated prefix proofs')
            for day, snapshot in catalog._snapshots.items():
                if day in sources:
                    raise ValueError('path-aligned L1 source generations contain a duplicate scan date')
                # The accepted per-source reader already enforces complete
                # same-date geometry across every predicate. Cross-date IDs
                # are intentionally never compared or transported.
                if frozenset(row.path for row in snapshot.entries[0].buckets) != declared:
                    raise ValueError('path-aligned L1 source bucket paths differ from the complete declared logical scope')
                sources[day] = _DatedSource(catalog, manifest, day)
        reader = cls.__new__(cls)
        reader.logical_store, reader.paths, reader.dates = logical_store, tuple(sorted(bucket_paths)), tuple(sorted(sources))
        reader._sources = sources
        return reader

    def metadata(self) -> dict:
        return {'schema': 'path-aligned-l1-registry-v1', 'logical_store': self.logical_store,
                'dates': [{'scan_date': day, 'source': self._sources[day].identity(), 'registry': self._sources[day].registry()} for day in self.dates],
                'bucket_paths': list(self.paths), 'levels': 1, 'scope': SCOPE,
                'geometry': 'snapshot-local; stable bucket paths only', 'bucket_drill': False, 'fallback': False}

    def view(
        self,
        date: str,
        pattern: str,
        *,
        path: str = '',
    ) -> dict:
        if path != '':
            raise CatalogRequest('path-aligned L1 serves the global root only; no bucket or child drill')
        try:
            _date(date)
        except ValueError:
            raise CatalogRequest('path-aligned L1 requires a valid ISO scan date') from None
        try:
            normalized = _literal(pattern)
        except (ValueError, UnicodeError):
            raise CatalogRequest('path-aligned L1 requires one valid nonempty NUL/slash-free literal of at most 512 characters') from None
        source = self._sources.get(date)
        if source is None:
            raise CatalogRequest('path-aligned L1 scan is unavailable; no zero or scan fallback')
        try:
            body = source.catalog.view(date, normalized)
        except CatalogRequest:
            raise CatalogRequest('path-aligned L1 literal is unavailable on the requested scan; no zero or scan fallback') from None
        by_path = {row['path']: row for row in body['buckets']}
        return {'schema': 'path-aligned-l1-v1', 'logical_store': self.logical_store, 'scan_date': date,
                'pattern': normalized, 'path': '', 'exact': True, 'incremental': False, 'levels': 1, 'scope': SCOPE,
                'root': body['root'],
                'buckets': [{'path': name, 'b': by_path[name]['b'], 'o': by_path[name]['o'],
                             'geometry': {key: by_path[name][key] for key in ('pre', 'post')}, 'drill': False} for name in self.paths],
                'source': source.identity(), 'registry': source.registry(), 'validation': body['validation'],
                'capabilities': {'bucket_drill': False, 'child_drill': False, 'fallback': False}}

    def diff(
        self,
        before_date: str,
        after_date: str,
        pattern: str,
        *,
        path: str = '',
    ) -> dict:
        before, after = self.view(before_date, pattern, path=path), self.view(after_date, pattern, path=path)
        weights = lambda a, b: {key: b[key] - a[key] for key in ('b', 'o')}
        left = {row['path']: row for row in before['buckets']}
        right = {row['path']: row for row in after['buckets']}
        return {'schema': 'path-aligned-l1-diff-v1', 'logical_store': self.logical_store,
                'from_scan_date': before_date, 'scan_date': after_date, 'pattern': after['pattern'], 'path': '',
                'exact': True, 'incremental': False, 'levels': 1, 'scope': SCOPE,
                'before': before, 'after': after, 'delta': weights(before['root'], after['root']),
                'buckets': [{'path': name,
                             'before': {key: left[name][key] for key in ('b', 'o', 'geometry')},
                             'after': {key: right[name][key] for key in ('b', 'o', 'geometry')},
                             'delta': weights(left[name], right[name]), 'drill': False} for name in self.paths],
                'capabilities': {'bucket_drill': False, 'child_drill': False, 'fallback': False}}
