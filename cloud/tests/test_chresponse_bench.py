"""Canonical references precede optimized timing without retaining all bodies."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from dt_cloud.bench import queryset
from dt_cloud.bench.queryset import Case
from dt_cloud.chstore import narrow_serve
from dt_cloud.chstore.response_bench import reference_bodies
from dt_cloud.chstore.client import Ch
from dt_cloud.cli import main


@pytest.mark.parametrize("cold", [False, True])
def test_reference_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cold: bool,
) -> None:
    calls = []
    cases = [Case("first", "a", "simple", ("",), ""), Case("second", "b", "regex", ("",), "")]

    def compare(store, date, prefix, query, **kwargs):
        calls.append((date, prefix, query, kwargs))
        return {"body": {"query": query, "date": date}, "response_s": 1.25}

    monkeypatch.setattr(narrow_serve, "compare_response", compare)
    with reference_bodies(None, ("d1", "d2"), "bucket", cases, previous="d0", root=tmp_path,
                          reset=(lambda: calls.append("reset")) if cold else None) as paths:
        assert list(paths) == [(date, case.id) for date in ("d1", "d2") for case in cases]
        assert [json.loads(path.read_text()) for path in paths.values()] == [
            {"body": {"query": query, "date": date}, "response_s": 1.25}
            for date in ("d1", "d2") for query in ("a", "b")
        ]
        assert sorted(path.name for path in paths.values()) == ["0.json", "1.json", "2.json", "3.json"]
    assert calls == [entry for date in ("d1", "d2") for query, syntax in (("a", "simple"), ("b", "regex"))
                     for entry in (["reset"] if cold else []) + [(date, "bucket", query, {"previous": "d0", "syntax": syntax})]]
    assert sorted(tmp_path.iterdir()) == []


def test_reference_cache_cleans_up_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    retained = tmp_path / "keep.txt"
    retained.write_text("keep\n")
    def fail(*args, **kwargs):
        raise RuntimeError("canonical unavailable")

    monkeypatch.setattr(narrow_serve, "compare_response", fail)
    with pytest.raises(RuntimeError) as caught:
        with reference_bodies(None, ("d1",), "", [Case("q", "a", "simple", ("",), "")], root=tmp_path):
            raise AssertionError("failed references must not start the timed run")
    assert str(caught.value) == "canonical unavailable"
    assert sorted(tmp_path.iterdir()) == [retained]
    assert retained.read_text() == "keep\n"


def test_reference_cache_rejects_duplicate_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as caught:
        with reference_bodies(None, ("d1", "d1"), "", [Case("q", "a", "simple", ("",), "")], root=tmp_path):
            raise AssertionError("ambiguous references must be refused")
    assert str(caught.value) == "reference cases must have unique date/query keys"
    assert sorted(tmp_path.iterdir()) == []


@pytest.mark.parametrize("cold", [False, True])
@pytest.mark.parametrize("mismatch", [False, True])
@pytest.mark.parametrize("observe_io", [False, True])
def test_compare_first_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cold: bool,
    mismatch: bool,
    observe_io: bool,
) -> None:
    from dt_cloud.chstore import bench, resources

    calls, logs = [], []
    monkeypatch.setattr("dt_cloud.cli.err", logs.append)
    manifest = {"source_db": "default", "prefix": "bucket", "dates": ["2026-10-01"]}
    monkeypatch.setattr(Ch, "scalar", lambda *args: json.dumps(manifest))
    monkeypatch.setattr(queryset, "load", lambda *args: [Case(q, q, "simple", ("",), "") for q in ("a", "b")])
    monkeypatch.setattr(bench, "drop_caches", lambda url: calls.append("reset"))
    samples = []

    def sample(device: str) -> int:
        calls.append(f"io:{device}")
        samples.append(len(samples))
        return samples[-1]

    monkeypatch.setattr(resources, "read_disk", sample)
    monkeypatch.setattr(resources, "disk_delta", lambda before, after: {"before": before, "after": after})

    def compare(store, date, prefix, query, **kwargs):
        calls.append(f"canonical:{query}")
        return {"body": {"query": query}, "response_s": 1.25}

    def response(url, target, date, query, **kwargs):
        calls.append(f"optimized:{query}")
        return {"body": {"query": "wrong" if mismatch else query}, "response_s": .25}

    monkeypatch.setattr(narrow_serve, "compare_response", compare)
    monkeypatch.setattr(narrow_serve, "response", response)
    result = CliRunner().invoke(main, ["ch-narrow-response-bench", "-cf", *(["-C"] if cold else []), *(["-i", "sda"] if observe_io else []),
                                      "-d", "2026-10-01", "-n", "2", "-Q", "unused", "-T", str(tmp_path), "narrow_test"])
    assert result.exit_code == (1 if mismatch else 0), result.output
    if mismatch:
        assert str(result.exception) == "complete response mismatch: 2026-10-01 / None / a"
    completed = [(0, "a")] if mismatch else [(trial, query) for trial in range(2) for query in ("a", "b")]
    expected_order = [item for query in ("a", "b") for item in (["reset"] if cold else []) + [f"canonical:{query}"]]
    expected_order += [item for _, query in completed for item in
                       (["reset"] if cold else []) + (["io:sda"] if observe_io else []) + [f"optimized:{query}"] + (["io:sda"] if observe_io else [])]
    assert calls == (["io:sda"] if observe_io else []) + expected_order
    assert logs == ["reference 2026-10-01 / a: 1.25s", "reference 2026-10-01 / b: 1.25s"]
    assert result.stderr == ""
    diagnostics = {}
    if mismatch:
        directory = Path(json.loads(result.stdout)["mismatch_dir"])
        diagnostics = {"mismatch_dir": str(directory)}
        assert directory.parent == tmp_path
        assert sorted(path.name for path in directory.iterdir()) == ["canonical.json", "optimized.json"]
        assert json.loads((directory / "canonical.json").read_text()) == {"query": "a"}
        assert json.loads((directory / "optimized.json").read_text()) == {"query": "wrong"}
    assert [{key: value for key, value in json.loads(line).items() if key != "sha"} for line in result.stdout.splitlines()] == [
        {"date": "2026-10-01", "previous": None, "query": query, "query_text": query, "syntax": "simple",
         "trial": trial, "prefix": "bucket", "cold": cold, "threads": 8, "response_s": .25,
         "path_free": True, "name_index": False, "name_index_variant": None, "parent_index": False,
         "ancestor_preaggregate": False, "comparison_phase": "first", "exact": not mismatch, "baseline": {"response_s": 1.25},
         **diagnostics,
         **({"device_io": {"before": 2 * index + 1, "after": 2 * index + 2}} if observe_io else {})}
        for index, (trial, query) in enumerate(completed)
    ]
    assert sorted(tmp_path.iterdir()) == ([directory] if mismatch else [])


def test_compare_first_requires_compare() -> None:
    result = CliRunner().invoke(main, ["ch-narrow-response-bench", "-f", "-d", "2026-10-01", "-Q", "unused", "narrow_test"])
    assert result.exit_code == 2
    assert result.output.splitlines() == [
        "Usage: main ch-narrow-response-bench [OPTIONS] TARGET",
        "Try 'main ch-narrow-response-bench --help' for help.", "",
        "Error: --compare-first requires --compare",
    ]
