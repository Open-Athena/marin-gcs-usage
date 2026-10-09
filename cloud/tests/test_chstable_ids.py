from collections.abc import Iterator
from typing import Any
from pathlib import Path
from hashlib import sha256
from struct import pack

import pytest

from dt_cloud.chstore.client import Ch
from dt_cloud.chstore.stable_ids import StagedIds, checkpoint_fingerprint, stage, stage_reserved

from chserver import ch_db, ch_url  # noqa: F401


@pytest.fixture
def identity_tables(ch_db: str, ch_url: str) -> Iterator[Ch]:
    ch = Ch(ch_url, db=ch_db)
    ch.exec("CREATE TEMPORARY TABLE known (id UInt64, depth UInt8, path String) ENGINE = Memory")
    ch.exec("INSERT INTO known VALUES (0,0,''),(7,1,'bucket'),(99,2,'bucket/old')")
    ch.exec("CREATE TEMPORARY TABLE incoming (depth UInt8, path String) ENGINE = Memory")
    try:
        yield ch
    finally:
        ch.exec("DROP TEMPORARY TABLE incoming")
        ch.exec("DROP TEMPORARY TABLE known")
        ch.close()


def test_new_parents_resolve_in_the_same_batch_and_ids_are_deterministic(identity_tables: Ch) -> None:
    ch = identity_tables
    ch.exec("INSERT INTO incoming VALUES (3,'bucket/new/file'),(2,'bucket/new'),(2,'bucket/old'),(3,'bucket/new/file'),(2,'bucket/New')")
    first = stage(ch, "known", "incoming", 100)
    expected = [[100, 2, "bucket/New", 7], [101, 2, "bucket/new", 7], [102, 3, "bucket/new/file", 101]]
    assert first == StagedIds("stable_ids_result", 3, 103)
    assert ch.json(f"SELECT id, depth, path, parent_id FROM {first.table} ORDER BY id") == expected
    second = stage(ch, "known", "incoming", 100, prefix="retry")
    assert ch.json(f"SELECT id, depth, path, parent_id FROM {second.table} ORDER BY id") == expected
    assert ch.json("SELECT id, depth, path FROM known ORDER BY id") == [[0, 0, ""], [7, 1, "bucket"], [99, 2, "bucket/old"]]


def test_published_bindings_can_be_reused_without_reallocation(identity_tables: Ch) -> None:
    ch = identity_tables
    ch.exec("INSERT INTO incoming VALUES (2,'bucket/new'),(3,'bucket/new/file')")
    first = stage(ch, "known", "incoming", 1 << 40)
    ch.exec(f"INSERT INTO known SELECT id,depth,path FROM {first.table}")
    retry = stage(ch, "known", "incoming", first.next_id)
    assert retry == StagedIds("stable_ids_result", 0, (1 << 40) + 2)
    assert ch.json(f"SELECT id, path FROM {retry.table}") == []
    assert ch.json("SELECT id, path FROM known ORDER BY id") == [
        [0, ""], [7, "bucket"], [99, "bucket/old"], [1 << 40, "bucket/new"], [(1 << 40) + 1, "bucket/new/file"],
    ]


def test_an_empty_dictionary_can_stage_its_root_and_descendants(identity_tables: Ch) -> None:
    ch = identity_tables
    ch.exec("TRUNCATE TABLE known")
    ch.exec("INSERT INTO incoming VALUES (0,''),(1,'bucket'),(2,'bucket/file')")
    staged = stage(ch, "known", "incoming", 0)
    assert ch.json(f"SELECT id, depth, path, parent_id FROM {staged.table} ORDER BY id") == [
        [0, 0, "", -1], [1, 1, "bucket", 0], [2, 2, "bucket/file", 1],
    ]


@pytest.mark.parametrize("rows,next_id,kwargs,error", [
    ("(2,'bucket/orphan/file')", 100, {}, "incoming path depth or root scope is inconsistent"),
    ("(3,'bucket/orphan/file')", 100, {}, "new-path batch is missing a structural parent"),
    ("(1,'elsewhere')", 100, {"root": "bucket"}, "incoming path depth or root scope is inconsistent"),
    ("(2,'bucket/new')", 99, {}, "ID reservation overlaps an existing identity"),
    ("(2,'bucket/new'),(2,'bucket/another')", 100, {"max_new": 1}, "new-path batch exceeds its 1-path work budget"),
    ("(2,'bucket/new'),(2,'bucket/another')", (1 << 63) - 1, {}, "ID reservation exceeds the signed parent-ID domain"),
    ("(2,'bucket/new')", 100, {"max_known": 2}, "known bindings exceed the bounded staging budget"),
])
def test_invalid_or_incomplete_batches_refuse_without_changing_known_bindings(
    identity_tables: Ch,
    rows: str,
    next_id: int,
    kwargs: dict[str, Any],
    error: str,
) -> None:
    ch = identity_tables
    ch.exec(f"INSERT INTO incoming VALUES {rows}")
    with pytest.raises(ValueError) as caught:
        stage(ch, "known", "incoming", next_id, **kwargs)
    assert str(caught.value) == error
    assert ch.json("SELECT id, depth, path FROM known ORDER BY id") == [[0, 0, ""], [7, 1, "bucket"], [99, 2, "bucket/old"]]


def test_synthetic_benchmark_checks_complete_retry_bindings(ch_url: str) -> None:
    from dt_cloud.chstore.stable_ids import bench

    body = bench(ch_url, 2, 3, trials=2)
    assert (body["incoming_rows"], body["known_rows"], body["reproducible_bindings"]) == (8, 2, True)
    assert [(r["trial"], r["new_paths"], r["next_id"]) for r in body["trials"]] == [
        (0, 8, (1 << 40) + 8), (1, 8, (1 << 40) + 8),
    ]
    assert [r["binding_fingerprint"] for r in body["trials"]] == [body["trials"][0]["binding_fingerprint"]] * 2


def test_staging_table_names_cannot_overwrite_an_input(identity_tables: Ch) -> None:
    ch = identity_tables
    ch.exec("CREATE TEMPORARY TABLE stable_ids_result ENGINE = Memory AS SELECT * FROM known")
    try:
        with pytest.raises(ValueError) as caught:
            stage(ch, "stable_ids_result", "incoming", 100)
        assert str(caught.value) == "staging table prefix overlaps an input table"
        assert ch.json("SELECT id,depth,path FROM stable_ids_result ORDER BY id") == [[0, 0, ""], [7, 1, "bucket"], [99, 2, "bucket/old"]]
    finally:
        ch.exec("DROP TEMPORARY TABLE stable_ids_result")


@pytest.mark.parametrize("reserved_count", [0, 1, 3])
def test_staging_refuses_a_different_reserved_cardinality(identity_tables: Ch, reserved_count: int) -> None:
    ch = identity_tables
    ch.exec("INSERT INTO incoming VALUES (2,'bucket/new'),(3,'bucket/new/file')")
    with pytest.raises(ValueError) as caught:
        stage(ch, "known", "incoming", 100, reserved_count=reserved_count)
    assert str(caught.value) == "new-path cardinality differs from the durable reservation"
    assert ch._tmp == ["stable_ids_new"]
    assert ch.json("SELECT id,depth,path FROM known ORDER BY id") == [[0, 0, ""], [7, 1, "bucket"], [99, 2, "bucket/old"]]


def test_journal_reopen_reuses_exact_staged_bindings(identity_tables: Ch, tmp_path: Path) -> None:
    from dt_cloud.chstore.reservations import ReservationLedger

    ch = identity_tables
    ch.exec("INSERT INTO incoming VALUES (2,'bucket/new'),(3,'bucket/new/file')")
    path = tmp_path / "reservations.sqlite"
    expected = [[100, 2, "bucket/new", 7], [101, 3, "bucket/new/file", 100]]
    with ReservationLedger(path, "fixture", 100).writer() as ledger:
        reservation = ledger.reserve("scan-a", "a" * 64, 0, 2)
        staged = stage(ch, "known", "incoming", reservation.first_id, reserved_count=reservation.count)
        assert ch.json(f"SELECT id,depth,path,parent_id FROM {staged.table} ORDER BY id") == expected
    # No external commit was observed: resume the same checkpoint/range.
    with ReservationLedger(path, "fixture", 100).writer() as ledger:
        retry = ledger.reserve("scan-a", "a" * 64, 0, 2)
        assert retry == reservation
        staged = stage(ch, "known", "incoming", retry.first_id, reserved_count=retry.count)
        assert staged.next_id == retry.next_id == 102
        assert ch.json(f"SELECT id,depth,path,parent_id FROM {staged.table} ORDER BY id") == expected


@pytest.mark.parametrize("sql", [
    "SELECT toInt64(-2) id,toUInt8(1) depth,'bucket' path",
    f"SELECT toUInt64({1 << 63}) id,toUInt8(1) depth,'bucket' path",
    "SELECT toUInt64(7) id,toUInt8(2) depth,'bucket' path",
    "SELECT toUInt64(7) id,toUInt8(1) depth,CAST(NULL AS Nullable(String)) path",
])
def test_invalid_known_bindings_cannot_become_parent_ids(identity_tables: Ch, sql: str) -> None:
    ch = identity_tables
    ch.tmp("invalid_known", sql)
    ch.exec("INSERT INTO incoming VALUES (2,'bucket/new')")
    with pytest.raises(ValueError) as caught:
        stage(ch, "invalid_known", "incoming", 100)
    assert str(caught.value) == "existing identities have an invalid parent-ID domain or path depth"
    assert ch._tmp == ["invalid_known"]


def test_checkpoint_fingerprint_covers_canonical_bindings_input_and_scope(identity_tables: Ch) -> None:
    ch = identity_tables
    ch.exec("INSERT INTO incoming VALUES (3,'bucket/new/file'),(2,'bucket/new')")
    known = b"".join(pack("<QI", identity, depth) + bytes([len(path)]) + path.encode() for identity, depth, path in [
        (0, 0, ""), (7, 1, "bucket"), (99, 2, "bucket/old"),
    ])
    incoming = b"".join(pack("<I", depth) + bytes([len(path)]) + path.encode() for depth, path in [
        (2, "bucket/new"), (3, "bucket/new/file"),
    ])
    header = b"disk-tree/stable-id-checkpoint/v1\0"
    for root in ("", "bucket", "bücket"):
        expected = sha256(header + pack("<Q", len(root.encode())) + root.encode() + b"known\0" + pack("<Q", 3) + known + b"incoming\0" + pack("<Q", 2) + incoming).hexdigest()
        assert checkpoint_fingerprint(ch, "known", "incoming", root=root) == expected


def test_reserved_staging_checks_real_inputs_and_refuses_changed_retry(identity_tables: Ch, tmp_path: Path) -> None:
    from dt_cloud.chstore.reservations import ReservationLedger

    ch = identity_tables
    ch.exec("INSERT INTO incoming VALUES (2,'bucket/new'),(3,'bucket/new/file')")
    fingerprint = checkpoint_fingerprint(ch, "known", "incoming")
    path = tmp_path / "checked-reservation.sqlite"
    with ReservationLedger(path, "checked-fixture", 100).writer() as ledger:
        saved = ledger.reserve("scan-a", fingerprint, 0, 2)
        result = stage_reserved(ch, "known", "incoming", saved)
        assert ch.json(f"SELECT id,depth,path,parent_id FROM {result.table} ORDER BY id") == [
            [100, 2, "bucket/new", 7], [101, 3, "bucket/new/file", 100],
        ]
    with ReservationLedger(path, "checked-fixture", 100).writer() as ledger:
        retry = ledger.reserve("scan-a", fingerprint, 0, 2)
        assert stage_reserved(ch, "known", "incoming", retry).next_id == 102
        ch.exec("INSERT INTO incoming VALUES (3,'bucket/new/extra')")
        with pytest.raises(ValueError) as caught:
            stage_reserved(ch, "known", "incoming", retry, prefix="changed_retry")
        assert str(caught.value) == "staging inputs differ from the reserved checkpoint fingerprint"
        assert ch._tmp == ["stable_ids_new", "stable_ids_assigned", "stable_ids_result"] * 2


def test_checkpoint_fingerprint_refuses_oversized_streams(identity_tables: Ch) -> None:
    with pytest.raises(ValueError) as caught:
        checkpoint_fingerprint(identity_tables, "known", "incoming", max_bytes=1)
    assert str(caught.value) == "checkpoint fingerprint exceeds its 1-byte streaming budget"


@pytest.mark.parametrize("sql", [
    "SELECT toUInt8(2) depth,CAST(NULL AS Nullable(String)) path",
    "SELECT CAST(NULL AS Nullable(UInt8)) depth,'bucket/new' path",
])
def test_null_inputs_cannot_bypass_parent_resolution(identity_tables: Ch, sql: str) -> None:
    ch = identity_tables
    ch.tmp("nullable_incoming", sql)
    with pytest.raises(ValueError) as caught:
        stage(ch, "known", "nullable_incoming", 100)
    assert str(caught.value) == "incoming path depth or root scope is inconsistent"
    assert ch._tmp == ["nullable_incoming"]
