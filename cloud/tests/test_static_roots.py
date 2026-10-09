"""`dt_cloud.static_roots`: every member's roots (from the suffix shards, fed in chunks) and every short
literal's (from the coalesced versions) are exactly its first-hit versions, and the per-directory counts
equal brute force."""
from __future__ import annotations

import pytest

from dt_cloud import static_catalog as sc
from dt_cloud import static_names as sn
from dt_cloud import static_roots as sr

from test_static_catalog import built  # noqa: F401
from test_static_names import _coalesced_oracle, _oracle, fixture  # noqa: F401

V = 4


def _versions(merged) -> list[tuple]:
    return _coalesced_oracle(_oracle(merged))


def _is_root(t: str, path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    parent = path.rsplit("/", 1)[0].lower() if "/" in path else ""
    return t in name and t not in parent


def _brute_roots(versions: list[tuple], terms: set[str]) -> list[tuple]:
    return sorted((t, *v) for v in versions if v[0] >= 1 for t in terms if _is_root(t, v[1]))


def _members(built, v: int = V) -> dict[int, list[tuple[str, int]]]:
    *_, plan, build, out, con, tmp = built
    by_shard = {}
    for s in plan["shards"]:
        sx = str(out / "sx" / f"s{s['i']:04d}.parquet")
        nodes = sc.census(con, f"read_parquet({sn.q(sx)})", 1).to_pylist()
        by_shard[s["i"]] = [(r["q"], r["rows"]) for r in nodes if r["rows"] > v]
    return by_shard


def _member_roots(built, agg: bool, chunk_rows: int) -> tuple[list[tuple], set[str]]:
    *_, plan, build, out, con, tmp = built
    members = _members(built)
    con.execute("DROP TABLE IF EXISTS rt")
    for s in plan["shards"]:
        sx = str(out / "sx" / f"s{s['i']:04d}.parquet")
        con.execute("CREATE OR REPLACE TABLE mem (q VARCHAR, rows BIGINT)")
        if members[s["i"]]:
            con.executemany("INSERT INTO mem VALUES (?, ?)", members[s["i"]])
        for where in sr.chunk_wheres(sx, chunk_rows):
            sr.member_roots(con, sr.sx_rows_sql(f"(SELECT * FROM read_parquet({sn.q(sx)}) WHERE {where})"), "mem", "rt", agg=agg)
    rows = con.execute("SELECT * FROM rt ORDER BY ALL").fetchall()
    return rows, {m for ms in members.values() for m, _ in ms}


@pytest.mark.parametrize("chunk_rows", [7, 10**9])
def test_member_roots_equal_brute_force(built, chunk_rows):  # noqa: F811
    root, scans, merged, *_ = built
    got, terms = _member_roots(built, agg=False, chunk_rows=chunk_rows)
    assert len(terms) > 20
    expected = _brute_roots(_versions(merged), terms)
    assert len(expected) > 100
    assert got == expected


def test_short_roots_equal_brute_force(built):  # noqa: F811
    root, scans, merged, ranges, plan, build, out, con, tmp = built
    con.execute("DROP TABLE IF EXISTS st")
    for r in ranges["ranges"]:
        src = str(build / "cintervals" / f"r{r['i']:04d}.parquet")
        sr.short_roots(con, f"SELECT * FROM read_parquet({sn.q(src)})", "st")
    got = con.execute("SELECT * FROM st ORDER BY ALL").fetchall()
    versions = _versions(merged)
    names = {v[1].rsplit("/", 1)[-1].lower() for v in versions if v[0] >= 1}
    shorts = {n[p:p + L] for n in names for L in (1, 2) for p in range(len(n) - L + 1)}
    assert got == _brute_roots(versions, shorts)


def _brute_dirs(roots: list[tuple]) -> dict[tuple[str, str], tuple[int, int, int]]:
    """Per (q, directory): root rows strictly under it, distinct root paths, distinct children."""
    rows: dict[tuple[str, str], int] = {}
    paths: dict[tuple[str, str], set] = {}
    kids: dict[tuple[str, str], set] = {}
    for t, depth, path, *_ in roots:
        segs = path.split("/")
        for k in range(1, depth):
            d = "/".join(segs[:k])
            rows[(t, d)] = rows.get((t, d), 0) + 1
            paths.setdefault((t, d), set()).add(path)
            kids.setdefault((t, d), set()).add("/".join(segs[:k + 1]))
    return {key: (rows[key], len(paths[key]), len(kids[key])) for key in rows}


@pytest.mark.parametrize("floor", [1, 3])
def test_stats_equal_brute_force(built, floor):  # noqa: F811
    root, scans, merged, ranges, plan, build, out, con, tmp = built
    rows, terms = _member_roots(built, agg=True, chunk_rows=7)
    roots = _brute_roots(_versions(merged), terms)
    qs, qd = sr.q_stats(con, "rt")
    per_q: dict[str, list] = {}
    for t, _depth, path, *_ in roots:
        e = per_q.setdefault(t, [0, set()])
        e[0] += 1
        e[1].add(path)
    assert sorted((r["q"], r["rows"], r["paths"]) for r in qs.to_pylist()) == sorted((t, n, len(p)) for t, (n, p) in per_q.items())
    dirs, hist, _ = sr.dir_stats(con, "rt", floor)
    brute = _brute_dirs(roots)
    assert sorted((r["q"], r["dir"], r["rows"], r["paths"], r["children"]) for r in dirs.to_pylist()) == sorted(
        (t, d, n, p, c) for (t, d), (n, p, c) in brute.items() if n >= floor)
    assert sum(r["dirs"] for r in hist.to_pylist()) == len(brute)


def test_partitioned_dir_stats_sum_to_whole(built):  # noqa: F811
    """`measure-short`'s subtree partitions: directories at depth ≥ 2 are each whole in one partition, and the
    depth-1 partials sum to the whole (children included)."""
    root, scans, merged, ranges, plan, build, out, con, tmp = built
    src = f"SELECT * FROM read_parquet({sn.q(str(build / 'cintervals' / '*.parquet'))})"
    con.execute("DROP TABLE IF EXISTS sw")
    sr.short_roots(con, src, "sw", agg=True)
    whole, _, _ = sr.dir_stats(con, "sw", 1)
    whole = sorted((r["q"], r["k"], r["dir"], r["rows"], r["paths"], r["children"], r["direct"], r["max_child"]) for r in whole.to_pylist())
    deep, top = [], {}
    for t in range(3):
        con.execute("DROP TABLE IF EXISTS sp")
        sr.short_roots(con, f"SELECT * FROM ({src}) WHERE {sr._partition(3)} = {t}", "sp", agg=True)
        d, _, tp = sr.dir_stats(con, "sp", 1, partial_top=True)
        deep += [(r["q"], r["k"], r["dir"], r["rows"], r["paths"], r["children"], r["direct"], r["max_child"]) for r in d.to_pylist()]
        for r in tp.to_pylist():
            e = top.setdefault((r["q"], 1, r["dir"]), [0, 0, 0, 0, 0])
            e[:4] = [e[0] + r["rows"], e[1] + r["paths"], e[2] + r["children"], e[3] + r["direct"]]
            e[4] = max(e[4], r["max_child"])
    assert len(whole) > 50
    assert sorted(deep + [(*k, *v) for k, v in top.items()]) == whole
