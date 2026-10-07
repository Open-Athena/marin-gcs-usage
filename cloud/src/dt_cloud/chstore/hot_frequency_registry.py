"""Shared completed single-date and explicitly multi-date registry validation."""

from datetime import date as Date
from io import StringIO
from json import loads
from pathlib import Path
from re import fullmatch

from .hot_frequency import MAX_CHARS
from .hot_l1_catalog import _unique_object
from .narrow import identifier

V1_SCHEMA = "hot-frequency-queries-v1"
UNION_SCHEMA = "hot-frequency-union-queries-v1"
UNION_CAP = 500_000
FREQUENCY_SEMANTICS = "dated exact direct frequencies; null means below that source minimum, never zero; hot on any source date"
QUALIFICATION = "hot on at least one source registry date; not necessarily hot on this scan"


class PinnedExport:
    """An open()-compatible immutable UTF-8 input for existing registry readers."""

    def __init__(self, raw: bytes) -> None:
        self.text = raw.decode("utf-8")

    def open(self) -> StringIO:
        return StringIO(self.text)


def integer(value: object, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def iso_date(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return Date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def union_header(header: object, target: str) -> dict:
    """Check bounded source provenance without asserting an independent scan."""
    message = "union registry requires matching sorted dates and complete valid source provenance"
    if (not isinstance(header, dict) or set(header) != {"schema", "target", "dates", "threshold_paths", "max_chars", "max_patterns", "sources", "frequency_semantics"} or
            header["schema"] != UNION_SCHEMA or header["target"] != target or
            header["frequency_semantics"] != FREQUENCY_SEMANTICS or
            not integer(header["threshold_paths"], 1) or not integer(header["max_chars"], 1) or header["max_chars"] > MAX_CHARS or
            not integer(header["max_patterns"], 1) or header["max_patterns"] > UNION_CAP):
        raise ValueError(message)
    dates, sources = header["dates"], header["sources"]
    if (not isinstance(dates, list) or not dates or any(not iso_date(date) for date in dates) or dates != sorted(set(dates)) or
            not isinstance(sources, list) or len(sources) != len(dates)):
        raise ValueError(message)
    for date, source in zip(dates, sources, strict=True):
        if (not isinstance(source, dict) or set(source) != {"date", "snapshot_db", "threshold_paths", "max_chars", "accepted_hot_pattern_cap", "census", "queries"} or
                source["date"] != date or not isinstance(source["snapshot_db"], str) or
                not integer(source["threshold_paths"], 1) or source["threshold_paths"] > header["threshold_paths"] or
                not integer(source["max_chars"], 1) or not header["max_chars"] <= source["max_chars"] <= MAX_CHARS):
            raise ValueError(message)
        identifier(source["snapshot_db"])
        for field, keys in (("census", {"sha256", "bytes"}), ("queries", {"sha256", "bytes", "patterns"})):
            proof = source[field]
            if (not isinstance(proof, dict) or set(proof) != keys or not isinstance(proof["sha256"], str) or
                    fullmatch(r"[a-f0-9]{64}", proof["sha256"]) is None or not integer(proof["bytes"], 1)):
                raise ValueError(message)
        count, cap = source["queries"]["patterns"], source["accepted_hot_pattern_cap"]
        if not integer(count) or (cap is not None and (not integer(cap, 1) or count > cap)):
            raise ValueError(message)
    return header


def load_queries(
    path: Path,
    target: str,
    date: str,
    *,
    registry_date: str | None = None,
    allow_union: bool = True,
) -> tuple[dict, tuple[str, ...]]:
    """Never turn a truncated, misqualified or malformed registry into []."""
    identifier(target)
    if not iso_date(date):
        raise ValueError("batch date must be an ISO scan date")
    expected_date = date if registry_date is None else registry_date
    if not iso_date(expected_date):
        raise ValueError("registry date must be an ISO scan date")
    patterns, seen, footer = [], set(), None
    with path.open() as source:
        header = loads(source.readline(), object_pairs_hook=_unique_object)
        union = isinstance(header, dict) and header.get("schema") == UNION_SCHEMA
        if union:
            if not allow_union:
                raise ValueError("this operation requires a single-date hot-frequency-queries-v1 export")
            union_header(header, target)
            if registry_date is not None or date not in header["dates"]:
                raise ValueError("union batch date must be a source date and cannot use a single registry_date override")
        elif (not isinstance(header, dict) or set(header) != {"schema", "target", "date", "threshold_paths", "max_chars"} or
                header["schema"] != V1_SCHEMA or header["target"] != target or header["date"] != expected_date or
                not integer(header["threshold_paths"], 1) or not integer(header["max_chars"], 1) or header["max_chars"] > MAX_CHARS):
            raise ValueError("batch requires a matching completed hot-frequency query export header")
        known = {date: 0 for date in header["dates"]} if union else {}
        previous = None
        for line in source:
            row = loads(line, object_pairs_hook=_unique_object)
            if footer is not None:
                raise ValueError("hot query export has data after its completion footer")
            if isinstance(row, dict) and set(row) == {"complete", "patterns"}:
                footer = row
                continue
            if not isinstance(row, dict) or set(row) != {"chars", "pattern", "direct_matching_paths"}:
                raise ValueError("hot query export contains an invalid query record")
            pattern, chars = row["pattern"], row["chars"]
            if (not isinstance(pattern, str) or not integer(chars, 1) or chars > header["max_chars"] or
                    len(pattern) != chars or "/" in pattern or "\0" in pattern or pattern.lower() != pattern or pattern in seen):
                raise ValueError(f"hot query export literals must be unique, normalized, NUL/slash-free hot queries of lengths 1..{MAX_CHARS}")
            pattern.encode("utf-8")
            if union and previous is not None and (chars, pattern) <= previous:
                raise ValueError("union query records must be sorted by length and literal")
            previous = chars, pattern
            frequency = row["direct_matching_paths"]
            if union:
                if (not isinstance(frequency, dict) or set(frequency) != set(header["dates"]) or
                        any(value is not None and not integer(value, declaration["threshold_paths"])
                            for declaration in header["sources"] for value in (frequency[declaration["date"]],)) or
                        not any(value is not None and value >= header["threshold_paths"] for value in frequency.values())):
                    raise ValueError("union query requires valid dated frequencies qualifying on at least one source date")
                for date, value in frequency.items():
                    known[date] += value is not None
            elif not integer(frequency, header["threshold_paths"]):
                raise ValueError(f"hot query export literals must be unique, normalized, NUL/slash-free hot queries of lengths 1..{MAX_CHARS}")
            patterns.append(pattern)
            seen.add(pattern)
            if union and len(patterns) > header["max_patterns"]:
                raise ValueError("union registry exceeds its accepted-pattern cap")
    if (footer is None or footer["complete"] is not True or not integer(footer["patterns"]) or footer["patterns"] != len(patterns)):
        raise ValueError("hot query export lacks a valid exact-count completion footer")
    if union and any(known[declaration["date"]] > declaration["queries"]["patterns"] for declaration in header["sources"]):
        raise ValueError("union known-frequency counts exceed their accepted source registries")
    return header, tuple(patterns)
