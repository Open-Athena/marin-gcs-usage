"""Offline hot-query L1 construction and independent-oracle measurements."""

from contextlib import nullcontext
from hashlib import sha256
from json import dumps
from pathlib import Path
from sys import stderr
from time import monotonic
from uuid import uuid4

from .client import Ch, lit
from .hot_l1 import build, oracle, validate_caps
from .hot_l1_batch_catalog import HotL1BatchCatalog
from .resources import RssMonitor


def _phase_profiles(ch: Ch, tags: dict[str, str]) -> tuple[dict, dict]:
    """Observed QueryFinish counters, not bytes transferred or disk I/O.

    A missing log group has zero observed statements; it is not proof of
    zero source work. Reference-only validation actually issues no SQL.
    """
    fields = ('statements', 'tracked_peak_memory_bytes', 'read_rows', 'read_bytes', 'summed_query_ms')
    profiles = {phase: dict.fromkeys(fields, 0) for phase in tags}
    by_tag = {tag: phase for phase, tag in tags.items()}
    rows = ch.json(f"""SELECT log_comment,count(),max(memory_usage),sum(read_rows),sum(read_bytes),sum(query_duration_ms)
        FROM system.query_log WHERE log_comment IN ({','.join(lit(tag) for tag in tags.values())})
        AND type='QueryFinish' GROUP BY log_comment""")
    seen = set()
    for row in rows:
        if (not isinstance(row, list) or len(row) != 6 or row[0] not in by_tag or row[0] in seen or
                any(type(value) is not int or value < 0 for value in row[1:])):
            raise RuntimeError('hot L1 phase profile returned duplicate, unknown or invalid counters')
        seen.add(row[0])
        profiles[by_tag[row[0]]] = dict(zip(fields, row[1:], strict=True))
    total = {field: (max if field == 'tracked_peak_memory_bytes' else sum)(profile[field] for profile in profiles.values()) for field in fields}
    return total, profiles


def bench(
    url: str,
    target: str,
    date: str,
    pattern: str,
    out: Path,
    *,
    memory_gib: int = 8,
    seconds: int = 300,
    spill_gib: int = 8,
    pids: tuple[int, ...] = (),
    reference_batch: Path | None = None,
    max_names: int | None = None,
    max_postings: int | None = None,
    max_roots: int | None = None,
) -> dict:
    if not 1 <= memory_gib <= 8 or not 1 <= seconds <= 600 or not 1 <= spill_gib <= 16:
        raise ValueError("hot L1 census requires 1..8 GiB memory, 1..600 seconds and 1..16 GiB spill")
    validate_caps(max_names, max_postings, max_roots)
    caps = {key: value for key, value in (('max_names', max_names), ('max_postings', max_postings), ('max_roots', max_roots)) if value is not None}
    if out.exists():
        raise ValueError("hot L1 artifact already exists")
    reference = None
    reference_metadata = None
    reference_load_s = None
    if reference_batch is not None:
        start = monotonic()
        raw = reference_batch.read_bytes()
        catalog = HotL1BatchCatalog.from_bytes((raw,))
        if catalog.target != target:
            raise ValueError("hot L1 batch reference target differs from requested target")
        reference = catalog.view(date, pattern)
        if reference["artifact_schema"] != "hot-l1-batch-stream-v1":
            raise ValueError("hot L1 batch reference requires a completed native stream artifact")
        snapshot_db = catalog.metadata()["dates"][0]["snapshot_db"]
        reference_metadata = {"path": str(reference_batch), "sha256": sha256(raw).hexdigest(), "bytes": len(raw),
                              "artifact_schema": reference["artifact_schema"], "snapshot_db": snapshot_db,
                              "validation": reference["validation"]}
        reference_load_s = monotonic() - start
    monitor = RssMonitor(pids, out.with_suffix(".rss.jsonl")) if pids else None
    tag = "hot_l1_bench_" + uuid4().hex
    phase_tags = {'build': tag, 'validation': tag + '_validation', 'control': tag + '_control'}
    limits = {"memory_gib": memory_gib, "seconds": seconds, "spill_gib": spill_gib, "threads": 4}
    print(dumps({"log_comment": tag, "limits": limits}), file=stderr)
    ch = Ch(url, db=target, timeout=seconds + 60, max_threads=4, max_memory_usage=memory_gib << 30,
            max_execution_time=seconds, timeout_before_checking_execution_speed=0, timeout_overflow_mode="throw",
            max_bytes_before_external_sort=256 << 20, max_bytes_ratio_before_external_sort=0,
            max_bytes_before_external_group_by=256 << 20, max_bytes_ratio_before_external_group_by=0,
            max_temporary_data_on_disk_size_for_query=spill_gib << 30, log_comment=tag)
    try:
        with monitor if monitor is not None else nullcontext():
            body = build(ch, target, date, pattern, **caps)
            start = monotonic()
            if reference is None:
                if caps:
                    ch.settings['log_comment'] = phase_tags['validation']
                try:
                    oracle(ch, body)
                finally:
                    if caps:
                        ch.settings['log_comment'] = tag
                measured_validation = {"oracle_s": monotonic() - start}
                validation = "complete independent full-path frontier scan"
            else:
                if ({key: body[key] for key in ("target", "date", "pattern", "root", "buckets")} !=
                        {key: reference[key] for key in ("target", "date", "pattern", "root", "buckets")} or
                        body["snapshot_db"] != reference_metadata["snapshot_db"]):
                    raise AssertionError("hot L1 aggregate disagrees with the complete accepted native batch reference")
                measured_validation = {"validation_s": monotonic() - start, "reference_load_s": reference_load_s,
                                       "reference_batch": reference_metadata}
                validation = "exact agreement with accepted native batch reference; not independent full-source oracle"
        if caps:
            ch.settings['log_comment'] = phase_tags['control']
        ch.exec("SYSTEM FLUSH LOGS")
        if caps:
            profile, by_phase = _phase_profiles(ch, phase_tags)
        else:
            rows = ch.json(f"""SELECT count(),max(memory_usage),sum(read_rows),sum(read_bytes),sum(query_duration_ms)
                FROM system.query_log WHERE log_comment={lit(tag)} AND type='QueryFinish'""")
            statements, memory, read_rows, read_bytes, duration = rows[0]
            profile = {"statements": statements, "tracked_peak_memory_bytes": memory,
                       "read_rows": read_rows, "read_bytes": read_bytes, "summed_query_ms": duration}
        result = {**body, "validation": validation, **measured_validation, "limits": limits,
                  "profile": profile,
                  "cache_state": "uncontrolled; offline construction, not serving latency"}
        if caps:
            result['profile_by_phase'] = by_phase
        if monitor is not None:
            result["rss"] = monitor.summary()
        with out.open("x") as output:
            output.write(dumps(result) + "\n")
        return result
    finally:
        if caps:
            ch.settings['log_comment'] = tag
        ch.close()
