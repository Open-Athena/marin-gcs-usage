from hashlib import sha256
from json import dumps, loads
from pathlib import Path

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import hot_frequency_report
from dt_cloud.chstore.hot_frequency_report import SCOPE, compare, report
from dt_cloud.cli import main


@pytest.fixture
def artifacts(tmp_path: Path) -> tuple[Path, Path, dict, list[dict], list[bytes]]:
    counts = {".": 1_000_000, "x": 1_000_000, "🙂": 200_000}
    for pattern, frequency in ((".json", 500_000), (".npy", 100_000), ("zarr.json", 300_000)):
        for length in range(1, len(pattern) + 1):
            counts.setdefault(pattern[:length], frequency)
    rows = [{"chars": len(pattern), "pattern": pattern, "direct_matching_paths": count}
            for pattern, count in sorted(counts.items(), key=lambda row: (len(row[0]), row[0]))]
    header = {"schema": "hot-frequency-queries-v1", "target": "fixture", "date": "2026-10-05", "threshold_paths": 100_000, "max_chars": 16}
    lines = [(dumps(header) + "\n").encode(),
             *[(dumps(row, ensure_ascii=False) + "\n").encode() for row in rows],
             (dumps({"complete": True, "patterns": len(rows)}) + "\n").encode()]
    queries = tmp_path / "queries.jsonl"
    queries.write_bytes(b"".join(lines))
    body = {"schema": "hot-frequency-v1", "target": "fixture", "date": "2026-10-05", "snapshot_db": "fixture_snapshot",
            "scope": SCOPE, "threshold_paths": 100_000, "max_chars": 16, "persistent_index_created": False,
            "accepted_hot_pattern_cap": 500_000, "thresholds_paths": [100_000, 300_000, 1_000_000],
            "weighted_names_s": 2.5, "selected_patterns": [],
            "queries": {"path": "/data/original-queries.jsonl", "patterns": len(rows), "bytes": sum(map(len, lines)), "export_s": .75},
            "limits": {"threads": 4}, "profile": {"summed_query_ms": 1200}, "rss": {"sampled_peak_total_rss_bytes": 4096},
            "lengths": [{"chars": chars, "hot_patterns": sum(row["chars"] == chars for row in rows),
                         "hot_query_utf8_bytes": sum(len(row["pattern"].encode()) for row in rows if row["chars"] == chars),
                         "sum_hot_direct_matching_paths": sum(row["direct_matching_paths"] for row in rows if row["chars"] == chars),
                         "elapsed_s": 1, "pruned_by_empty_prefix": chars > 10,
                         "threshold_counts": [{"threshold_paths": cut, "hot_patterns": sum(row["chars"] == chars and row["direct_matching_paths"] >= cut for row in rows)}
                                              for cut in (100_000, 300_000, 1_000_000)]} for chars in range(1, 17)]}
    census = tmp_path / "census.json"
    census.write_text(dumps(body) + "\n")
    return census, queries, body, rows, lines


def test_exact_grid_original_record_bytes_unicode_frequencies_and_actual_build_provenance(artifacts) -> None:
    census, queries, body, rows, lines = artifacts
    raw = census.read_bytes()
    header = {"schema": "hot-frequency-queries-v1", "target": "fixture", "date": "2026-10-05", "threshold_paths": 100_000, "max_chars": 16}
    expected_grid = []
    for cut, pairs in ((100_000, ((17, 57), (19, 74), (19, 74))),
                       (300_000, ((13, 44), (15, 61), (15, 61))),
                       (1_000_000, ((2, 2), (2, 2), (2, 2)))):
        for chars, (count, utf8_bytes) in zip((7, 12, 16), pairs, strict=True):
            expected_grid.append({"threshold_paths": cut, "max_chars": chars, "literals": count, "literal_utf8_bytes": utf8_bytes,
                                  "export_records_raw_bytes": sum(len(line) for row, line in zip(rows, lines[1:-1], strict=True)
                                                                  if row["chars"] <= chars and row["direct_matching_paths"] >= cut)})
    assert report(census, queries) == {
        "schema": "hot-frequency-report-v1", "complete": True, "target": "fixture", "date": "2026-10-05",
        "snapshot_db": "fixture_snapshot", "scope": SCOPE,
        "provenance": {"census": {"path": str(census), "sha256": sha256(raw).hexdigest(), "bytes": len(raw)},
                       "queries": {"path": str(queries), "sha256": sha256(b"".join(lines)).hexdigest(), "bytes": sum(map(len, lines)), "patterns": 19, "header": header},
                       "validation": "complete export plus exact census/header/layer/count/byte agreement; not an independent source scan"},
        "grid": expected_grid,
        "byte_definition": "original query-record line bytes including newline; excludes header/footer; not materialized drill storage",
        "selected_literals": [
            {"pattern": ".json", "enumerated": True, "registered": True, "direct_matching_paths": 500_000, "cold_upper_bound_exclusive": None},
            {"pattern": "zarr.json", "enumerated": True, "registered": True, "direct_matching_paths": 300_000, "cold_upper_bound_exclusive": None},
            {"pattern": ".npy", "enumerated": True, "registered": True, "direct_matching_paths": 100_000, "cold_upper_bound_exclusive": None},
        ],
        "actual_build": {"threshold_paths": 100_000, "max_chars": 16, "weighted_names_s": 2.5, "layer_elapsed_s": 16,
                         "reported_export_s": .75, "limits": {"threads": 4}, "profile": {"summed_query_ms": 1200},
                         "rss": {"sampled_peak_total_rss_bytes": 4096},
                         "timing_scope": "one observed minimum-threshold census; export may be included in layer durations; no measured higher-threshold build timings"},
    }


def test_cold_is_unknown_not_zero_and_long_selected_literals_not_enumerated(artifacts) -> None:
    census, queries, *_ = artifacts
    result = report(census, queries, thresholds=(300_000, 300_000), lengths=(16,), patterns=("COLD", "cold", "x" * 17))
    assert result["selected_literals"] == [
        {"pattern": "cold", "enumerated": True, "registered": False, "direct_matching_paths": None, "cold_upper_bound_exclusive": 100_000},
        {"pattern": "x" * 17, "enumerated": False, "registered": False, "direct_matching_paths": None, "cold_upper_bound_exclusive": None},
    ]
    assert [(row["threshold_paths"], row["max_chars"], row["literals"], row["literal_utf8_bytes"]) for row in result["grid"]] == [(300_000, 16, 15, 61)]


@pytest.mark.parametrize("kwargs,error", [
    ({"thresholds": (99_999,)}, "report thresholds must be nonempty integers at least the source minimum"),
    ({"thresholds": (True,)}, "report thresholds must be nonempty integers at least the source minimum"),
    ({"lengths": (17,)}, "report lengths must be nonempty integers within the completed source depth"),
    ({"lengths": ()}, "report lengths must be nonempty integers within the completed source depth"),
    ({"patterns": ("a/b",)}, "selected report literals must be nonempty NUL/slash-free names"),
])
def test_outside_completed_grid_refuses_instead_of_estimating(artifacts, kwargs: dict, error: str) -> None:
    census, queries, *_ = artifacts
    with pytest.raises(ValueError) as caught:
        report(census, queries, **kwargs)
    assert str(caught.value) == error


@pytest.mark.parametrize("kind,error", [
    ("count", "census export count/bytes disagree with the completed registry"),
    ("bytes", "census export count/bytes disagree with the completed registry"),
    ("threshold", "census and export threshold/maximum length disagree"),
    ("layer", "census layer counts/bytes/frequencies disagree with completed export"),
    ("cut", "census threshold counts disagree with completed export"),
    ("cut-float", "census threshold counts disagree with completed export"),
    ("cap", "completed export exceeds its accepted pattern cap"),
    ("date", "batch requires a matching completed hot-frequency query export header"),
    ("target", "batch requires a matching completed hot-frequency query export header"),
])
def test_mismatched_census_refuses_exactly(artifacts, kind: str, error: str) -> None:
    census, queries, body, *_ = artifacts
    if kind == "count": body["queries"]["patterns"] += 1
    elif kind == "bytes": body["queries"]["bytes"] += 1
    elif kind == "threshold": body["threshold_paths"] += 1
    elif kind == "layer": body["lengths"][0]["sum_hot_direct_matching_paths"] += 1
    elif kind == "cut": body["lengths"][0]["threshold_counts"][0]["hot_patterns"] += 1
    elif kind == "cut-float": body["lengths"][0]["threshold_counts"][0]["hot_patterns"] = float(body["lengths"][0]["threshold_counts"][0]["hot_patterns"])
    elif kind == "cap": body["accepted_hot_pattern_cap"] = 1
    elif kind == "date": body["date"] = "2026-10-04"
    elif kind == "target": body["target"] = "another_fixture"
    census.write_text(dumps(body) + "\n")
    with pytest.raises(ValueError) as caught:
        report(census, queries)
    assert str(caught.value) == error


def test_incomplete_export_refuses_without_report(artifacts) -> None:
    census, queries, _, _, lines = artifacts
    queries.write_bytes(b"".join(lines[:-1]))
    with pytest.raises(ValueError) as caught:
        report(census, queries)
    assert str(caught.value) == "hot query export lacks a valid exact-count completion footer"


def test_source_files_are_pinned_once_for_existing_export_validation(artifacts, monkeypatch: pytest.MonkeyPatch) -> None:
    census, queries, *_ = artifacts
    original = Path.read_bytes
    calls = []

    def read_once(path: Path) -> bytes:
        calls.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_once)
    report(census, queries)
    assert calls == [census, queries]


@pytest.mark.parametrize("overrides", [False, True])
def test_cli_forwards_defaults_or_custom_grid_and_prints_only_exact_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    overrides: bool,
) -> None:
    calls = []

    def report_fixture(*args: object, **kwargs: object) -> dict:
        calls.append((args, kwargs))
        return {"schema": "fixture-report"}

    monkeypatch.setattr(hot_frequency_report, "report", report_fixture)
    census, queries = tmp_path / "census.json", tmp_path / "queries.jsonl"
    args = ["ch-hot-frequency-report", str(census), str(queries)]
    if overrides:
        args.extend(["-t", "1000000", "-k", "12", "-k", "16", "-n", ".json"])
    result = CliRunner().invoke(main, args)
    assert (result.exit_code, result.stdout, result.stderr) == (0, '{"schema": "fixture-report"}\n', "")
    assert calls == [((census, queries), {"thresholds": (1_000_000,) if overrides else (100_000, 300_000, 1_000_000),
                                      "lengths": (12, 16) if overrides else (7, 12, 16),
                                      "patterns": (".json",) if overrides else (".json", "zarr.json", ".npy")})]


@pytest.mark.parametrize("pattern", ["COLD", "a/b", "a\0b", "x" * 17, ""])
def test_source_selected_literals_require_valid_normalized_enumerated_identity(artifacts, pattern: str) -> None:
    census, queries, body, *_ = artifacts
    body["selected_patterns"] = [{"pattern": pattern, "hot": False, "direct_matching_paths": None}]
    census.write_text(dumps(body) + "\n")
    with pytest.raises(ValueError) as caught:
        report(census, queries)
    assert str(caught.value) == "census selected literals disagree with completed export"


def control(
    artifacts: tuple,
    directory: Path,
    *,
    threshold: int = 1_000_000,
    length: int = 16,
    identity: dict | None = None,
    change: str | None = None,
) -> tuple[Path, Path]:
    _, _, source_body, source_rows, _ = artifacts
    body = loads(dumps(source_body))
    body.update(identity or {})
    rows = [dict(row) for row in source_rows if row["direct_matching_paths"] >= threshold and row["chars"] <= length]
    for row in rows:
        if row["pattern"] == "x" and change == "pattern": row["pattern"] = "y"
        if row["pattern"] == "x" and change == "frequency": row["direct_matching_paths"] += 1
        if row["pattern"] == ".json" and change == "outside": row["direct_matching_paths"] += 1
    header = {"schema": "hot-frequency-queries-v1", "target": body["target"], "date": body["date"], "threshold_paths": threshold, "max_chars": length}
    raw = ((dumps(header) + "\n") + "".join(dumps(row, ensure_ascii=False) + "\n" for row in rows) +
           dumps({"complete": True, "patterns": len(rows)}) + "\n").encode()
    body.update({"threshold_paths": threshold, "max_chars": length, "thresholds_paths": [threshold],
                 "queries": {"patterns": len(rows), "bytes": len(raw), "export_s": .5}, "lengths": []})
    previous = None
    for chars in range(1, length + 1):
        group = [row for row in rows if row["chars"] == chars]
        body["lengths"].append({"chars": chars, "hot_patterns": len(group),
            "hot_query_utf8_bytes": sum(len(row["pattern"].encode()) for row in group),
            "sum_hot_direct_matching_paths": sum(row["direct_matching_paths"] for row in group),
            "elapsed_s": 1, "pruned_by_empty_prefix": previous == 0,
            "threshold_counts": [{"threshold_paths": threshold, "hot_patterns": len(group)}]})
        previous = len(group)
    directory.mkdir()
    census, queries = directory / "census.json", directory / "queries.jsonl"
    census.write_text(dumps(body) + "\n")
    queries.write_bytes(raw)
    return census, queries


def test_controls_compare_entire_exact_common_domain_with_both_hash_provenances(artifacts, tmp_path: Path) -> None:
    a, qa, *_ = artifacts
    b, qb = control(artifacts, tmp_path / "control", length=12)
    result = compare(a, qa, b, qb)
    assert result == {
        "schema": "hot-frequency-compare-v1", "agreement": True, "target": "fixture", "date": "2026-10-05",
        "snapshot_db": "fixture_snapshot", "domain": {"threshold_paths": 1_000_000, "max_chars": 12}, "literals": 2,
        "comparison": "entire sorted (chars,pattern,direct_matching_paths) registry in the complete common domain",
        "provenance": [report(a, qa, thresholds=(1_000_000,), lengths=(12,), patterns=())["provenance"],
                       report(b, qb, thresholds=(1_000_000,), lengths=(12,), patterns=())["provenance"]],
    }


@pytest.mark.parametrize("change", ["pattern", "frequency"])
def test_equal_cardinality_does_not_hide_literal_or_frequency_mismatch(artifacts, tmp_path: Path, change: str) -> None:
    a, qa, *_ = artifacts
    b, qb = control(artifacts, tmp_path / "control", change=change)
    assert report(b, qb, thresholds=(1_000_000,), lengths=(16,))["grid"][0]["literals"] == 2
    with pytest.raises(ValueError) as caught:
        compare(a, qa, b, qb)
    assert str(caught.value) == "accepted census controls disagree in the complete common T/L frequency domain"


@pytest.mark.parametrize("identity", [{"target": "other_fixture"}, {"date": "2026-10-04"}, {"snapshot_db": "other_snapshot"}])
def test_controls_require_same_target_date_and_snapshot(artifacts, tmp_path: Path, identity: dict) -> None:
    a, qa, *_ = artifacts
    b, qb = control(artifacts, tmp_path / "control", identity=identity)
    with pytest.raises(ValueError) as caught:
        compare(a, qa, b, qb)
    assert str(caught.value) == "census controls must have the same target, date and snapshot database"


def test_lower_threshold_only_differences_do_not_enter_common_domain(artifacts, tmp_path: Path) -> None:
    a, qa = control(artifacts, tmp_path / "minimum", threshold=100_000, change="outside")
    b, qb = control(artifacts, tmp_path / "control")
    result = compare(a, qa, b, qb)
    assert (result["agreement"], result["domain"], result["literals"]) == (True, {"threshold_paths": 1_000_000, "max_chars": 16}, 2)


def test_comparison_pins_each_file_once_and_refuses_incomplete_control(artifacts, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a, qa, *_ = artifacts
    b, qb = control(artifacts, tmp_path / "control")
    original, calls = Path.read_bytes, []

    def read_once(path: Path) -> bytes:
        calls.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_once)
    compare(a, qa, b, qb)
    assert calls == [a, qa, b, qb]
    qb.write_bytes(b"\n".join(original(qb).splitlines()[:-1]) + b"\n")
    with pytest.raises(ValueError) as caught:
        compare(a, qa, b, qb)
    assert str(caught.value) == "hot query export lacks a valid exact-count completion footer"


def test_compare_cli_forwards_four_explicit_artifacts_and_outputs_exact_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths = [tmp_path / name for name in ("a.json", "a.jsonl", "b.json", "b.jsonl")]
    calls = []

    def compare_fixture(*args: object) -> dict:
        calls.append(args)
        return {"schema": "fixture-compare", "agreement": True}

    monkeypatch.setattr(hot_frequency_report, "compare", compare_fixture)
    result = CliRunner().invoke(main, ["ch-hot-frequency-compare", *map(str, paths)])
    assert (result.exit_code, result.stdout, result.stderr) == (0, '{"schema": "fixture-compare", "agreement": true}\n', "")
    assert calls == [tuple(paths)]
