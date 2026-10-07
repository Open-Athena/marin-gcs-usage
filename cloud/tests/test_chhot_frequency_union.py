from copy import deepcopy
from hashlib import sha256
from json import dumps, loads
from pathlib import Path

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import hot_frequency_union
from dt_cloud.chstore.hot_frequency_registry import FREQUENCY_SEMANTICS, QUALIFICATION, UNION_SCHEMA, load_queries
from dt_cloud.chstore.hot_frequency_report import SCOPE, report
from dt_cloud.chstore.hot_frequency_union import union
from dt_cloud.chstore.hot_l1_batch_catalog import HotL1BatchCatalog, VALIDATION
from dt_cloud.chstore.hot_l1_catalog import SCOPE as COVERAGE_SCOPE
from dt_cloud.cli import main


DATES = ["2026-10-04", "2026-10-05"]
FREQUENCIES = [{"a": 20, "c": 12, "d": 50, "z": 5}, {"b": 30, "c": 8, "d": 60}]


def source(directory: Path, date: str, frequencies: dict[str, int], *, target: str = "fixture", max_chars: int | None = 2) -> tuple[Path, Path]:
    """`max_chars` None: a complete census, its layers through the first empty length."""
    directory.mkdir()
    rows = [{"chars": len(pattern), "pattern": pattern, "direct_matching_paths": frequency}
            for pattern, frequency in sorted(frequencies.items(), key=lambda item: (len(item[0]), item[0]))]
    header = {"schema": "hot-frequency-queries-v1", "target": target, "date": date, "threshold_paths": 5, "max_chars": max_chars}
    layers = range(1, (max_chars or max(map(len, frequencies)) + 1) + 1)
    raw = (dumps(header) + "\n" + "".join(dumps(row) + "\n" for row in rows) + dumps({"complete": True, "patterns": len(rows)}) + "\n").encode()
    body = {"schema": "hot-frequency-v1", "target": target, "date": date,
        "snapshot_db": "snapshot_" + date.replace("-", ""), "scope": SCOPE, "threshold_paths": 5, "max_chars": max_chars,
        "persistent_index_created": False, "accepted_hot_pattern_cap": 500_000, "weighted_names_s": 1,
        "queries": {"patterns": len(rows), "bytes": len(raw), "export_s": .1}, "selected_patterns": [],
        "lengths": [{"chars": chars, "hot_patterns": len(group), "hot_query_utf8_bytes": sum(len(row["pattern"].encode()) for row in group),
                     "sum_hot_direct_matching_paths": sum(row["direct_matching_paths"] for row in group), "elapsed_s": 1,
                     "pruned_by_empty_prefix": False} for chars in layers for group in ([row for row in rows if row["chars"] == chars],)]}
    census, queries = directory / "census.json", directory / "queries.jsonl"
    census.write_text(dumps(body) + "\n")
    queries.write_bytes(raw)
    return census, queries


@pytest.fixture
def sources(tmp_path: Path) -> tuple[tuple[Path, Path], ...]:
    return tuple(source(tmp_path / str(index), date, frequencies) for index, (date, frequencies) in enumerate(zip(DATES, FREQUENCIES, strict=True)))


def expected_header(sources: tuple[tuple[Path, Path], ...]) -> dict:
    declarations = []
    for (census, queries), date, frequencies in zip(sources, DATES, FREQUENCIES, strict=True):
        declarations.append({"date": date, "snapshot_db": "snapshot_" + date.replace("-", ""),
            "threshold_paths": 5, "max_chars": 2, "accepted_hot_pattern_cap": 500_000,
            "census": {"sha256": sha256(census.read_bytes()).hexdigest(), "bytes": len(census.read_bytes())},
            "queries": {"sha256": sha256(queries.read_bytes()).hexdigest(), "bytes": len(queries.read_bytes()), "patterns": len(frequencies)}})
    return {"schema": UNION_SCHEMA, "target": "fixture", "dates": DATES, "threshold_paths": 10, "max_chars": 2,
            "max_patterns": 500_000, "sources": declarations, "frequency_semantics": FREQUENCY_SEMANTICS}


ROWS = [
    {"chars": 1, "pattern": "a", "direct_matching_paths": {"2026-10-04": 20, "2026-10-05": None}},
    {"chars": 1, "pattern": "b", "direct_matching_paths": {"2026-10-04": None, "2026-10-05": 30}},
    {"chars": 1, "pattern": "c", "direct_matching_paths": {"2026-10-04": 12, "2026-10-05": 8}},
    {"chars": 1, "pattern": "d", "direct_matching_paths": {"2026-10-04": 50, "2026-10-05": 60}},
]


def test_exact_date_vectors_qualification_overlap_bytes_and_deterministic_order(sources, tmp_path: Path) -> None:
    out = tmp_path / "union.jsonl"
    header = expected_header(sources)
    result = union(tuple(reversed(sources)), 10, 2, out)
    raw = ((dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n") +
           "".join(dumps(row, separators=(",", ":")) + "\n" for row in ROWS) + '{"complete":true,"patterns":4}\n').encode()
    assert out.read_bytes() == raw
    assert result == {"schema": "hot-frequency-union-report-v1", "complete": True, "target": "fixture", "registry_dates": DATES,
        "threshold_paths": 10, "max_chars": 2, "patterns": 4, "literal_utf8_bytes": 4,
        "export_records_raw_bytes": sum(len((dumps(row, separators=(",", ":")) + "\n").encode()) for row in ROWS),
        "per_date": [{"date": "2026-10-04", "threshold_hot_patterns": 3, "known_frequencies": 3, "below_source_minimum_patterns": 1},
                     {"date": "2026-10-05", "threshold_hot_patterns": 2, "known_frequencies": 3, "below_source_minimum_patterns": 1}],
        "overlap": [{"dates": DATES, "patterns": 1}],
        "queries": {"path": str(out), "bytes": len(raw), "sha256": sha256(raw).hexdigest(), "header": header},
        "validation": "completed accepted single-date artifacts checked; union hot on any source date; not an independent source scan"}
    assert load_queries(out, "fixture", DATES[0]) == (header, ("a", "b", "c", "d"))
    assert load_queries(out, "fixture", DATES[1]) == (header, ("a", "b", "c", "d"))
    another = tmp_path / "another.jsonl"
    union(sources, 10, 2, another)
    assert another.read_bytes() == raw


def test_single_source_union_is_that_scans_own_registry(sources, tmp_path: Path) -> None:
    out = tmp_path / "own.jsonl"
    census, queries = sources[1]
    declaration = expected_header(sources)["sources"][1]
    header = {"schema": UNION_SCHEMA, "target": "fixture", "dates": [DATES[1]], "threshold_paths": 10, "max_chars": 2,
              "max_patterns": 500_000, "sources": [declaration], "frequency_semantics": FREQUENCY_SEMANTICS}
    rows = [{"chars": 1, "pattern": "b", "direct_matching_paths": {DATES[1]: 30}},
            {"chars": 1, "pattern": "d", "direct_matching_paths": {DATES[1]: 60}}]
    raw = ((dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n") +
           "".join(dumps(row, separators=(",", ":")) + "\n" for row in rows) + '{"complete":true,"patterns":2}\n').encode()
    result = union(((census, queries),), 10, 2, out)
    assert out.read_bytes() == raw
    assert (result["registry_dates"], result["patterns"], result["per_date"], result["overlap"]) == (
        [DATES[1]], 2, [{"date": DATES[1], "threshold_hot_patterns": 2, "known_frequencies": 2, "below_source_minimum_patterns": 0}], [])
    assert load_queries(out, "fixture", DATES[1]) == (header, ("b", "d"))
    with pytest.raises(ValueError) as caught:
        load_queries(out, "fixture", DATES[0])
    assert str(caught.value) == "union batch date must be a source date and cannot use a single registry_date override"


def test_explicit_logical_target_binds_mixed_stores_with_honest_per_date_qualification(sources, tmp_path: Path) -> None:
    out = tmp_path / "mixed.jsonl"
    other = source(tmp_path / "other", DATES[1], FREQUENCIES[1], target="other_fixture")
    expected = expected_header((sources[0], other))
    for declaration, physical in zip(expected["sources"], ("fixture", "other_fixture"), strict=True):
        declaration["target"] = physical
    header = {**expected, "target": "logical_store"}
    result = union((other, sources[0]), 10, 2, out, target="logical_store")
    lines = out.read_bytes().splitlines()
    assert loads(lines[0]) == header
    assert [loads(line) for line in lines[1:]] == [*ROWS, {"complete": True, "patterns": 4}]
    # "c" is registered because 10-04 qualifies it (12); 10-05 keeps its exact
    # below-threshold 8, and "a" is null on 10-05: never claimed hot there.
    assert result["per_date"] == [
        {"date": DATES[0], "threshold_hot_patterns": 3, "known_frequencies": 3, "below_source_minimum_patterns": 1},
        {"date": DATES[1], "threshold_hot_patterns": 2, "known_frequencies": 3, "below_source_minimum_patterns": 1}]
    assert load_queries(out, "logical_store", DATES[1]) == (header, ("a", "b", "c", "d"))
    with pytest.raises(ValueError) as caught:
        load_queries(out, "fixture", DATES[1])
    assert str(caught.value) == "union registry requires matching sorted dates and complete valid source provenance"


@pytest.mark.parametrize("kind", ["partial", "invalid"])
def test_union_loader_refuses_partial_or_invalid_source_targets(sources, tmp_path: Path, kind: str) -> None:
    out = tmp_path / "mixed.jsonl"
    union(sources, 10, 2, out, target="logical_store")
    records = [loads(line) for line in out.read_bytes().splitlines()]
    if kind == "partial":
        del records[0]["sources"][1]["target"]
    else:
        records[0]["sources"][1]["target"] = "not an identifier"
    out.write_text("".join(dumps(row) + "\n" for row in records))
    with pytest.raises(ValueError):
        load_queries(out, "logical_store", DATES[0])


def test_complete_length_domain_union(tmp_path: Path) -> None:
    # Complete censuses (`max_chars` None) union in the complete domain, keeping
    # every length; a bounded union over them keeps its bound.
    complete = tuple(source(tmp_path / str(index), date, {**frequencies, "dd": 9 + index}, max_chars=None)
                     for index, (date, frequencies) in enumerate(zip(DATES, FREQUENCIES, strict=True)))
    whole = union(complete, 10, None, tmp_path / "complete.jsonl")
    header, patterns = load_queries(tmp_path / "complete.jsonl", "fixture", DATES[0])
    assert (whole["max_chars"], header["max_chars"], [source["max_chars"] for source in header["sources"]], patterns) == (
        None, None, [None, None], ("a", "b", "c", "d", "dd"))
    assert loads((tmp_path / "complete.jsonl").read_text().splitlines()[5]) == {
        "chars": 2, "pattern": "dd", "direct_matching_paths": {"2026-10-04": 9, "2026-10-05": 10}}
    bounded = union(complete, 10, 1, tmp_path / "bounded.jsonl")
    header, patterns = load_queries(tmp_path / "bounded.jsonl", "fixture", DATES[0])
    assert (bounded["max_chars"], header["max_chars"], [source["max_chars"] for source in header["sources"]], patterns) == (
        1, 1, [None, None], ("a", "b", "c", "d"))


def test_complete_union_refuses_a_bounded_source(sources, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="^report lengths must be nonempty integers within the completed source depth$"):
        union(sources, 10, None, tmp_path / "out.jsonl")
    assert not (tmp_path / "out.jsonl").exists()


def test_union_without_sources_refuses(tmp_path: Path) -> None:
    out = tmp_path / "none.jsonl"
    with pytest.raises(ValueError) as caught:
        union((), 10, 2, out)
    assert str(caught.value) == "union requires at least one dated source census"
    assert out.exists() is False


@pytest.mark.parametrize("kind,message", [
    ("cap", "union exceeds its accepted-pattern cap; no complete export"),
    ("below", "report thresholds must be nonempty integers at least the source minimum"),
    ("depth", "report lengths must be nonempty integers within the completed source depth"),
    ("source-cap", "completed export exceeds its accepted pattern cap"),
    ("source-incomplete", "hot query export lacks a valid exact-count completion footer"),
    ("duplicate", "union source scan dates must be unique"),
    ("target", "union sources require the same frozen target unless an explicit logical target binds them"),
])
def test_refused_union_never_writes_partial_output(sources, tmp_path: Path, kind: str, message: str) -> None:
    selected, threshold, chars, cap = sources, 10, 2, 500_000
    if kind == "cap": cap = 3
    elif kind == "below": threshold = 4
    elif kind == "depth": chars = 3
    elif kind == "source-cap":
        body = loads(sources[0][0].read_bytes())
        body["accepted_hot_pattern_cap"] = 1
        sources[0][0].write_text(dumps(body) + "\n")
    elif kind == "source-incomplete": sources[0][1].write_bytes(b"\n".join(sources[0][1].read_bytes().splitlines()[:-1]) + b"\n")
    elif kind == "duplicate": selected = (sources[0], sources[0])
    elif kind == "target": selected = (sources[0], source(tmp_path / "other", DATES[1], FREQUENCIES[1], target="other_fixture"))
    out = tmp_path / "refused.jsonl"
    with pytest.raises(ValueError) as caught:
        union(selected, threshold, chars, out, max_patterns=cap)
    assert str(caught.value) == message
    assert out.exists() is False


def test_existing_output_is_preserved_without_reading_sources(sources, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "existing.jsonl"
    out.write_text("existing\n")
    monkeypatch.setattr(Path, "read_bytes", lambda *args: pytest.fail("existing output caused source reads"))
    with pytest.raises(ValueError) as caught:
        union(sources, 10, 2, out)
    assert str(caught.value) == "union output must be a new artifact"
    assert out.read_text() == "existing\n"


@pytest.mark.parametrize("kind,message", [
    ("footer", "hot query export lacks a valid exact-count completion footer"),
    ("zero", "union query requires valid dated frequencies qualifying on at least one source date"),
    ("bool", "union query requires valid dated frequencies qualifying on at least one source date"),
    ("cold", "union query requires valid dated frequencies qualifying on at least one source date"),
    ("source-cap", "union registry requires matching sorted dates and complete valid source provenance"),
    ("known-count", "union known-frequency counts exceed their accepted source registries"),
    ("order", "union query records must be sorted by length and literal"),
])
def test_union_loader_refuses_malformed_or_misqualified_complete_registry(sources, tmp_path: Path, kind: str, message: str) -> None:
    out = tmp_path / "union.jsonl"
    union(sources, 10, 2, out)
    records = [loads(line) for line in out.read_bytes().splitlines()]
    if kind == "footer": records[-1]["patterns"] = 5
    elif kind == "zero": records[1]["direct_matching_paths"][DATES[1]] = 0
    elif kind == "bool": records[1]["direct_matching_paths"][DATES[1]] = True
    elif kind == "cold": records[1]["direct_matching_paths"] = dict.fromkeys(DATES, None)
    elif kind == "source-cap": records[0]["sources"][0]["accepted_hot_pattern_cap"] = 1
    elif kind == "known-count": records[0]["sources"][0]["queries"]["patterns"] = 1
    elif kind == "order": records[1], records[2] = records[2], records[1]
    out.write_text("".join(dumps(row) + "\n" for row in records))
    with pytest.raises(ValueError) as caught:
        load_queries(out, "fixture", DATES[0])
    assert str(caught.value) == message


def test_census_reporter_explicitly_refuses_union_export_as_single_date_source(sources, tmp_path: Path) -> None:
    out = tmp_path / "union.jsonl"
    union(sources, 10, 2, out)
    with pytest.raises(ValueError) as caught:
        report(sources[0][0], out, thresholds=(10,), lengths=(2,), patterns=())
    assert str(caught.value) == "this operation requires a single-date hot-frequency-queries-v1 export"


def test_union_cli_exact_forwarding_and_output(monkeypatch: pytest.MonkeyPatch, sources, tmp_path: Path) -> None:
    calls, out = [], tmp_path / "union.jsonl"

    def fixture_union(*args: object, **kwargs: object) -> dict:
        calls.append((args, kwargs))
        return {"schema": "fixture-union", "complete": True}

    monkeypatch.setattr(hot_frequency_union, "union", fixture_union)
    result = CliRunner().invoke(main, ["ch-hot-frequency-union", "-s", *map(str, sources[0]), "-s", *map(str, sources[1]),
                                      "-t", "10", "-k", "2", "-c", "100", "-o", str(out)])
    assert (result.exit_code, result.stdout, result.stderr) == (0, '{"schema": "fixture-union", "complete": true}\n', "")
    assert calls == [((sources, 10, 2, out), {"max_patterns": 100})]


def batch_artifact(header: dict, date: str) -> dict:
    frequencies = FREQUENCIES[DATES.index(date)]
    results = []
    for predicate_id, pattern in enumerate(("a", "b", "c", "d"), 1):
        # This tiny fixture's actual objects are leaves; these weights come
        # from its source, not from treating an unknown union frequency as 0.
        count = frequencies.get(pattern, 0)
        weights = {"b": 2 * count, "o": count}
        results.append({"predicate_id": predicate_id, "pattern": pattern, "root": weights,
                        "buckets": [{"pre": 1, "post": 128, "path": "bucket", **weights}]})
    return {"schema": "hot-l1-batch-sql-v1", "target": "fixture", "snapshot_db": "snapshot_" + date.replace("-", ""),
            "date": date, "exact": True, "incremental": False, "levels": 1, "scope": COVERAGE_SCOPE,
            "compiled_patterns": 4, "source_validation": {"rows": sum(frequencies.values()) + 2,
                "invalid_utf8_paths": 0, "invalid_scalar_rows": 0, "path_bytes": 1000},
            "queries": {"path": "union.jsonl", "patterns": 4, "header": deepcopy(header), "registry_dates": DATES},
            "validation": {"description": VALIDATION, "references": [], "independently_scanned_entire_catalog": False},
            "results": results}


def expected_union_view(body: dict, predicate_id: int) -> dict:
    row = body["results"][predicate_id - 1]
    return {"schema": "hot-l1-batch-catalog-v1", "artifact_schema": body["schema"], "target": "fixture",
            "date": body["date"], "registry_dates": DATES, "pattern": row["pattern"], "path": "", "exact": True,
            "incremental": False, "levels": 1, "scope": COVERAGE_SCOPE,
            "registry": {"registry_dates": DATES, "patterns": 4, "threshold_paths": 10, "max_chars": 2,
                         "qualification": QUALIFICATION, "predicate_id": predicate_id},
            "validation": body["validation"], "source": "registered precomputed batch artifact",
            "root": row["root"], "buckets": row["buckets"]}


def test_catalog_serves_union_queries_on_both_dates_without_fake_registry_date(sources, tmp_path: Path) -> None:
    header = union(sources, 10, 2, tmp_path / "union.jsonl")["queries"]["header"]
    before, after = batch_artifact(header, DATES[0]), batch_artifact(header, DATES[1])
    catalog = HotL1BatchCatalog.from_bytes([dumps(before).encode(), dumps(after).encode()])
    assert catalog.view(DATES[0], "a") == expected_union_view(before, 1)
    assert catalog.view(DATES[1], "a") == expected_union_view(after, 1)
    assert catalog.view(DATES[1], "c") == expected_union_view(after, 3)
    assert catalog.diff(*DATES, "a") == {
        "schema": "hot-l1-batch-catalog-diff-v1", "target": "fixture", "pattern": "a", "path": "", "exact": True,
        "incremental": False, "levels": 1, "scope": COVERAGE_SCOPE, "source": "registered precomputed batch artifacts",
        "before": expected_union_view(before, 1), "after": expected_union_view(after, 1), "delta": {"b": -40, "o": -20},
        "buckets": [{"pre": 1, "post": 128, "path": "bucket", "before": {"b": 40, "o": 20},
                     "after": {"b": 0, "o": 0}, "delta": {"b": -40, "o": -20}}],
    }
    assert catalog.metadata() == {"schema": "hot-l1-batch-catalog-registry-v1", "target": "fixture", "dates": [
        {"date": date, "snapshot_db": "snapshot_" + date.replace("-", ""), "artifact_schema": "hot-l1-batch-sql-v1",
         "registry_dates": DATES, "patterns": 4, "threshold_paths": 10, "max_chars": 2, "qualification": QUALIFICATION} for date in DATES]}


def test_unknown_census_frequency_does_not_force_zero_served_weights(sources, tmp_path: Path) -> None:
    out = tmp_path / "union.jsonl"
    header = union(sources, 10, 2, out)["queries"]["header"]
    records = [loads(line) for line in out.read_bytes().splitlines()]
    assert records[1] == ROWS[0]
    body = batch_artifact(header, DATES[1])
    # Three real matching leaves remain below this source's census cutoff5;
    # their independently built aggregate must still be served, not inferred 0.
    body["results"][0]["root"] = {"b": 6, "o": 3}
    body["results"][0]["buckets"][0].update(b=6, o=3)
    body["source_validation"]["rows"] += 3
    catalog = HotL1BatchCatalog.from_bytes([dumps(body).encode()])
    assert catalog.view(DATES[1], "a") == expected_union_view(body, 1)


@pytest.mark.parametrize("kind,message", [
    ("fake-date", "batch catalog union registry dates/snapshot declarations disagree"),
    ("dates", "batch catalog union registry dates/snapshot declarations disagree"),
    ("snapshot", "batch catalog union registry dates/snapshot declarations disagree"),
    ("cap", "batch catalog union exceeds its accepted-pattern cap"),
    ("source-cap", "union registry requires matching sorted dates and complete valid source provenance"),
    ("source-count", "batch catalog union count exceeds its accepted source registries"),
])
def test_catalog_refuses_mismatched_union_provenance(sources, tmp_path: Path, kind: str, message: str) -> None:
    header = union(sources, 10, 2, tmp_path / "union.jsonl")["queries"]["header"]
    body = batch_artifact(header, DATES[0])
    if kind == "fake-date": body["queries"]["registry_date"] = DATES[0]
    elif kind == "dates": body["queries"]["registry_dates"] = [DATES[0]]
    elif kind == "snapshot": body["snapshot_db"] = "other_snapshot"
    elif kind == "cap": body["queries"]["header"]["max_patterns"] = 3
    elif kind == "source-cap": body["queries"]["header"]["sources"][0]["accepted_hot_pattern_cap"] = 1
    elif kind == "source-count":
        for declaration in body["queries"]["header"]["sources"]:
            declaration["queries"]["patterns"] = 1
    with pytest.raises(ValueError) as caught:
        HotL1BatchCatalog.from_bytes([dumps(body).encode()])
    assert str(caught.value) == message


def test_builder_forwards_same_complete_union_to_both_scan_dates(sources, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud.chstore import hot_l1_batch_bench

    out = tmp_path / "union.jsonl"
    header = union(sources, 10, 2, out)["queries"]["header"]
    calls = []

    class Client:
        def __init__(self, url: str, **kwargs: object) -> None:
            calls.append(("create", url))

        def exec(self, sql: str) -> None:
            calls.append(("exec", sql))

        def json(self, sql: str) -> list:
            calls.append(("profile",))
            return [[1, 100, 1, 1, 1]]

        def close(self) -> None:
            calls.append(("close",))

    def build(ch: object, target: str, date: str, patterns: tuple[str, ...]) -> dict:
        calls.append(("build", target, date, patterns))
        body = batch_artifact(header, date)
        body.pop("queries")
        body.pop("validation")
        return body

    monkeypatch.setattr(hot_l1_batch_bench, "Ch", Client)
    monkeypatch.setattr(hot_l1_batch_bench, "build", build)
    completed = []
    for date in DATES:
        result = hot_l1_batch_bench.bench("http://fixture", "fixture", date, out, tmp_path / f"{date}.json")
        assert result["queries"] == {"path": str(out), "patterns": 4, "header": header, "registry_dates": DATES}
        completed.append(dumps(result).encode())
    assert calls == [event for date in DATES for event in (
        ("create", "http://fixture"), ("build", "fixture", date, ("a", "b", "c", "d")),
        ("exec", "SYSTEM FLUSH LOGS"), ("profile",), ("close",),
    )]
    assert HotL1BatchCatalog.from_bytes(completed).registered_patterns(DATES[0]) == ("a", "b", "c", "d")


@pytest.mark.parametrize("date,override", [("2026-10-03", None), ("2026-10-04", "2026-10-05")])
def test_union_loader_refuses_uncovered_build_date_or_fake_single_date_override(sources, tmp_path: Path, date: str, override: str | None) -> None:
    out = tmp_path / "union.jsonl"
    union(sources, 10, 2, out)
    with pytest.raises(ValueError) as caught:
        load_queries(out, "fixture", date, registry_date=override)
    assert str(caught.value) == "union batch date must be a source date and cannot use a single registry_date override"
