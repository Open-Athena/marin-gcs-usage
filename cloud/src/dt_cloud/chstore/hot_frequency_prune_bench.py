"""Isolated seeded weighted-name pruning experiment, never a serving index.

Complete trusted census/export artifacts supply the seed and every layer's
exact oracle. Only names stay on the server; client arrays contain bounded
accepted hot tuples. One pruning pass is measured, not an adaptive cascade.
"""

from contextlib import nullcontext
from collections.abc import Callable
from hashlib import sha256
from json import dumps, loads
from pathlib import Path
from sys import stderr
from time import monotonic
from uuid import uuid4

from .client import Ch, lit
from .hot_frequency import MAX_CHARS
from .hot_frequency_report import _PinnedExport, integer, report
from .hot_l1_batch_bench import load_queries
from .hot_l1_catalog import _unique_object
from .narrow import disk_reserve, identifier
from .resources import RssMonitor


def load_seed(
    census: Path,
    queries: Path,
    target: str,
    date: str,
    threshold: int,
    prune_chars: int,
    max_chars: int | None,
) -> dict:
    identifier(target)
    integer(threshold, "pruning threshold", 1)
    integer(prune_chars, "pruning length", 1)
    end = prune_chars + 1 if max_chars is None else integer(max_chars, "end length", 1)
    if not prune_chars < end <= MAX_CHARS:
        raise ValueError("pruning requires a seed length below the end length, at most 32")
    census_raw, queries_raw = census.read_bytes(), queries.read_bytes()
    header, patterns = load_queries(_PinnedExport(queries_raw), target, date, allow_union=False)
    if header["threshold_paths"] != threshold or header["max_chars"] < end:
        raise ValueError("pruning requires the same source minimum threshold and completed depth through the end length")
    if len(patterns) > 500_000:
        raise ValueError("pruning reference exceeds the 500K accepted-pattern cap")
    accepted = report(census, queries, thresholds=(threshold,), lengths=(end,), patterns=())
    if (accepted["provenance"]["census"]["sha256"] != sha256(census_raw).hexdigest() or
            accepted["provenance"]["queries"]["sha256"] != sha256(queries_raw).hexdigest()):
        raise ValueError("pruning reference files changed during validation")
    body = loads(census_raw, object_pairs_hook=_unique_object)
    frequencies = {chars: [] for chars in range(prune_chars, end + 1)}
    for line in queries_raw.splitlines()[1:-1]:
        row = loads(line, object_pairs_hook=_unique_object)
        if row["chars"] in frequencies:
            frequencies[row["chars"]].append((row["pattern"], row["direct_matching_paths"]))
    return {"target": target, "date": date, "threshold": threshold, "prune_chars": prune_chars, "max_chars": end,
            "snapshot_db": body["snapshot_db"], "distinct_names": integer(body["distinct_names"], "source names"),
            "paths": integer(body["paths"], "source paths"), "frequencies": {k: tuple(sorted(v)) for k, v in frequencies.items()},
            "control_layers": {layer["chars"]: layer["elapsed_s"] for layer in body["lengths"]},
            "provenance": accepted["provenance"]}


def _stats(
    ch: Ch,
    table: str,
    lengths: range,
) -> dict:
    windows = [f"sum(greatest(toInt64(lengthUTF8(l)) - {chars} + 1, 0))" for chars in lengths]
    rows, paths, size, chars, invalid, *counts = ch.json(f"""SELECT count(),sum(c),sum(length(l)),sum(lengthUTF8(l)),
        countIf(c = 0 OR position(l,'/') > 0 OR NOT isValidUTF8(l)),{','.join(windows)} FROM {table}""")[0]
    if invalid:
        raise ValueError("pruning weighted vocabulary contains invalid UTF-8, separators or nonpositive path frequencies")
    return {"distinct_names": rows, "paths": paths, "name_utf8_bytes": size, "name_characters": chars,
            "occurrence_windows": [{"chars": k, "windows": n} for k, n in zip(lengths, counts, strict=True)]}


def run(
    ch: Ch,
    seed: dict,
    *,
    progress: Callable[[dict], None] | None = None,
    log_tag: str = "",
) -> dict:
    """Run in the caller's owned session; caller must close on every outcome."""
    target, date = seed["target"], seed["date"]
    identifier(target)
    manifest = loads(ch.scalar(f"SELECT doc FROM {target}.history_manifest"))
    if manifest["prefix"] != "" or date not in manifest["dates"]:
        raise ValueError("pruning requires a global frozen target covering the selected date")
    database = identifier(manifest["dbs"][manifest["dates"].index(date)])
    if database != seed["snapshot_db"]:
        raise ValueError("pruning snapshot database disagrees with the accepted census")
    tag = uuid4().hex
    weighted, candidates, previous = (f"hot_prune_{label}_{tag}" for label in ("names", "candidates", "seed"))
    lengths = range(seed["prune_chars"] + 1, seed["max_chars"] + 1)

    def emit(stage: dict) -> None:
        if progress is not None:
            progress(stage)

    start = monotonic()
    disk_reserve(ch, "pruning weighted vocabulary", 20 << 30)
    ch.tmp(weighted, f"""SELECT n.nid AS nid, lowerUTF8(n.l) AS l, f.c AS c
        FROM (SELECT nid,l FROM {target}.names ORDER BY nid) n
        INNER JOIN (SELECT nid,count() AS c FROM {database}.nodes_by_name GROUP BY nid ORDER BY nid) f ON n.nid=f.nid""",
        disk=True, order_by="nid", settings={
            "join_algorithm": "full_sorting_merge", "optimize_aggregation_in_order": 1,
            "query_plan_join_swap_table": "false", "max_rows_in_set_to_optimize_join": 0,
            "max_block_size": 8192, "max_bytes_before_external_sort": 67108864,
            **({"log_comment": log_tag + ":weighted"} if log_tag else {}),
        })
    source = _stats(ch, weighted, lengths)
    if (source["distinct_names"], source["paths"]) != (seed["distinct_names"], seed["paths"]):
        raise ValueError("pruning source name/path counts disagree with the accepted census")
    weighted_s = monotonic() - start
    emit({"stage": "weighted-names", **source, "elapsed_s": weighted_s})
    ch.tmp(previous, "SELECT CAST('','String') AS gram WHERE 0")
    if seed["frequencies"][seed["prune_chars"]]:
        ch.insert(f"INSERT INTO {previous} FORMAT JSONEachRow", ((dumps({"gram": gram}) + "\n").encode() for gram, _ in seed["frequencies"][seed["prune_chars"]]))
    start = monotonic()
    disk_reserve(ch, "pruning candidates", 20 << 30)
    free_before = int(ch.scalar("SELECT min(free_space) FROM system.disks"))
    k = seed["prune_chars"]
    ch.tmp(candidates, f"""SELECT nid,l,c FROM {weighted} WHERE lengthUTF8(l) > {k}
        AND arrayExists(g -> g IN (SELECT gram FROM {previous}), ngrams(l,{k}))""",
        disk=True, ordered=False, settings={"log_comment": log_tag + ":prune"} if log_tag else None)
    filter_s = monotonic() - start
    retained = _stats(ch, candidates, lengths)
    disk_bytes, disk_rows = ch.json(f"SELECT sum(bytes_on_disk),sum(rows) FROM system.parts WHERE active AND table={lit(candidates)}")[0]
    retained["table_disk_bytes"] = disk_bytes if disk_rows == retained["distinct_names"] else None
    retained["disk_free_bytes_before"] = free_before
    retained["disk_free_bytes_after"] = int(ch.scalar("SELECT min(free_space) FROM system.disks"))
    prune_s = monotonic() - start
    emit({"stage": "prune-names", **retained, "filter_s": filter_s, "elapsed_s": prune_s})
    layers = []
    for chars in lengths:
        start = monotonic()
        table = f"hot_prune_layer_{chars}_{tag}"
        disk_reserve(ch, f"pruning length {chars}", 20 << 30)
        grams = f"arrayFilter(g -> substringUTF8(g,1,{chars - 1}) IN (SELECT gram FROM {previous}), arrayDistinct(ngrams(l,{chars})))"
        ch.tmp(table, f"""SELECT gram,sum(c) AS direct_matching_paths FROM (
            SELECT c,arrayJoin({grams}) AS gram FROM {candidates}
        ) GROUP BY gram HAVING direct_matching_paths >= {seed['threshold']}""", disk=True, order_by="gram",
            settings={"log_comment": log_tag + f":layer-{chars}"} if log_tag else None)
        enumeration_s = monotonic() - start
        expected = seed["frequencies"][chars]
        count = int(ch.scalar(f"SELECT count() FROM {table}"))
        # Count refusal bounds the client read; successful acceptance also
        # compares every literal and frequency, never only aggregate statistics.
        if count != len(expected):
            raise RuntimeError(f"pruning exact reference mismatch at length {chars}; no accepted result")
        actual = tuple(tuple(row) for row in ch.json(f"SELECT gram,direct_matching_paths FROM {table} ORDER BY gram"))
        if actual != expected:
            raise RuntimeError(f"pruning exact reference mismatch at length {chars}; no accepted result")
        layer = {"chars": chars, "hot_patterns": count, "hot_query_utf8_bytes": sum(len(g.encode()) for g, _ in actual),
                 "sum_hot_direct_matching_paths": sum(n for _, n in actual), "enumeration_s": enumeration_s,
                 "elapsed_s": monotonic() - start, "control_observed_elapsed_s": seed["control_layers"][chars],
                 "validation": "complete sorted literal/frequency tuple equality against accepted export"}
        layers.append(layer)
        emit({"stage": "hot-substrings", **layer})
        previous = table
    return {"schema": "hot-frequency-prune-bench-v1", "complete": True, "target": target, "date": date,
            "snapshot_db": database, "threshold_paths": seed["threshold"], "prune_chars": k, "max_chars": seed["max_chars"],
            "seed_hot_patterns": len(seed["frequencies"][k]), "source": source, "retained": retained,
            "weighted_names_s": weighted_s, "prune_filter_s": filter_s, "prune_elapsed_s": prune_s, "lengths": layers,
            "provenance": seed["provenance"], "persistent_index_created": False,
            "disk_measurement": "active table bytes when system.parts exposes all rows; free-space observations are whole-server, not table-write counters",
            "cache_state": "uncontrolled; retained control is not a cache-normalized A/B; control layer times may include export"}


def bench(
    url: str,
    target: str,
    date: str,
    threshold: int,
    prune_chars: int,
    census: Path,
    queries: Path,
    out: Path,
    *,
    max_chars: int | None = None,
    memory_gib: int = 8,
    seconds: int = 600,
    spill_gib: int = 8,
    pids: tuple[int, ...] = (),
) -> dict:
    if any(type(value) is not int for value in (memory_gib, seconds, spill_gib)) or not 1 <= memory_gib <= 8 or not 1 <= seconds <= 600 or not 1 <= spill_gib <= 8:
        raise ValueError("pruning requires 1..8 GiB memory/spill and 1..600 seconds")
    if out.exists() or out.is_symlink():
        raise ValueError("pruning artifact already exists")
    if not out.parent.is_dir():
        raise ValueError("pruning artifact parent does not exist")
    seed = load_seed(census, queries, target, date, threshold, prune_chars, max_chars)
    rss_out = out.with_suffix(".rss.jsonl")
    if pids and rss_out.exists():
        raise ValueError("pruning RSS artifact already exists")
    monitor = RssMonitor(pids, rss_out) if pids else None
    tag = "hot_frequency_prune_" + uuid4().hex
    limits = {"memory_gib": memory_gib, "seconds": seconds, "spill_gib": spill_gib, "threads": 4}
    print(dumps({"log_comment": tag, "limits": limits}), file=stderr)
    ch = Ch(url, db=target, timeout=seconds + 60, max_threads=4, max_memory_usage=memory_gib << 30,
            max_execution_time=seconds, timeout_before_checking_execution_speed=0, timeout_overflow_mode="throw",
            max_bytes_before_external_sort=256 << 20, max_bytes_ratio_before_external_sort=0,
            max_bytes_before_external_group_by=256 << 20, max_bytes_ratio_before_external_group_by=0,
            max_temporary_data_on_disk_size_for_query=spill_gib << 30, log_comment=tag)
    try:
        with monitor if monitor is not None else nullcontext():
            body = run(ch, seed, progress=lambda stage: print(dumps(stage), file=stderr), log_tag=tag)
        ch.exec("SYSTEM FLUSH LOGS")
        rows = ch.json(f"""SELECT log_comment,count(),max(memory_usage),sum(read_rows),sum(read_bytes),sum(query_duration_ms),
            sum(ProfileEvents['UserTimeMicroseconds']) + sum(ProfileEvents['SystemTimeMicroseconds']),
            sum(ProfileEvents['ExternalProcessingCompressedBytesTotal'])
            FROM system.query_log WHERE startsWith(log_comment,{lit(tag)}) AND type='QueryFinish'
            GROUP BY log_comment ORDER BY log_comment""")
        body.update(limits=limits, profile=[{"stage": stage, "statements": n, "tracked_peak_memory_bytes": memory,
                                          "read_rows": read, "read_bytes": size, "summed_query_ms": ms,
                                          "cpu_s": cpu / 1e6, "external_processing_compressed_bytes": spill}
                                         for stage, n, memory, read, size, ms, cpu, spill in rows])
        if monitor is not None:
            body["rss"] = monitor.summary()
        with out.open("x") as output:
            output.write(dumps(body) + "\n")
        return body
    finally:
        ch.close()
