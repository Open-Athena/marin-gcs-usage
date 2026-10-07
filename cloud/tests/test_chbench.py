"""`dt-cloud ch-bench`'s pure parts: the seeded `minArea` jitter, body
normalization across engines, and the two-run comparison."""

import json
from dataclasses import replace

import pytest

from dt_cloud.chstore import bench as cb


def test_jitter_is_seeded_and_only_on_views():
    import random

    def m(name, trial):
        return f"12.{random.Random(f'7:{name}:{trial}').randrange(10**6):06d}"

    # `minArea` replaced (moved last), a function of (seed, name, trial) alone.
    assert [cb.with_jitter("/api/subtree?date=2026-10-01&path=a%2Fb&minArea=40&q=x", "root", t, 7) for t in (0, 0, 1)] == [
        f"/api/subtree?date=2026-10-01&path=a%2Fb&q=x&minArea={m('root', 0)}",
        f"/api/subtree?date=2026-10-01&path=a%2Fb&q=x&minArea={m('root', 0)}",
        f"/api/subtree?date=2026-10-01&path=a%2Fb&q=x&minArea={m('root', 1)}",
    ]
    assert m("root", 0) != m("root", 1)
    assert cb.with_jitter("/api/series?path=a", "s", 0, 7) == "/api/series?path=a"


def test_normalize_drops_engine_labels_and_provenance():
    box = {"date": "d", "tier": "ch", "index": "ch", "threshold": 3, "tree": {"n": "r", "b": 1, "c": [{"n": "a", "b": 1}]}}
    worker = {"date": "d", "tier": "bysize", "index": "d1", "threshold": 3, "partial": None, "tree": {"n": "r", "b": 1, "pv": {"x": 1}, "c": [{"n": "a", "b": 1, "pv": 2}]}}
    other = {**box, "threshold": 4}
    assert [cb.normalize(json.dumps(x).encode()) == cb.normalize(json.dumps(box).encode()) for x in (worker, other)] == [True, False]
    assert cb.normalize(b"path not found") is None


def test_compare():
    def rec(name, trial, ms, sha):
        return cb.Rec(name, trial, "/u", 200, ms, None, None, None, 1, sha, False)

    a = [rec("root", 0, 100, "s1"), rec("root", 1, 300, "s2"), rec("bk", 0, 50, "s3")]
    b = [rec("root", 0, 1000, "s1"), rec("root", 1, 2000, "zz"), rec("bk", 0, 70, "s3")]
    assert cb.compare(a, b) == [
        {"name": "root", "a_p50": 200.0, "a_max": 300, "b_p50": 1500.0, "b_max": 2000, "exact": "1/2"},
        {"name": "bk", "a_p50": 50, "a_max": 50, "b_p50": 70, "b_max": 70, "exact": "1/1"},
    ]


@pytest.mark.parametrize("before,after,error", [
    ([], [], "an empty HTTP run cannot be compared"),
    ([0, 0], [0], "duplicate HTTP request case: ('root', 0)"),
    ([0], [1], "HTTP workloads differ; names and trials must match"),
])
def test_http_compare_rejects_invalid_workloads(before, after, error):
    rows = [cb.Rec("root", trial, "/api/subtree?q=zarr", 200, 10, None, None, None, 1, "same", False) for trial in (0, 1)]
    with pytest.raises(ValueError) as caught:
        cb.compare([rows[i] for i in before], [rows[i] for i in after])
    assert str(caught.value) == error


def test_http_compare_rejects_different_request_parameters():
    row = cb.Rec("root", 0, "/api/subtree?q=zarr&minArea=12", 200, 10, None, None, None, 1, "same", False)
    with pytest.raises(ValueError) as caught:
        cb.compare([row], [replace(row, url="/api/subtree?q=zarr&minArea=24")])
    assert str(caught.value) == "HTTP request parameters differ: ('root', 0)"


@pytest.mark.parametrize("status,sha", [(500, None), (0, None), (200, None), (500, "same")])
def test_failed_http_bodies_are_counted_as_unverified(status, sha):
    row = cb.Rec("root", 0, "/api/subtree?q=zarr", status, 10, None, None, None, 1, sha, False)
    ms = 10 if status == 200 else None
    assert cb.compare([row], [replace(row, cold=True, clients=2)]) == [{
        "name": "root", "a_p50": ms, "a_max": ms, "b_p50": ms, "b_max": ms, "exact": "0/1",
    }]


def test_completed_records_survive_an_interrupted_run(tmp_path, monkeypatch):
    path = str(tmp_path / "bench.jsonl")
    expected = [cb.Rec("first", 0, "/first", 200, 123, None, None, None, 2, "44136fa355b3678a", False)]

    def fetch(base, request, token, timeout):
        if request == "/second":
            assert cb.load(path) == expected
            raise KeyboardInterrupt
        return 200, 123, {}, b"{}"

    monkeypatch.setattr(cb, "fetch", fetch)
    with pytest.raises(KeyboardInterrupt):
        cb.run("http://box", [("first", "/first"), ("second", "/second")], token=None, record_path=path)
    assert cb.load(path) == expected


def test_parallel_requests_overlap_and_persist_complete_records(tmp_path, monkeypatch):
    from threading import Barrier

    pair = Barrier(2)
    path = str(tmp_path / "parallel.jsonl")

    def fetch(base, request, token, timeout):
        pair.wait(timeout=2)
        return 200, 123, {}, b"{}"

    monkeypatch.setattr(cb, "fetch", fetch)
    actual = cb.run("http://box", [("a", "/a"), ("b", "/b")], token=None, parallel=2, record_path=path)
    expected = [cb.Rec(name, 0, f"/{name}", 200, 123, None, None, None, 2, "44136fa355b3678a", False, clients=2) for name in ("a", "b")]
    assert sorted(actual, key=lambda row: row.name) == expected
    assert sorted(cb.load(path), key=lambda row: row.name) == expected


@pytest.mark.parametrize("options,error", [
    ({"parallel": 0}, "parallel clients and trials must be positive"),
    ({"trials": 0}, "parallel clients and trials must be positive"),
    ({"parallel": 2, "cold": True}, "independent per-request cold resets cannot overlap parallel requests"),
])
def test_parallel_benchmark_rejects_invalid_runs_before_fetch(monkeypatch, options, error):
    def fetch(*args):
        raise AssertionError("invalid run must not issue requests")

    monkeypatch.setattr(cb, "fetch", fetch)
    with pytest.raises(ValueError) as caught:
        cb.run("http://box", [("a", "/a")], token=None, **options)
    assert str(caught.value) == error
