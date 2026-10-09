"""Bounded short-substring sizing census, never a sampled query answer."""

from contextlib import nullcontext
from json import dumps, loads
from pathlib import Path
from sys import stderr
from time import monotonic
from uuid import uuid4

from .client import Ch, lit
from .narrow import identifier
from .resources import RssMonitor


def census(
    ch: Ch,
    target: str,
    max_chars: int = 7,
    modulus: int = 8192,
    max_names: int = 100_000,
) -> dict:
    identifier(target)
    if not 1 <= max_chars <= 7 or modulus < 1 or not 1 <= max_names <= 100_000:
        raise ValueError("short-query census requires lengths 1..7, positive modulus and a 1..100K-name budget")
    start = monotonic()
    windows = ",".join(f"sum(greatest(toInt64(lengthUTF8(l))-{k - 1},0))" for k in range(1, max_chars + 1))
    names, n_bytes, n_chars, all_windows = ch.json(f"SELECT count(),sum(length(l)),sum(lengthUTF8(l)),[{windows}] FROM {target}.names")[0]
    ch.tmp("short_query_sample", f"SELECT nid,l FROM {target}.names WHERE cityHash64(nid) % {modulus}=0 LIMIT {max_names + 1}")
    sampled, longest = ch.json("SELECT count(),max(lengthUTF8(l)) FROM short_query_sample")[0]
    if sampled > max_names:
        raise ValueError("short-query sample exceeds its name budget; increase the sampling modulus")
    if longest > 2048:
        raise ValueError("short-query sample contains a name over 2048 characters")
    rows = ch.json(f"""SELECT k,count(),uniqExact(gram) FROM (
        SELECT k,arrayJoin(arrayDistinct(arrayMap(pos -> substringUTF8(l,pos,k),
            range(toUInt64(1),toUInt64(greatest(toInt64(lengthUTF8(l))-k+2,1)))))) gram
        FROM short_query_sample ARRAY JOIN range(1,{max_chars + 1}) AS k
    ) GROUP BY k ORDER BY k""")
    counts = {k: (pairs, patterns) for k, pairs, patterns in rows}
    return {"scope": "union lowercase basename vocabulary; sizing sample, not full-path/query acceptance",
            "names": names, "name_bytes": n_bytes, "name_chars": n_chars,
            "sample_modulus": modulus, "sampled_names": sampled,
            "lengths": [{"chars": k, "global_occurrence_windows": all_windows[k - 1],
                         "sample_distinct_name_pattern_pairs": counts.get(k, (0, 0))[0],
                         "sample_distinct_patterns": counts.get(k, (0, 0))[1]} for k in range(1, max_chars + 1)],
            "elapsed_s": round(monotonic() - start, 4),
            "global_distinct_patterns_estimated": False, "full_path_boundaries_included": False}


def bench(
    url: str,
    target: str,
    max_chars: int,
    modulus: int,
    max_names: int,
) -> dict:
    ch = Ch(url, db=target, max_threads=4, max_memory_usage=1 << 30,
            max_execution_time=30, timeout_before_checking_execution_speed=0,
            timeout_overflow_mode="throw")
    try:
        return census(ch, target, max_chars, modulus, max_names)
    finally:
        ch.close()


def frequency_census(
    ch: Ch,
    target: str,
    date: str,
    thresholds: tuple[int, ...],
) -> dict:
    """Exact full-snapshot basename reuse; no substring expansion or stored index."""
    identifier(target)
    if not thresholds or len(thresholds) > 10 or any(t < 1 for t in thresholds):
        raise ValueError("one through ten positive thresholds required")
    manifest = loads(ch.scalar(f"SELECT doc FROM {target}.history_manifest"))
    db = identifier(manifest["dbs"][manifest["dates"].index(date)])
    start = monotonic()
    fields = ",".join(f"countIf(c >= {t}),sumIf(c,c >= {t})" for t in thresholds)
    rows = ch.json(f"""SELECT count(),sum(c),max(c),{fields} FROM (
        SELECT nid,count() c FROM {db}.nodes_by_name GROUP BY nid
    ) SETTINGS optimize_aggregation_in_order=1""")
    names, paths, largest, *values = rows[0]
    return {"scope": "exact snapshot basename frequency, not substring support or query latency",
            "date": date, "distinct_names": names, "paths": paths, "most_reused_name_paths": largest,
            "thresholds": [{"paths_at_least": t, "names": values[2 * i], "paths": values[2 * i + 1]} for i, t in enumerate(thresholds)],
            "elapsed_s": monotonic() - start, "stored_index_created": False}


def frequency_bench(
    url: str,
    target: str,
    date: str,
    thresholds: tuple[int, ...],
) -> dict:
    ch = Ch(url, db=target, max_threads=4, max_memory_usage=1 << 30,
            max_execution_time=30, timeout_before_checking_execution_speed=0,
            timeout_overflow_mode="throw")
    try:
        return frequency_census(ch, target, date, thresholds)
    finally:
        ch.close()


def pattern_frequency_census(
    ch: Ch,
    target: str,
    date: str,
    patterns: tuple[str, ...],
    *,
    query_id: str | None = None,
) -> dict:
    """Merge ordered name frequencies with text once; return exact selected-pattern counts."""
    identifier(target)
    if not 1 <= len(patterns) <= 16 or any(not p or "/" in p or len(p) > 7 for p in patterns):
        raise ValueError("one through sixteen name-only literals of 1..7 characters required")
    patterns = tuple(pattern.lower() for pattern in patterns)
    manifest = loads(ch.scalar(f"SELECT doc FROM {target}.history_manifest"))
    db = identifier(manifest["dbs"][manifest["dates"].index(date)])
    start = monotonic()
    mask = "+".join(f"if(position(l,{lit(p)}) > 0,{1 << i},0)" for i, p in enumerate(patterns))
    fields = ",".join(f"countIf(bitTest(n.mask,{i})),sumIf(f.c,bitTest(n.mask,{i}))" for i in range(len(patterns)))
    rows = ch.json(f"""SELECT count(),sum(f.c),{fields}
        FROM (SELECT nid,toUInt16({mask}) mask FROM {target}.names ORDER BY nid) n
        INNER JOIN (SELECT nid,count() c FROM {db}.nodes_by_name GROUP BY nid ORDER BY nid) f ON n.nid=f.nid
        SETTINGS join_algorithm='full_sorting_merge',optimize_aggregation_in_order=1,
        query_plan_join_swap_table='false',max_rows_in_set_to_optimize_join=0,
        max_block_size=8192,max_bytes_before_external_sort=67108864""",
        settings={"query_id": query_id} if query_id is not None else {},
    )
    names, paths, *values = rows[0]
    return {"scope": "exact selected name-substring frequencies; no inherited coverage or aggregate payload",
            "date": date, "distinct_names": names, "paths": paths,
            "patterns": [{"pattern": p, "names": values[2 * i], "direct_matching_paths": values[2 * i + 1]} for i, p in enumerate(patterns)],
            "elapsed_s": monotonic() - start, "stored_index_created": False}


def pattern_frequency_bench(
    url: str,
    target: str,
    date: str,
    patterns: tuple[str, ...],
    *,
    memory_gib: int = 8,
    seconds: int = 180,
    spill_gib: int = 8,
    pids: tuple[int, ...] = (),
    rss_out: Path | None = None,
    profile: bool = False,
) -> dict:
    """Offline census budgets are independent of live serving request budgets."""
    if not 1 <= memory_gib <= 8 or not 1 <= seconds <= 600 or not 1 <= spill_gib <= 16:
        raise ValueError("offline census requires 1..8 GiB memory, 1..600 seconds and 1..16 GiB spill")
    if bool(pids) != (rss_out is not None):
        raise ValueError("RSS monitoring requires both process IDs and an output path")
    monitor = RssMonitor(pids, rss_out) if pids else None
    query_id = "pattern_census_" + uuid4().hex
    limits = {"memory_gib": memory_gib, "seconds": seconds, "spill_gib": spill_gib, "threads": 4}
    print(dumps({"query_id": query_id, "limits": limits}), file=stderr)
    ch = Ch(url, db=target, timeout=seconds + 60, max_threads=4, max_memory_usage=memory_gib << 30,
            max_execution_time=seconds, timeout_before_checking_execution_speed=0,
            timeout_overflow_mode="throw", max_temporary_data_on_disk_size_for_query=spill_gib << 30)
    try:
        with monitor if monitor is not None else nullcontext():
            result = pattern_frequency_census(ch, target, date, patterns, query_id=query_id)
        result.update(query_id=query_id, limits=limits, cache_state="uncontrolled; offline census, not serving latency")
        if monitor is not None:
            result["rss"] = monitor.summary()
        if profile:
            ch.exec("SYSTEM FLUSH LOGS")
            rows = ch.json(f"""SELECT query_duration_ms,memory_usage,read_rows,read_bytes,ProfileEvents
                FROM system.query_log WHERE query_id={lit(query_id)} AND type='QueryFinish' ORDER BY event_time DESC LIMIT 1""")
            if len(rows) != 1:
                raise RuntimeError("successful census query profile missing")
            duration, memory, read_rows, read_bytes, events = rows[0]
            result["profile"] = {"duration_ms": duration, "tracked_peak_memory_bytes": memory,
                                 "read_rows": read_rows, "read_bytes": read_bytes, "events": events}
        return result
    finally:
        ch.close()
