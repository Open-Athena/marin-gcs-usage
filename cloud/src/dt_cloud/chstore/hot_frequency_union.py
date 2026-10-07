"""Explicit date-qualified unions of accepted single-date census registries.

A one-source union is a scan's own registry: membership is exactly that
scan's threshold-hot literals, in the format a registry selection pins.
Sources from different physical stores need an explicit logical `target`
binding; each source then declares its own physical target. Per-date
frequencies stay null below a source's minimum, so a literal registered
because another date qualified it is never claimed hot on this date.
"""

from hashlib import sha256
from itertools import combinations
from json import dumps, loads
from pathlib import Path

from .hot_frequency import within
from .hot_frequency_registry import FREQUENCY_SEMANTICS, UNION_CAP, UNION_SCHEMA, covers, union_header
from .hot_frequency_report import _validated_report, integer
from .hot_l1_catalog import _unique_object
from .narrow import identifier


def union(
    sources: tuple[tuple[Path, Path], ...],
    threshold: int,
    max_chars: int | None,
    out: Path,
    *,
    max_patterns: int = UNION_CAP,
    target: str | None = None,
) -> dict:
    """Write one deterministic fresh complete export, with no database access.

    Known below-requested-threshold frequencies stay exact. Absent source
    literals get None, which means below that source's minimum, not zero.
    `max_chars` None is the complete length domain; every source must be a
    complete census then.
    """
    integer(threshold, "union threshold", 1)
    if max_chars is not None:
        integer(max_chars, "union maximum length", 1)
    if integer(max_patterns, "union accepted-pattern cap", 1) > UNION_CAP:
        raise ValueError("union accepted-pattern cap cannot exceed 500000")
    if not sources:
        raise ValueError("union requires at least one dated source census")
    if out.exists():
        raise ValueError("union output must be a new artifact")
    accepted = []
    for census, queries in sources:
        raw, export = census.read_bytes(), queries.read_bytes()
        result, rows = _validated_report(census, queries, raw, export, thresholds=(threshold,), lengths=(max_chars,), patterns=())
        if not covers(result["provenance"]["queries"]["header"]["max_chars"], max_chars):
            raise ValueError("union length domain exceeds a source census")
        doc = loads(raw, object_pairs_hook=_unique_object)
        accepted.append((result, rows, doc.get("accepted_hot_pattern_cap")))
    accepted.sort(key=lambda item: item[0]["date"])
    dates = [result["date"] for result, _, _ in accepted]
    if len(set(dates)) != len(dates):
        raise ValueError("union source scan dates must be unique")
    if target is None:
        target = accepted[0][0]["target"]
        if any(result["target"] != target for result, _, _ in accepted):
            raise ValueError("union sources require the same frozen target unless an explicit logical target binds them")
        declared_targets = False
    else:
        identifier(target)
        declared_targets = True
    declarations, frequencies, candidates = [], {}, set()
    for result, rows, cap in accepted:
        original = result["provenance"]["queries"]["header"]
        declarations.append({"date": result["date"], **({"target": result["target"]} if declared_targets else {}), "snapshot_db": result["snapshot_db"],
            "threshold_paths": original["threshold_paths"], "max_chars": original["max_chars"], "accepted_hot_pattern_cap": cap,
            "census": {key: result["provenance"]["census"][key] for key in ("sha256", "bytes")},
            "queries": {key: result["provenance"]["queries"][key] for key in ("sha256", "bytes", "patterns")}})
        frequencies[result["date"]] = {row["pattern"]: row["direct_matching_paths"] for row in rows if within(row["chars"], max_chars)}
        candidates.update(row["pattern"] for row in rows if within(row["chars"], max_chars) and row["direct_matching_paths"] >= threshold)
        if len(candidates) > max_patterns:
            raise ValueError("union exceeds its accepted-pattern cap; no complete export")
    header = union_header({"schema": UNION_SCHEMA, "target": target, "dates": dates,
        "threshold_paths": threshold, "max_chars": max_chars, "max_patterns": max_patterns,
        "sources": declarations, "frequency_semantics": FREQUENCY_SEMANTICS}, target)
    rows = [{"chars": len(pattern), "pattern": pattern,
             "direct_matching_paths": {date: frequencies[date].get(pattern) for date in dates}}
            for pattern in sorted(candidates, key=lambda value: (len(value), value))]
    records = [(dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode() for row in rows]
    raw = ((dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n").encode() + b"".join(records) +
           (dumps({"complete": True, "patterns": len(rows)}, separators=(",", ":")) + "\n").encode())
    with out.open("xb") as output:
        output.write(raw)
    qualifying = {date: {row["pattern"] for row in rows if row["direct_matching_paths"][date] is not None and row["direct_matching_paths"][date] >= threshold} for date in dates}
    return {"schema": "hot-frequency-union-report-v1", "complete": True, "target": target, "registry_dates": dates,
        "threshold_paths": threshold, "max_chars": max_chars, "patterns": len(rows),
        "literal_utf8_bytes": sum(len(row["pattern"].encode()) for row in rows), "export_records_raw_bytes": sum(map(len, records)),
        "per_date": [{"date": date, "threshold_hot_patterns": len(qualifying[date]),
                      "known_frequencies": sum(row["direct_matching_paths"][date] is not None for row in rows),
                      "below_source_minimum_patterns": sum(row["direct_matching_paths"][date] is None for row in rows)} for date in dates],
        "overlap": [{"dates": [a, b], "patterns": len(qualifying[a] & qualifying[b])} for a, b in combinations(dates, 2)],
        "queries": {"path": str(out), "bytes": len(raw), "sha256": sha256(raw).hexdigest(), "header": header},
        "validation": "completed accepted single-date artifacts checked; union hot on any source date; not an independent source scan"}
