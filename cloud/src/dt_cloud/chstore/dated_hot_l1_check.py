"""Selected full-source first-hit oracles, independent of native construction.

The caller loads its explicit artifact once through the validated dated reader.
The supplied accepted scalar source is immutable; its completed marker is
pinned before and after. This checks every row for selected literals, not all
registered predicates and not the original object-store listing's truth.
"""

from hashlib import sha256
from json import dumps, loads
from math import isfinite
from re import fullmatch
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

from .client import Ch, lit

if TYPE_CHECKING:
    from .dated_hot_l1 import DatedHotL1Catalog

VALIDATION = 'complete independent full-path first-hit source scan'
PARENT = "if(position(path, '/') = 0, '', substring(path, 1, length(path) - position(reverse(path), '/')))"


def _canonical(body: dict) -> bytes:
    return (dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')


def _uint(value: object) -> int:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and fullmatch(r'0|[1-9][0-9]*', value):
        return int(value)
    raise ValueError('dated L1 check source returned an invalid unsigned scalar')


def _cancel(ch: Ch, ids: list[str]) -> None:
    """Separate, bounded control session; never kill another caller's queries."""
    if not ids:
        return
    control = Ch(ch.url, db=ch.db, session=False, timeout=2, max_threads=1,
                 max_memory_usage=64 << 20, max_execution_time=2,
                 timeout_before_checking_execution_speed=0, timeout_overflow_mode='throw')
    selected = ','.join(map(lit, ids))
    try:
        control.exec(f'KILL QUERY WHERE query_id IN ({selected}) SYNC', fmt=None)
        if control.scalar(f'SELECT count() FROM system.processes WHERE query_id IN ({selected})') != '0':
            raise RuntimeError('dated L1 check could not verify owned-query cleanup')
    finally:
        control.close()


def check(
    ch: Ch,
    catalog: 'DatedHotL1Catalog',
    patterns: tuple[str, ...],
) -> dict:
    """Return a compact proof only after entire selected root/bucket equality.

    The passed Ch must have positive finite memory/execution/transport budgets.
    They are retained; every statement gets a fresh owned ID and throws on
    timeout. No output writes, producer helpers, native kernel or sampling.
    """
    selection = catalog.selection
    if (not isinstance(patterns, tuple) or not 1 <= len(patterns) <= 8 or
            any(not isinstance(p, str) or not p or '/' in p or '\0' in p or len(p) > 512 for p in patterns) or
            len(set(patterns)) != len(patterns) or any(p not in selection.patterns for p in patterns)):
        raise ValueError('dated L1 check requires one to eight unique registered literals')
    for pattern in patterns:
        pattern.encode('utf-8')
    try:
        seconds = float(ch.settings['max_execution_time'])
        memory = int(ch.settings['max_memory_usage'])
        transport = float(ch.timeout)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('dated L1 check requires finite positive caller memory/time budgets') from error
    if not isfinite(seconds) or seconds <= 0 or memory <= 0 or not isfinite(transport) or transport <= 0:
        raise ValueError('dated L1 check requires finite positive caller memory/time budgets')
    db = selection.snapshot_db
    if not isinstance(db, str) or not fullmatch('[a-z][a-z0-9_]*', db):
        raise ValueError('dated L1 check requires a valid source database identifier')
    raw = selection.source_manifest_raw
    source = loads(raw)
    if (_canonical(source) != raw or source.get('complete') is not True or source.get('prefix') != '' or
            source.get('snapshot_db') != db or source.get('target') != selection.target or
            source.get('date') != catalog.date or source.get('nodes') != selection.nodes):
        raise ValueError('dated L1 check source selection is not a completed canonical global manifest')
    root, buckets = source['root'], source['buckets']
    geometry = tuple((r['pre'], r['post'], r['path']) for r in buckets)
    if (not 1 <= len(buckets) <= 6 or geometry != selection.buckets or root['path'] != '' or
            root['pre'] != 0 or root['post'] != selection.nodes - 1):
        raise ValueError('dated L1 check requires complete explicit source root/bucket metadata')
    expected = {p: catalog.view(catalog.date, p) for p in patterns}
    metadata = catalog.metadata()
    prefix, ids = 'dated_hot_l1_check_' + uuid4().hex, []
    start = monotonic()

    def rows(sql: str) -> list[list]:
        query_id = f'{prefix}_{len(ids) + 1:04d}'
        ids.append(query_id)
        return ch.json(sql, settings={'query_id': query_id, 'timeout_overflow_mode': 'throw', 'timeout_before_checking_execution_speed': 0})

    def marker() -> bytes:
        values = rows(f'SELECT doc FROM {db}.source_manifest')
        if len(values) != 1 or len(values[0]) != 1 or not isinstance(values[0][0], str):
            raise ValueError('dated L1 check requires one completed source marker')
        doc = values[0][0].encode('utf-8')
        if doc + b'\n' != raw:
            raise ValueError('dated L1 check actual source marker differs from the pinned selection')
        return doc

    try:
        before = marker()
        domain = rows(f'''SELECT count(),min(pre),max(pre),
            countIf(isNull(path) OR NOT isValidUTF8(path) OR isNull(pre) OR isNull(post) OR isNull(b) OR isNull(o)
                OR pre < 0 OR post < pre OR post >= {selection.nodes} OR b < 0 OR o < 0)
            FROM {db}.nodes''')
        if len(domain) != 1 or len(domain[0]) != 4 or [_uint(v) for v in domain[0]] != [selection.nodes, 0, selection.nodes - 1, 0]:
            raise ValueError('dated L1 check complete source count/domain differs from selection')
        dictionary = rows(f'SELECT path,pre,post,depth FROM {db}.dictionary ORDER BY pre')
        wanted = [['', 0, selection.nodes - 1, 0]] + [[r['path'], r['pre'], r['post'], 1] for r in buckets]
        if dictionary != wanted or any(len(row) != 4 or any(type(v) is not int for v in row[1:]) for row in dictionary):
            raise ValueError('dated L1 check actual root/bucket dictionary differs from selection')
        actual_root = rows(f'SELECT path,pre,post,b,o FROM {db}.nodes WHERE pre=0')
        if (len(actual_root) != 1 or len(actual_root[0]) != 5 or actual_root[0][:3] != ['', 0, selection.nodes - 1] or any(type(v) is not int for v in actual_root[0][1:3]) or
                [_uint(v) for v in actual_root[0][3:]] != [root['b'], root['o']]):
            raise ValueError('dated L1 check actual source root differs from manifest')
        checked = []
        for pattern in patterns:
            sql = (f'WITH lowerUTF8({lit(pattern)}) AS literal '
                   f"SELECT arrayElement(splitByChar('/',path),1) AS bucket,sum(toUInt128(b)),sum(toUInt128(o)) FROM {db}.nodes "
                   f'WHERE position(lowerUTF8(path),literal)>0 AND position(lowerUTF8({PARENT}),literal)=0 GROUP BY bucket ORDER BY bucket')
            sums = {r['path']: {'b': 0, 'o': 0} for r in buckets}
            seen = set()
            for row in rows(sql):
                if len(row) != 3 or not isinstance(row[0], str) or row[0] not in sums or row[0] in seen:
                    raise ValueError('dated L1 check frontier returned invalid or undeclared buckets')
                seen.add(row[0])
                sums[row[0]] = {'b': _uint(row[1]), 'o': _uint(row[2])}
            completed = [{**r, **sums[r['path']]} for r in buckets]
            total = {key: sum(r[key] for r in completed) for key in ('b', 'o')}
            if expected[pattern]['root'] != total or expected[pattern]['buckets'] != completed:
                raise AssertionError('dated L1 selected root/buckets disagree with independent full-source first-hit scan')
            checked.append({'pattern': pattern, 'validation': VALIDATION, 'full_source_scan': True, 'buckets_checked': len(buckets)})
        if marker() != before:
            raise ValueError('dated L1 check source marker changed during verification')
        artifact = metadata['source']
        return {'schema': 'dated-hot-l1-check-v1', 'complete': True, 'logical_store': catalog.logical_store,
                'date': catalog.date, 'target': selection.target, 'snapshot_db': db,
                'artifact': {'sha256': artifact['artifact_sha256'], 'bytes': artifact['artifact_bytes']},
                'selection': selection.metadata(), 'source_manifest': {'sha256': sha256(raw).hexdigest(), 'bytes': len(raw),
                'marker_sha256': sha256(before).hexdigest(), 'marker_bytes': len(before)},
                'source_nodes': selection.nodes, 'selected_patterns': checked, 'selected_patterns_checked': len(checked),
                'independent_full_catalog_source_oracle': False, 'check_s': round(monotonic() - start, 6)}
    except BaseException:
        _cancel(ch, ids)
        raise
