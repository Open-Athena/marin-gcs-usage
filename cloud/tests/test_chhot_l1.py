"""Exact L1 coverage compared with independent disjoint own contributions."""

from json import dumps
from types import SimpleNamespace
from typing import Iterator

import pytest

from dt_cloud.chstore.client import Ch, lit
from dt_cloud.chstore.coarse import CoarseRequest
from dt_cloud.chstore.hot_l1 import build, oracle
from dt_cloud.chstore import hot_l1

from chserver import ch_db, ch_url  # noqa: F401


@pytest.fixture
def bounded_client(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(calls=[], counts={'names': 2, 'postings': 3, 'directories': 1, 'roots': 1})
    state.buckets = [[1, 4, 'a'], [5, 8, 'b'], [9, 9, 'c'], [10, 10, 'd'], [11, 11, 'e'], [12, 12, 'f']]
    tag = 'c' * 32

    class Client:
        def scalar(self, sql):
            state.calls.append(('scalar', sql))
            if sql == 'SELECT doc FROM fleet.history_manifest':
                return dumps({'prefix': '', 'dates': ['2026-10-04'], 'dbs': ['snapshot']})
            table = sql.removeprefix('SELECT count() FROM hot_l1_').removesuffix('_' + tag)
            return str(state.counts[table])

        def tmp(self, table, sql, **kwargs):
            state.calls.append(('tmp', table.removesuffix('_' + tag), ' '.join(sql.split()), kwargs))

        def json(self, sql, **kwargs):
            state.calls.append(('json', ' '.join(sql.split()), kwargs))
            return [[1, 12, 3], [5, 0, 1]]

    monkeypatch.setattr(hot_l1, '_buckets', lambda ch, target: state.buckets)
    monkeypatch.setattr(hot_l1, 'disk_reserve', lambda *args: None)
    monkeypatch.setattr(hot_l1, 'uuid4', lambda: SimpleNamespace(hex=tag))
    monkeypatch.setattr(hot_l1, 'monotonic', lambda: 1.)
    state.client = Client()
    return state


def test_capped_postings_materialize_before_any_directory_sort_and_reuse_exact_six_buckets(bounded_client: SimpleNamespace) -> None:
    state, tag = bounded_client, 'c' * 32
    uncapped = build(state.client, 'fleet', '2026-10-04', '.json')
    state.calls.clear()
    body = build(state.client, 'fleet', '2026-10-04', '.JSON', max_names=2, max_postings=3)
    assert body == {**uncapped, 'work_bounds': {'max_names': 2, 'max_postings': 3, 'max_outer_roots': None},
                    'direct_matching_rows': 3, 'stages': {**uncapped['stages'], 'postings_s': 0.0}}
    assert body['buckets'] == [{'pre': lo, 'post': hi, 'path': path, 'b': 12 if path == 'a' else 0,
                               'o': 3 if path == 'a' else 1 if path == 'b' else 0} for lo, hi, path in state.buckets]
    assert body['root'] == {'b': 12, 'o': 4}
    names_sql = "SELECT nid FROM fleet.names WHERE l LIKE '%.json%' LIMIT 3"
    postings = f'hot_l1_postings_{tag}'
    dirs = f'hot_l1_directories_{tag}'
    assert state.calls[2:7] == [
        ('tmp', 'hot_l1_names', names_sql, {'disk': True, 'order_by': 'nid'}),
        ('scalar', f'SELECT count() FROM hot_l1_names_{tag}'),
        ('tmp', 'hot_l1_postings', f'SELECT toUInt64(pre) AS pre, toUInt64(post) AS post, b, o FROM snapshot.nodes_by_name WHERE nid IN (SELECT nid FROM hot_l1_names_{tag}) LIMIT 4', {'disk': True, 'ordered': False}),
        ('scalar', f'SELECT count() FROM {postings}'),
        ('tmp', 'hot_l1_directories', f'SELECT * FROM (SELECT pre, post, b, o FROM {postings}) WHERE pre != post', {'disk': True, 'order_by': 'pre'}),
    ]
    assert state.calls[7:10] == [
        ('scalar', f'SELECT count() FROM {dirs}'),
        ('tmp', 'hot_l1_roots', f'SELECT pre, post, b, o FROM ( SELECT *, row_number() OVER (ORDER BY pre) AS rn, max(post) OVER (ORDER BY pre ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS previous_end FROM {dirs} ) WHERE rn = 1 OR pre > previous_end', {'disk': True, 'order_by': 'pre'}),
        ('scalar', f'SELECT count() FROM hot_l1_roots_{tag}'),
    ]
    assert state.calls[10] == ('json', f'SELECT c.pre, sum(s.b), sum(s.o) FROM ( SELECT l.pre, l.b, l.o, toUInt8(0) AS shard FROM ( SELECT *, toUInt8(0) AS shard FROM (SELECT pre, post, b, o FROM {postings}) WHERE pre = post ) l ASOF LEFT JOIN ( SELECT pre, post, toUInt8(0) AS shard, toUInt8(1) AS covered FROM hot_l1_roots_{tag} ) r ON l.shard = r.shard AND l.pre >= r.pre WHERE r.covered = 0 OR l.pre > r.post UNION ALL SELECT pre, b, o, toUInt8(0) AS shard FROM hot_l1_roots_{tag} ) s ASOF INNER JOIN hot_l1_buckets_{tag} c ON s.shard = c.shard AND s.pre >= c.pre WHERE s.pre <= c.post GROUP BY c.pre', {'settings': {'join_algorithm': 'hash', 'join_use_nulls': 0}})


@pytest.mark.parametrize('stage,message', [
    ('names', 'hot L1 vocabulary exceeds its 2-name work budget'),
    ('postings', 'hot L1 direct-posting set exceeds its 3-row work budget'),
])
def test_caps_refuse_plus_one_before_directory_window_or_aggregate(bounded_client: SimpleNamespace, stage: str, message: str) -> None:
    state, tag = bounded_client, 'c' * 32
    state.counts[stage] += 1
    with pytest.raises(CoarseRequest) as caught:
        build(state.client, 'fleet', '2026-10-04', '.json', max_names=2, max_postings=3)
    assert str(caught.value) == message
    assert [call[0:2] for call in state.calls] == [
        ('scalar', 'SELECT doc FROM fleet.history_manifest'), ('tmp', 'hot_l1_buckets'),
        ('tmp', 'hot_l1_names'), ('scalar', f'SELECT count() FROM hot_l1_names_{tag}'),
        *([('tmp', 'hot_l1_postings'), ('scalar', f'SELECT count() FROM hot_l1_postings_{tag}')] if stage == 'postings' else []),
    ]


@pytest.mark.parametrize('kwargs,message', [
    ({'max_names': True}, 'hot L1 vocabulary budget must be a positive integer when supplied'),
    ({'max_names': 0}, 'hot L1 vocabulary budget must be a positive integer when supplied'),
    ({'max_postings': 2.5}, 'hot L1 direct-posting budget must be a positive integer when supplied'),
    ({'max_postings': -1}, 'hot L1 direct-posting budget must be a positive integer when supplied'),
    ({'max_roots': True}, 'hot L1 outer-directory budget must be positive when supplied'),
])
def test_invalid_cap_types_refuse_before_io(bounded_client: SimpleNamespace, kwargs: dict, message: str) -> None:
    with pytest.raises(CoarseRequest) as caught:
        build(bounded_client.client, 'fleet', '2026-10-04', '.json', **kwargs)
    assert (str(caught.value), bounded_client.calls) == (message, [])


@pytest.mark.parametrize('pattern', ['bad\0name', '\ud800'])
def test_nul_and_invalid_utf8_literal_refuse_before_io(bounded_client: SimpleNamespace, pattern: str) -> None:
    with pytest.raises(CoarseRequest) as caught:
        build(bounded_client.client, 'fleet', '2026-10-04', pattern)
    assert (str(caught.value), bounded_client.calls) == ('hot L1 requires a valid UTF-8 literal without NUL', [])


def test_vocabulary_only_cap_does_not_materialize_postings_or_claim_direct_counts(bounded_client: SimpleNamespace) -> None:
    body = build(bounded_client.client, 'fleet', '2026-10-04', '.json', max_names=2)
    assert (body['work_bounds'], body['direct_matching_rows'], body['stages']) == (
        {'max_names': 2, 'max_postings': None, 'max_outer_roots': None}, None,
        {'vocabulary_s': 0.0, 'directory_roots_s': 0.0, 'aggregate_s': 0.0},
    )
    assert [call[1] for call in bounded_client.calls if call[0] == 'tmp'] == ['hot_l1_buckets', 'hot_l1_names', 'hot_l1_directories', 'hot_l1_roots']


def test_all_bucket_shortcut_does_not_claim_zero_direct_matches_or_apply_row_caps(bounded_client: SimpleNamespace) -> None:
    bounded_client.buckets[:] = [[lo, hi, f'bucket-{path}'] for lo, hi, path in bounded_client.buckets]
    body = build(bounded_client.client, 'fleet', '2026-10-04', 'bucket', max_names=1, max_postings=1, max_roots=1)
    assert (body['work_bounds'], body['direct_matching_rows'], body['all_buckets_covered'], body['stages']) == (
        {'max_names': 1, 'max_postings': 1, 'max_outer_roots': 1}, None, True,
        {'vocabulary_s': 0.0, 'directory_roots_s': 0.0, 'aggregate_s': 0.0, 'postings_s': 0.0},
    )
    assert [call for call in bounded_client.calls if call[0] != 'tmp'] == [
        ('scalar', 'SELECT doc FROM fleet.history_manifest'),
        ('json', 'SELECT pre, b, o FROM snapshot.nodes WHERE pre IN (1,5,9,10,11,12)', {}),
    ]


@pytest.mark.parametrize("pattern,budget,message", [
    ("foo/bar", 100_000, "hot L1 requires one nonempty literal without slashes, at most 512 characters"),
    ("", 100_000, "hot L1 requires one nonempty literal without slashes, at most 512 characters"),
    ("x" * 513, 100_000, "hot L1 requires one nonempty literal without slashes, at most 512 characters"),
    (".json", 0, "hot L1 outer-directory budget must be positive when supplied"),
    (".json", -1, "hot L1 outer-directory budget must be positive when supplied"),
])
def test_invalid_inputs_refuse_before_server_io(
    pattern: str,
    budget: int,
    message: str,
) -> None:
    ch = Ch("http://unused.invalid")
    with pytest.raises(CoarseRequest) as caught:
        build(ch, "fixture", "2026-10-04", pattern, max_roots=budget)
    assert str(caught.value) == message
    assert ch._tmp == []


@pytest.mark.parametrize("prefix,date,message", [
    ("bucket-a", "2026-10-04", "hot L1 requires a global frozen target"),
    ("", "2026-10-03", "scan outside the frozen index"),
])
def test_manifest_scope_refusal_before_staging(
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
    date: str,
    message: str,
) -> None:
    ch = Ch("http://unused.invalid")
    asked = []

    def manifest(sql: str) -> str:
        asked.append(sql)
        return dumps({"prefix": prefix, "dates": ["2026-10-04"], "dbs": ["snapshot"]})

    monkeypatch.setattr(ch, "scalar", manifest)
    with pytest.raises(CoarseRequest) as caught:
        build(ch, "fixture", date, ".json")
    assert str(caught.value) == message
    assert asked == ["SELECT doc FROM fixture.history_manifest"]
    assert ch._tmp == []


@pytest.fixture(scope="module")
def fleet(ch_db: str, ch_url: str) -> Iterator[dict]:
    own = {
        "bucket-a/run.json": (0, 1),
        "bucket-a/run.json/plain.bin": (5, 1),
        "bucket-a/run.json/nested.json": (0, 1),
        "bucket-a/run.json/nested.json/leaf.json": (7, 1),
        "bucket-a/loose.json": (11, 1),
        "bucket-a/nohits/plain.bin": (13, 1),
        "bucket-b/folder/hit.json": (17, 1),
        "bucket-b/folder/percent%.bin": (3, 1),
        "bucket-b/folder/under_.bin": (0, 1),
        "bucket-c/json-bucket/other.bin": (19, 1),
        "bucket-d/json.json": (0, 1),
        "bucket-d/json.json/plain.bin": (23, 1),
        "bucket-e/other": (29, 1),
        "bucket-e/ÅRO/Blå.dat": (37, 1),
        "bucket-f/missing.json": (31, 1),
    }
    after = {path: value for path, value in own.items() if not path.startswith(("bucket-a/", "bucket-f/"))}
    paths = {""}
    for path in own:
        parts = path.split("/")
        paths.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    ordered = sorted(paths)
    positions = {path: i for i, path in enumerate(ordered)}
    bounds = {path: max(i for i, child in enumerate(ordered) if child == path or not path or child.startswith(path + "/")) for path in ordered}
    names = sorted({path.rsplit("/", 1)[-1].lower() for path in ordered})
    nids = {name: i for i, name in enumerate(names)}
    ch = Ch(ch_url, db=ch_db)
    try:
        ch.exec("CREATE TABLE dictionary (pre UInt64, post UInt64, depth UInt8, path String) ENGINE = Memory")
        ch.exec("INSERT INTO dictionary VALUES " + ",".join(
            f"({positions[path]},{bounds[path]},{path.count('/') + 1 if path else 0},{lit(path)})" for path in ordered
        ))
        ch.exec("CREATE TABLE names (nid UInt32, l String) ENGINE = Memory")
        ch.exec("INSERT INTO names VALUES " + ",".join(f"({nid},{lit(name)})" for name, nid in nids.items()))
        after_db = ch_db + "_after"
        ch.exec(f"CREATE DATABASE {after_db}")
        for db, values in ((ch_db, own), (after_db, after)):
            ch.exec(f"CREATE TABLE {db}.nodes (pre UInt64, post UInt64, path String, nid UInt32, b UInt64, o UInt64) ENGINE = Memory")
            rows = []
            for path in ordered:
                subtree = [value for child, value in values.items() if child == path or not path or child.startswith(path + "/")]
                if path and not subtree:
                    continue
                b, o = (sum(value[column] for value in subtree) for column in (0, 1))
                rows.append(f"({positions[path]},{bounds[path]},{lit(path)},{nids[path.rsplit('/', 1)[-1].lower()]},{b},{o})")
            ch.exec(f"INSERT INTO {db}.nodes VALUES " + ",".join(rows))
            ch.exec(f"CREATE VIEW {db}.nodes_by_name AS SELECT * FROM {db}.nodes")
        ch.exec("CREATE TABLE history_manifest (doc String) ENGINE = Memory")
        ch.exec("INSERT INTO history_manifest VALUES (" + lit(dumps({"prefix": "", "dates": ["2026-10-04", "2026-10-05"], "dbs": [ch_db, after_db]})) + ")")
        yield {"own": own, "after": after, "buckets": [f"bucket-{letter}" for letter in "abcdef"]}
    finally:
        ch.exec(f"DROP DATABASE IF EXISTS {ch_db}_after SYNC")
        ch.close()


@pytest.mark.parametrize("date,pattern", [
    ("2026-10-04", ".JSON"), ("2026-10-04", "json"),
    ("2026-10-04", "hit.json"), ("2026-10-04", "%"), ("2026-10-04", "_"),
    ("2026-10-04", "bucket"), ("2026-10-04", "absent"),
    ("2026-10-04", "Å"), ("2026-10-04", "åRo"),
    ("2026-10-05", ".json"), ("2026-10-05", "bucket-a"), ("2026-10-05", "bucket"),
])
def test_complete_buckets_equal_fullpath_own_oracle(
    fleet: dict,
    ch_db: str,
    ch_url: str,
    date: str,
    pattern: str,
) -> None:
    ch = Ch(ch_url, db=ch_db)
    try:
        body = build(ch, ch_db, date, pattern, min_free_bytes=0)
        own = fleet["own"] if date == "2026-10-04" else fleet["after"]
        expected = []
        for bucket in fleet["buckets"]:
            values = [value for path, value in own.items() if path.split("/")[0] == bucket and pattern.lower() in path.lower()]
            expected.append({"path": bucket, "b": sum(value[0] for value in values), "o": sum(value[1] for value in values)})
        actual = [{key: row[key] for key in ("path", "b", "o")} for row in body["buckets"]]
        assert actual == expected
        assert body["root"] == {"b": sum(row["b"] for row in expected), "o": sum(row["o"] for row in expected)}
        assert (body["pattern"], body["exact"], body["incremental"]) == (pattern.lower(), True, False)
        assert oracle(ch, body) is True
    finally:
        ch.close()


def test_nested_directory_hits_and_repeated_builds_are_disjoint(
    fleet: dict,
    ch_db: str,
    ch_url: str,
) -> None:
    ch = Ch(ch_url, db=ch_db)
    try:
        first = build(ch, ch_db, "2026-10-04", ".json", min_free_bytes=0)
        second = build(ch, ch_db, "2026-10-04", ".json", max_roots=100_001, min_free_bytes=0)
        assert (first["root"], first["matching_nonleaf_rows"], first["outer_directory_roots"]) == ({"b": 94, "o": 9}, 3, 2)
        assert second["buckets"] == first["buckets"]
        assert oracle(ch, first) is True
        assert oracle(ch, second) is True
        assert len(ch._tmp) == 8
        ch.close()
        assert ch._tmp == []
    finally:
        ch.close()


@pytest.mark.parametrize('date', ['2026-10-04', '2026-10-05'])
def test_capped_complete_fixture_matches_independent_own_object_and_uncapped_coverage(
    fleet: dict,
    ch_db: str,
    ch_url: str,
    date: str,
) -> None:
    ch = Ch(ch_url, db=ch_db)
    try:
        body = build(ch, ch_db, date, '.json', max_names=100, max_postings=100, max_roots=100, min_free_bytes=0)
        uncapped = build(ch, ch_db, date, '.json', min_free_bytes=0)
        own = fleet['own'] if date == '2026-10-04' else fleet['after']
        expected = []
        for bucket in fleet['buckets']:
            contributions = [value for path, value in own.items() if path.split('/')[0] == bucket and '.json' in path.lower()]
            expected.append({'path': bucket, 'b': sum(value[0] for value in contributions), 'o': sum(value[1] for value in contributions)})
        assert [{key: row[key] for key in ('path', 'b', 'o')} for row in body['buckets']] == expected
        assert (body['root'], body['buckets']) == (uncapped['root'], uncapped['buckets'])
        assert body['direct_matching_rows'] == (7 if date == '2026-10-04' else 2)
        assert body['work_bounds'] == {'max_names': 100, 'max_postings': 100, 'max_outer_roots': 100}
        assert oracle(ch, body) is True
    finally:
        ch.close()
        assert ch._tmp == []


def test_outer_directory_budget_refuses_without_partial_answer(
    fleet: dict,
    ch_db: str,
    ch_url: str,
) -> None:
    ch = Ch(ch_url, db=ch_db)
    try:
        with pytest.raises(CoarseRequest) as caught:
            build(ch, ch_db, "2026-10-04", ".json", max_roots=1, min_free_bytes=0)
        assert str(caught.value) == "hot L1 outer-directory set exceeds its 1-root work budget"
    finally:
        ch.close()


@pytest.mark.parametrize("date,pattern,message", [
    ("2026-10-03", ".json", "scan outside the frozen index"),
    ("2026-10-04", "foo/bar", "hot L1 requires one nonempty literal without slashes, at most 512 characters"),
    ("2026-10-04", "", "hot L1 requires one nonempty literal without slashes, at most 512 characters"),
])
def test_invalid_requests_create_no_temporary_indexes(
    fleet: dict,
    ch_db: str,
    ch_url: str,
    date: str,
    pattern: str,
    message: str,
) -> None:
    ch = Ch(ch_url, db=ch_db)
    try:
        with pytest.raises(CoarseRequest) as caught:
            build(ch, ch_db, date, pattern, min_free_bytes=0)
        assert str(caught.value) == message
        assert ch._tmp == []
    finally:
        ch.close()
