"""The ClickHouse client's temporary-table storage choices."""

import pytest

from dt_cloud.chstore.client import Ch, rowbinary_strings


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 1024])
def test_rowbinary_strings_split_records(chunk_size):
    rows = [b"", b"\0z", b"a\nb", b"x" * 128, "á".encode()]
    encoded = b"\0\x02\0z\x03a\nb\x80\x01" + b"x" * 128 + b"\x02" + "á".encode()
    chunks = [encoded[i:i + chunk_size] for i in range(0, len(encoded), chunk_size)]
    assert list(rowbinary_strings(chunks)) == rows


@pytest.mark.parametrize("encoded,error", [
    (b"\x80", "truncated RowBinary string stream"),
    (b"\x02a", "truncated RowBinary string stream"),
    (b"\xff" * 10, "invalid RowBinary string length"),
    (b"\x81\x80\x80\x20", "RowBinary string exceeds 64 MiB"),
])
def test_invalid_rowbinary_string(encoded, error):
    with pytest.raises(ValueError) as caught:
        list(rowbinary_strings([encoded]))
    assert str(caught.value) == error


@pytest.mark.parametrize(("disk", "ordered", "engine"), [
    (False, True, "Memory"),
    (True, True, "MergeTree ORDER BY path"),
    (True, False, "MergeTree ORDER BY tuple()"),
])
def test_temporary_table_order(disk, ordered, engine, monkeypatch):
    statements = []

    def execute(sql, *, settings):
        statements.append((sql, settings))

    ch = Ch()
    monkeypatch.setattr(ch, "exec", execute)
    ch.tmp("candidate", "SELECT 'b/a' AS path", {"max_threads": 2}, disk=disk, ordered=ordered)
    assert statements == [
        ("DROP TEMPORARY TABLE IF EXISTS candidate", {"max_threads": 2}),
        (f"CREATE TEMPORARY TABLE candidate ENGINE = {engine} AS SELECT 'b/a' AS path", {"max_threads": 2}),
    ]
    assert ch._tmp == ["candidate"]


def test_failed_temporary_creation_is_cleaned_up(monkeypatch):
    statements = []
    ch = Ch()

    def execute(sql, **kwargs):
        statements.append(sql)
        if sql.startswith("CREATE TEMPORARY TABLE"):
            raise RuntimeError("insertion failed")

    monkeypatch.setattr(ch, "exec", execute)
    with pytest.raises(RuntimeError, match="insertion failed"):
        ch.tmp("candidate", "SELECT 'b/a' AS path")
    ch.close()
    assert statements == [
        "DROP TEMPORARY TABLE IF EXISTS candidate",
        "CREATE TEMPORARY TABLE candidate ENGINE = Memory AS SELECT 'b/a' AS path",
        "DROP TEMPORARY TABLE IF EXISTS candidate",
    ]
    assert ch._tmp == []


def test_temporary_numeric_order_key(monkeypatch):
    statements = []
    ch = Ch()
    monkeypatch.setattr(ch, "exec", lambda sql, **kwargs: statements.append(sql))
    ch.tmp("postings", "SELECT 1 AS pre", disk=True, order_by="pre")
    assert statements == [
        "DROP TEMPORARY TABLE IF EXISTS postings",
        "CREATE TEMPORARY TABLE postings ENGINE = MergeTree ORDER BY pre AS SELECT 1 AS pre",
    ]


@pytest.mark.parametrize("disk,key", [
    (False, "pre"), (True, "pre; DROP TABLE x"), (True, "pre, path"),
    (True, ()), (True, ("pre", "path; DROP TABLE x")), (False, ("pre", "path")),
    (True, ["pre", "path"]), (True, ("pre", 1)),
])
def test_temporary_order_key_refuses_nonidentifiers(disk, key, monkeypatch):
    statements = []
    ch = Ch()
    monkeypatch.setattr(ch, "exec", lambda sql, **kwargs: statements.append(sql))
    with pytest.raises(ValueError) as caught:
        ch.tmp("postings", "SELECT 1 AS pre", disk=disk, order_by=key)
    assert str(caught.value) == "temporary order key requires disk and SQL identifiers"
    assert statements == []


def test_temporary_compound_order_key(monkeypatch):
    statements = []
    ch = Ch()
    monkeypatch.setattr(ch, "exec", lambda sql, **kwargs: statements.append(sql))
    ch.tmp("postings", "SELECT 1 AS pre, 2 AS tick", disk=True, order_by=("pre", "tick"))
    assert statements == [
        "DROP TEMPORARY TABLE IF EXISTS postings",
        "CREATE TEMPORARY TABLE postings ENGINE = MergeTree ORDER BY (pre,tick) AS SELECT 1 AS pre, 2 AS tick",
    ]


def test_temporary_prepared_set_has_no_persistent_backup(monkeypatch):
    statements = []
    ch = Ch()
    monkeypatch.setattr(ch, "exec", lambda sql, **kwargs: statements.append(sql))
    ch.tmp("vocabulary_set", "SELECT toUInt32(1) AS nid", set_index=True)
    assert statements == [
        "DROP TEMPORARY TABLE IF EXISTS vocabulary_set",
        "CREATE TEMPORARY TABLE vocabulary_set ENGINE = Set SETTINGS persistent = 0 AS SELECT toUInt32(1) AS nid",
    ]
