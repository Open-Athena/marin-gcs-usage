"""`dt-cloud ch-bench`'s pure parts: the seeded `minArea` jitter, body
normalization across engines, and the two-run comparison."""

import json

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
