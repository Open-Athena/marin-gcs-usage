"""Read-only completed native batch artifacts; no database or scan fallback.

Structural checks and supplied trusted references are not an independent scan
of the whole catalog. Loading trusts the operator's immutable artifact files.
The frozen bucket endpoint is trusted, as the artifact has no source rootpost.
"""

from dataclasses import dataclass
from datetime import date as Date
from json import loads
from pathlib import Path
from re import fullmatch
from typing import Iterable

from .hot_frequency import within
from .hot_frequency_registry import QUALIFICATION, UNION_SCHEMA, length_domain, union_header

from .hot_l1_catalog import CatalogRequest, SCOPE, _Bucket, _Entry, _integer, _object, _unique_object

VALIDATION = "complete catalog structure; supplied trusted references checked, not an independent full-catalog scan"
REFERENCE_VALIDATIONS = {"complete independent full-path frontier scan", "complete independent full-path leaf scan"}


def _identity(value: object) -> str:
    if not isinstance(value, str) or fullmatch(r"[a-z][a-z0-9_]*", value) is None:
        raise ValueError("batch catalog target/snapshot identity is invalid")
    return value


def _date(value: object) -> str:
    if not isinstance(value, str) or fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError("batch catalog scan/registry date is invalid")
    Date.fromisoformat(value)
    return value


def _literal(value: object) -> str:
    if not isinstance(value, str) or not value or "/" in value or "\0" in value or len(value) > 512:
        raise ValueError("batch catalog requires a nonempty NUL/slash-free literal of at most 512 characters")
    value.encode("utf-8")
    return value.lower()


def _weights(value: object, field: str) -> dict:
    raw = _object(value, field)
    return {key: _integer(raw.get(key), f"{field}.{key}") for key in ("b", "o")}


def _entry(
    row: dict,
    target: str,
    date: str,
    max_chars: int | None,
) -> _Entry:
    pattern = _literal(row.get("pattern"))
    if pattern != row["pattern"] or not within(len(pattern), max_chars):
        raise ValueError("batch catalog artifact literals must be normalized and within registry max_chars")
    raw = row.get("buckets")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 6:
        raise ValueError("batch catalog requires one to six complete buckets")
    buckets = []
    for item in raw:
        bucket = _object(item, "bucket")
        path = bucket.get("path")
        if not isinstance(path, str) or not path or "/" in path or "\0" in path:
            raise ValueError("batch catalog bucket path is invalid")
        path.encode("utf-8")
        weights = _weights(bucket, "bucket")
        buckets.append(_Bucket(_integer(bucket.get("pre"), "bucket.pre"), _integer(bucket.get("post"), "bucket.post"), path, **weights))
    ordered = sorted(buckets, key=lambda bucket: bucket.pre)
    if (ordered[0].pre != 1 or len({bucket.path for bucket in buckets}) != len(buckets) or
            any(bucket.pre > bucket.post for bucket in buckets) or
            any(a.post + 1 != b.pre for a, b in zip(ordered, ordered[1:]))):
        raise ValueError("batch catalog buckets are not a disjoint contiguous partition")
    entry = _Entry(target, date, pattern, tuple(sorted(buckets, key=lambda bucket: bucket.path)))
    if _weights(row.get("root"), "root") != entry.root:
        raise ValueError("batch catalog root totals disagree with complete buckets")
    return entry


@dataclass(frozen=True)
class _Snapshot:
    schema: str
    target: str
    date: str
    registry_date: str | None
    snapshot_db: str
    threshold_paths: int
    max_chars: int | None
    entries: tuple[_Entry, ...]
    references: tuple[tuple[str, str, str], ...]
    native: tuple[tuple[str, object], ...]
    registry_dates: tuple[str, ...] = ()

    def registry_identity(self) -> dict:
        return {"registry_dates": list(self.registry_dates)} if self.registry_dates else {"registry_date": self.registry_date}

    def registry(self) -> dict:
        return {**self.registry_identity(), "patterns": len(self.entries), "threshold_paths": self.threshold_paths, "max_chars": self.max_chars,
                **({"qualification": QUALIFICATION} if self.registry_dates else {})}

    def validation(self) -> dict:
        return {"description": VALIDATION, "independently_scanned_entire_catalog": False,
                "references": [{"path": path, "pattern": pattern, "validation": validation} for path, pattern, validation in self.references]}


def _snapshot(value: object) -> _Snapshot:
    body = _object(value, "body")
    schema = body.get("schema")
    if (schema not in ("hot-l1-batch-sql-v1", "hot-l1-batch-stream-v1") or body.get("exact") is not True or
            body.get("incremental") is not False or type(body.get("levels")) is not int or body["levels"] != 1 or body.get("scope") != SCOPE):
        raise ValueError("batch catalog requires the exact frozen L1 names/directory contract")
    target, date, db = _identity(body.get("target")), _date(body.get("date")), _identity(body.get("snapshot_db"))
    audit = _object(body.get("source_validation"), "source_validation")
    if (_integer(audit.get("rows"), "source_validation.rows") == 0 or
            _integer(audit.get("invalid_utf8_paths"), "source_validation.invalid_utf8_paths") != 0 or
            _integer(audit.get("invalid_scalar_rows"), "source_validation.invalid_scalar_rows") != 0):
        raise ValueError("batch catalog requires a completed valid source audit")
    _integer(audit.get("path_bytes"), "source_validation.path_bytes")
    queries = _object(body.get("queries"), "queries")
    header = _object(queries.get("header"), "queries.header")
    registry_dates = ()
    if header.get("schema") == UNION_SCHEMA:
        union_header(header, target)
        registry_dates, registry_date = tuple(header["dates"]), None
        if ("registry_date" in queries or queries.get("registry_dates", header["dates"]) != header["dates"] or
                date not in registry_dates or header["sources"][registry_dates.index(date)]["snapshot_db"] != db):
            raise ValueError("batch catalog union registry dates/snapshot declarations disagree")
    else:
        if (set(header) != {"schema", "target", "date", "threshold_paths", "max_chars"} or
                header["schema"] != "hot-frequency-queries-v1" or header["target"] != target):
            raise ValueError("batch catalog requires matching completed registry target metadata")
        registry_date = _date(header["date"])
        if queries.get("registry_date", registry_date) != registry_date:
            raise ValueError("batch catalog registry date declarations disagree")
    threshold, max_chars = _integer(header["threshold_paths"], "threshold_paths"), header["max_chars"]
    if threshold == 0 or not length_domain(max_chars):
        raise ValueError("batch catalog registry threshold/max_chars is invalid")
    count = _integer(body.get("compiled_patterns"), "compiled_patterns")
    raw = body.get("results")
    if not isinstance(raw, list) or count == 0 or count != len(raw) or _integer(queries.get("patterns"), "queries.patterns") != count:
        raise ValueError("batch catalog registry/result counts disagree")
    if registry_dates and count > header["max_patterns"]:
        raise ValueError("batch catalog union exceeds its accepted-pattern cap")
    if registry_dates and count > sum(source["queries"]["patterns"] for source in header["sources"]):
        raise ValueError("batch catalog union count exceeds its accepted source registries")
    native = ()
    if schema == "hot-l1-batch-stream-v1":
        stats = _object(body.get("native"), "native")
        if (stats.get("schema") != "hot-l1-native-stream-v1" or stats.get("exact") is not True or
                stats.get("incremental") is not False or type(stats.get("levels")) is not int or stats["levels"] != 1 or
                not isinstance(body.get("source_query_id"), str) or not body["source_query_id"]):
            raise ValueError("batch catalog stream lacks completed native provenance")
        for field in ("nodes_read", "registered_predicates", "peak_stack", "peak_active", "native_peak_rss_bytes"):
            _integer(stats.get(field), f"native.{field}")
        if (stats["nodes_read"] != audit["rows"] or stats["registered_predicates"] != count or
                not 1 <= stats["peak_stack"] <= stats["nodes_read"] or stats["peak_active"] > count):
            raise ValueError("batch catalog native stream counts disagree with source/registry")
        native = tuple((field, stats[field]) for field in ("schema", "exact", "incremental", "levels", "nodes_read", "registered_predicates", "peak_stack", "peak_active", "native_peak_rss_bytes"))
    expected_engine = "stream" if native else "sql"
    if body.get("engine", expected_engine) != expected_engine:
        raise ValueError("batch catalog engine declaration disagrees with artifact schema")
    entries = []
    for q, item in enumerate(raw, 1):
        row = _object(item, "result")
        if _integer(row.get("predicate_id"), "predicate_id") != q:
            raise ValueError("batch catalog predicate IDs must be complete and ordered from one")
        entries.append(_entry(row, target, date, max_chars))
    if len({entry.pattern for entry in entries}) != count:
        raise ValueError("batch catalog contains duplicate normalized literals")
    validation = _object(body.get("validation"), "validation")
    if (set(validation) != {"description", "references", "independently_scanned_entire_catalog"} or
            validation["description"] != VALIDATION or validation["independently_scanned_entire_catalog"] is not False or
            not isinstance(validation["references"], list)):
        raise ValueError("batch catalog requires honest completed benchmark validation metadata")
    references = []
    patterns = {entry.pattern for entry in entries}
    for raw_ref in validation["references"]:
        ref = _object(raw_ref, "reference")
        if (set(ref) != {"path", "pattern", "validation"} or not isinstance(ref["path"], str) or not ref["path"] or
                not isinstance(ref["pattern"], str) or ref["pattern"] not in patterns or
                not isinstance(ref["validation"], str) or ref["validation"] not in REFERENCE_VALIDATIONS):
            raise ValueError("batch catalog reference metadata is invalid")
        references.append((ref["path"], ref["pattern"], ref["validation"]))
    return _Snapshot(schema, target, date, registry_date, db, threshold, max_chars, tuple(entries), tuple(references), native, registry_dates)


class HotL1BatchCatalog:
    def __init__(self, paths: Iterable[Path]) -> None:
        self._initialize(_snapshot(loads(Path(path).read_text(), object_pairs_hook=_unique_object)) for path in paths)

    def _initialize(self, sources: Iterable[_Snapshot]) -> None:
        snapshots, entries = {}, {}
        target, bounds = None, None
        for snapshot in sources:
            if snapshot.date in snapshots:
                raise ValueError("batch catalog contains duplicate scan dates")
            if target is not None and target != snapshot.target:
                raise ValueError("batch catalog requires one frozen generation target")
            for q, entry in enumerate(snapshot.entries, 1):
                current = tuple(bucket.bounds for bucket in entry.buckets)
                if bounds is not None and bounds != current:
                    raise ValueError("batch catalog bucket identities/bounds changed across results or dates")
                bounds = current
                entries[(snapshot.date, entry.pattern)] = q, entry
            target = snapshot.target
            snapshots[snapshot.date] = snapshot
        if target is None:
            raise ValueError("batch catalog requires at least one completed artifact")
        self.target, self._snapshots, self._entries = target, snapshots, entries

    @classmethod
    def load(cls, paths: Iterable[Path]) -> "HotL1BatchCatalog":
        return cls(paths)

    @classmethod
    def from_bytes(cls, blobs: Iterable[bytes]) -> "HotL1BatchCatalog":
        """Parse the same immutable bytes a caller verified, with no path reread."""
        def snapshots() -> Iterable[_Snapshot]:
            for blob in blobs:
                if not isinstance(blob, bytes):
                    raise ValueError("batch catalog verified inputs must be immutable bytes")
                yield _snapshot(loads(blob.decode("utf-8"), object_pairs_hook=_unique_object))

        catalog = cls.__new__(cls)
        catalog._initialize(snapshots())
        return catalog

    def metadata(self) -> dict:
        return {"schema": "hot-l1-batch-catalog-registry-v1", "target": self.target,
                "dates": [{"date": snapshot.date, "snapshot_db": snapshot.snapshot_db, "artifact_schema": snapshot.schema, **snapshot.registry()}
                          for _, snapshot in sorted(self._snapshots.items())]}

    def registered_patterns(self, date: str) -> tuple[str, ...]:
        """Normalized literals in this scan's registry order, without file IO."""
        snapshot = self._snapshots.get(date)
        if snapshot is None:
            raise CatalogRequest("hot L1 batch date is not registered; no scan fallback")
        return tuple(entry.pattern for entry in snapshot.entries)

    def view(
        self,
        date: str,
        pattern: str,
        *,
        path: str = "",
    ) -> dict:
        if path != "":
            raise CatalogRequest("hot L1 batch catalog serves the global root only")
        try:
            pattern = _literal(pattern)
        except ValueError as e:
            raise CatalogRequest(str(e)) from e
        selected = self._entries.get((date, pattern))
        if selected is None:
            raise CatalogRequest("hot L1 batch pattern/date is not registered; no scan fallback")
        q, entry = selected
        snapshot = self._snapshots[date]
        body = {"schema": "hot-l1-batch-catalog-v1", "artifact_schema": snapshot.schema, "target": self.target, "date": date, **snapshot.registry_identity(), "pattern": pattern, "path": "",
                "exact": True, "incremental": False, "levels": 1, "scope": SCOPE, "registry": {**snapshot.registry(), "predicate_id": q},
                "validation": snapshot.validation(), "source": "registered precomputed batch artifact",
                "root": entry.root, "buckets": [bucket.body() for bucket in entry.buckets]}
        if snapshot.native:
            body["native"] = dict(snapshot.native)
        return body

    def diff(
        self,
        before_date: str,
        after_date: str,
        pattern: str,
        *,
        path: str = "",
    ) -> dict:
        before, after = self.view(before_date, pattern, path=path), self.view(after_date, pattern, path=path)
        if before_date >= after_date:
            raise CatalogRequest("hot L1 batch comparison requires before date to precede after date")
        rows = [{"pre": a["pre"], "post": a["post"], "path": a["path"],
                 "before": {key: a[key] for key in ("b", "o")}, "after": {key: b[key] for key in ("b", "o")},
                 "delta": {key: b[key] - a[key] for key in ("b", "o")}}
                for a, b in zip(before["buckets"], after["buckets"], strict=True)]
        return {"schema": "hot-l1-batch-catalog-diff-v1", "target": self.target, "pattern": before["pattern"], "path": "",
                "exact": True, "incremental": False, "levels": 1, "scope": SCOPE, "source": "registered precomputed batch artifacts",
                "before": before, "after": after, "delta": {key: after["root"][key] - before["root"][key] for key in ("b", "o")}, "buckets": rows}
