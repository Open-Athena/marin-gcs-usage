"""Registered aggregate reads are exact, isolated and do not scan on misses."""

from copy import deepcopy
from json import dumps
from pathlib import Path

import pytest
from click.testing import CliRunner

from dt_cloud.chstore.hot_l1_catalog import CatalogRequest, HotL1Catalog, SCOPE, VALIDATION
from dt_cloud.cli import main


def artifact(date: str = "2026-10-04", pattern: str = ".json") -> dict:
    return {"schema": "hot-l1-v1", "target": "fixture", "snapshot_db": "snapshot", "date": date, "pattern": pattern,
            "exact": True, "incremental": False, "scope": SCOPE, "validation": VALIDATION,
            "root": {"b": 5, "o": 5}, "buckets": [
                {"pre": 1, "post": 3, "path": "a", "b": 5, "o": 2},
                {"pre": 4, "post": 7, "path": "b", "b": 0, "o": 3},
            ]}


def write_artifact(
    tmp_path: Path,
    name: str,
    body: dict,
) -> Path:
    path = tmp_path / name
    path.write_text(dumps(body) + "\n")
    return path


def expected_view(date: str = "2026-10-04") -> dict:
    return {"schema": "hot-l1-catalog-v1", "target": "fixture", "date": date, "pattern": ".json", "path": "",
            "exact": True, "incremental": False, "levels": 1,
            "scope": "case-insensitive substring within names; directory hits cover descendants; bytes/objects only",
            "validation": "complete independent full-path frontier scan", "source": "registered precomputed artifact",
            "root": {"b": 5, "o": 5}, "buckets": [
                {"pre": 1, "post": 3, "path": "a", "b": 5, "o": 2},
                {"pre": 4, "post": 7, "path": "b", "b": 0, "o": 3},
            ]}


def test_complete_root_and_diff_preserve_zero_counts_and_negative_changes(tmp_path: Path) -> None:
    before = artifact()
    after = artifact("2026-10-05")
    after["root"] = {"b": 7, "o": 5}
    after["buckets"][0].update(b=0, o=1)
    after["buckets"][1].update(b=7, o=4)
    catalog = HotL1Catalog.load([write_artifact(tmp_path, "before.json", before), write_artifact(tmp_path, "after.json", after)])
    assert catalog.view("2026-10-04", ".JSON") == expected_view()
    expected_after = expected_view("2026-10-05")
    expected_after["root"] = {"b": 7, "o": 5}
    expected_after["buckets"][0].update(b=0, o=1)
    expected_after["buckets"][1].update(b=7, o=4)
    assert catalog.diff("2026-10-04", "2026-10-05", ".JSON") == {
        "schema": "hot-l1-catalog-diff-v1", "target": "fixture", "pattern": ".json", "path": "",
        "exact": True, "incremental": False, "levels": 1,
        "scope": "case-insensitive substring within names; directory hits cover descendants; bytes/objects only",
        "validation": "complete independent full-path frontier scan", "source": "registered precomputed artifacts",
        "before": expected_view(), "after": expected_after, "delta": {"b": 2, "o": 0}, "buckets": [
            {"pre": 1, "post": 3, "path": "a", "before": {"b": 5, "o": 2}, "after": {"b": 0, "o": 1}, "delta": {"b": -5, "o": -1}},
            {"pre": 4, "post": 7, "path": "b", "before": {"b": 0, "o": 3}, "after": {"b": 7, "o": 4}, "delta": {"b": 7, "o": 1}},
        ],
    }


@pytest.mark.parametrize("pattern", ["%", "_", "a*b", " two words ", "ÅrO"])
def test_query_normalization_keeps_literal_punctuation_and_whitespace(tmp_path: Path, pattern: str) -> None:
    catalog = HotL1Catalog.load([write_artifact(tmp_path, "body.json", artifact(pattern=pattern))])
    assert catalog.view("2026-10-04", pattern.upper())["pattern"] == pattern.lower()


def test_query_does_not_read_files_or_network_and_returns_detached_bodies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_artifact(tmp_path, "body.json", artifact())
    catalog = HotL1Catalog.load([path])
    path.write_text("not JSON\n")

    def no_io(*args: object, **kwargs: object) -> None:
        raise AssertionError("catalog query attempted I/O")

    monkeypatch.setattr(Path, "read_text", no_io)
    monkeypatch.setattr("socket.create_connection", no_io)
    first = catalog.view("2026-10-04", ".json")
    first["root"]["b"] = 999
    first["buckets"][0]["o"] = 999
    assert catalog.view("2026-10-04", ".json") == expected_view()
    with pytest.raises(CatalogRequest) as caught:
        catalog.view("2026-10-05", ".json")
    assert str(caught.value) == "hot L1 pattern/date is not registered; no scan fallback"


@pytest.mark.parametrize("date,pattern,path,message", [
    ("2026-10-03", ".json", "", "hot L1 pattern/date is not registered; no scan fallback"),
    ("2026-10-04", ".npy", "", "hot L1 pattern/date is not registered; no scan fallback"),
    ("2026-10-04", ".json", "a", "hot L1 catalog serves the global root only"),
    ("2026-10-04", ".json", "/", "hot L1 catalog serves the global root only"),
    ("2026-10-04", "", "", "hot L1 requires one nonempty slash-free literal of at most 512 characters"),
    ("2026-10-04", "a/b", "", "hot L1 requires one nonempty slash-free literal of at most 512 characters"),
])
def test_unregistered_or_nonroot_requests_refuse(
    tmp_path: Path,
    date: str,
    pattern: str,
    path: str,
    message: str,
) -> None:
    catalog = HotL1Catalog.load([write_artifact(tmp_path, "body.json", artifact())])
    with pytest.raises(CatalogRequest) as caught:
        catalog.view(date, pattern, path=path)
    assert str(caught.value) == message


@pytest.mark.parametrize("field,value,message", [
    (("schema",), "other", "hot L1 artifact lacks the exact independently verified names/directory contract"),
    (("exact",), 1, "hot L1 artifact lacks the exact independently verified names/directory contract"),
    (("incremental",), 0, "hot L1 artifact lacks the exact independently verified names/directory contract"),
    (("validation",), "totals only", "hot L1 artifact lacks the exact independently verified names/directory contract"),
    (("scope",), "leaf-only", "hot L1 artifact lacks the exact independently verified names/directory contract"),
    (("target",), "bad-target", "hot L1 artifact target is invalid"),
    (("date",), "yesterday", "hot L1 artifact date is invalid"),
    (("root", "b"), True, "hot L1 artifact root.b must be a nonnegative integer"),
    (("root", "o"), -1, "hot L1 artifact root.o must be a nonnegative integer"),
    (("root", "b"), 6, "hot L1 artifact root totals disagree with complete buckets"),
    (("buckets", 0, "b"), 5.0, "hot L1 artifact bucket.b must be a nonnegative integer"),
    (("buckets", 0, "o"), False, "hot L1 artifact bucket.o must be a nonnegative integer"),
    (("buckets", 0, "pre"), True, "hot L1 artifact bucket.pre must be a nonnegative integer"),
    (("buckets", 0, "pre"), 2, "hot L1 artifact buckets are not a disjoint contiguous partition"),
    (("buckets", 0, "post"), -1, "hot L1 artifact bucket.post must be a nonnegative integer"),
    (("buckets", 0, "post"), 0, "hot L1 artifact buckets are not a disjoint contiguous partition"),
    (("buckets", 1, "pre"), 3, "hot L1 artifact buckets are not a disjoint contiguous partition"),
    (("buckets", 1, "pre"), 5, "hot L1 artifact buckets are not a disjoint contiguous partition"),
    (("buckets", 1, "path"), "a", "hot L1 artifact buckets are not a disjoint contiguous partition"),
    (("buckets", 1, "path"), "a/b", "hot L1 artifact bucket path is invalid"),
    (("buckets",), [], "hot L1 artifact must contain one to six complete buckets"),
])
def test_malformed_artifact_refuses(
    tmp_path: Path,
    field: tuple,
    value: object,
    message: str,
) -> None:
    body = artifact()
    parent = body
    for key in field[:-1]:
        parent = parent[key]
    parent[field[-1]] = value
    with pytest.raises(ValueError) as caught:
        HotL1Catalog.load([write_artifact(tmp_path, "body.json", body)])
    assert str(caught.value) == message


@pytest.mark.parametrize("change,message", [
    ("duplicate", "hot L1 catalog contains duplicate target/date/pattern entries"),
    ("target", "hot L1 catalog requires one target"),
    ("bounds", "hot L1 catalog bucket identities/bounds changed across artifacts"),
    ("path", "hot L1 catalog bucket identities/bounds changed across artifacts"),
])
def test_duplicate_or_incompatible_artifacts_refuse(
    tmp_path: Path,
    change: str,
    message: str,
) -> None:
    before, after = artifact(), artifact("2026-10-05")
    if change == "duplicate":
        after.update(date=before["date"], pattern=".JSON")
    elif change == "target":
        after["target"] = "another"
    elif change == "bounds":
        after["buckets"][1]["post"] = 8
    else:
        after["buckets"][1]["path"] = "c"
    with pytest.raises(ValueError) as caught:
        HotL1Catalog.load([write_artifact(tmp_path, "before.json", before), write_artifact(tmp_path, "after.json", after)])
    assert str(caught.value) == message


def test_incomplete_or_ambiguous_input_refuses(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as caught:
        HotL1Catalog.load([])
    assert str(caught.value) == "hot L1 catalog requires at least one completed artifact"
    path = tmp_path / "duplicate.json"
    path.write_text('{"exact":true,"exact":false}\n')
    with pytest.raises(ValueError) as caught:
        HotL1Catalog.load([path])
    assert str(caught.value) == "hot L1 artifact contains duplicate JSON keys"


def test_diff_refuses_reversed_dates_or_missing_side(tmp_path: Path) -> None:
    paths = [write_artifact(tmp_path, f"{date}.json", artifact(date)) for date in ("2026-10-04", "2026-10-05")]
    catalog = HotL1Catalog.load(paths)
    with pytest.raises(CatalogRequest) as caught:
        catalog.diff("2026-10-05", "2026-10-04", ".json")
    assert str(caught.value) == "hot L1 comparison requires before date to precede after date"
    with pytest.raises(CatalogRequest) as caught:
        catalog.diff("2026-10-03", "2026-10-04", ".json")
    assert str(caught.value) == "hot L1 pattern/date is not registered; no scan fallback"


def test_one_bucket_contract_is_accepted(tmp_path: Path) -> None:
    body = deepcopy(artifact())
    body["buckets"] = body["buckets"][:1]
    body["root"] = {"b": 5, "o": 2}
    catalog = HotL1Catalog.load([write_artifact(tmp_path, "one.json", body)])
    expected = expected_view()
    expected["buckets"] = expected["buckets"][:1]
    expected["root"] = {"b": 5, "o": 2}
    assert catalog.view("2026-10-04", ".json") == expected


@pytest.mark.parametrize("compare_from", [None, "2026-10-04"])
def test_cli_forwards_root_or_diff_and_prints_only_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    compare_from: str | None,
) -> None:
    calls = []
    paths = [tmp_path / "before.json", tmp_path / "after.json"]
    body = {"kind": "root" if compare_from is None else "diff", "fixture": True}

    class Catalog:
        @classmethod
        def load(cls, artifacts: tuple[Path, ...]) -> "Catalog":
            calls.append(("load", artifacts))
            return cls()

        def view(
            self,
            date: str,
            pattern: str,
            *,
            path: str,
        ) -> dict:
            calls.append(("view", date, pattern, path))
            return body

        def diff(
            self,
            before_date: str,
            after_date: str,
            pattern: str,
            *,
            path: str,
        ) -> dict:
            calls.append(("diff", before_date, after_date, pattern, path))
            return body

    monkeypatch.setattr("dt_cloud.chstore.hot_l1_catalog.HotL1Catalog", Catalog)
    args = ["ch-hot-l1-read", "-d", "2026-10-05", "-n", ".JSON", "-p", ""]
    if compare_from:
        args += ["-D", compare_from]
    result = CliRunner().invoke(main, [*args, *map(str, paths)])
    expected_call = ("view", "2026-10-05", ".JSON", "") if compare_from is None else ("diff", "2026-10-04", "2026-10-05", ".JSON", "")
    assert calls == [("load", tuple(paths)), expected_call]
    assert (result.exit_code, result.exception, result.stdout, result.stderr) == (0, None, dumps(body) + "\n", "")


def test_cli_complete_registered_root_output(tmp_path: Path) -> None:
    path = write_artifact(tmp_path, "body.json", artifact())
    result = CliRunner().invoke(main, ["ch-hot-l1-read", "-d", "2026-10-04", "-n", ".JSON", str(path)])
    assert (result.exit_code, result.exception, result.stdout, result.stderr) == (0, None, dumps(expected_view()) + "\n", "")


@pytest.mark.parametrize("args,message", [
    (["-p", "a"], "hot L1 catalog serves the global root only"),
    (["-n", ".npy"], "hot L1 pattern/date is not registered; no scan fallback"),
    (["-D", "2026-10-03"], "hot L1 pattern/date is not registered; no scan fallback"),
])
def test_cli_nonroot_and_cold_requests_refuse_without_output(
    tmp_path: Path,
    args: list[str],
    message: str,
) -> None:
    path = write_artifact(tmp_path, "body.json", artifact())
    result = CliRunner().invoke(main, ["ch-hot-l1-read", "-d", "2026-10-04", "-n", ".json", *args, str(path)])
    assert (result.exit_code, type(result.exception), str(result.exception), result.stdout, result.stderr) == (1, CatalogRequest, message, "", "")
