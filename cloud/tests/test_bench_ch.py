import numpy as np

from dt_cloud.bench.ch import intervals, lit, like_lit, name_sql
from dt_cloud.bench.terms import NameTest


def forest(paths: list[str]) -> tuple[list[str], np.ndarray, np.ndarray]:
    """Paths in `(depth, path)` order (the `mem` index's), with parents."""
    ps = sorted(paths, key=lambda p: (p.count("/"), p))
    ix = {p: i for i, p in enumerate(ps)}
    parent = np.array([ix[p.rsplit("/", 1)[0]] if "/" in p else -1 for p in ps], np.int32)
    depth = np.array([p.count("/") + 1 for p in ps], np.uint8)
    return ps, parent, depth


def test_intervals_nest_exactly():
    paths = ["a", "a/b", "a/b-c", "a/b/x", "a/b/y", "a/b-c/z", "a/b/x/1", "d", "d/e"]
    ps, parent, depth = forest(paths)
    pre, post = intervals(parent, depth)
    assert sorted(pre.tolist()) == list(range(len(ps)))
    for i, p in enumerate(ps):
        under = {q for j, q in enumerate(ps) if pre[i] < pre[j] <= post[i]}
        assert under == {q for q in ps if q.startswith(p + "/")}


def test_intervals_random_forest():
    rng = np.random.default_rng(7)
    paths = set()
    for _ in range(400):
        segs = [f"s{rng.integers(0, 4)}" for _ in range(rng.integers(1, 6))]
        for k in range(1, len(segs) + 1):
            paths.add("/".join(segs[:k]))
    ps, parent, depth = forest(sorted(paths))
    pre, post = intervals(parent, depth)
    for i, p in enumerate(ps):
        assert int(post[i] - pre[i]) == sum(q.startswith(p + "/") for q in ps)
        assert all(pre[i] < pre[j] <= post[i] for j, q in enumerate(ps) if q.startswith(p + "/"))


def test_literals():
    assert lit("a'b\\c") == "'a\\'b\\\\c'"
    assert like_lit("x_1%") == "'%x\\\\_1\\\\%%'"
    assert [name_sql(NameTest(op, "ab")) for op in ("contains", "starts", "ends", "equals", "regex")] == [
        "l LIKE '%ab%'",
        "startsWith(l, 'ab')",
        "endsWith(l, 'ab')",
        "l = 'ab'",
        "match(l, 'ab')",
    ]


def test_direct_trigram_lookup_is_exact_and_schema_opt_in():
    assert [name_sql(NameTest("contains", value), trigram=True) for value in ("abc", "000", "ABC", "ab", "abcd", "a_b", "漢字語")] == [
        "hasAllTokens(l, ['abc'])",
        "hasAllTokens(l, ['000'])",
        "hasAllTokens(l, ['ABC'])",
        "l LIKE '%ab%'",
        "l LIKE '%abcd%'",
        "l LIKE '%a\\\\_b%'",
        "l LIKE '%漢字語%'",
    ]
    assert name_sql(NameTest("contains", "abc")) == "l LIKE '%abc%'"
    assert name_sql(NameTest("contains", "abc"), col="name", trigram=True) == "hasAllTokens(name, ['abc'])"
    assert [name_sql(NameTest(op, "abc"), trigram=True) for op in ("starts", "ends", "equals", "regex")] == [
        "startsWith(l, 'abc')", "endsWith(l, 'abc')", "l = 'abc'", "match(l, 'abc')",
    ]
