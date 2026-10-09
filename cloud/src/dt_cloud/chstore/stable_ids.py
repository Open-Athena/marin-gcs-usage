"""Deterministic staging of new path identities, not a published allocator.

The caller must hold a single-writer reservation for `next_id` and bind it
to immutable input/checkpoint metadata. This helper writes session tables
only. It does not reserve IDs durably, publish generations, write scalar
versions, or maintain the frozen range reader's DFS geometry.
"""

from dataclasses import dataclass
from contextlib import closing
from hashlib import sha256
from struct import pack
from time import monotonic

from .client import Ch, lit
from .narrow import identifier
from .serve import depth_of
from .reservations import Reservation


@dataclass(frozen=True)
class StagedIds:
    table: str
    new_paths: int
    next_id: int


def checkpoint_fingerprint(
    ch: Ch,
    existing: str,
    incoming: str,
    *,
    root: str = "",
    max_bytes: int = 512 << 20,
) -> str:
    """Complete canonical rows plus scope, not a sampled or file-only hash.

    Known bindings must come from the immutable predecessor checkpoint,
    including every relevant path/parent binding. Validation and publication
    remain separate from this bounded input fingerprint.
    """
    for name in (existing, incoming):
        identifier(name)
    if not 1 <= max_bytes <= 512 << 20:
        raise ValueError("checkpoint fingerprint byte budget must be from 1 to 512MiB")
    counts = [int(ch.scalar(f"SELECT count() FROM {table}")) for table in (existing, incoming)]
    if any(count > 1_000_000 for count in counts):
        raise ValueError("checkpoint fingerprint inputs exceed the 1M-row budget")
    digest = sha256(b"disk-tree/stable-id-checkpoint/v1\0")
    scope = root.encode()
    digest.update(pack("<Q", len(scope)))
    digest.update(scope)
    consumed = 0
    for table, count, tag, columns, order in (
        (existing, counts[0], b"known\0", "CAST(id AS UInt64),CAST(depth AS UInt32),CAST(path AS String)", "depth,path,id"),
        (incoming, counts[1], b"incoming\0", "CAST(depth AS UInt32),CAST(path AS String)", "depth,path"),
    ):
        digest.update(tag)
        digest.update(pack("<Q", count))
        with closing(ch.stream(f"SELECT {columns} FROM {table} ORDER BY {order}", "RowBinary")) as chunks:
            for chunk in chunks:
                consumed += len(chunk)
                if consumed > max_bytes:
                    raise ValueError(f"checkpoint fingerprint exceeds its {max_bytes:,}-byte streaming budget")
                digest.update(chunk)
    return digest.hexdigest()


def stage_reserved(
    ch: Ch,
    existing: str,
    incoming: str,
    reservation: Reservation,
    *,
    root: str = "",
    prefix: str = "stable_ids",
) -> StagedIds:
    """Verify the complete reserved input before session-only ID staging.

    Caller must hold the ledger writer lock through eventual external writes;
    this does not publish, validate a global bootstrap or mark a commit.
    """
    if reservation.state != "pending":
        raise ValueError("cannot stage an already observed identity reservation")
    if checkpoint_fingerprint(ch, existing, incoming, root=root) != reservation.input_digest:
        raise ValueError("staging inputs differ from the reserved checkpoint fingerprint")
    result = stage(ch, existing, incoming, reservation.first_id, root=root, prefix=prefix, reserved_count=reservation.count)
    if result.next_id != reservation.next_id:
        raise ValueError("staged IDs exceed the durable reserved range")
    return result


def stage(
    ch: Ch,
    existing: str,
    incoming: str,
    next_id: int,
    *,
    root: str = "",
    prefix: str = "stable_ids",
    max_new: int = 1_000_000,
    max_known: int = 1_000_000,
    reserved_count: int | None = None,
) -> StagedIds:
    """Inputs: existing `(id, depth, path)`, incoming `(depth, path)`.

    Original path spelling is identity; lowercase names alone cannot identify
    distinct paths. Every new path's parent must be known or in this batch.
    Assign by `(depth, path)` so recovery under the same reservation/input is
    deterministic, including new parents and children in the same batch.
    """
    for name in (existing, incoming, prefix):
        identifier(name)
    new, assigned, result = (f"{prefix}_{part}" for part in ("new", "assigned", "result"))
    if {existing, incoming} & {new, assigned, result}:
        raise ValueError("staging table prefix overlaps an input table")
    if not 0 <= next_id < 1 << 63 or not 1 <= max_new <= 1_000_000 or not 1 <= max_known <= 1_000_000:
        raise ValueError("ID reservation and new-path work budget are out of bounds")
    if reserved_count is not None and not 0 <= reserved_count <= max_new:
        raise ValueError("reserved path count is outside the staging budget")
    if int(ch.scalar(f"SELECT count() FROM {existing}")) > max_known:
        raise ValueError("known bindings exceed the bounded staging budget")
    if int(ch.scalar(f"SELECT count() FROM {incoming}")) > 1_000_000:
        raise ValueError("incoming rows exceed the bounded staging budget")
    count, ids, keys, maximum = ch.json(f"SELECT count(), uniqExact(id), uniqExact(tuple(depth,path)), maxOrNull(id) FROM {existing}")[0]
    if count != ids or count != keys:
        raise ValueError("existing path identities are not unique")
    if int(ch.scalar(f"SELECT count() FROM {existing} WHERE isNull(id) OR isNull(depth) OR isNull(path) OR id < 0 OR id >= {1 << 63} OR depth != if(path = '', 0, length(splitByChar('/', path)))")):
        raise ValueError("existing identities have an invalid parent-ID domain or path depth")
    if maximum is not None and next_id <= maximum:
        raise ValueError("ID reservation overlaps an existing identity")
    root_depth = depth_of(root)
    where = f"path != {lit(root)} AND NOT startsWith(path, {lit(root + '/')})" if root else "startsWith(path, '/')"
    if int(ch.scalar(f"SELECT count() FROM {incoming} WHERE isNull(depth) OR isNull(path) OR {where} OR depth != if(path = '', 0, length(splitByChar('/', path)))")):
        raise ValueError("incoming path depth or root scope is inconsistent")
    ch.tmp(new, f"""SELECT DISTINCT depth, path FROM {incoming}
        WHERE (depth, path) NOT IN (SELECT depth, path FROM {existing})""")
    size = int(ch.scalar(f"SELECT count() FROM {new}"))
    if size > max_new:
        raise ValueError(f"new-path batch exceeds its {max_new:,}-path work budget")
    if reserved_count is not None and size != reserved_count:
        raise ValueError("new-path cardinality differs from the durable reservation")
    if next_id + size > 1 << 63:
        raise ValueError("ID reservation exceeds the signed parent-ID domain")
    ch.tmp(assigned, f"""SELECT toUInt64({next_id}) + row_number() OVER (ORDER BY depth,path) - 1 AS id,
        depth, path FROM {new}""")
    all_paths = f"SELECT id, depth, path FROM {existing} UNION ALL SELECT id, depth, path FROM {assigned}"
    ch.tmp(result, f"""SELECT a.id AS id, a.depth AS depth, a.path AS path,
        if(a.path = {lit(root)}, toInt64(-1), toInt64(p.id)) AS parent_id,
        a.path = {lit(root)} OR p.present = 1 AS resolved
        FROM {assigned} a LEFT JOIN (
            SELECT *, toUInt8(1) AS present FROM ({all_paths})
        ) p ON p.depth = a.depth - 1 AND p.path =
            if(position(a.path, '/') = 0, '', substring(a.path, 1, length(a.path) - position(reverse(a.path), '/')))
    """)
    if int(ch.scalar(f"SELECT count() FROM {result} WHERE NOT resolved")):
        raise ValueError("new-path batch is missing a structural parent")
    if int(ch.scalar(f"SELECT count() FROM {result} WHERE path = {lit(root)} AND depth != {root_depth}")):
        raise ValueError("staged root identity has an inconsistent depth")
    if int(ch.scalar(f"SELECT count() FROM {result}")) != size:
        raise ValueError("staged identity join changed the new-path cardinality")
    return StagedIds(result, size, next_id + size)


def bench(
    url: str,
    groups: int,
    leaves: int,
    *,
    trials: int = 3,
) -> dict:
    """Synthetic bounded staging only; no fleet copy, reservations or writes."""
    rows = groups * (leaves + 1)
    if not 1 <= groups <= 10_000 or not 1 <= leaves <= 10_000 or not 1 <= rows <= 1_000_000 or not 1 <= trials <= 5:
        raise ValueError("synthetic staging requires <=1M incoming rows and 1..5 trials")
    ch = Ch(url, max_threads=8, max_memory_usage=8 << 30, max_execution_time=120,
            max_bytes_before_external_sort=256 << 20, timeout_before_checking_execution_speed=0,
            timeout_overflow_mode="throw")
    try:
        ch.tmp("known", "SELECT toUInt64(0) id, toUInt8(0) depth, '' path UNION ALL SELECT toUInt64(7),toUInt8(1),'bucket'")
        ch.tmp("incoming", f"""SELECT toUInt8(2) depth, concat('bucket/new-',leftPad(toString(number),8,'0')) path
            FROM numbers({groups}) UNION ALL SELECT toUInt8(3),
            concat('bucket/new-',leftPad(toString(intDiv(number,{leaves})),8,'0'),'/file-',leftPad(toString(number % {leaves}),8,'0'))
            FROM numbers({groups * leaves})""")
        results, fingerprints = [], []
        for trial in range(trials):
            start = monotonic()
            result = stage(ch, "known", "incoming", 1 << 40)
            seconds = monotonic() - start
            digest = sha256()
            for chunk in ch.stream(f"SELECT id,depth,path,parent_id FROM {result.table} ORDER BY id", "RowBinary"):
                digest.update(chunk)
            fingerprints.append(digest.hexdigest())
            results.append({"trial": trial, "staging_s": round(seconds, 4), "new_paths": result.new_paths,
                            "next_id": result.next_id, "binding_fingerprint": fingerprints[-1]})
        if fingerprints != [fingerprints[0]] * trials:
            raise ValueError("deterministic staging changed complete bindings between retries")
        if ch.json("SELECT id,depth,path FROM known ORDER BY id") != [[0, 0, ""], [7, 1, "bucket"]]:
            raise ValueError("staging modified the known identity inputs")
        return {"scope": "synthetic session-only staging; no durable reservation, publication or fleet ingestion",
                "threads": 8, "incoming_rows": rows, "known_rows": 2, "trials": results, "reproducible_bindings": True}
    finally:
        ch.close()
