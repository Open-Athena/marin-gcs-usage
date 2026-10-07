"""Offline full-fleet hot-substring enumeration with explicit resource budgets."""

from contextlib import ExitStack, nullcontext
from json import dumps
from os import fchmod
from pathlib import Path
from sys import exception, stderr
from time import monotonic
from uuid import uuid4

from .client import Ch, lit
from .hot_frequency import census, validate_limits
from .resources import RssMonitor


def bench(
    url: str,
    target: str,
    date: str,
    threshold: int,
    max_chars: int,
    out: Path,
    *,
    memory_gib: int = 8,
    seconds: int = 600,
    spill_gib: int = 8,
    pids: tuple[int, ...] = (),
    patterns: tuple[str, ...] = (),
    queries_out: Path | None = None,
    thresholds: tuple[int, ...] = (),
    max_patterns: int = 500_000,
    daily_source: Path | None = None,
    wall_seconds: int = 3600,
    staging_gib: int = 16,
) -> dict:
    if not 1 <= memory_gib <= 8 or not 1 <= seconds <= 600 or not 1 <= spill_gib <= 16:
        raise ValueError("hot frequency census requires 1..8 GiB memory, 1..600 seconds and 1..16 GiB spill")
    validate_limits(threshold, max_chars, thresholds, max_patterns)
    if out.exists():
        raise ValueError("hot frequency artifact already exists")
    if queries_out is not None and (queries_out == out or queries_out.exists()):
        raise ValueError("hot query export must be a distinct new artifact")
    raw = None
    if daily_source is not None:
        from .hot_frequency_daily import DailyCensusCh, census as daily_census, source_bytes
        if type(wall_seconds) is not int or not 1 <= wall_seconds <= 7200 or type(staging_gib) is not int or not 1 <= staging_gib <= 32:
            raise ValueError('daily hot-frequency requires 1..7200 wall seconds and 1..32 GiB temporary staging')
        raw = source_bytes(daily_source, target, date)
    monitor = RssMonitor(pids, out.with_suffix(".rss.jsonl")) if pids else None
    tag = "hot_frequency_" + uuid4().hex
    limits = {"memory_gib": memory_gib, "seconds": seconds, "spill_gib": spill_gib, "threads": 4}
    if raw is not None:
        limits.update(wall_seconds=wall_seconds, staging_gib=staging_gib, reserve_gib=20,
                      disk_checks='active session temporary table bytes and free space at stage boundaries; not an in-flight quota')
    print(dumps({"log_comment": tag, "limits": limits}), file=stderr)
    factory = Ch if raw is None else DailyCensusCh
    ch = factory(
        url, db=target, timeout=seconds + 60, max_threads=4,
        max_memory_usage=memory_gib << 30, max_execution_time=seconds,
        timeout_before_checking_execution_speed=0, timeout_overflow_mode="throw",
        max_bytes_before_external_sort=256 << 20, max_bytes_ratio_before_external_sort=0,
        max_bytes_before_external_group_by=256 << 20, max_bytes_ratio_before_external_group_by=0,
        max_temporary_data_on_disk_size_for_query=spill_gib << 30, log_comment=tag,
        **({} if raw is None else {'wall_seconds': wall_seconds, 'staging_bytes': staging_gib << 30}),
    )
    closed = False
    try:
        with monitor if monitor is not None else nullcontext(), ExitStack() as stack:
            written, export_s = 0, 0.
            output = None
            if queries_out is not None:
                output = stack.enter_context(queries_out.open("xb"))
                if raw is not None:
                    fchmod(output.fileno(), 0o600)
                output.write((dumps({"schema": "hot-frequency-queries-v1", "target": target, "date": date,
                                     "threshold_paths": threshold, "max_chars": max_chars}) + "\n").encode())

            def export_layer(chars: int, table: str) -> None:
                nonlocal written, export_s
                assert output is not None
                start = monotonic()
                for chunk in ch.stream(f"SELECT {chars} AS chars,gram AS pattern,direct_matching_paths FROM {table} ORDER BY gram",
                                       fmt="JSONEachRow", settings={"output_format_json_quote_64bit_integers": 0}):
                    output.write(chunk)
                    if raw is not None:
                        ch.check_wall()
                    written += chunk.count(b"\n")
                output.flush()
                export_s += monotonic() - start
                print(dumps({"stage": "export-hot-substrings", "chars": chars, "exported_patterns": written}), file=stderr)

            export = {"on_hot_table": export_layer} if output is not None else {}
            selected_census = census if raw is None else daily_census
            source_arg = () if raw is None else (raw,)
            body = selected_census(ch, target, date, *source_arg, threshold, max_chars, patterns=patterns,
                          progress=lambda stage: print(dumps(stage), file=stderr),
                          thresholds=thresholds, max_patterns=max_patterns, **export)
            if output is not None:
                if written != sum(layer["hot_patterns"] for layer in body["lengths"]):
                    raise RuntimeError("hot query export count disagrees with complete census")
                output.write((dumps({"complete": True, "patterns": written}) + "\n").encode())
                output.flush()
                body["queries"] = {"path": str(queries_out), "patterns": written,
                                   "bytes": queries_out.stat().st_size, "export_s": export_s}
        ch.exec("SYSTEM FLUSH LOGS")
        statements, memory, rows, size, duration = ch.json(f"""SELECT count(),max(memory_usage),sum(read_rows),sum(read_bytes),sum(query_duration_ms)
            FROM system.query_log WHERE log_comment={lit(tag)} AND type='QueryFinish'""")[0]
        result = {**body, "limits": limits,
                  "profile": {"statements": statements, "tracked_peak_memory_bytes": memory,
                              "read_rows": rows, "read_bytes": size, "summed_query_ms": duration},
                  "cache_state": "uncontrolled; offline census, not serving latency"}
        if monitor is not None:
            result["rss"] = monitor.summary()
        if raw is not None:
            result['source_provenance']['peak_tracked_temporary_table_bytes'] = ch.peak_staging_bytes
            closed = True
            ch.close()
            result['source_provenance']['owned_cleanup_verified'] = True
        with out.open("x") as output:
            if raw is not None:
                fchmod(output.fileno(), 0o600)
            output.write(dumps(result) + "\n")
        return result
    finally:
        if not closed:
            error = exception()
            try:
                ch.close()
            except BaseException as cleanup:
                if raw is None or error is None:
                    raise
                error.add_note('daily hot-frequency owned cleanup failed: ' + type(cleanup).__name__)
