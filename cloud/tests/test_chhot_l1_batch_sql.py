"""Native literal frontier batches agree with independent own contributions."""

from json import dumps
from typing import Iterator

import pytest

from dt_cloud.chstore.client import Ch, lit
from dt_cloud.chstore.coarse import CoarseRequest
from dt_cloud.chstore.hot_l1_batch_sql import _query, build

from chserver import ch_db, ch_url  # noqa: F401
from test_chhot_l1 import fleet  # noqa: F401


class Client:
    def __init__(self, *, invalid_utf8: int = 0, error: RuntimeError | None = None) -> None:
        self.calls = []
        self.invalid_utf8, self.error = invalid_utf8, error

    def scalar(self, sql: str) -> str:
        assert sql == "SELECT doc FROM fixture.history_manifest"
        return dumps({"prefix": "", "dates": ["2026-10-05"], "dbs": ["snapshot"]})

    def json(self, sql: str, settings: dict | None = None) -> list[list]:
        self.calls.append((sql, settings))
        if len(self.calls) == 1:
            return [[1, 3, "a"], [4, 7, "b"]]
        if len(self.calls) == 2:
            return [[0, 7]]
        if len(self.calls) == 3:
            return [[8, self.invalid_utf8, 40, 0]]
        if self.error:
            raise self.error
        return [[2, 1, "18446744073709551616", "2"], [1, 4, 0, 3]]


@pytest.mark.parametrize("patterns,message", [
    ((), "native batch requires nonempty slash-free, NUL-free literals of at most 512 characters"),
    (("",), "native batch requires nonempty slash-free, NUL-free literals of at most 512 characters"),
    (("a/b",), "native batch requires nonempty slash-free, NUL-free literals of at most 512 characters"),
    (("a\x00b",), "native batch requires nonempty slash-free, NUL-free literals of at most 512 characters"),
    ((".JSON", ".json"), "native batch normalized literals must be unique"),
])
def test_invalid_registry_refuses_before_io(patterns: tuple[str, ...], message: str) -> None:
    ch = Ch("http://unused.invalid")
    with pytest.raises(CoarseRequest) as caught:
        build(ch, "fixture", "2026-10-05", patterns)
    assert str(caught.value) == message


def test_native_unordered_ids_and_uint128_strings_are_preserved() -> None:
    client = Client()
    body = build(client, "fixture", "2026-10-05", (".JSON", "åRo"))
    assert body["results"] == [
        {"predicate_id": 1, "pattern": ".json", "root": {"b": 0, "o": 3}, "buckets": [
            {"pre": 1, "post": 3, "path": "a", "b": 0, "o": 0}, {"pre": 4, "post": 7, "path": "b", "b": 0, "o": 3},
        ]},
        {"predicate_id": 2, "pattern": "åro", "root": {"b": 18446744073709551616, "o": 2}, "buckets": [
            {"pre": 1, "post": 3, "path": "a", "b": 18446744073709551616, "o": 2}, {"pre": 4, "post": 7, "path": "b", "b": 0, "o": 0},
        ]},
    ]
    assert body["source_validation"] == {"rows": 8, "invalid_utf8_paths": 0, "path_bytes": 40, "invalid_scalar_rows": 0}
    assert body["compiled_patterns"] == 2
    assert client.calls[-1][1] == {"join_algorithm": "hash", "join_use_nulls": 0, "max_query_size": 64 << 20,
                                  "max_ast_elements": 50_000, "max_expanded_ast_elements": 500_000}
    assert body["sql_bytes"] == len((client.calls[-1][0] + " FORMAT JSONCompactEachRow").encode("utf-8"))


def test_utf8_preflight_refuses_before_any_match_statement() -> None:
    client = Client(invalid_utf8=1)
    with pytest.raises(CoarseRequest) as caught:
        build(client, "fixture", "2026-10-05", (".json",))
    assert str(caught.value) == "native batch source contains null or invalid UTF-8 paths"
    assert len(client.calls) == 3
    assert client.calls[-1] == ("""SELECT count(), countIf(isNull(path) OR NOT isValidUTF8(path)), sum(length(path)),
        countIf(isNull(pre) OR isNull(post) OR isNull(b) OR isNull(o) OR pre < 0 OR post < pre OR b < 0 OR o < 0)
        FROM snapshot.nodes""", None)


def test_native_error_propagates_without_fallback_or_partial_body() -> None:
    error = RuntimeError("Hyperscan is unavailable")
    client = Client(error=error)
    with pytest.raises(RuntimeError) as caught:
        build(client, "fixture", "2026-10-05", (".json",))
    assert caught.value is error
    assert len(client.calls) == 4


def test_explicit_sql_byte_budget_refuses_before_source_scan() -> None:
    client = Client()
    with pytest.raises(CoarseRequest) as caught:
        build(client, "fixture", "2026-10-05", (".json",), max_sql_bytes=1)
    assert str(caught.value) == "native batch SQL exceeds its explicit byte budget"
    assert len(client.calls) == 2


def test_frontier_sql_uses_native_row_local_set_difference() -> None:
    assert _query("snapshot", ("a", "aa"), [[1, 3, "a"]]) == """WITH arrayMap(p -> regexpQuoteMeta(p), ['a','aa']) AS regexes
        SELECT s.q, c.pre, sum(toUInt128(s.b)), sum(toUInt128(s.o)) FROM (
            SELECT toUInt64(pre) AS pre, b, o, toUInt8(0) AS shard,
                arrayJoin(arrayExcept(name_hits, parent_hits)) AS q
            FROM (
                SELECT pre, b, o,
                    multiMatchAllIndices(lowerUTF8(if(position(path, '/') = 0, path, substring(path, length(path) - position(reverse(path), '/') + 2))), regexes) AS name_hits,
                    multiMatchAllIndices(lowerUTF8(if(position(path, '/') = 0, '', substring(path, 1, length(path) - position(reverse(path), '/')))), regexes) AS parent_hits
                FROM snapshot.nodes
            )
        ) s ASOF INNER JOIN (SELECT toUInt64(1) AS pre, toUInt64(3) AS post, 'a' AS path, toUInt8(0) AS shard) c ON s.shard = c.shard AND s.pre >= c.pre
        WHERE s.pre <= c.post GROUP BY s.q, c.pre"""


def test_native_set_difference_and_singlematch_uniqueness(ch_url: str) -> None:
    ch = Ch(ch_url)
    try:
        assert ch.json("SELECT arrayExcept([3,1,3,2], [1,1])") == [[[3, 3, 2]]]
        assert ch.json("""WITH ['a','aa','b'] AS regexes
            SELECT arraySort(name_hits), arraySort(parent_hits), arraySort(arrayExcept(name_hits, parent_hits))
            FROM (
                SELECT multiMatchAllIndices(name, regexes) AS name_hits,
                    multiMatchAllIndices(parent, regexes) AS parent_hits
                FROM values('name String, parent String', ('aaab', 'aaaa'), ('aaaa', 'bbbb'), ('', ''))
            )""") == [[[1, 2, 3], [1, 2], [3]], [[1, 2], [3], [1, 2]], [[], [], []]]
    finally:
        ch.close()


@pytest.mark.parametrize("date", ["2026-10-04", "2026-10-05"])
def test_native_batch_all_pattern_buckets_match_own_object_oracle(
    fleet: dict,
    ch_db: str,
    ch_url: str,
    date: str,
) -> None:
    patterns = (".JSON", "json", "hit.json", "%", "_", "bucket", "absent", "Å", "åRo")
    ch = Ch(ch_url, db=ch_db)
    try:
        body = build(ch, ch_db, date, patterns)
        own = fleet["own"] if date == "2026-10-04" else fleet["after"]
        expected = []
        for q, pattern in enumerate(patterns, start=1):
            bucket_rows = []
            for row in body["results"][q - 1]["buckets"]:
                matches = [value for path, value in own.items() if path.split("/")[0] == row["path"] and pattern.lower() in path.lower()]
                bucket_rows.append({"pre": row["pre"], "post": row["post"], "path": row["path"], "b": sum(v[0] for v in matches), "o": sum(v[1] for v in matches)})
            expected.append({"predicate_id": q, "pattern": pattern.lower(), "root": {"b": sum(row["b"] for row in bucket_rows), "o": sum(row["o"] for row in bucket_rows)}, "buckets": bucket_rows})
        assert body["results"] == expected
    finally:
        ch.close()


@pytest.fixture(scope="module")
def literals(ch_db: str, ch_url: str) -> Iterator[dict]:
    db = ch_db + "_literals"
    own = {"a/q.q/q.q🙂": (0, 1), "a/q.q/q.q🙂/plain": (17, 1), "a/ÅRO/Blå.dat": (3, 1),
           "a/line\nbreak": (7, 1), "a/regexp[+]\\literal": (11, 1), "a/repeat repeat": (0, 1),
           "a/percent%_value": (13, 1), "a/last-299": (19, 1)}
    paths = {""}
    for path in own:
        parts = path.split("/")
        paths.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    paths = sorted(paths)
    bounds = {path: max(i for i, child in enumerate(paths) if not path or child == path or child.startswith(path + "/")) for path in paths}
    ch = Ch(ch_url)
    try:
        ch.exec(f"CREATE DATABASE {db}")
        ch.exec(f"CREATE TABLE {db}.dictionary (pre UInt64, post UInt64, depth UInt8, path String) ENGINE = Memory")
        ch.exec(f"CREATE TABLE {db}.nodes (pre UInt64, post UInt64, path String, b UInt64, o UInt64) ENGINE = Memory")
        for pre, path in enumerate(paths):
            values = [value for child, value in own.items() if not path or child == path or child.startswith(path + "/")]
            ch.exec(f"INSERT INTO {db}.dictionary VALUES ({pre},{bounds[path]},{path.count('/') + 1 if path else 0},{lit(path)})")
            ch.exec(f"INSERT INTO {db}.nodes VALUES ({pre},{bounds[path]},{lit(path)},{sum(v[0] for v in values)},{sum(v[1] for v in values)})")
        ch.exec(f"CREATE TABLE {db}.history_manifest (doc String) ENGINE = Memory")
        ch.exec(f"INSERT INTO {db}.history_manifest VALUES (" + lit(dumps({"prefix": "", "dates": ["2026-10-05"], "dbs": [db]})) + ")")
        yield {"db": db, "own": own, "post": bounds[""]}
    finally:
        ch.exec(f"DROP DATABASE IF EXISTS {db} SYNC")
        ch.close()


def test_unicode_newline_regex_punctuation_and_repeated_literal_hits(literals: dict, ch_url: str) -> None:
    patterns = ("q.q", "🙂", "Å", "åRo", "line\nbreak", "[+]", "\\", "repeat", "%_", "a", "absent")
    ch = Ch(ch_url)
    try:
        body = build(ch, literals["db"], "2026-10-05", patterns)
        expected = []
        for q, pattern in enumerate(patterns, start=1):
            values = [value for path, value in literals["own"].items() if pattern.lower() in path.lower()]
            b, o = (sum(value[column] for value in values) for column in (0, 1))
            expected.append({"predicate_id": q, "pattern": pattern.lower(), "root": {"b": b, "o": o},
                             "buckets": [{"pre": 1, "post": literals["post"], "path": "a", "b": b, "o": o}]})
        assert body["results"] == expected
    finally:
        ch.close()


def test_native_ids_above_256_are_not_capped(literals: dict, ch_url: str) -> None:
    patterns = tuple(f"never-{i:03d}" for i in range(299)) + ("last-299",)
    ch = Ch(ch_url)
    try:
        body = build(ch, literals["db"], "2026-10-05", patterns)
        assert body["results"] == [{"predicate_id": q, "pattern": pattern, "root": {"b": 19 if q == 300 else 0, "o": 1 if q == 300 else 0},
                                    "buckets": [{"pre": 1, "post": literals["post"], "path": "a", "b": 19 if q == 300 else 0, "o": 1 if q == 300 else 0}]}
                                   for q, pattern in enumerate(patterns, start=1)]
        assert body["compiled_patterns"] == 300
    finally:
        ch.close()


def test_invalid_utf8_snapshot_is_refused_before_matching(
    literals: dict,
    ch_db: str,
    ch_url: str,
) -> None:
    db = ch_db + "_invalid_utf8"
    ch = Ch(ch_url)
    try:
        ch.exec(f"CREATE DATABASE {db}")
        ch.exec(f"CREATE VIEW {db}.dictionary AS SELECT * FROM {literals['db']}.dictionary")
        ch.exec(f"CREATE TABLE {db}.nodes AS {literals['db']}.nodes ENGINE = Memory")
        ch.exec(f"INSERT INTO {db}.nodes SELECT pre, post, if(pre = 2, unhex('FF'), path), b, o FROM {literals['db']}.nodes")
        ch.exec(f"CREATE TABLE {db}.history_manifest (doc String) ENGINE = Memory")
        ch.exec(f"INSERT INTO {db}.history_manifest VALUES (" + lit(dumps({"prefix": "", "dates": ["2026-10-05"], "dbs": [db]})) + ")")
        with pytest.raises(CoarseRequest) as caught:
            build(ch, db, "2026-10-05", ("q.q",))
        assert str(caught.value) == "native batch source contains null or invalid UTF-8 paths"
    finally:
        ch.exec(f"DROP DATABASE IF EXISTS {db} SYNC")
        ch.close()
