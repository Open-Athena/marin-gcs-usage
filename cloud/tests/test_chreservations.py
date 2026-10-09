from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from os import _exit
from pathlib import Path

import pytest

from dt_cloud.chstore.reservations import Reservation, ReservationLedger


def ledger(tmp_path: Path) -> ReservationLedger:
    return ReservationLedger(tmp_path / "reservations.sqlite", "synthetic-fixture", 1 << 40)


def crash_writer(path: Path, phase: str) -> None:
    with ledger(path).writer() as writer:
        if phase == "reserved":
            writer.reserve("scan-a", "a" * 64, 0, 3)
            _exit(23)
        with writer._transaction() as db:
            db.execute("UPDATE metadata SET next_id='123' WHERE singleton=1")
            _exit(24)


@pytest.mark.parametrize("phase,exitcode", [("reserved", 23), ("uncommitted", 24)])
def test_process_death_recovers_committed_state_and_releases_writer_lock(
    tmp_path: Path,
    phase: str,
    exitcode: int,
) -> None:
    worker = get_context("spawn").Process(target=crash_writer, args=(tmp_path, phase))
    worker.start()
    worker.join(10)
    if worker.is_alive():
        worker.terminate()
        worker.join(5)
        pytest.fail("reservation crash fixture did not exit within its budget")
    assert worker.exitcode == exitcode
    with ledger(tmp_path).writer() as writer:
        assert writer.reserve("scan-a", "a" * 64, 0, 3) == Reservation("scan-a", "a" * 64, 0, 1 << 40, 3, "pending")


def test_reopen_recovers_the_exact_pending_reservation(tmp_path: Path) -> None:
    with ledger(tmp_path).writer() as writer:
        saved = writer.reserve("scan-a", "a" * 64, 0, 3)
    with ledger(tmp_path).writer() as writer:
        assert writer.reserve("scan-a", "a" * 64, 0, 3) == saved
        assert saved == Reservation("scan-a", "a" * 64, 0, 1 << 40, 3, "pending")
        with pytest.raises(ValueError) as caught:
            writer.reserve("scan-b", "b" * 64, 0, 2)
        assert str(caught.value) == "an interrupted reservation must finish before the next batch"


def test_observed_external_commit_advances_once_and_reuses_no_ids(tmp_path: Path) -> None:
    with ledger(tmp_path).writer() as writer:
        first = writer.reserve("scan-a", "a" * 64, 0, 3)
        writer.observe_commit(first, "b" * 64, "c" * 64)
    with ledger(tmp_path).writer() as writer:
        writer.observe_commit(first, "b" * 64, "c" * 64)
        assert writer.reserve("scan-a", "a" * 64, 0, 3) == replace(first, state="observed")
        assert writer.reserve("scan-b", "d" * 64, 1, 2) == Reservation("scan-b", "d" * 64, 1, first.next_id, 2, "pending")


@pytest.mark.parametrize("changed", [
    {"input_digest": "b" * 64}, {"parent_generation": 1}, {"count": 4},
])
def test_changed_retry_inputs_are_refused(tmp_path: Path, changed: dict) -> None:
    with ledger(tmp_path).writer() as writer:
        writer.reserve("scan-a", "a" * 64, 0, 3)
        inputs = {"token": "scan-a", "input_digest": "a" * 64, "parent_generation": 0, "count": 3, **changed}
        with pytest.raises(ValueError) as caught:
            writer.reserve(**inputs)
        assert str(caught.value) == "reservation retry changed immutable checkpoint inputs"


def test_competing_writer_is_refused_and_lock_releases_on_error(tmp_path: Path) -> None:
    first, second = ledger(tmp_path), ledger(tmp_path)
    with pytest.raises(RuntimeError) as raised:
        with first.writer():
            with pytest.raises(ValueError) as caught:
                with second.writer():
                    pytest.fail("competing writer entered")
            assert str(caught.value) == "another reservation writer holds the local journal"
            raise RuntimeError("interrupted")
    assert str(raised.value) == "interrupted"
    with second.writer() as writer:
        assert writer.reserve("scan-a", "a" * 64, 0, 0).count == 0


def test_bootstrap_mismatch_cannot_reinitialize_the_counter(tmp_path: Path) -> None:
    with ledger(tmp_path).writer() as writer:
        saved = writer.reserve("scan-a", "a" * 64, 0, 3)
    for namespace, first_id in [("other-dataset", 1 << 40), ("synthetic-fixture", 0)]:
        with pytest.raises(ValueError) as caught:
            with ReservationLedger(tmp_path / "reservations.sqlite", namespace, first_id).writer():
                pytest.fail("mismatched bootstrap entered")
        assert str(caught.value) == "reservation journal bootstrap does not match this dataset"
    with ledger(tmp_path).writer() as writer:
        assert writer.reserve("scan-a", "a" * 64, 0, 3) == saved


def test_failed_transaction_rolls_back_counter_and_batch_together(tmp_path: Path) -> None:
    with ledger(tmp_path).writer() as writer:
        with writer._transaction() as db:
            assert (db.execute("PRAGMA synchronous").fetchone(), db.execute("PRAGMA fullfsync").fetchone()) == ((3,), (1,))
        with pytest.raises(RuntimeError):
            with writer._transaction() as db:
                db.execute("UPDATE metadata SET next_id='123' WHERE singleton=1")
                raise RuntimeError("before commit")
        assert writer.reserve("scan-a", "a" * 64, 0, 3).first_id == 1 << 40


def test_mutation_without_writer_lock_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as caught:
        ledger(tmp_path).reserve("scan-a", "a" * 64, 0, 1)
    assert str(caught.value) == "reservation mutation requires the writer lock"


def test_writer_lock_cannot_be_borrowed_from_another_thread(tmp_path: Path) -> None:
    with ledger(tmp_path).writer() as writer, ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(writer.reserve, "scan-a", "a" * 64, 0, 1)
        with pytest.raises(ValueError) as caught:
            task.result()
        assert str(caught.value) == "reservation mutation requires the writer lock"
        assert writer.reserve("scan-a", "a" * 64, 0, 1).first_id == 1 << 40


def test_forged_observation_or_changed_fingerprints_are_refused(tmp_path: Path) -> None:
    with ledger(tmp_path).writer() as writer:
        saved = writer.reserve("scan-a", "a" * 64, 0, 3)
        with pytest.raises(ValueError) as caught:
            writer.observe_commit(replace(saved, first_id=5), "b" * 64, "c" * 64)
        assert str(caught.value) == "commit observation does not match the saved reservation"
        writer.observe_commit(saved, "b" * 64, "c" * 64)
        with pytest.raises(ValueError) as caught:
            writer.observe_commit(saved, "d" * 64, "c" * 64)
        assert str(caught.value) == "commit observation changed immutable fingerprints"


def test_domain_edge_can_be_reserved_once_without_counter_overflow(tmp_path: Path) -> None:
    journal = ReservationLedger(tmp_path / "edge.sqlite", "edge", (1 << 63) - 1)
    with journal.writer() as writer:
        last = writer.reserve("last", "a" * 64, 0, 1)
        assert last.next_id == 1 << 63
        writer.observe_commit(last, "b" * 64, "c" * 64)
        with pytest.raises(ValueError) as caught:
            writer.reserve("overflow", "d" * 64, 1, 1)
        assert str(caught.value) == "reservation exhausts the signed parent-ID domain"
