"""Exact offline thresholded basename-substring frequencies.

All names remain on the server. Distinct substrings of each active lowercase
name contribute its snapshot path frequency once. An extension cannot be hot
if its prefix is cold, so later layers filter by the previous complete hot
table before exploding substrings. Tables belong to the caller's session;
this is neither inherited directory coverage nor a persistent serving index.
"""

from collections.abc import Callable
from json import loads
from time import monotonic
from uuid import uuid4

from .client import Ch, lit
from .narrow import disk_reserve, identifier

MAX_CHARS = 32


def validate_limits(
    threshold: int,
    max_chars: int,
    thresholds: tuple[int, ...],
    max_patterns: int,
) -> tuple[int, ...]:
    if type(threshold) is not int or threshold < 1 or type(max_chars) is not int or not 1 <= max_chars <= MAX_CHARS:
        raise ValueError("hot-frequency census requires a positive integer threshold and 1..32 characters")
    if any(type(cut) is not int or cut < threshold for cut in thresholds):
        raise ValueError("hot-frequency threshold cuts must be integers at least the minimum threshold")
    if type(max_patterns) is not int or max_patterns < 1:
        raise ValueError("hot-frequency accepted-pattern cap must be a positive integer")
    return tuple(sorted({threshold, *thresholds})) if thresholds else ()


def normalize_patterns(patterns: tuple[str, ...]) -> tuple[str, ...]:
    if len(patterns) > 16 or any(not pattern or "/" in pattern or "\0" in pattern or len(pattern) > MAX_CHARS for pattern in patterns):
        raise ValueError("selected hot-frequency patterns require at most sixteen NUL/slash-free literals of 1..32 characters")
    patterns = tuple(pattern.lower() for pattern in patterns)
    if any(len(pattern) > MAX_CHARS for pattern in patterns):
        raise ValueError("normalized hot-frequency patterns must contain at most 32 characters")
    for pattern in patterns:
        pattern.encode('utf-8')
    return patterns


def census(
    ch: Ch,
    target: str,
    date: str,
    threshold: int,
    max_chars: int = 7,
    patterns: tuple[str, ...] = (),
    *,
    progress: Callable[[dict], None] | None = None,
    on_hot_table: Callable[[int, str], None] | None = None,
    thresholds: tuple[int, ...] = (),
    max_patterns: int = 500_000,
) -> dict:
    """Enumerate complete hot tables under caller-chosen resource limits.

    Selected cold patterns return None, not a false zero frequency. Requested
    patterns longer than the enumerated depth are omitted. The 20 GiB disk
    reserve is a stage-start check, not an in-flight disk quota.
    Additional cuts count the same minimum-threshold tables, never rerun
    enumeration. The cumulative pattern cap refuses an entire oversized
    layer before its export callback; no successful partial result is returned.
    """
    identifier(target)
    validate_limits(threshold, max_chars, thresholds, max_patterns)
    patterns = normalize_patterns(patterns)
    manifest = loads(ch.scalar(f"SELECT doc FROM {target}.history_manifest"))
    if manifest["prefix"] != "":
        raise ValueError("hot-frequency census requires a global frozen target")
    if date not in manifest["dates"]:
        raise ValueError("scan outside the frozen index")
    database = identifier(manifest["dbs"][manifest["dates"].index(date)])
    tag = uuid4().hex
    weighted = f"hot_frequency_names_{tag}"
    start = monotonic()
    disk_reserve(ch, "hot-frequency weighted vocabulary", 20 << 30)
    ch.tmp(weighted, f"""SELECT n.nid AS nid, lowerUTF8(n.l) AS l, f.c AS c
        FROM (SELECT nid,l FROM {target}.names ORDER BY nid) n
        INNER JOIN (SELECT nid,count() AS c FROM {database}.nodes_by_name GROUP BY nid ORDER BY nid) f ON n.nid=f.nid""",
        disk=True, order_by="nid", settings={
            "join_algorithm": "full_sorting_merge", "optimize_aggregation_in_order": 1,
            "query_plan_join_swap_table": "false", "max_rows_in_set_to_optimize_join": 0,
            "max_block_size": 8192, "max_bytes_before_external_sort": 67108864,
        },
    )
    return census_weighted(ch, target, database, date, threshold, max_chars, patterns,
                           weighted=weighted, tag=tag, started=start, progress=progress,
                           on_hot_table=on_hot_table, thresholds=thresholds, max_patterns=max_patterns)


def census_weighted(
    ch: Ch,
    target: str,
    database: str,
    date: str,
    threshold: int,
    max_chars: int,
    patterns: tuple[str, ...],
    *,
    weighted: str,
    tag: str,
    started: float,
    progress: Callable[[dict], None] | None = None,
    on_hot_table: Callable[[int, str], None] | None = None,
    thresholds: tuple[int, ...] = (),
    max_patterns: int = 500_000,
    expected_paths: int | None = None,
) -> dict:
    """Shared exact pruning over a caller-owned complete (l,c) vocabulary."""
    for name in (target, database, weighted):
        identifier(name)
    cuts = validate_limits(threshold, max_chars, thresholds, max_patterns)
    patterns = normalize_patterns(patterns)
    names, paths, separators = ch.json(f"SELECT count(),sum(c),countIf(position(l,'/') > 0) FROM {weighted}")[0]
    if expected_paths is not None and paths != expected_paths:
        raise ValueError('daily hot-frequency weighted path count differs from its accepted source')
    if separators:
        raise ValueError("snapshot basename vocabulary contains path separators")
    weighted_s = monotonic() - started
    if progress is not None:
        progress({"stage": "weighted-names", "distinct_names": names, "paths": paths, "elapsed_s": weighted_s})
    previous, previous_count = None, None
    lengths, selected, accepted = [], {}, 0
    for chars in range(1, max_chars + 1):
        start = monotonic()
        table = f"hot_frequency_{chars}_{tag}"
        skipped = previous_count == 0
        if skipped:
            count, text_bytes, weighted_sum = 0, 0, 0
            cut_counts = [0] * len(cuts)
        else:
            disk_reserve(ch, f"hot-frequency length {chars}", 20 << 30)
            grams = f"arrayDistinct(ngrams(l,{chars}))"
            if previous is not None:
                grams = f"arrayFilter(candidate -> substringUTF8(candidate,1,{chars - 1}) IN (SELECT gram FROM {previous}), {grams})"
            ch.tmp(table, f"""SELECT gram,sum(c) AS direct_matching_paths FROM (
                SELECT c,arrayJoin({grams}) AS gram FROM {weighted}
            ) GROUP BY gram HAVING direct_matching_paths >= {threshold}""", disk=True, order_by="gram")
            count, text_bytes, weighted_sum = ch.json(f"SELECT count(),sum(length(gram)),sum(direct_matching_paths) FROM {table}")[0]
            accepted += count
            if accepted > max_patterns:
                raise RuntimeError(f"hot-frequency accepted-pattern cap exceeded at length {chars}: {accepted} > {max_patterns}; no complete census")
            cut_counts = ch.json(f"SELECT {','.join(f'countIf(direct_matching_paths >= {cut})' for cut in cuts)} FROM {table}")[0] if cuts else []
            if on_hot_table is not None:
                on_hot_table(chars, table)
            wanted = sorted({pattern for pattern in patterns if len(pattern) == chars})
            if wanted and count:
                selected.update(ch.json(f"SELECT gram,direct_matching_paths FROM {table} WHERE gram IN ({','.join(map(lit, wanted))}) ORDER BY gram"))
            previous = table
        elapsed = monotonic() - start
        lengths.append({"chars": chars, "hot_patterns": count, "hot_query_utf8_bytes": text_bytes,
                        "sum_hot_direct_matching_paths": weighted_sum, "elapsed_s": elapsed,
                        "pruned_by_empty_prefix": skipped})
        if cuts:
            lengths[-1]["threshold_counts"] = [{"threshold_paths": cut, "hot_patterns": amount} for cut, amount in zip(cuts, cut_counts, strict=True)]
        previous_count = count
        if progress is not None:
            progress({"stage": "hot-substrings", "chars": chars, "hot_patterns": count, "elapsed_s": elapsed,
                      **({"threshold_counts": lengths[-1]["threshold_counts"]} if cuts else {})})
    body = {
        "schema": "hot-frequency-v1", "target": target, "snapshot_db": database, "date": date,
        "scope": "complete snapshot lowercase basename substrings; direct paths, not inherited coverage or occurrence windows",
        "threshold_paths": threshold, "max_chars": max_chars, "distinct_names": names, "paths": paths,
        "weighted_names_s": weighted_s, "lengths": lengths,
        "selected_patterns": [{"pattern": pattern, "hot": pattern in selected,
                               "direct_matching_paths": selected.get(pattern)} for pattern in patterns if len(pattern) <= max_chars],
        "temporary_index": "session-owned; Ch.close cleans up", "persistent_index_created": False,
    }
    if cuts:
        body["thresholds_paths"] = list(cuts)
    body["accepted_hot_pattern_cap"] = max_patterns
    return body
