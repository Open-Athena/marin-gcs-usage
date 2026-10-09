"""Bounded, read-only A/B of ingestion's changed-row SELECT, not publication."""

from __future__ import annotations

import hashlib
import time
from dataclasses import replace
from typing import Callable

from .client import Ch
from .ingest import Ingest, IngestError, Prev, range_settings, sample_bounds
from .schema import scan_epochs


def plans(threads: int) -> list[tuple[str, bool, dict]]:
    if threads <= 0:
        raise ValueError("benchmark threads must be positive")
    baseline = range_settings(threads)
    headroom = {**baseline, "max_bytes_before_external_group_by": 1 << 30}
    return [
        ("legacy", False, baseline),
        ("epoch", True, baseline),
        ("epoch-1g", True, headroom),
        ("epoch-merge", True, {**baseline, "join_algorithm": "full_sorting_merge"}),
    ]


def benchmark(
    ch: Ch,
    scan_id: str,
    server_file: str,
    indices: tuple[int, ...],
    *,
    samples: int = 128,
    threads: int = 4,
    trials: int = 2,
    emit: Callable[[dict], None] = print,
) -> None:
    """Time identical reads, then compare sorted changed-row SHA256 fingerprints.

    No ingest.run(), staging upload, inserts, DDL or cache resets. Source count
    checks cannot prove that a supplied file belongs to the requested scan; use
    the retained, independently audited source. Digests compare complete row
    bytes, not just totals, but are fingerprint evidence rather than a literal
    row-by-row proof. Timed FORMAT Null excludes verification and publication.
    """
    variants = plans(threads)
    if samples <= 0 or trials <= 0 or not indices or len(set(indices)) != len(indices) or min(indices) < 0:
        raise ValueError("provide distinct nonnegative range indices and positive samples/trials")
    scans = scan_epochs(ch)
    positions = [i for i, row in enumerate(scans) if row[0] == scan_id]
    if len(positions) != 1 or positions[0] == 0:
        raise IngestError("benchmark requires a published scan with a published predecessor")
    position = positions[0]
    ident, at, version, rows, epoch = scans[position - 1]
    previous = Prev(at, ident, version, rows, epoch)
    current = scans[position]
    ing = Ingest(ch, scan_id, server_file, server_file=True, version=current[2])
    if ing._source() != current[3]:
        raise IngestError("source row count differs from the published scan; no benchmark run")
    bounds = sample_bounds(ch, samples)
    if max(indices) >= len(bounds):
        raise ValueError(f"range index exceeds the {len(bounds)} sampled ranges")
    fingerprints = {}
    for trial in range(trials):
        for index in indices:
            # Reverse order on alternating cases to expose warm-cache bias.
            order = variants if (trial + indices.index(index)) % 2 == 0 else list(reversed(variants))
            for name, pruned, settings in order:
                prev = previous if pruned else replace(previous, epoch=scans[0][1])
                query = ing.pair_query(prev, bounds[index])
                label = f"ingest-plan:{scan_id}:{index}:{trial}:{name}"
                start = time.monotonic()
                ch.exec(query, fmt="Null", settings={**settings, "log_comment": label})
                seconds = time.monotonic() - start
                digest, length = hashlib.sha256(), 0
                start = time.monotonic()
                for chunk in ch.stream(f"SELECT * FROM ({query}) ORDER BY depth, path, usr", "RowBinary",
                                       settings={**settings, "log_comment": f"{label}:verify"}):
                    digest.update(chunk)
                    length += len(chunk)
                fingerprint = (length, digest.hexdigest())
                expected = fingerprints.setdefault(index, fingerprint)
                result = {"scan": scan_id, "previous": previous.id, "range": index, "condition": bounds[index],
                          "ranges": len(bounds), "trial": trial, "plan": name, "threads": threads,
                          "epoch_pruning": pruned, "seconds": seconds, "verification_s": time.monotonic() - start,
                          "result_bytes": length, "sha256": fingerprint[1], "same_rows_fingerprint": fingerprint == expected,
                          "cache": "no-reset-alternating-order", "publication": False, "log_comment": label}
                emit(result)
                if fingerprint != expected:
                    raise IngestError(f"changed-row fingerprint mismatch at range {index}: {name}")
