"""`dt_cloud.static_catalog`: the census finds exactly the prefixes above a floor, membership is exactly
"one or two characters, or more than V range rows", and every member's catalog answer equals brute force
on every date (and every non-member's range is at most V)."""
from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import static_catalog as sc
from dt_cloud import static_names as sn

from test_static_names import DATES, _brute_answer, _build, _oracle, fixture  # noqa: F401


def _range_rows(versions: list[tuple], t: str) -> int:
    """Σ versions × occurrences of `t` in the lowercase name (the suffix range's rows), depth ≥ 1."""
    n = 0
    for v in versions:
        if v[0] < 1:
            continue
        name = v[1].rsplit("/", 1)[-1].lower()
        n += sum(1 for p in range(len(name) - len(t) + 1) if name[p:p + len(t)] == t)
    return n


@pytest.fixture(scope="module")
def built(fixture, tmp_path_factory):  # noqa: F811
    root, scans, merged = fixture
    tmp = tmp_path_factory.mktemp("cat")
    ranges = sn.plan_ranges(scans, 2, str(root))
    build = tmp / "build"
    _build(root, scans, ranges, build)
    con = sn.connect(2, "1GB", tmp / "tmp")
    for r in ranges["ranges"]:
        sn.coalesce_range(str(build / "intervals" / f"r{r['i']:04d}.parquet"), r["i"], build, con)
    plan = sn.plan_shards(sorted((build / "chist").glob("*.parquet")), target_rows=60, tasks=2)
    mapped, out = tmp / "map", tmp / "out"
    for r in ranges["ranges"]:
        sn.map_range(str(build / "cintervals" / f"r{r['i']:04d}.parquet"), plan, r["i"], mapped, con)
    rg, sn.SX_RG = sn.SX_RG, 5  # several row groups per shard, so answers are fed in several chunks
    try:
        for t in range(len(plan["tasks"])):
            files = sorted(str(f) for f in (mapped / "sxmap" / f"g{t:03d}").glob("*.parquet"))
            sn.build_shards(files, plan, t, out, threads=2, mem="1GB", tmp=tmp / "tmp")
    finally:
        sn.SX_RG = rg
    return root, scans, merged, ranges, plan, build, out, con, tmp


V = 4


def _catalog(built, v: int = V):
    root, scans, merged, ranges, plan, build, out, con, tmp = built
    cat = tmp / f"cat-{v}"
    nodes = []
    for s in plan["shards"]:
        name = f"s{s['i']:04d}"
        t = sc.census(con, f"read_parquet({sn.q(str(out / 'sx' / f'{name}.parquet'))})", 1)
        nodes += [(r["q"], s["i"], r["rows"]) for r in t.to_pylist()]
    members = [(qq, sh, n) for qq, sh, n in nodes if n > v]
    for s in plan["shards"]:
        name = f"s{s['i']:04d}"
        con.execute("CREATE OR REPLACE TABLE mem (q VARCHAR, rows BIGINT)")
        con.executemany("INSERT INTO mem VALUES (?, ?)", [(qq, n) for qq, sh, n in members if sh == s["i"]])
        sc.shard_cells(con, str(out / "sx" / f"{name}.parquet"), "mem", cat / "cells" / f"{name}.parquet", chunk_rows=11)
    for r in ranges["ranges"]:
        sc.range_short(con, str(build / "cintervals" / f"r{r['i']:04d}.parquet"), cat / "short", f"r{r['i']:04d}")
    meta = sc.assemble(con, str(cat / "short/events/*.parquet"), str(cat / "short/vocab/*.parquet"), str(cat / "cells/*.parquet"), cat / "final")
    path = cat / "final" / "cells.parquet"

    def fetch(lo: int, hi: int) -> bytes:
        with open(path, "rb") as fh:
            fh.seek(lo)
            return fh.read(hi - lo)

    return sc.Catalog(fetch, path.stat().st_size, pq.read_table(cat / "final" / "index.parquet")), nodes, members, meta


def _substrings(names: list[str], k: int) -> set[str]:
    return {n[p:p + L] for n in names for L in range(1, k + 1) for p in range(len(n) - L + 1)}


def test_census_equals_brute_force(built):
    root, scans, merged, *_ = built
    _, nodes, _, _ = _catalog(built)
    from test_static_names import _coalesced_oracle

    versions = _coalesced_oracle(_oracle(merged))
    names = sorted({v[1].rsplit("/", 1)[-1].lower() for v in versions if v[0] >= 1})
    expected = sorted((t, _range_rows(versions, t)) for t in _substrings(names, 40) if len(t) >= 3)
    assert sorted((qq, n) for qq, _, n in nodes) == [e for e in expected if e[1] >= 1]


@pytest.mark.parametrize("v", [1, V, 8])
def test_catalog_answers_equal_brute_force(built, v):
    root, scans, merged, *_ = built
    cat, nodes, members, meta = _catalog(built, v)
    from test_static_names import _coalesced_oracle

    versions = _coalesced_oracle(_oracle(merged))
    oracle = _oracle(merged)
    names = sorted({p.rsplit("/", 1)[-1].lower() for d, p, *_ in oracle if d >= 1})
    short = {t for t in _substrings(names, 2)}
    long_members = {qq for qq, _, n in members}
    assert meta["members_short"] == len(short)
    assert meta["members_long"] == len(long_members)
    answered = 0
    for t in sorted(_substrings(names, 40) | {"zz", "q", "zzz", "b1"}):
        got = cat.answer(t, DATES)
        rows = _range_rows(versions, t)
        if len(t) <= 2:
            assert (got is not None) == (t in short), t
        else:
            assert (got is not None) == (rows > v), (t, rows)
        if got is None:
            if len(t) >= 3:
                assert rows <= v
            continue
        assert got["answers"] == {d: _brute_answer(oracle, t, d) for d in DATES}, t
        assert got["rows"] == (rows if len(t) >= 3 else -1)
        answered += 1
    assert answered > 100


def test_index_groups_are_sorted(built):
    cat, *_ = _catalog(built)
    qs = [(g["q_min"], g["q_max"]) for g in cat.groups]
    assert all(a <= b for a, b in qs)
    assert all(qs[k][1] <= qs[k + 1][0] for k in range(len(qs) - 1))


def test_brute_sql_equals_oracle(fixture):  # noqa: F811
    """`brute` straight from each scan file equals the versions' first hits on that date."""
    root, scans, merged = fixture
    con = sn.connect(2, "1GB", None)
    terms = ["gof", "5418", "pio", "a'b", "é5", "b", "1", "zzz"]
    con.execute("CREATE TABLE terms (term VARCHAR)")
    con.executemany("INSERT INTO terms VALUES (?)", [(t,) for t in terms])
    oracle = _oracle(merged)
    for s in scans["scans"]:
        got: dict = {t: {} for t in terms}
        for term, bkt, b_, o_ in con.execute(sc.brute_sql(str(root / s["src"]), s["version"], "terms")).fetchall():
            if b_ or o_:
                got[term][bkt] = [b_, o_]
        assert got == {t: _brute_answer(oracle, t, s["id"]) for t in terms}, s["id"]
