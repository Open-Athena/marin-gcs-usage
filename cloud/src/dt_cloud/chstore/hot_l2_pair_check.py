"""Bounded independent L2 checks over an operator-trusted private artifact.

Stored provenance paths are trusted inputs, not an untrusted-JSON security
boundary. Hash-verified bytes are pinned once before any source query. This
checks accepted snapshot weights, never independent raw-listing truth.
"""

from dataclasses import dataclass
from hashlib import sha256
from json import dumps, loads
from pathlib import Path
from time import monotonic

from ..bench.ch import like_lit
from .client import Ch
from .hot_l1_batch_sql import SCOPE, _number
from .hot_l1_catalog import _unique_object
from .hot_l2_pair_stream import MAX_CELLS, MAX_FRAMES, _uint, complete, frames, prepare


@dataclass(frozen=True)
class PinnedPath:
    path: Path
    raw: bytes

    def read_bytes(self) -> bytes:
        return self.raw

    def __str__(self) -> str:
        return str(self.path)


def _pin(descriptor: dict) -> PinnedPath:
    if not isinstance(descriptor, dict) or not isinstance(descriptor.get('path'), str):
        raise ValueError('paired L2 check requires explicit provenance paths')
    path = Path(descriptor['path'])
    raw = path.read_bytes()
    if type(descriptor.get('bytes')) is not int or descriptor['bytes'] != len(raw) or descriptor.get('sha256') != sha256(raw).hexdigest():
        raise ValueError('paired L2 provenance bytes/hash mismatch')
    return PinnedPath(path, raw)


def load(artifact: Path, covered: str) -> tuple[dict, dict, bytes]:
    raw = artifact.read_bytes()
    body = loads(raw, object_pairs_hook=_unique_object)
    if (not isinstance(body, dict) or body.get('schema') != 'hot-l2-pair-stream-v1' or body.get('exact') is not True
            or body.get('incremental') is not False or type(body.get('levels')) is not int or body['levels'] != 2
            or body.get('scope') != SCOPE or body.get('persistent_index_created') is not False):
        raise ValueError('paired L2 check requires a completed paired experiment contract')
    patterns = tuple(row['pattern'] for row in body['results'])
    if covered != 'm' or covered not in patterns:
        raise ValueError('paired L2 check requires the registered covered-ancestor control m')
    provenance = body['provenance']
    refs = tuple(_pin(row) for row in provenance['references'])
    proofs = tuple(_pin(row) for row in provenance['prefix_proofs'])
    queries = _pin(provenance['queries'])
    prepared = prepare(body['target'], *body['dates'], refs, proofs, queries, patterns, body['budget'])
    if (provenance != prepared['provenance'] or body['snapshot_dbs'] != prepared['dbs']
            or body['registered_predicates'] != len(patterns) or body['query_subset'] != prepared['query_subset']
            or type(body['registered_frames']) is not int or body['registered_frames'] != len(body['frames'])
            or not 1 <= body['registered_frames'] <= MAX_FRAMES
            or any(covered not in name.lower() for _, _, name in prepared['buckets'])):
        raise ValueError('paired L2 check source/proof/registry identities disagree')
    for i, frame in enumerate(body['frames'], 1):
        if (set(frame) != {'frame_id', 'pre', 'post', 'path', 'bucket'} or _uint(frame['frame_id']) != i
                or _uint(frame['post']) < _uint(frame['pre']) or _uint(frame['bucket']) >= len(prepared['buckets'])
                or not isinstance(frame['path'], str)):
            raise ValueError('paired L2 check frame declaration is invalid')
    maximum = _uint(body['native']['max_cells'])
    if not 1 <= maximum <= MAX_CELLS:
        raise ValueError('paired L2 check cell guard is invalid')
    native = {**body['native'], 'roots': [
        {'predicate_id': row['predicate_id'], 'buckets': [
            [str(bucket['b'][0]), str(bucket['o'][0]), str(bucket['b'][1]), str(bucket['o'][1])]
            for bucket in row['buckets']]} for row in body['results']],
        'cells': [{'predicate_id': row['predicate_id'], 'frame_id': row['frame_id'],
                   'b': list(map(str, row['b'])), 'o': list(map(str, row['o']))} for row in body['cells']]}
    checked = complete(native, prepared, body['frames'], maximum)
    if any(body[key] != checked[key] for key in checked):
        raise ValueError('paired L2 check reconstructed artifact shape/totals disagree')
    return body, prepared, raw


def points(ch: Ch, database: str, geometry: list[dict]) -> dict[int, tuple[int, int]]:
    by_pre = {row['pre']: row for row in geometry}
    rows = ch.json(f"SELECT pre,post,path,b,o FROM {database}.nodes WHERE pre IN ({','.join(map(str, sorted(by_pre)))}) ORDER BY pre")
    result = {}
    for pre, post, path, b, o in rows:
        expected = by_pre.get(pre)
        if type(pre) is not int or type(post) is not int or expected is None or pre in result or (post, path) != (expected['post'], expected['path']):
            raise ValueError('paired L2 ordinary point geometry is invalid')
        result[pre] = (_uint(b), _uint(o))
    # An absent frame has no present descendants under the bound dated prefix proof.
    return {pre: result.get(pre, (0, 0)) for pre in by_pre}


def covered_oracle(ch: Ch, body: dict, prepared: dict) -> dict:
    index = prepared['patterns'].index('m')
    qid = index + 1
    geometry = [{'pre': lo, 'post': hi, 'path': path} for lo, hi, path in prepared['buckets']] + body['frames']
    snapshots = [points(ch, db, geometry) for db in prepared['dbs']]
    expected_cells, expected_buckets = [], []
    for bucket, (lo, hi, path) in enumerate(prepared['buckets']):
        values = [snapshots[side][lo] for side in range(2)]
        remaining = [list(pair) for pair in values]
        threshold = prepared['thresholds'][index][bucket]
        for frame in body['frames']:
            if frame['bucket'] != bucket:
                continue
            weights = [snapshots[side][frame['pre']] for side in range(2)]
            if max(pair[0] for pair in weights) < threshold:
                continue
            expected_cells.append({'predicate_id': qid, **frame, 'b': [pair[0] for pair in weights], 'o': [pair[1] for pair in weights]})
            remaining = [[a - b for a, b in zip(rest, pair, strict=True)] for rest, pair in zip(remaining, weights, strict=True)]
        if any(value < 0 for pair in remaining for value in pair):
            raise ValueError('paired L2 ordinary children exceed their bucket rollup')
        expected_buckets.append({'pre': lo, 'post': hi, 'path': path, 'threshold_bytes': threshold,
                                 'b': [pair[0] for pair in values], 'o': [pair[1] for pair in values],
                                 'other': {'b': [pair[0] for pair in remaining], 'o': [pair[1] for pair in remaining]}})
    actual = [row for row in body['cells'] if row['predicate_id'] == qid]
    if actual != expected_cells or body['results'][index]['buckets'] != expected_buckets:
        raise AssertionError('paired L2 covered-ancestor complete cells/other disagree with ordinary rollups')
    return {'pattern': 'm', 'validation': 'complete ordinary recursive bucket/frame rollups on both dates',
            'frames_checked': len(body['frames']), 'heavy_cells_checked': len(expected_cells), 'buckets_checked': len(expected_buckets)}


def scoped_oracles(ch: Ch, body: dict, max_span: int, max_cells: int) -> list[dict]:
    results = []
    parent = "if(position(path, '/') = 0, '', substring(path, 1, length(path) - position(reverse(path), '/')))"
    first_eligible = {}
    for cell in body['cells']:
        if cell['post'] - cell['pre'] + 1 <= max_span:
            first_eligible.setdefault(cell['predicate_id'], cell)
    checked = 0
    for query in body['results']:
        pattern, qid = query['pattern'], query['predicate_id']
        if pattern == 'm':
            continue
        cell = first_eligible.get(qid)
        if checked == max_cells or cell is None:
            results.append({'predicate_id': qid, 'pattern': pattern, 'checked': False,
                            'reason': 'selected-cell budget exhausted' if checked == max_cells else 'no emitted cell within interval cap'})
            continue
        match = like_lit(pattern)
        actual = []
        for db in body['snapshot_dbs']:
            sql = (f"SELECT sum(toUInt128(b)),sum(toUInt128(o)) FROM {db}.nodes "
                   f"WHERE pre BETWEEN {cell['pre']} AND {cell['post']} AND lowerUTF8(path) LIKE {match} "
                   f"AND (pre = {cell['pre']} OR NOT (lowerUTF8({parent}) LIKE {match}))")
            rows = ch.json(sql)
            if len(rows) != 1 or len(rows[0]) != 2:
                raise ValueError('paired L2 scoped frontier returned invalid totals')
            actual.append(tuple(_uint(_number(value), 128) for value in rows[0]))
        expected = list(zip(cell['b'], cell['o'], strict=True))
        if actual != expected:
            raise AssertionError('paired L2 selected cell disagrees with independent full-path frontier')
        results.append({'predicate_id': qid, 'pattern': pattern, 'checked': True, 'frame_id': cell['frame_id'],
                        'interval_span': cell['post'] - cell['pre'] + 1, 'validation': 'complete scoped first-hit full-path frontier on both dates'})
        checked += 1
    return results


def check(
    url: str,
    artifact: Path,
    out: Path,
    *,
    seconds: int = 30,
    max_span: int = 1_000_000,
    max_cells: int = 4,
) -> dict:
    if type(seconds) is not int or not 15 <= seconds <= 30 or type(max_span) is not int or not 1 <= max_span <= 1_000_000 or type(max_cells) is not int or not 0 <= max_cells <= 4:
        raise ValueError('paired L2 check limits require 15..30 seconds, 1..1M span and 0..4 cells')
    if out.exists() or out.is_symlink() or not out.parent.is_dir():
        raise ValueError('paired L2 check output must be new in an existing directory')
    start = monotonic()
    body, prepared, raw = load(artifact, 'm')
    ch = Ch(url, db=prepared['target'], timeout=seconds + 60, max_threads=4, max_memory_usage=2 << 30,
            max_execution_time=seconds, timeout_before_checking_execution_speed=0, timeout_overflow_mode='throw',
            max_temporary_data_on_disk_size_for_query=1 << 30)
    try:
        if frames(ch, prepared, len(body['frames'])) != body['frames']:
            raise ValueError('paired L2 check complete source frame geometry differs')
        counts = []
        for db in prepared['dbs']:
            rows = ch.json(f'SELECT count() FROM {db}.nodes')
            if len(rows) != 1 or len(rows[0]) != 1:
                raise ValueError('paired L2 check source count shape is invalid')
            counts.append(_uint(_number(rows[0][0])))
        if counts != prepared['rows'] or any(type(count) is not int for count in counts):
            raise ValueError('paired L2 check source row counts differ from bound references')
        covered = covered_oracle(ch, body, prepared)
        scoped = scoped_oracles(ch, body, max_span, max_cells)
        result = {'schema': 'hot-l2-pair-check-v1', 'complete': True, 'target': prepared['target'], 'dates': prepared['dates'],
                  'artifact': {'path': str(artifact), 'sha256': sha256(raw).hexdigest(), 'bytes': len(raw)},
                  'source_rows': counts, 'prefix_proofs_checked': True, 'covered_control': covered, 'selected_cells': scoped,
                  'full_catalog_source_oracle': False, 'source_contract': prepared['provenance']['source_contract'],
                  'limits': {'seconds': seconds, 'memory_gib': 2, 'max_interval_span': max_span, 'max_selected_cells': max_cells},
                  'check_s': monotonic() - start}
        data = dumps(result) + '\n'
        owned = False
        try:
            with out.open('x') as output:
                owned = True
                output.write(data)
        except BaseException:
            if owned:
                out.unlink()
            raise
        return result
    finally:
        ch.close()
