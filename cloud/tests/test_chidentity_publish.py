from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from dt_cloud.chstore.client import Ch, lit
from dt_cloud.chstore.identity_publish import IdentityPublisher
from dt_cloud.chstore.reservations import ReservationLedger
from dt_cloud.chstore.stable_ids import checkpoint_fingerprint

from chserver import ch_db, ch_url  # noqa: F401


@pytest.fixture
def publisher(ch_db: str, ch_url: str, tmp_path: Path) -> Iterator[IdentityPublisher]:
    ch = Ch(ch_url, db=ch_db)
    ledger = ReservationLedger(tmp_path / "identity.sqlite", "publication-fixture", 0)
    service = IdentityPublisher(ch, "identity_" + uuid4().hex, ledger)
    try:
        with ledger.writer():
            service.initialize()
        ch.tmp("initial_paths", "SELECT toUInt32(0) depth,'' path UNION ALL SELECT toUInt32(1),'bucket' UNION ALL SELECT toUInt32(2),'bucket/new'")
        with ledger.writer():
            service.publish("initial_paths", "initial", 0)
        yield service
    finally:
        ch.close()
        ch.exec(f"DROP TABLE IF EXISTS {service.commits}")
        ch.exec(f"DROP TABLE IF EXISTS {service.bindings}")


OLD = [[0, 0, "", -1], [1, 1, "bucket", 0], [2, 2, "bucket/new", 1]]
NEW = [*OLD, [3, 2, "bucket/other", 1], [4, 3, "bucket/new/file", 2]]


@pytest.mark.parametrize("phase,physical_rows", [("data", 7), ("marker", 5)])
def test_interrupted_identity_publication_retries_without_visible_duplicates(
    publisher: IdentityPublisher,
    phase: str,
    physical_rows: int,
) -> None:
    ch, ledger = publisher.ch, publisher.ledger
    ch.tmp("new_paths", "SELECT toUInt32(3) depth,'bucket/new/file' path UNION ALL SELECT toUInt32(2),'bucket/other'")

    def fail(at: str) -> None:
        reader_ch = ch.fork()
        reader = IdentityPublisher(reader_ch, publisher.prefix, ledger)
        try:
            assert reader_ch.json(f"SELECT * FROM ({reader.source(1)}) ORDER BY id") == OLD
            assert reader_ch.json(f"SELECT * FROM ({reader.source()}) ORDER BY id") == (OLD if at == "data" else NEW)
        finally:
            reader_ch.close()
        if at == phase:
            raise RuntimeError("interrupted after " + at)

    with ledger.writer():
        with pytest.raises(RuntimeError) as caught:
            publisher.publish("new_paths", "second", 1, after=fail)
        assert str(caught.value) == "interrupted after " + phase
    assert ch.json(f"SELECT * FROM ({publisher.source(1)}) ORDER BY id") == OLD
    assert ch.json(f"SELECT * FROM ({publisher.source()}) ORDER BY id") == (OLD if phase == "data" else NEW)
    reopened = ReservationLedger(ledger.path, ledger.namespace, 0)
    recovered = IdentityPublisher(ch, publisher.prefix, reopened)
    with reopened.writer():
        result = recovered.publish("new_paths", "second", 1)
        assert (result["generation"], result["first_id"], result["count"]) == (2, 3, 2)
    assert ch.json(f"SELECT * FROM ({recovered.source()}) ORDER BY id") == NEW
    assert ch.scalar(f"SELECT count() FROM {publisher.bindings}") == str(physical_rows)
    assert ch.scalar(f"SELECT count() FROM {publisher.commits}") == "2"


def test_invalid_input_does_not_strand_a_durable_reservation(publisher: IdentityPublisher) -> None:
    ch, ledger = publisher.ch, publisher.ledger
    ch.tmp("orphan", "SELECT toUInt32(3) depth,'bucket/missing/file' path")
    with ledger.writer():
        with pytest.raises(ValueError) as caught:
            publisher.publish("orphan", "invalid", 1)
        assert str(caught.value) == "new-path batch is missing a structural parent"
        assert ledger.preview_first_id("invalid") == 3
        ch.tmp("valid", "SELECT toUInt32(2) depth,'bucket/valid' path")
        assert publisher.publish("valid", "valid", 1)["first_id"] == 3


def test_equivalent_late_commit_attempt_selects_one_complete_copy(publisher: IdentityPublisher) -> None:
    ch = publisher.ch
    _, old_attempt, _ = publisher.markers()[0]
    copy = "f" * 32
    ch.exec(f"INSERT INTO {publisher.bindings} SELECT {lit(copy)},id,depth,path,parent_id FROM {publisher.bindings} WHERE attempt={lit(old_attempt)}")
    ch.exec(f"INSERT INTO {publisher.commits} SELECT generation,{lit(copy)},doc FROM {publisher.commits} WHERE attempt={lit(old_attempt)}")
    assert ch.json(f"SELECT * FROM ({publisher.source()}) ORDER BY id") == OLD
    assert ch.scalar(f"SELECT count() FROM {publisher.bindings}") == "6"


def test_conflicting_marker_is_refused_not_merged(publisher: IdentityPublisher) -> None:
    ch = publisher.ch
    ch.exec(f"INSERT INTO {publisher.commits} SELECT generation,attempt,replaceOne(doc,'publication-fixture','wrong-namespace') FROM {publisher.commits}")
    with pytest.raises(ValueError) as caught:
        publisher.source()
    assert str(caught.value) == "conflicting identity commit manifests"


def test_partial_unpublished_attempt_never_becomes_readable(publisher: IdentityPublisher) -> None:
    ch, ledger = publisher.ch, publisher.ledger
    ch.tmp("partial_input", "SELECT toUInt32(3) depth,'bucket/new/file' path UNION ALL SELECT toUInt32(2),'bucket/other'")
    ch.tmp("partial_known", f"SELECT id,depth,path FROM ({publisher.source(1)}) WHERE path IN ('bucket','bucket/new')")
    fingerprint = checkpoint_fingerprint(ch, "partial_known", "partial_input")
    with ledger.writer():
        ledger.reserve("partial", fingerprint, 1, 2)
        ch.exec(f"INSERT INTO {publisher.bindings} VALUES ({lit(uuid4().hex)},3,2,'bucket/other',1)")
    assert ch.json(f"SELECT * FROM ({publisher.source()}) ORDER BY id") == OLD
    with ledger.writer():
        assert publisher.publish("partial_input", "partial", 1)["count"] == 2
    assert ch.json(f"SELECT * FROM ({publisher.source()}) ORDER BY id") == NEW
    assert ch.scalar(f"SELECT count() FROM {publisher.bindings}") == "6"


def test_bounded_publication_benchmark_checks_complete_bindings(ch_url: str, tmp_path: Path) -> None:
    from dt_cloud.chstore.identity_publish import bench

    body = bench(ch_url, tmp_path / "benchmark", 2, 3)
    assert (body["bootstrap_paths"], body["appended_paths"], body["old_bindings_unchanged"], body["retry_wrote_no_rows"], body["fsync_after_insert"]) == (10, 2, True, True, True)
    assert sorted((name, stats["rows"]) for name, stats in body["physical_table_stats"].items()) == [
        ("identity_bindings", 12), ("identity_commits", 2),
    ]


def test_immutable_base_is_reused_without_copy_or_reallocation(ch_db: str, ch_url: str, tmp_path: Path) -> None:
    ch = Ch(ch_url, db=ch_db)
    prefix = "overlay_" + uuid4().hex
    baseline = prefix + "_base"
    ledger = ReservationLedger(tmp_path / "overlay.sqlite", "overlay-fixture", 8)
    service = IdentityPublisher(ch, prefix, ledger, baseline=(ch_db, baseline), baseline_id="pre")
    ch.exec(f"CREATE TABLE {baseline} (pre UInt64,depth UInt32,path String) ENGINE=Memory")
    ch.exec(f"INSERT INTO {baseline} VALUES (0,0,''),(7,1,'bucket')")
    try:
        ch.tmp("overlay_input", "SELECT toUInt32(1) depth,'bucket' path UNION ALL SELECT toUInt32(2),'bucket/new' UNION ALL SELECT toUInt32(3),'bucket/new/file'")
        with ledger.writer():
            service.initialize()
            result = service.publish("overlay_input", "overlay", 0)
        assert (result["first_id"], result["count"]) == (8, 2)
        assert ch.json(f"SELECT * FROM ({service.source()}) ORDER BY id") == [[8, 2, "bucket/new", 7], [9, 3, "bucket/new/file", 8]]
        assert ch.json(f"SELECT * FROM ({service.known_source()}) ORDER BY id") == [
            [0, 0, ""], [7, 1, "bucket"], [8, 2, "bucket/new"], [9, 3, "bucket/new/file"],
        ]
        assert ch.json(f"SELECT * FROM {baseline} ORDER BY pre") == [[0, 0, ""], [7, 1, "bucket"]]
        assert ch.scalar(f"SELECT count() FROM {service.bindings}") == "2"
        with pytest.raises(ValueError) as caught:
            IdentityPublisher(ch, prefix, ledger).source()
        assert str(caught.value) == "identity commit changed its immutable baseline"
    finally:
        ch.close()
        ch.exec(f"DROP TABLE IF EXISTS {service.commits}")
        ch.exec(f"DROP TABLE IF EXISTS {service.bindings}")
        ch.exec(f"DROP TABLE {baseline}")


def test_baseline_overlap_refuses_before_creating_publication_tables(ch_db: str, ch_url: str, tmp_path: Path) -> None:
    ch = Ch(ch_url, db=ch_db)
    prefix = "overlap_" + uuid4().hex
    baseline = prefix + "_base"
    ledger = ReservationLedger(tmp_path / "overlap.sqlite", "overlap-fixture", 7)
    service = IdentityPublisher(ch, prefix, ledger, baseline=(ch_db, baseline), baseline_id="pre")
    ch.exec(f"CREATE TABLE {baseline} (pre UInt64,depth UInt32,path String) ENGINE=Memory")
    ch.exec(f"INSERT INTO {baseline} VALUES (7,0,'')")
    try:
        with ledger.writer():
            with pytest.raises(ValueError) as caught:
                service.initialize()
            assert str(caught.value) == "immutable baseline IDs overlap the journal bootstrap"
            assert ledger.preview_first_id("unchanged") == 7
        assert ch.json(f"SELECT name FROM system.tables WHERE database={lit(ch_db)} AND name IN ({lit(service.bindings)},{lit(service.commits)})") == []
    finally:
        ch.close()
        ch.exec(f"DROP TABLE {baseline}")
