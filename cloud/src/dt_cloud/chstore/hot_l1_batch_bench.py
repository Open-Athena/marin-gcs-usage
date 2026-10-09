"""Offline native batch construction with completed-catalog/reference checks.

Reference files are explicitly supplied, operator-trusted artifacts. Their
oracle declarations are checked, not independently re-executed for the fleet.
"""

from contextlib import nullcontext
from json import dumps, loads
from os import X_OK, access
from pathlib import Path
from sys import stderr
from uuid import uuid4

from .client import Ch, lit
from .hot_frequency_registry import UNION_SCHEMA, load_queries
from .hot_l1_batch_sql import build
from .hot_l1_batch_stream import build as build_stream
from .hot_l1_catalog import SCOPE, VALIDATION, _Entry, _entry, _unique_object
from .resources import RssMonitor

LEAF_VALIDATION = "complete independent full-path leaf scan"


def _references(
    paths: tuple[Path, ...],
    target: str,
    date: str,
    patterns: tuple[str, ...],
) -> list[tuple[Path, dict, _Entry]]:
    result, queries = [], set(patterns)
    for path in paths:
        data = loads(path.read_text(), object_pairs_hook=_unique_object)
        if not isinstance(data, dict):
            raise ValueError("hot L1 reference must be an artifact object")
        validation = data.get("validation")
        if validation == LEAF_VALIDATION:
            if type(data.get("matching_nonleaf_rows")) is not int or data["matching_nonleaf_rows"] != 0 or data.get("all_buckets_covered") is not False:
                raise ValueError("leaf reference requires zero matching nonleaf rows and no bucket shortcut")
        elif validation != VALIDATION:
            raise ValueError("hot L1 reference requires completed independent frontier or eligible leaf validation")
        entry = _entry({**data, "validation": VALIDATION})
        if entry.target != target or entry.date != date or entry.pattern != data["pattern"] or entry.pattern not in queries:
            raise ValueError("hot L1 reference target/date/query does not match the complete catalog")
        result.append((path, data, entry))
    return result


def bench(
    url: str,
    target: str,
    date: str,
    queries_path: Path,
    out: Path,
    *,
    memory_gib: int = 8,
    seconds: int = 600,
    spill_gib: int = 8,
    pids: tuple[int, ...] = (),
    references: tuple[Path, ...] = (),
    registry_date: str | None = None,
    engine: str = "sql",
    binary: Path | None = None,
) -> dict:
    """Build every registered predicate at date, preserving its registry date.

    An explicit different registry date selects that date's complete hot-query
    catalog; it does not claim those predicates are all hot at the build date.
    References must still describe the requested build date.
    """
    if any(type(value) is not int for value in (memory_gib, seconds, spill_gib)) or not 1 <= memory_gib <= 8 or not 1 <= seconds <= 3600 or not 1 <= spill_gib <= 16:
        raise ValueError("hot L1 batch requires 1..8 GiB memory, 1..3600 seconds and 1..16 GiB spill")
    if engine not in ("sql", "stream"):
        raise ValueError("hot L1 batch engine must be sql or stream")
    if engine == "stream" and (binary is None or not binary.is_file() or not access(binary, X_OK)):
        raise ValueError("stream engine requires an explicit existing executable")
    if engine == "sql" and binary is not None:
        raise ValueError("a native binary requires the stream engine")
    if out.exists() or (pids and out.with_suffix(".rss.jsonl").exists()):
        raise ValueError("hot L1 batch output artifacts must be new")
    header, patterns = load_queries(queries_path, target, date, registry_date=registry_date)
    if not patterns:
        raise ValueError("completed hot query catalog contains no queries")
    refs = _references(references, target, date, patterns)
    monitor = RssMonitor(pids, out.with_suffix(".rss.jsonl")) if pids else None
    tag = "hot_l1_batch_" + uuid4().hex
    limits = {"memory_gib": memory_gib, "seconds": seconds, "spill_gib": spill_gib, "threads": 4}
    print(dumps({"log_comment": tag, "limits": limits}), file=stderr)
    ch = Ch(
        url, db=target, timeout=seconds + 60, max_threads=4,
        max_memory_usage=memory_gib << 30, max_execution_time=seconds,
        timeout_before_checking_execution_speed=0, timeout_overflow_mode="throw",
        max_bytes_before_external_sort=256 << 20, max_bytes_ratio_before_external_sort=0,
        max_bytes_before_external_group_by=256 << 20, max_bytes_ratio_before_external_group_by=0,
        max_temporary_data_on_disk_size_for_query=spill_gib << 30, log_comment=tag,
    )
    try:
        with monitor if monitor is not None else nullcontext():
            body = build_stream(ch, target, date, patterns, binary=binary) if engine == "stream" else build(ch, target, date, patterns)
            if body.get("target") != target or body.get("date") != date or body.get("exact") is not True or body.get("incremental") is not False or body.get("scope") != SCOPE:
                raise RuntimeError("native batch result contract disagrees with the requested catalog")
            rows = body.get("results")
            if (not isinstance(rows, list) or tuple(row.get("pattern") for row in rows) != patterns or
                    [row.get("predicate_id") for row in rows] != list(range(1, len(patterns) + 1))):
                raise RuntimeError("native batch did not return the complete ordered query catalog")
            entries = {}
            for row in rows:
                entries[row["pattern"]] = _entry({**row, "schema": "hot-l1-v1", "target": target, "date": date,
                                                  "exact": True, "incremental": False, "scope": SCOPE, "validation": VALIDATION})
            checked = []
            for path, data, expected in refs:
                actual = entries[expected.pattern]
                if actual.root != expected.root or actual.buckets != expected.buckets:
                    raise AssertionError(f"native batch disagrees with complete reference: {path}")
                checked.append({"path": str(path), "pattern": expected.pattern, "validation": data["validation"]})
        ch.exec("SYSTEM FLUSH LOGS")
        statements, memory, read_rows, read_bytes, duration = ch.json(f"""SELECT count(),max(memory_usage),sum(read_rows),sum(read_bytes),sum(query_duration_ms)
            FROM system.query_log WHERE log_comment={lit(tag)} AND type='QueryFinish'""")[0]
        provenance = {"registry_dates": header["dates"]} if header["schema"] == UNION_SCHEMA else {"registry_date": header["date"]}
        result = {**body, "engine": engine, "queries": {"path": str(queries_path), "patterns": len(patterns), "header": header, **provenance},
                  "validation": {"description": "complete catalog structure; supplied trusted references checked, not an independent full-catalog scan",
                                 "references": checked, "independently_scanned_entire_catalog": False},
                  "limits": limits, "profile": {"statements": statements, "tracked_peak_memory_bytes": memory,
                                                "read_rows": read_rows, "read_bytes": read_bytes, "summed_query_ms": duration},
                  "cache_state": "uncontrolled; offline construction, not serving latency"}
        if engine == "stream":
            result["native_binary"] = str(binary.resolve())
        if monitor is not None:
            result["rss"] = monitor.summary()
        output = out.open("x")
        try:
            with output:
                output.write(dumps(result) + "\n")
        except BaseException:
            # Only a file exclusively created by this call can reach cleanup.
            out.unlink()
            raise
        return result
    finally:
        ch.close()
