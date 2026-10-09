"""Read-only exact T/L grids from one accepted census and its complete export.

Byte totals count retained original query-record lines, not inferred drill
storage. Higher-threshold grids are filters of the minimum-threshold build;
their construction times have not been measured.
"""

from hashlib import sha256
from json import loads
from math import isfinite
from pathlib import Path

from .hot_frequency import MAX_CHARS, within
from .hot_frequency_registry import PinnedExport as _PinnedExport, load_queries
from .hot_l1_catalog import _unique_object
from .narrow import identifier

SCOPE = "complete snapshot lowercase basename substrings; direct paths, not inherited coverage or occurrence windows"


def integer(value: object, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def duration(value: object, label: str) -> float:
    if type(value) not in (int, float) or not isfinite(value) or value < 0:
        raise ValueError(f"{label} must be a finite nonnegative duration")
    return value


def _validated_report(
    census_path: Path,
    queries_path: Path,
    census_raw: bytes,
    queries_raw: bytes,
    *,
    thresholds: tuple[int, ...] = (100_000, 300_000, 1_000_000),
    lengths: tuple[int, ...] = (7, 12, 16),
    patterns: tuple[str, ...] = (".json", "zarr.json", ".npy"),
) -> tuple[dict, list[dict]]:
    body = loads(census_raw, object_pairs_hook=_unique_object)
    if (not isinstance(body, dict) or body.get("schema") != "hot-frequency-v1" or body.get("scope") != SCOPE or
            body.get("persistent_index_created") is not False):
        raise ValueError("report requires an accepted hot-frequency census artifact")
    target, date = body.get("target"), body.get("date")
    if not isinstance(target, str) or not isinstance(date, str):
        raise ValueError("census requires a target and scan date")
    if not isinstance(body.get("snapshot_db"), str):
        raise ValueError("census requires a snapshot database identifier")
    identifier(body["snapshot_db"])
    # load_queries validates the exact header, normalized unique literals and
    # completion footer using these already-pinned bytes, without reopening disk.
    header, registered = load_queries(_PinnedExport(queries_raw), target, date, allow_union=False)
    minimum, maximum = header["threshold_paths"], header["max_chars"]
    if (body.get("threshold_paths") != minimum or type(body.get("threshold_paths")) is not int or body.get("max_chars") != maximum or
            (maximum is not None and type(body.get("max_chars")) is not int)):
        raise ValueError("census and export threshold/maximum length disagree")
    if body.get("short_chars") != header.get("short_chars"):
        raise ValueError("census and export short-literal domains disagree")
    if not thresholds or any(type(cut) is not int or cut < minimum for cut in thresholds):
        raise ValueError("report thresholds must be nonempty integers at least the source minimum")
    # A None length is the complete domain, which only a complete census covers.
    if not lengths or any(chars != maximum if chars is None else type(chars) is not int or not chars >= 1 or not within(chars, maximum)
                          for chars in lengths):
        raise ValueError("report lengths must be nonempty integers within the completed source depth")
    thresholds = tuple(sorted(set(thresholds)))
    lengths = tuple(sorted(set(lengths), key=lambda chars: (chars is None, chars or 0)))
    if any(not isinstance(pattern, str) or not pattern or "/" in pattern or "\0" in pattern for pattern in patterns):
        raise ValueError("selected report literals must be nonempty NUL/slash-free names")
    patterns = tuple(dict.fromkeys(pattern.lower() for pattern in patterns))
    # A complete census classifies literals of any length (up to the serving literal limit).
    limit = MAX_CHARS if maximum is not None else 512
    if any(len(pattern) > limit for pattern in patterns):
        raise ValueError(f"selected report literals must contain at most {limit} characters")
    for pattern in patterns:
        pattern.encode("utf-8")
    meta = body.get("queries")
    if (not isinstance(meta, dict) or integer(meta.get("patterns"), "census exported patterns") != len(registered) or
            integer(meta.get("bytes"), "census export bytes") != len(queries_raw)):
        raise ValueError("census export count/bytes disagree with the completed registry")
    rows = []
    for raw in queries_raw.splitlines(keepends=True)[1:-1]:
        row = loads(raw, object_pairs_hook=_unique_object)
        rows.append({**row, "record_bytes": len(raw), "literal_bytes": len(row["pattern"].encode("utf-8"))})
    if tuple(row["pattern"] for row in rows) != registered:
        raise ValueError("pinned export records disagree with validated registry")
    layers = body.get("lengths")
    # A complete census's layers run through its first empty length.
    if (not isinstance(layers, list) or
            (len(layers) != maximum if maximum is not None else not layers or
             len(layers) != max((row["chars"] for row in rows), default=0) + 1)):
        raise ValueError("census must cover every completed length")
    cuts = body.get("thresholds_paths", [])
    if not isinstance(cuts, list) or any(type(cut) is not int or cut < minimum for cut in cuts) or (cuts and (cuts != sorted(set(cuts)) or cuts[0] != minimum)):
        raise ValueError("census threshold cuts are invalid")
    for chars, layer in enumerate(layers, 1):
        group = [row for row in rows if row["chars"] == chars]
        expected = (len(group), sum(row["literal_bytes"] for row in group), sum(row["direct_matching_paths"] for row in group))
        if (not isinstance(layer, dict) or type(layer.get("chars")) is not int or layer["chars"] != chars or
                tuple(integer(layer.get(key), f"layer {key}") for key in ("hot_patterns", "hot_query_utf8_bytes", "sum_hot_direct_matching_paths")) != expected or
                layer.get("pruned_by_empty_prefix") is not (chars > 1 and layers[chars - 2]["hot_patterns"] == 0)):
            raise ValueError("census layer counts/bytes/frequencies disagree with completed export")
        expected_cuts = [{"threshold_paths": cut, "hot_patterns": sum(row["direct_matching_paths"] >= cut for row in group)} for cut in cuts]
        actual_cuts = layer.get("threshold_counts", [])
        if (not isinstance(actual_cuts, list) or any(not isinstance(row, dict) or
                type(row.get("threshold_paths")) is not int or type(row.get("hot_patterns")) is not int for row in actual_cuts) or
                actual_cuts != expected_cuts):
            raise ValueError("census threshold counts disagree with completed export")
        duration(layer.get("elapsed_s"), "layer elapsed_s")
    cap = body.get("accepted_hot_pattern_cap")
    if cap is not None and integer(cap, "accepted pattern cap", 1) < len(rows):
        raise ValueError("completed export exceeds its accepted pattern cap")
    frequencies = {row["pattern"]: row["direct_matching_paths"] for row in rows}
    if not isinstance(body.get("selected_patterns", []), list):
        raise ValueError("census selected literals must be a list")
    for selected in body.get("selected_patterns", []):
        if (not isinstance(selected, dict) or not isinstance(selected.get("pattern"), str) or
                not selected["pattern"] or selected["pattern"] != selected["pattern"].lower() or
                "/" in selected["pattern"] or "\0" in selected["pattern"] or not within(len(selected["pattern"]), maximum) or
                selected.get("hot") is not (selected["pattern"] in frequencies) or
                selected.get("direct_matching_paths") != frequencies.get(selected["pattern"]) or
                (selected["pattern"] in frequencies and type(selected.get("direct_matching_paths")) is not int)):
            raise ValueError("census selected literals disagree with completed export")
        selected["pattern"].encode("utf-8")
    grid = []
    for cut in thresholds:
        for chars in lengths:
            selected = [row for row in rows if within(row["chars"], chars) and row["direct_matching_paths"] >= cut]
            grid.append({"threshold_paths": cut, "max_chars": chars, "literals": len(selected),
                         "literal_utf8_bytes": sum(row["literal_bytes"] for row in selected),
                         "export_records_raw_bytes": sum(row["record_bytes"] for row in selected)})
    result = {
        "schema": "hot-frequency-report-v1", "complete": True, "target": target, "date": date,
        "snapshot_db": body["snapshot_db"], "scope": SCOPE,
        "provenance": {"census": {"path": str(census_path), "sha256": sha256(census_raw).hexdigest(), "bytes": len(census_raw)},
                       "queries": {"path": str(queries_path), "sha256": sha256(queries_raw).hexdigest(), "bytes": len(queries_raw),
                                   "patterns": len(rows), "header": header},
                       "validation": "complete export plus exact census/header/layer/count/byte agreement; not an independent source scan"},
        "grid": grid,
        "byte_definition": "original query-record line bytes including newline; excludes header/footer; not materialized drill storage",
        "selected_literals": [{"pattern": pattern, "enumerated": within(len(pattern), maximum),
                               "registered": pattern in frequencies, "direct_matching_paths": frequencies.get(pattern),
                               "cold_upper_bound_exclusive": minimum if within(len(pattern), maximum) and pattern not in frequencies else None}
                              for pattern in patterns],
        "actual_build": {"threshold_paths": minimum, "max_chars": maximum,
                         "weighted_names_s": duration(body.get("weighted_names_s"), "weighted_names_s"),
                         "layer_elapsed_s": sum(layer["elapsed_s"] for layer in layers),
                         "reported_export_s": duration(meta.get("export_s"), "export_s"),
                         "limits": body.get("limits"), "profile": body.get("profile"), "rss": body.get("rss"),
                         "timing_scope": "one observed minimum-threshold census; export may be included in layer durations; no measured higher-threshold build timings"},
    }
    return result, rows


def report(
    census_path: Path,
    queries_path: Path,
    *,
    thresholds: tuple[int, ...] = (100_000, 300_000, 1_000_000),
    lengths: tuple[int, ...] = (7, 12, 16),
    patterns: tuple[str, ...] = (".json", "zarr.json", ".npy"),
) -> dict:
    result, _ = _validated_report(census_path, queries_path, census_path.read_bytes(), queries_path.read_bytes(),
                                  thresholds=thresholds, lengths=lengths, patterns=patterns)
    return result


def compare(
    census_a: Path,
    queries_a: Path,
    census_b: Path,
    queries_b: Path,
) -> dict:
    """Compare all literal/frequency identities in the complete common domain."""
    pairs = [(census_a, queries_a), (census_b, queries_b)]
    pinned = [(census.read_bytes(), queries.read_bytes()) for census, queries in pairs]
    docs = [loads(raw, object_pairs_hook=_unique_object) for raw, _ in pinned]
    if any(not isinstance(doc, dict) for doc in docs):
        raise ValueError("comparison requires two accepted census artifacts")
    threshold = max(integer(doc.get("threshold_paths"), "census threshold", 1) for doc in docs)
    bounded = [integer(doc.get("max_chars"), "census maximum length", 1) for doc in docs if doc.get("max_chars") is not None]
    length = min(bounded) if bounded else None
    validated = [_validated_report(census, queries, raw, export, thresholds=(threshold,), lengths=(length,), patterns=())
                 for (census, queries), (raw, export) in zip(pairs, pinned, strict=True)]
    reports = [result for result, _ in validated]
    identities = [tuple(result[key] for key in ("target", "date", "snapshot_db")) for result in reports]
    if identities[0] != identities[1]:
        raise ValueError("census controls must have the same target, date and snapshot database")
    catalogs = [sorted((row["chars"], row["pattern"], row["direct_matching_paths"]) for row in rows
                       if within(row["chars"], length) and row["direct_matching_paths"] >= threshold)
                for _, rows in validated]
    if catalogs[0] != catalogs[1]:
        raise ValueError("accepted census controls disagree in the complete common T/L frequency domain")
    return {"schema": "hot-frequency-compare-v1", "agreement": True,
            **{key: reports[0][key] for key in ("target", "date", "snapshot_db")},
            "domain": {"threshold_paths": threshold, "max_chars": length}, "literals": len(catalogs[0]),
            "comparison": "entire sorted (chars,pattern,direct_matching_paths) registry in the complete common domain",
            "provenance": [result["provenance"] for result in reports]}
