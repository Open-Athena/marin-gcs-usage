"""A daily scalar target's own name index answers below-threshold literals.

The fixture is a complete global daily scalar target's serving tables
(`nodes`, `dictionary`, `source_manifest`); `daily_name_index.build` adds
`names`/`nodes_by_name`, and `hot_l1.build(daily=True)` must equal the
independent full-path frontier oracle for every literal shape."""

from hashlib import sha256
from json import dumps, loads
from typing import Iterator

import pytest

from dt_cloud.chstore import daily_name_index
from dt_cloud.chstore.client import Ch, lit
from dt_cloud.chstore.coarse import CoarseRequest
from dt_cloud.chstore.daily_scalar import manifest_bytes
from dt_cloud.chstore.hot_l1 import build, oracle
from dt_cloud.chstore.name_summary import CAPS

from chserver import ch_db, ch_url  # noqa: F401

OWN = {
    'bucket-a/run.json': (0, 1),
    'bucket-a/run.json/plain.bin': (5, 1),
    'bucket-a/run.json/nested.json': (0, 1),
    'bucket-a/run.json/nested.json/leaf.json': (7, 1),
    'bucket-a/loose.json': (11, 1),
    'bucket-a/nohits/plain.bin': (13, 1),
    'bucket-b/folder/hit.json': (17, 1),
    'bucket-b/folder/percent%.bin': (3, 1),
    'bucket-b/folder/under_.bin': (0, 1),
    'bucket-c/json-bucket/other.bin': (19, 1),
    'bucket-d/json.json/plain.bin': (23, 1),
    'bucket-e/ÅRO/Blå.dat': (37, 1),
    'bucket-f/missing.json': (31, 1),
}


def preorder() -> list[tuple[int, int, int, str, int, int]]:
    paths = {''}
    for path in OWN:
        parts = path.split('/')
        paths.update('/'.join(parts[:i]) for i in range(1, len(parts) + 1))
    # Children directly after their parent, as the daily tree order requires.
    ordered = sorted(paths, key=lambda p: p.split('/') if p else [])
    rows = []
    for pre, path in enumerate(ordered):
        inside = [child for child in ordered if not path or child == path or child.startswith(path + '/')]
        post = pre + len(inside) - 1
        sub = [value for child, value in OWN.items() if not path or child == path or child.startswith(path + '/')]
        rows.append((pre, post, path.count('/') + 1 if path else 0, path, sum(v[0] for v in sub), sum(v[1] for v in sub)))
    return rows


@pytest.fixture(scope='module')
def daily(ch_db: str, ch_url: str) -> Iterator[str]:
    ch = Ch(ch_url, db=ch_db)
    rows = preorder()
    try:
        ch.exec(f'CREATE TABLE {ch_db}.nodes (pre UInt32, post UInt32, depth UInt8, path String, b UInt64, o UInt64) ENGINE = MergeTree ORDER BY pre')
        ch.exec(f'INSERT INTO {ch_db}.nodes VALUES ' + ','.join(f'({pre},{post},{depth},{lit(path)},{b},{o})' for pre, post, depth, path, b, o in rows))
        ch.exec(f'CREATE TABLE {ch_db}.dictionary ENGINE = MergeTree ORDER BY pre AS SELECT pre, post, depth, path FROM {ch_db}.nodes WHERE pre = 0 OR depth = 1')
        root = rows[0]
        buckets = [{'path': path, 'pre': pre, 'post': post} for pre, post, depth, path, _, _ in rows if depth == 1]
        source = {'schema': 'daily-scalar-source-v1', 'complete': True, 'logical_store': 'fixture', 'date': '2026-10-06',
                  'target': ch_db, 'snapshot_db': ch_db, 'prefix': '', 'nodes': len(rows), 'buckets': buckets,
                  'root': {'path': '', 'pre': 0, 'post': root[1], 'b': root[4], 'o': root[5]}}
        ch.exec(f'CREATE TABLE {ch_db}.source_manifest (doc String) ENGINE = TinyLog')
        ch.exec(f'INSERT INTO {ch_db}.source_manifest VALUES ({lit(manifest_bytes(source).decode().rstrip())})')
        yield ch_db
    finally:
        ch.close()


def test_no_name_index_no_bounded_answer(daily: str, ch_url: str) -> None:
    # Before the index exists, the daily target has no postings to answer from.
    ch = Ch(ch_url, db=daily)
    try:
        with pytest.raises(ValueError) as caught:
            daily_name_index.load(ch, daily)
        assert str(caught.value) == f'daily name index for {daily} has no completion marker'
    finally:
        ch.close()


@pytest.fixture(scope='module')
def indexed(daily: str, ch_url: str) -> dict:
    ch = Ch(ch_url, db=daily)
    try:
        body = daily_name_index.build(ch, daily)
        assert daily_name_index.load(ch, daily) == body
        with pytest.raises(ValueError) as caught:
            daily_name_index.build(ch, daily)
        assert str(caught.value) == f'daily name index table {daily}.names already exists; refusing to overwrite'
    finally:
        ch.close()
    return body


def test_manifest_binds_exact_source_and_counts(indexed: dict, daily: str, ch_url: str) -> None:
    ch = Ch(ch_url, db=daily)
    try:
        raw = manifest_bytes(loads(ch.scalar(f'SELECT doc FROM {daily}.source_manifest')))
        names = int(ch.scalar(f"SELECT uniqExact(lowerUTF8(arrayElement(splitByChar('/', path), -1))) FROM {daily}.nodes"))
    finally:
        ch.close()
    assert {key: value for key, value in indexed.items() if key != 'stages'} == {
        'schema': 'daily-name-index-v1', 'complete': True, 'target': daily, 'logical_store': 'fixture', 'date': '2026-10-06',
        'prefix': '', 'names': names, 'postings': len(preorder()), 'buckets': loads(raw)['buckets'],
        'source_manifest_sha256': sha256(raw).hexdigest(), 'source_manifest_bytes': len(raw),
    }
    assert sorted(indexed['stages']) == ['names', 'nodes_by_name']


@pytest.mark.parametrize('pattern', ['.JSON', 'json', 'hit.json', '%', '_', 'bucket', 'absent', 'Å', 'åRo', 'plain'])
def test_bounded_daily_build_equals_fullpath_oracle(indexed: dict, daily: str, ch_url: str, pattern: str) -> None:
    ch = Ch(ch_url, db=daily)
    try:
        body = build(ch, daily, '2026-10-06', pattern, daily=True, **CAPS)
        assert (body['snapshot_db'], body['date'], body['pattern']) == (daily, '2026-10-06', pattern.lower())
        assert oracle(ch, body) is True
    finally:
        ch.close()


def test_daily_build_refuses_another_date_before_staging(indexed: dict, daily: str, ch_url: str) -> None:
    ch = Ch(ch_url, db=daily)
    try:
        with pytest.raises(CoarseRequest) as caught:
            build(ch, daily, '2026-10-05', 'json', daily=True)
        assert (str(caught.value), ch._tmp) == ('scan outside the daily name index', [])
    finally:
        ch.close()


def test_load_refuses_index_bound_to_other_source(indexed: dict, daily: str, ch_url: str) -> None:
    ch = Ch(ch_url, db=daily)
    try:
        doc = loads(ch.scalar(f'SELECT doc FROM {daily}.name_index_manifest'))
        ch.exec(f'CREATE TABLE {daily}_moved (doc String) ENGINE = TinyLog')
        ch.exec(f"INSERT INTO {daily}_moved VALUES ({lit(dumps({**doc, 'source_manifest_sha256': '0' * 64}))})")
        ch.exec(f'EXCHANGE TABLES {daily}.name_index_manifest AND {daily}_moved')
        try:
            with pytest.raises(ValueError) as caught:
                daily_name_index.load(ch, daily)
            assert str(caught.value) == 'daily name index is incomplete or bound to a different source manifest'
        finally:
            ch.exec(f'EXCHANGE TABLES {daily}.name_index_manifest AND {daily}_moved')
            ch.exec(f'DROP TABLE {daily}_moved')
    finally:
        ch.close()


def test_cold_lane_runs_daily_build_with_owned_cleanup(indexed: dict, daily: str, ch_url: str) -> None:
    # The legacy runtime's real one-slot deadline lane, over the daily target.
    from threading import BoundedSemaphore

    from dt_cloud.chstore.name_summary import NameSummaryRuntime

    lane = object.__new__(NameSummaryRuntime)
    lane.url, lane.gate, lane.quarantined = ch_url, BoundedSemaphore(1), False
    body = lane.bounded(daily, lambda source, checkpoint: build(source, daily, '2026-10-06', 'json', daily=True, **CAPS))
    ch = Ch(ch_url, db=daily)
    try:
        assert oracle(ch, body) is True
    finally:
        ch.close()
    assert (body['root'], body['direct_matching_rows'], lane.quarantined, lane.gate.acquire(blocking=False)) == ({'b': 113, 'o': 9}, 8, False, True)
