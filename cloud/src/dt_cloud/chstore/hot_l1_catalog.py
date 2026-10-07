"""Read-only exact root views from explicitly registered verified artifacts.

No database client is imported or called. Validation checks the artifact
contract, not the producer's honesty: the operator must trust the supplied
files and their completed independent-oracle declaration.
"""

from dataclasses import dataclass
from datetime import date as Date
from json import loads
from pathlib import Path
from re import fullmatch
from typing import Iterable

SCOPE = "case-insensitive substring within names; directory hits cover descendants; bytes/objects only"
VALIDATION = "complete independent full-path frontier scan"


class CatalogRequest(ValueError):
    """The registered root-only catalog cannot answer this request."""


def _integer(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"hot L1 artifact {field} must be a nonnegative integer")
    return value


def _pattern(value: object) -> str:
    if not isinstance(value, str) or not value or "/" in value or len(value) > 512:
        raise ValueError("hot L1 requires one nonempty slash-free literal of at most 512 characters")
    return value.lower()


def _object(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"hot L1 artifact {field} must be an object")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("hot L1 artifact contains duplicate JSON keys")
        result[key] = value
    return result


@dataclass(frozen=True)
class _Bucket:
    pre: int
    post: int
    path: str
    b: int
    o: int

    @property
    def bounds(self) -> tuple[str, int, int]:
        return self.path, self.pre, self.post

    def body(self) -> dict:
        return {"pre": self.pre, "post": self.post, "path": self.path, "b": self.b, "o": self.o}


@dataclass(frozen=True)
class _Entry:
    target: str
    date: str
    pattern: str
    buckets: tuple[_Bucket, ...]

    @property
    def root(self) -> dict:
        return {"b": sum(bucket.b for bucket in self.buckets), "o": sum(bucket.o for bucket in self.buckets)}


def _entry(value: object) -> _Entry:
    data = _object(value, "body")
    if (data.get("schema") != "hot-l1-v1" or data.get("exact") is not True or
            data.get("incremental") is not False or data.get("scope") != SCOPE or data.get("validation") != VALIDATION):
        raise ValueError("hot L1 artifact lacks the exact independently verified names/directory contract")
    target, date = data.get("target"), data.get("date")
    if not isinstance(target, str) or fullmatch(r"[a-z][a-z0-9_]*", target) is None:
        raise ValueError("hot L1 artifact target is invalid")
    if not isinstance(date, str) or fullmatch(r"\d{4}-\d{2}-\d{2}", date) is None:
        raise ValueError("hot L1 artifact date is invalid")
    Date.fromisoformat(date)
    pattern = _pattern(data.get("pattern"))
    raw = data.get("buckets")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 6:
        raise ValueError("hot L1 artifact must contain one to six complete buckets")
    buckets = []
    for item in raw:
        row = _object(item, "bucket")
        path = row.get("path")
        if not isinstance(path, str) or not path or "/" in path:
            raise ValueError("hot L1 artifact bucket path is invalid")
        buckets.append(_Bucket(_integer(row.get("pre"), "bucket.pre"), _integer(row.get("post"), "bucket.post"), path,
                               _integer(row.get("b"), "bucket.b"), _integer(row.get("o"), "bucket.o")))
    ordered = sorted(buckets, key=lambda bucket: bucket.pre)
    if (len({bucket.path for bucket in buckets}) != len(buckets) or ordered[0].pre != 1 or
            any(bucket.pre > bucket.post for bucket in buckets) or
            any(left.post + 1 != right.pre for left, right in zip(ordered, ordered[1:]))):
        raise ValueError("hot L1 artifact buckets are not a disjoint contiguous partition")
    entry = _Entry(target, date, pattern, tuple(sorted(buckets, key=lambda bucket: bucket.path)))
    root = _object(data.get("root"), "root")
    actual = {field: _integer(root.get(field), f"root.{field}") for field in ("b", "o")}
    if actual != entry.root:
        raise ValueError("hot L1 artifact root totals disagree with complete buckets")
    return entry


class HotL1Catalog:
    def __init__(self, paths: Iterable[Path]) -> None:
        entries = {}
        target, bounds = None, None
        for path in paths:
            entry = _entry(loads(Path(path).read_text(), object_pairs_hook=_unique_object))
            key = entry.target, entry.date, entry.pattern
            if key in entries:
                raise ValueError("hot L1 catalog contains duplicate target/date/pattern entries")
            current = tuple(bucket.bounds for bucket in entry.buckets)
            if target is not None and target != entry.target:
                raise ValueError("hot L1 catalog requires one target")
            if bounds is not None and bounds != current:
                raise ValueError("hot L1 catalog bucket identities/bounds changed across artifacts")
            target, bounds = entry.target, current
            entries[key] = entry
        if target is None:
            raise ValueError("hot L1 catalog requires at least one completed artifact")
        self.target = target
        self._entries = entries

    @classmethod
    def load(cls, paths: Iterable[Path]) -> "HotL1Catalog":
        return cls(paths)

    def _get(
        self,
        date: str,
        pattern: str,
        path: str,
    ) -> _Entry:
        if path != "":
            raise CatalogRequest("hot L1 catalog serves the global root only")
        try:
            normalized = _pattern(pattern)
        except ValueError as e:
            raise CatalogRequest(str(e)) from e
        entry = self._entries.get((self.target, date, normalized))
        if entry is None:
            raise CatalogRequest("hot L1 pattern/date is not registered; no scan fallback")
        return entry

    def view(
        self,
        date: str,
        pattern: str,
        *,
        path: str = "",
    ) -> dict:
        entry = self._get(date, pattern, path)
        return {"schema": "hot-l1-catalog-v1", "target": entry.target, "date": entry.date, "pattern": entry.pattern, "path": "",
                "exact": True, "incremental": False, "levels": 1, "scope": SCOPE, "validation": VALIDATION,
                "source": "registered precomputed artifact", "root": entry.root, "buckets": [bucket.body() for bucket in entry.buckets]}

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
            raise CatalogRequest("hot L1 comparison requires before date to precede after date")
        rows = []
        for a, b in zip(before["buckets"], after["buckets"], strict=True):
            rows.append({"pre": a["pre"], "post": a["post"], "path": a["path"],
                         "before": {field: a[field] for field in ("b", "o")}, "after": {field: b[field] for field in ("b", "o")},
                         "delta": {field: b[field] - a[field] for field in ("b", "o")}})
        return {"schema": "hot-l1-catalog-diff-v1", "target": self.target, "pattern": before["pattern"], "path": "",
                "exact": True, "incremental": False, "levels": 1, "scope": SCOPE, "validation": VALIDATION,
                "source": "registered precomputed artifacts", "before": before, "after": after,
                "delta": {field: after["root"][field] - before["root"][field] for field in ("b", "o")}, "buckets": rows}
