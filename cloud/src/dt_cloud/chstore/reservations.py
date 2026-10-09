"""Single-node reservation journal, not a ClickHouse publication transaction.

Keep this journal on the dev node's persistent local disk. Hold `writer()`
through staging and external publication. A pending reservation blocks the
next batch; interrupted work must resume with the same immutable input.
Readers must use the external committed manifest, never this journal.
Disk loss/restore and multiple machines require a separate recovery design.
"""

from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from pathlib import Path
from re import fullmatch
from sqlite3 import Connection, connect
from threading import get_ident


@dataclass(frozen=True)
class Reservation:
    token: str
    input_digest: str
    parent_generation: int
    first_id: int
    count: int
    state: str

    @property
    def next_id(self) -> int:
        return self.first_id + self.count


def digest(value: str) -> None:
    if not fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("checkpoint digests must be lowercase SHA256")


class ReservationLedger:
    def __init__(
        self,
        path: Path,
        namespace: str,
        initial_next_id: int,
    ) -> None:
        if not namespace or len(namespace) > 256 or not 0 <= initial_next_id < 1 << 63:
            raise ValueError("reservation bootstrap namespace or ID is invalid")
        self.path = path.resolve()
        self.namespace = namespace
        self.initial_next_id = initial_next_id
        self._owner: int | None = None

    @contextmanager
    def writer(self) -> Iterator["ReservationLedger"]:
        """Nonblocking process lock spanning the caller's external writes."""
        if self._owner is not None:
            raise ValueError("reservation writer lock is already held")
        with self.path.with_suffix(self.path.suffix + ".lock").open("a+b") as lock:
            try:
                flock(lock, LOCK_EX | LOCK_NB)
            except BlockingIOError:
                raise ValueError("another reservation writer holds the local journal") from None
            self._owner = get_ident()
            try:
                self._initialize()
                yield self
            finally:
                self._owner = None
                flock(lock, LOCK_UN)

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        if self._owner != get_ident():
            raise ValueError("reservation mutation requires the writer lock")
        with closing(connect(self.path, isolation_level=None, timeout=0)) as db:
            db.execute("PRAGMA synchronous=EXTRA")
            db.execute("PRAGMA fullfsync=ON")
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    def _initialize(self) -> None:
        with self._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS metadata (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), namespace TEXT NOT NULL,
                initial_id TEXT NOT NULL, next_id TEXT NOT NULL, generation INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS batches (
                token TEXT PRIMARY KEY, input_digest TEXT NOT NULL, parent_generation INTEGER NOT NULL,
                first_id TEXT NOT NULL, count INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','observed')),
                binding_digest TEXT, manifest_digest TEXT)""")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_pending ON batches(state) WHERE state='pending'")
            db.execute("INSERT OR IGNORE INTO metadata VALUES (1,?,?,?,0)",
                       (self.namespace, str(self.initial_next_id), str(self.initial_next_id)))
            namespace, initial = db.execute("SELECT namespace,initial_id FROM metadata WHERE singleton=1").fetchone()
            if (namespace, int(initial)) != (self.namespace, self.initial_next_id):
                raise ValueError("reservation journal bootstrap does not match this dataset")

    def reserve(
        self,
        token: str,
        input_digest: str,
        parent_generation: int,
        count: int,
    ) -> Reservation:
        digest(input_digest)
        if not token or len(token) > 128 or not 0 <= count <= 1_000_000 or not 0 <= parent_generation < (1 << 63) - 1:
            raise ValueError("reservation token, generation or batch size is invalid")
        with self._transaction() as db:
            row = db.execute("SELECT token,input_digest,parent_generation,first_id,count,state FROM batches WHERE token=?", (token,)).fetchone()
            if row is not None:
                saved = Reservation(*row[:3], int(row[3]), *row[4:])
                if (saved.input_digest, saved.parent_generation, saved.count) != (input_digest, parent_generation, count):
                    raise ValueError("reservation retry changed immutable checkpoint inputs")
                return saved
            if db.execute("SELECT token FROM batches WHERE state='pending'").fetchone() is not None:
                raise ValueError("an interrupted reservation must finish before the next batch")
            next_id, generation = db.execute("SELECT next_id,generation FROM metadata WHERE singleton=1").fetchone()
            first = int(next_id)
            if generation != parent_generation:
                raise ValueError("reservation predecessor is not the observed committed generation")
            if first >= 1 << 63 or first + count > 1 << 63:
                raise ValueError("reservation exhausts the signed parent-ID domain")
            db.execute("INSERT INTO batches VALUES (?,?,?,?,?,'pending',NULL,NULL)",
                       (token, input_digest, parent_generation, str(first), count))
            db.execute("UPDATE metadata SET next_id=? WHERE singleton=1", (str(first + count),))
            return Reservation(token, input_digest, parent_generation, first, count, "pending")

    def preview_first_id(self, token: str) -> int:
        """For bounded preflight only; this does not reserve or consume IDs."""
        with self._transaction() as db:
            saved = db.execute("SELECT first_id FROM batches WHERE token=?", (token,)).fetchone()
            return int(saved[0] if saved else db.execute("SELECT next_id FROM metadata WHERE singleton=1").fetchone()[0])

    def observe_commit(
        self,
        reservation: Reservation,
        binding_digest: str,
        manifest_digest: str,
    ) -> None:
        """Record an independently verified external commit; does NOT publish.

        The caller must first read the authoritative committed manifest and
        verify its namespace, token, input, reserved range and complete binding
        fingerprint. Hash arguments alone are not that verification.
        """
        digest(binding_digest)
        digest(manifest_digest)
        with self._transaction() as db:
            row = db.execute("SELECT token,input_digest,parent_generation,first_id,count,state,binding_digest,manifest_digest FROM batches WHERE token=?",
                             (reservation.token,)).fetchone()
            if row is None or (row[:3], int(row[3]), row[4]) != (
                (reservation.token, reservation.input_digest, reservation.parent_generation), reservation.first_id, reservation.count,
            ):
                raise ValueError("commit observation does not match the saved reservation")
            if row[5] == "observed":
                if tuple(row[6:]) != (binding_digest, manifest_digest):
                    raise ValueError("commit observation changed immutable fingerprints")
                return
            generation = db.execute("SELECT generation FROM metadata WHERE singleton=1").fetchone()[0]
            if generation != reservation.parent_generation:
                raise ValueError("commit observation has an inconsistent predecessor")
            db.execute("UPDATE batches SET state='observed',binding_digest=?,manifest_digest=? WHERE token=?",
                       (binding_digest, manifest_digest, reservation.token))
            db.execute("UPDATE metadata SET generation=? WHERE singleton=1", (generation + 1,))
