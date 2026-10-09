"""Precomputed batch queries retain exact weights and honest provenance."""

from copy import deepcopy
from json import dumps
from pathlib import Path

from click.testing import CliRunner
import pytest

from dt_cloud.chstore.hot_l1_batch_catalog import HotL1BatchCatalog, VALIDATION
from dt_cloud.chstore.hot_l1_catalog import CatalogRequest, SCOPE


def artifact(date: str = "2026-10-05", *, registry_date: str = "2026-10-05") -> dict:
    after = date == "2026-10-05"
    buckets = [{"pre": 1, "post": 4, "path": "a", "b": 2 if after else 8, "o": 1 if after else 3},
               {"pre": 5, "post": 8, "path": "b", "b": 0, "o": 4 if after else 2}]
    return {"schema": "hot-l1-batch-sql-v1", "target": "fleet", "snapshot_db": "snapshot_" + date.replace("-", ""),
            "date": date, "exact": True, "incremental": False, "levels": 1, "scope": SCOPE,
            "compiled_patterns": 2, "source_validation": {"rows": 9, "invalid_utf8_paths": 0, "invalid_scalar_rows": 0, "path_bytes": 80},
            "queries": {"path": "queries.jsonl", "patterns": 2,
                        "header": {"schema": "hot-frequency-queries-v1", "target": "fleet", "date": registry_date, "threshold_paths": 10, "max_chars": 7}},
            "validation": {"description": VALIDATION, "independently_scanned_entire_catalog": False,
                           "references": [{"path": "json-reference.json", "pattern": ".json", "validation": "complete independent full-path frontier scan"}]},
            "results": [{"predicate_id": 1, "pattern": ".json", "root": {"b": sum(row["b"] for row in buckets), "o": sum(row["o"] for row in buckets)}, "buckets": buckets},
                        {"predicate_id": 2, "pattern": ".npy", "root": {"b": 0, "o": 0}, "buckets": [{**row, "b": 0, "o": 0} for row in buckets]}]}


def write(path: Path, body: dict) -> Path:
    path.write_text(dumps(body) + "\n")
    return path


@pytest.mark.parametrize("pattern", ["zarr.json", "å" * 32])
def test_long_literal_catalog_preserves_exact_identity_and_totals(tmp_path: Path, pattern: str) -> None:
    body = artifact()
    body["queries"]["header"]["max_chars"] = 32
    body["results"][0]["pattern"] = pattern
    body["validation"]["references"][0]["pattern"] = pattern
    catalog = HotL1BatchCatalog.load([write(tmp_path / "long.json", body)])
    expected = expected_view(body)
    expected["registry"]["max_chars"] = 32
    assert catalog.view("2026-10-05", pattern.upper()) == expected


def expected_view(body: dict, q: int = 1) -> dict:
    row = body["results"][q - 1]
    header = body["queries"]["header"]
    result = {"schema": "hot-l1-batch-catalog-v1", "artifact_schema": body["schema"], "target": "fleet", "date": body["date"], "registry_date": header["date"], "pattern": row["pattern"], "path": "",
            "exact": True, "incremental": False, "levels": 1, "scope": SCOPE,
            "registry": {"registry_date": header["date"], "patterns": 2, "threshold_paths": 10, "max_chars": 7, "predicate_id": q},
            "validation": body["validation"], "source": "registered precomputed batch artifact", "root": row["root"], "buckets": row["buckets"]}
    if body["schema"] == "hot-l1-batch-stream-v1":
        result["native"] = body["native"]
    return result


def test_root_query_is_exact_normalized_and_honest_about_references(tmp_path: Path) -> None:
    body = artifact()
    catalog = HotL1BatchCatalog.load([write(tmp_path / "after.json", body)])
    assert catalog.view("2026-10-05", ".JSON") == expected_view(body)
    assert catalog.view("2026-10-05", ".npy") == expected_view(body, 2)
    assert catalog.metadata() == {"schema": "hot-l1-batch-catalog-registry-v1", "target": "fleet", "dates": [
        {"date": "2026-10-05", "snapshot_db": "snapshot_20261005", "artifact_schema": "hot-l1-batch-sql-v1", "registry_date": "2026-10-05", "patterns": 2, "threshold_paths": 10, "max_chars": 7},
    ]}


def test_diff_preserves_zero_byte_counts_and_negative_deltas(tmp_path: Path) -> None:
    before, after = artifact("2026-10-04"), artifact()
    catalog = HotL1BatchCatalog.load([write(tmp_path / "after.json", after), write(tmp_path / "before.json", before)])
    assert catalog.diff("2026-10-04", "2026-10-05", ".JSON") == {
        "schema": "hot-l1-batch-catalog-diff-v1", "target": "fleet", "pattern": ".json", "path": "",
        "exact": True, "incremental": False, "levels": 1, "scope": SCOPE, "source": "registered precomputed batch artifacts",
        "before": expected_view(before), "after": expected_view(after), "delta": {"b": -6, "o": 0}, "buckets": [
            {"pre": 1, "post": 4, "path": "a", "before": {"b": 8, "o": 3}, "after": {"b": 2, "o": 1}, "delta": {"b": -6, "o": -2}},
            {"pre": 5, "post": 8, "path": "b", "before": {"b": 0, "o": 2}, "after": {"b": 0, "o": 4}, "delta": {"b": 0, "o": 2}},
        ],
    }


def test_registry_provenance_can_be_newer_than_scan_and_refs_can_be_empty(tmp_path: Path) -> None:
    body = artifact("2026-10-04")
    body["validation"]["references"] = []
    catalog = HotL1BatchCatalog.load([write(tmp_path / "before.json", body)])
    assert catalog.view("2026-10-04", ".json") == expected_view(body)
    assert catalog.metadata()["dates"] == [{"date": "2026-10-04", "snapshot_db": "snapshot_20261004", "artifact_schema": "hot-l1-batch-sql-v1", "registry_date": "2026-10-05", "patterns": 2, "threshold_paths": 10, "max_chars": 7}]


def stream_artifact() -> dict:
    body = artifact()
    body.update(schema="hot-l1-batch-stream-v1", engine="stream", source_query_id="hot_l1_stream_fixture",
                native={"schema": "hot-l1-native-stream-v1", "exact": True, "incremental": False, "levels": 1,
                        "nodes_read": 9, "registered_predicates": 2, "peak_stack": 3, "peak_active": 2, "native_peak_rss_bytes": 1024})
    return body


def test_stream_reader_retains_native_counts_without_upgrading_validation(tmp_path: Path) -> None:
    body = stream_artifact()
    catalog = HotL1BatchCatalog.load([write(tmp_path / "stream.json", body)])
    assert catalog.view("2026-10-05", ".json") == expected_view(body)
    assert catalog.metadata()["dates"] == [{"date": "2026-10-05", "snapshot_db": "snapshot_20261005", "artifact_schema": "hot-l1-batch-stream-v1", "registry_date": "2026-10-05", "patterns": 2, "threshold_paths": 10, "max_chars": 7}]


@pytest.mark.parametrize("change,message", [
    (lambda b: b["native"].update(nodes_read=8), "batch catalog native stream counts disagree with source/registry"),
    (lambda b: b["native"].update(registered_predicates=3), "batch catalog native stream counts disagree with source/registry"),
    (lambda b: b["native"].update(peak_active=3), "batch catalog native stream counts disagree with source/registry"),
    (lambda b: b["native"].update(nodes_read=True), "hot L1 artifact native.nodes_read must be a nonnegative integer"),
    (lambda b: b["native"].update(schema="unverified"), "batch catalog stream lacks completed native provenance"),
    (lambda b: b["queries"].update(registry_date="2026-10-04"), "batch catalog registry date declarations disagree"),
    (lambda b: b.update(engine="sql"), "batch catalog engine declaration disagrees with artifact schema"),
])
def test_stream_provenance_and_counts_refuse(tmp_path: Path, change: object, message: str) -> None:
    body = stream_artifact()
    change(body)
    with pytest.raises(ValueError) as caught:
        HotL1BatchCatalog.load([write(tmp_path / "bad-stream.json", body)])
    assert str(caught.value) == message


@pytest.mark.parametrize("diff", [False, True])
def test_batch_read_cli_exact_forwarding_and_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, diff: bool) -> None:
    from dt_cloud.cli import main
    from dt_cloud.chstore import hot_l1_batch_catalog as module

    paths, calls = (tmp_path / "before.json", tmp_path / "after.json"), []
    output = {"fixture": "diff" if diff else "view", "exact": True}

    class Catalog:
        @classmethod
        def load(cls, artifacts: tuple[Path, ...]):
            calls.append(("load", artifacts))
            return cls()

        def view(self, *args: object, **kwargs: object) -> dict:
            calls.append(("view", args, kwargs))
            return output

        def diff(self, *args: object, **kwargs: object) -> dict:
            calls.append(("diff", args, kwargs))
            return output

    monkeypatch.setattr(module, "HotL1BatchCatalog", Catalog)
    args = ["ch-hot-l1-batch-read", "-d", "2026-10-05", "-n", ".JSON", "-p", "", *map(str, paths)]
    if diff:
        args += ["-D", "2026-10-04"]
    result = CliRunner().invoke(main, args)
    assert (result.exit_code, result.stdout, result.stderr) == (0, dumps(output) + "\n", "")
    assert calls == [("load", paths), ("diff", ("2026-10-04", "2026-10-05", ".JSON"), {"path": ""}) if diff else ("view", ("2026-10-05", ".JSON"), {"path": ""})]


@pytest.mark.parametrize("pattern,path,message", [
    ("cold", "", "hot L1 batch pattern/date is not registered; no scan fallback"),
    (".json", "a", "hot L1 batch catalog serves the global root only"),
])
def test_batch_read_cli_refusals_emit_no_json(tmp_path: Path, pattern: str, path: str, message: str) -> None:
    from dt_cloud.cli import main

    artifact_path = write(tmp_path / "stream.json", stream_artifact())
    result = CliRunner().invoke(main, ["ch-hot-l1-batch-read", "-d", "2026-10-05", "-n", pattern, "-p", path, str(artifact_path)])
    assert (result.exit_code, result.stdout, result.stderr) == (1, "", "")
    assert type(result.exception) is CatalogRequest
    assert str(result.exception) == message


def test_queries_have_no_file_reads_and_responses_cannot_mutate_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = artifact()
    catalog = HotL1BatchCatalog.load([write(tmp_path / "after.json", body)])

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("query attempted file access")

    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    result = catalog.view("2026-10-05", ".json")
    result["buckets"][0]["b"] = 999
    result["validation"]["references"][0]["pattern"] = "other"
    catalog.metadata()["dates"].clear()
    assert catalog.view("2026-10-05", ".json") == expected_view(body)


def test_registered_patterns_preserve_normalized_registry_order_without_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = artifact()
    body["results"][0]["pattern"], body["results"][1]["pattern"] = ".npy", ".json"
    catalog = HotL1BatchCatalog.load([write(tmp_path / "after.json", body)])

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("registered patterns attempted file access")

    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    assert catalog.registered_patterns("2026-10-05") == (".npy", ".json")
    with pytest.raises(CatalogRequest) as caught:
        catalog.registered_patterns("2026-10-03")
    assert str(caught.value) == "hot L1 batch date is not registered; no scan fallback"


@pytest.mark.parametrize("date,pattern,path,message", [
    ("2026-10-05", "cold", "", "hot L1 batch pattern/date is not registered; no scan fallback"),
    ("2026-10-03", ".json", "", "hot L1 batch pattern/date is not registered; no scan fallback"),
    ("2026-10-05", ".json", "a", "hot L1 batch catalog serves the global root only"),
    ("2026-10-05", "a/b", "", "batch catalog requires a nonempty NUL/slash-free literal of at most 512 characters"),
])
def test_unknown_queries_dates_and_drills_refuse(tmp_path: Path, date: str, pattern: str, path: str, message: str) -> None:
    catalog = HotL1BatchCatalog.load([write(tmp_path / "after.json", artifact())])
    with pytest.raises(CatalogRequest) as caught:
        catalog.view(date, pattern, path=path)
    assert str(caught.value) == message


@pytest.mark.parametrize("field,value,message", [
    ("rows", 0, "batch catalog requires a completed valid source audit"),
    ("invalid_utf8_paths", 1, "batch catalog requires a completed valid source audit"),
    ("invalid_scalar_rows", 1, "batch catalog requires a completed valid source audit"),
    ("invalid_scalar_rows", False, "hot L1 artifact source_validation.invalid_scalar_rows must be a nonnegative integer"),
])
def test_invalid_source_audits_refuse(tmp_path: Path, field: str, value: object, message: str) -> None:
    body = artifact()
    body["source_validation"][field] = value
    with pytest.raises(ValueError) as caught:
        HotL1BatchCatalog.load([write(tmp_path / "bad.json", body)])
    assert str(caught.value) == message


@pytest.mark.parametrize("change,message", [
    (lambda b: b.update(schema="hot-l1-v1"), "batch catalog requires the exact frozen L1 names/directory contract"),
    (lambda b: b["queries"]["header"].update(target="other"), "batch catalog requires matching completed registry target metadata"),
    (lambda b: b.update(compiled_patterns=3), "batch catalog registry/result counts disagree"),
    (lambda b: b["results"][1].update(predicate_id=1), "batch catalog predicate IDs must be complete and ordered from one"),
    (lambda b: b["results"][1].update(pattern=".json"), "batch catalog contains duplicate normalized literals"),
    (lambda b: b["results"][0].update(pattern=".JSON"), "batch catalog artifact literals must be normalized and within registry max_chars"),
    (lambda b: b["validation"].update(independently_scanned_entire_catalog=True), "batch catalog requires honest completed benchmark validation metadata"),
    (lambda b: b["results"][0]["root"].update(o=999), "batch catalog root totals disagree with complete buckets"),
    (lambda b: b["results"][0]["buckets"][0].update(pre=2), "batch catalog buckets are not a disjoint contiguous partition"),
    (lambda b: b["results"][0]["buckets"][0].update(b=True), "hot L1 artifact bucket.b must be a nonnegative integer"),
])
def test_malformed_contracts_registries_and_partitions_refuse(tmp_path: Path, change: object, message: str) -> None:
    body = artifact()
    change(body)
    with pytest.raises(ValueError) as caught:
        HotL1BatchCatalog.load([write(tmp_path / "bad.json", body)])
    assert str(caught.value) == message


@pytest.mark.parametrize("kind,message", [
    ("duplicate", "batch catalog contains duplicate scan dates"),
    ("target", "batch catalog requires one frozen generation target"),
    ("bounds", "batch catalog bucket identities/bounds changed across results or dates"),
])
def test_frozen_generation_consistency(tmp_path: Path, kind: str, message: str) -> None:
    before, after = artifact("2026-10-04"), artifact()
    if kind == "duplicate":
        after = deepcopy(before)
    elif kind == "target":
        after["target"] = after["queries"]["header"]["target"] = "other"
    else:
        for row in after["results"]:
            row["buckets"][1]["post"] = 9
    with pytest.raises(ValueError) as caught:
        HotL1BatchCatalog.load([write(tmp_path / "before.json", before), write(tmp_path / "after.json", after)])
    assert str(caught.value) == message


def test_empty_duplicate_keys_and_reverse_diff_refuse(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as caught:
        HotL1BatchCatalog.load([])
    assert str(caught.value) == "batch catalog requires at least one completed artifact"
    path = tmp_path / "keys.json"
    path.write_text('{"schema":"a","schema":"b"}\n')
    with pytest.raises(ValueError) as caught:
        HotL1BatchCatalog.load([path])
    assert str(caught.value) == "hot L1 artifact contains duplicate JSON keys"
    catalog = HotL1BatchCatalog.load([write(tmp_path / "before.json", artifact("2026-10-04")), write(tmp_path / "after.json", artifact())])
    with pytest.raises(CatalogRequest) as caught:
        catalog.diff("2026-10-05", "2026-10-04", ".json")
    assert str(caught.value) == "hot L1 batch comparison requires before date to precede after date"


def test_verified_bytes_parse_once_without_any_file_access(monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud.chstore import hot_l1_batch_catalog as module

    before, after = artifact("2026-10-04"), stream_artifact()
    blobs = tuple(dumps(body).encode() for body in (before, after))
    parsed, original_loads = [], module.loads

    def parse(text: str, **kwargs: object) -> object:
        parsed.append(text)
        return original_loads(text, **kwargs)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("verified-byte catalog attempted file access")

    monkeypatch.setattr(module, "loads", parse)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    catalog = HotL1BatchCatalog.from_bytes(iter(blobs))
    assert catalog.view("2026-10-05", ".JSON") == expected_view(after)
    assert catalog.diff("2026-10-04", "2026-10-05", ".json")["delta"] == {"b": -6, "o": 0}
    assert parsed == [blob.decode() for blob in blobs]


def test_verified_bytes_are_not_replaced_by_changed_path_contents(tmp_path: Path) -> None:
    body = artifact()
    path = write(tmp_path / "replace.json", body)
    verified = path.read_bytes()
    changed = deepcopy(body)
    changed["results"][0]["root"]["b"] = changed["results"][0]["buckets"][0]["b"] = 999
    write(path, changed)
    assert HotL1BatchCatalog.from_bytes([verified]).view("2026-10-05", ".json") == expected_view(body)
    assert HotL1BatchCatalog.load([path]).view("2026-10-05", ".json") == expected_view(changed)


@pytest.mark.parametrize("blobs,message", [
    ([], "batch catalog requires at least one completed artifact"),
    ([bytearray(b"{}")], "batch catalog verified inputs must be immutable bytes"),
    ([b'{"schema":"a","schema":"b"}'], "hot L1 artifact contains duplicate JSON keys"),
    ([dumps(artifact()).encode()] * 2, "batch catalog contains duplicate scan dates"),
    ([dumps({**artifact(), "compiled_patterns": 3}).encode()], "batch catalog registry/result counts disagree"),
])
def test_verified_bytes_keep_all_common_validation(blobs: list[bytes], message: str) -> None:
    with pytest.raises(ValueError) as caught:
        HotL1BatchCatalog.from_bytes(blobs)
    assert str(caught.value) == message
