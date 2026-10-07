"""Isolated single-node identity-batch publication prototype.

This is NOT scan/scalar/rich publication or fleet bootstrap. Each attempt
writes immutable rows under a fresh token. Only verified commit markers
make an attempt readable; retries never edit a published attempt. Readers
pin a generation and select one semantically identical attempt per batch.
"""

from collections.abc import Callable
from contextlib import closing
from hashlib import sha256
from json import dumps, loads
from uuid import uuid4
from pathlib import Path
from time import monotonic

from .client import Ch, lit
from .narrow import identifier
from .reservations import ReservationLedger
from .stable_ids import checkpoint_fingerprint, stage


class IdentityPublisher:
    def __init__(
        self,
        ch: Ch,
        prefix: str,
        ledger: ReservationLedger,
        *,
        baseline: tuple[str, str] | None = None,
        baseline_id: str = "id",
    ) -> None:
        identifier(prefix)
        self.ch, self.prefix, self.ledger = ch, prefix, ledger
        self.bindings, self.commits = f"{prefix}_bindings", f"{prefix}_commits"
        identifier(baseline_id)
        if baseline is not None:
            for name in baseline:
                identifier(name)
        self.baseline, self.baseline_id = baseline, baseline_id
        self.baseline_descriptor = None if baseline is None else {"database": baseline[0], "table": baseline[1], "id_column": baseline_id}

    def initialize(self) -> None:
        self.ledger.preview_first_id("initialize")  # Requires the writer lock.
        if self.baseline is not None:
            database, table = self.baseline
            maximum = self.ch.scalar(f"SELECT maxOrNull({self.baseline_id}) FROM {database}.{table}")
            if maximum is not None and self.ledger.initial_next_id <= int(maximum):
                raise ValueError("immutable baseline IDs overlap the journal bootstrap")
        self.ch.exec(f"""CREATE TABLE IF NOT EXISTS {self.bindings} (
            attempt String,id UInt64,depth UInt32,path String,parent_id Int64
        ) ENGINE=MergeTree ORDER BY (depth,path,attempt,id)
        SETTINGS fsync_after_insert=1,fsync_part_directory=1""")
        self.ch.exec(f"""CREATE TABLE IF NOT EXISTS {self.commits} (
            generation UInt64,attempt String,doc String
        ) ENGINE=MergeTree ORDER BY generation
        SETTINGS fsync_after_insert=1,fsync_part_directory=1""")

    def markers(self, head: int | None = None) -> list[tuple[int, str, dict]]:
        if head is not None and head < 0:
            raise ValueError("identity reader generation must be nonnegative")
        where = "1" if head is None else f"generation <= {head}"
        rows = self.ch.json(f"SELECT generation,uniqExact(doc),any(doc),max(attempt) FROM {self.commits} WHERE {where} GROUP BY generation ORDER BY generation")
        result = []
        for generation, distinct, text, attempt in rows:
            if distinct != 1:
                raise ValueError("conflicting identity commit manifests")
            doc = loads(text)
            if generation != len(result) + 1 or doc["generation"] != generation or doc["parent_generation"] != generation - 1:
                raise ValueError("identity commit generations are not a contiguous chain")
            if doc["schema"] != "identity-batch-v1" or doc["namespace"] != self.ledger.namespace:
                raise ValueError("identity commit namespace/schema does not match the journal")
            if doc["baseline"] != self.baseline_descriptor:
                raise ValueError("identity commit changed its immutable baseline")
            result.append((generation, attempt, doc))
        if head is not None and len(result) != head:
            raise ValueError("identity reader generation is not committed")
        return result

    def source(self, head: int | None = None) -> str:
        """Committed overlay only; the baseline is not copied into this table."""
        markers = self.markers(head)
        condition = "attempt IN (" + ",".join(lit(attempt) for _, attempt, _ in markers) + ")" if markers else "0"
        return f"SELECT id,depth,path,parent_id FROM {self.bindings} WHERE {condition}"

    def known_source(self, head: int | None = None) -> str:
        """Identity lookup against the immutable base plus committed overlay.

        A baseline supplies `(id,depth,path)` and must already have complete
        uniqueness/structural audits. Its old parent/lineage read model stays
        external; this method does not fabricate baseline parent bindings.
        """
        source = f"SELECT id,depth,path FROM ({self.source(head)})"
        if self.baseline is not None:
            database, table = self.baseline
            source += f" UNION ALL SELECT {self.baseline_id} id,depth,path FROM {database}.{table}"
        return source

    def _binding_fingerprint(self, source: str) -> str:
        digest = sha256(b"disk-tree/identity-bindings/v1\0")
        consumed = 0
        with closing(self.ch.stream(f"SELECT CAST(id AS UInt64),CAST(depth AS UInt32),CAST(path AS String),CAST(parent_id AS Int64) FROM ({source}) ORDER BY id", "RowBinary")) as chunks:
            for chunk in chunks:
                consumed += len(chunk)
                if consumed > 512 << 20:
                    raise ValueError("identity publication fingerprint exceeds its 512MiB budget")
                digest.update(chunk)
        return digest.hexdigest()

    def publish(
        self,
        incoming: str,
        token: str,
        parent_generation: int,
        *,
        root: str = "",
        after: Callable[[str], None] | None = None,
    ) -> dict:
        """Publish one bounded identity batch while the ledger lock is held.

        Fresh attempt IDs tolerate hidden partial writes and late completion
        of a prior attempt's commit INSERT: all committed copies must have
        identical semantic manifests/bindings, and only one copy is read.
        Failure injection is for native fixtures, not deployment settings.
        """
        identifier(incoming)
        first = self.ledger.preview_first_id(token)
        current = self.markers()
        previous = self.markers(parent_generation)
        if previous and previous[-1][2]["root"] != root:
            raise ValueError("identity publication changed the dataset root")
        matching = [marker for marker in current if marker[2]["token"] == token]
        if len(matching) > 1 or (not matching and len(current) != parent_generation):
            raise ValueError("identity publication predecessor/token conflicts with the committed chain")
        if int(self.ch.scalar(f"SELECT count() FROM {incoming}")) > 1_000_000:
            raise ValueError("identity publication input exceeds its 1M-row budget")
        tag = f"identity_stage_{uuid4().hex}"
        sealed, known = f"{tag}_input", f"{tag}_known"
        self.ch.tmp(sealed, f"SELECT depth,path FROM {incoming}")
        keys = f"""SELECT depth,path FROM {sealed} UNION ALL SELECT depth-1,
            if(position(path,'/')=0,'',substring(path,1,length(path)-position(reverse(path),'/')))
            FROM {sealed} WHERE path != {lit(root)}"""
        self.ch.tmp(known, f"SELECT id,depth,path FROM ({self.known_source(parent_generation)}) WHERE (depth,path) IN ({keys})")
        fingerprint = checkpoint_fingerprint(self.ch, known, sealed, root=root)
        # Preflight before committing a reservation: malformed inputs cannot
        # strand the journal with an immutable, impossible-to-finish batch.
        staged = stage(self.ch, known, sealed, first, root=root, prefix=tag)
        reservation = self.ledger.reserve(token, fingerprint, parent_generation, staged.new_paths)
        if (reservation.first_id, reservation.next_id) != (first, staged.next_id):
            raise ValueError("identity publication preflight differs from its reservation")
        staged_source = f"SELECT id,depth,path,parent_id FROM {staged.table}"
        binding = self._binding_fingerprint(staged_source)
        semantic = {"schema": "identity-batch-v1", "namespace": self.ledger.namespace, "baseline": self.baseline_descriptor,
                    "generation": parent_generation + 1, "parent_generation": parent_generation,
                    "token": token, "root": root, "input_digest": fingerprint,
                    "first_id": first, "count": staged.new_paths, "binding_digest": binding}
        text = dumps(semantic, sort_keys=True, separators=(",", ":"))
        if matching:
            if matching[0][2] != semantic:
                raise ValueError("identity publication retry changed its committed manifest")
            attempt = matching[0][1]
        else:
            attempt = uuid4().hex
            self.ch.exec(f"INSERT INTO {self.bindings} SELECT {lit(attempt)},id,depth,path,parent_id FROM ({staged_source})",
                         settings={"async_insert": 0})
            if after:
                after("data")
        stored = f"SELECT id,depth,path,parent_id FROM {self.bindings} WHERE attempt={lit(attempt)}"
        if int(self.ch.scalar(f"SELECT count() FROM ({stored})")) != reservation.count or self._binding_fingerprint(stored) != binding:
            raise ValueError("stored identity attempt does not match complete staged bindings")
        if not matching:
            self.ch.exec(f"INSERT INTO {self.commits} VALUES ({parent_generation + 1},{lit(attempt)},{lit(text)})",
                         settings={"async_insert": 0})
            if after:
                after("marker")
        # Re-read authority before journal acknowledgment, including retries
        # after a lost marker response or controller death.
        committed = self.markers(parent_generation + 1)[-1]
        if committed[2] != semantic:
            raise ValueError("identity commit authority differs from the verified attempt")
        self.ledger.observe_commit(reservation, binding, sha256(text.encode()).hexdigest())
        return semantic


def bench(
    url: str,
    out: Path,
    groups: int,
    leaves: int,
) -> dict:
    """Two bounded synthetic identity batches; never touches fleet tables."""
    count = groups * (leaves + 1) + 2
    if not 1 <= groups <= 10_000 or not 1 <= leaves or count > 1_000_000:
        raise ValueError("identity publication benchmark requires <=1M bootstrap paths")
    out.mkdir(parents=True, exist_ok=False)
    database = "identity_bench_" + uuid4().hex
    control = Ch(url)
    control.exec(f"CREATE DATABASE {database}")
    ch = Ch(url, db=database, max_threads=8, max_memory_usage=8 << 30,
            max_execution_time=120, max_bytes_before_external_sort=256 << 20,
            timeout_before_checking_execution_speed=0, timeout_overflow_mode="throw")
    ledger = ReservationLedger(out / "reservations.sqlite", database, 0)
    publisher = IdentityPublisher(ch, "identity", ledger)
    try:
        ch.tmp("bootstrap", f"""SELECT toUInt32(0) depth,'' path UNION ALL SELECT toUInt32(1),'bucket'
            UNION ALL SELECT toUInt32(2),concat('bucket/dir-',leftPad(toString(number),8,'0')) FROM numbers({groups})
            UNION ALL SELECT toUInt32(3),concat('bucket/dir-',leftPad(toString(intDiv(number,{leaves})),8,'0'),'/file-',leftPad(toString(number % {leaves}),8,'0')) FROM numbers({groups * leaves})""")
        ch.tmp("appends", f"SELECT toUInt32(3) depth,concat('bucket/dir-',leftPad(toString(number),8,'0'),'/appended') path FROM numbers({groups})")
        times = {}
        with ledger.writer():
            publisher.initialize()
            start = monotonic()
            first = publisher.publish("bootstrap", "bootstrap", 0)
            times["bootstrap_s"] = round(monotonic() - start, 4)
            old_fingerprint = publisher._binding_fingerprint(publisher.source(1))
            start = monotonic()
            second = publisher.publish("appends", "appends", 1)
            times["append_s"] = round(monotonic() - start, 4)
        complete = publisher._binding_fingerprint(publisher.source())
        before = ch.scalar(f"SELECT count() FROM {publisher.bindings}")
        reopened = ReservationLedger(ledger.path, database, 0)
        recovered = IdentityPublisher(ch, "identity", reopened)
        with reopened.writer():
            start = monotonic()
            retry = recovered.publish("appends", "appends", 1)
            times["observed_retry_s"] = round(monotonic() - start, 4)
        after = ch.scalar(f"SELECT count() FROM {publisher.bindings}")
        if first["count"] != count or second["count"] != groups or retry != second or before != after or int(after) != count + groups:
            raise ValueError("identity publication benchmark changed reservation/physical row counts")
        if complete != recovered._binding_fingerprint(recovered.source()) or old_fingerprint != recovered._binding_fingerprint(recovered.source(1)):
            raise ValueError("identity publication benchmark changed complete published bindings")
        stats = {row[0]: {"rows": row[1], "bytes": row[2]} for row in ch.json(
            f"SELECT name,total_rows,total_bytes FROM system.tables WHERE database={lit(database)} AND name IN ('identity_bindings','identity_commits')"
        )}
        return {"scope": "synthetic identity-only publication/recovery; no scalar/rich scans, fleet bootstrap or FTS",
                "threads": 8, "input_generation_excluded": True, "independent_final_fingerprints_excluded": True,
                "fsync_after_insert": True, "bootstrap_paths": count, "appended_paths": groups,
                "timings": times, "physical_table_stats": stats, "journal_bytes": ledger.path.stat().st_size,
                "published_binding_fingerprint": complete, "old_bindings_unchanged": True, "retry_wrote_no_rows": True}
    finally:
        ch.close()
        control.exec(f"DROP DATABASE {database}")
        control.close()
