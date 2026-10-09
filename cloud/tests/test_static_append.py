"""`dt_cloud.static_append`: a base generation plus per-scan runs, read through the reader's merge (smallest
`vt` per row), equals the generation rebuilt over every scan — the open versions and deltas, the suffix rows,
every literal's first hits (so every drill path) and answers on every date, and the catalog byte for byte;
merged runs (the binary counter) likewise."""
from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest

from dt_cloud import static_append as sa
from dt_cloud import static_catalog as sc
from dt_cloud import static_names as sn

from test_static_catalog import _gen
from test_static_names import DATES, _brute_answer, _oracle, _read, fixture  # noqa: F401

V = 3
K = 2  # scans appended after the base
TERMS = ["gof", "5418", "nk080", "48.parquet", "48.parquet.crc", "pio", "a'b", "é54", "zzz", "par", "txt", "ab", "sh", "b1", "e54", "e"]


def _sx(dirs: list[Path]) -> list[tuple]:
    return sorted(r for d in dirs for f in sorted((d / "sx").glob("*.parquet")) for r in _read(f))


def _combine(rows: list[tuple]) -> list[tuple]:
    """Suffix-row tuples `(s, depth, path, usr, vf, vt, size, n_files)`, one per `(s, path, usr, vf)`, smallest `vt`."""
    best: dict[tuple, tuple] = {}
    for r in rows:
        k = (r[0], r[2], r[3], r[4])
        if k not in best or r[5] < best[k][5]:
            best[k] = r
    return sorted(best.values())


def _reader(root: Path, side) -> sn.Reader:
    def fetch(file: str, lo: int, hi: int) -> bytes:
        with open(root / file, "rb") as fh:
            fh.seek(lo)
            return fh.read(hi - lo)

    return sn.Reader(fetch, lambda f: (root / f).stat().st_size, side)


def _catalog(d: Path) -> sc.Catalog:
    path = d / "cells.parquet"

    def fetch(lo: int, hi: int) -> bytes:
        with open(path, "rb") as fh:
            fh.seek(lo)
            return fh.read(hi - lo)

    return sc.Catalog(fetch, path.stat().st_size, pq.read_table(d / "index.parquet"))


@pytest.fixture(scope="module")
def runs(fixture, tmp_path_factory):  # noqa: F811
    root, scans, merged = fixture
    tmp = tmp_path_factory.mktemp("append")
    n = len(scans["scans"])
    full = _gen(root, scans, tmp / "full", V)
    base = _gen(root, {"bucket": "b", "scans": scans["scans"][:n - K]}, tmp / "base", V)
    con = base["con"]
    shards = sc.BaseShards(str(base["out"]), base["side"])
    ranges = base["ranges"]["ranges"]
    prev = {r["i"]: f"SELECT * FROM read_parquet({sn.q(str(base['build'] / 'cintervals' / f'r{r['i']:04d}.parquet'))})" for r in ranges}
    run_dirs, deltas, docs = [], [], []
    rg, sa.SX_RG = sa.SX_RG, 4  # several row groups per run shard
    try:
        for j in range(n - K, n):
            scan = scans["scans"][j]
            run = tmp / "runs" / scan["id"]
            for r in ranges:
                name = f"r{r['i']:04d}"
                docs.append(sa.append_open(con, prev[r["i"]], scan, r, name, run, bucket="b", mount=str(root)))
                prev[r["i"]] = f"SELECT * FROM read_parquet({sn.q(str(run / 'copen' / f'{name}.parquet'))})"
            files = sorted(str(f) for f in (run / "cdelta").glob("*.parquet"))
            deltas.append(files)
            sa.delta_shards(con, files, run, target_rows=7)
            tiers = [base["final"], *(d / "catalog" for d in run_dirs)]
            sa.catalog_delta(con, tiers, shards, deltas, V, run / "catalog", tmp / "work" / scan["id"])
            run_dirs.append(run)
        merged_dir = tmp / "runs" / "merged"
        sa.merge_shards(run_dirs, merged_dir, target_rows=9)
        sa.merge_catalogs([d / "catalog" for d in run_dirs], merged_dir / "catalog")
    finally:
        sa.SX_RG = rg
    return {"root": root, "scans": scans, "merged": merged, "full": full, "base": base, "runs": run_dirs, "merged_run": merged_dir,
            "docs": docs, "con": con, "tmp": tmp}


def test_append_open_equals_rebuild(runs):
    """Appending on the open coalesced versions alone: the open versions after each scan, and its delta (opened at D,
    closed at D), are exactly the rebuild's."""
    full = runs["full"]["build"]
    want = sorted(r for f in sorted((full / "cintervals").glob("*.parquet")) for r in _read(f))
    last = runs["runs"][-1]
    assert sorted(r for f in sorted((last / "copen").glob("*.parquet")) for r in _read(f)) == [r for r in want if r[4] == sn.OPEN]
    for run, scan in zip(runs["runs"], runs["scans"]["scans"][-K:]):
        D = scan["ts"]
        delta = [r for f in sorted((run / "cdelta").glob("*.parquet")) for r in _read(f)]
        # opened at D, open as of D (the rebuild may close it on a later scan)
        opened = sorted(r[:-1] for r in delta if r[-1] == 1)
        assert [(*r[:4], *r[5:]) for r in opened] == [(*r[:4], *r[5:]) for r in want if r[3] == D]
        assert all(r[4] == sn.OPEN for r in opened)
        # a close record is the version with its final `vt`: the rebuild's version closed at D, which may close later
        closed = sorted(r[:-1] for r in delta if r[-1] == -1)
        assert [r[:4] for r in closed] == [r[:4] for r in want if r[4] == D]
        assert all(r[4] == D for r in closed)
    assert sum(d["opened"] for d in runs["docs"]) == sum(1 for r in want if r[3] in {s["ts"] for s in runs["scans"]["scans"][-K:]})


@pytest.mark.parametrize("merged", [False, True])
def test_suffix_rows_equal_rebuild(runs, merged):
    """Base ⊕ runs under the combine rule holds exactly the rebuild's suffix rows."""
    tiers = [runs["merged_run"]] if merged else runs["runs"]
    got = _combine(_sx([runs["base"]["out"], *tiers]))
    assert got == _sx([runs["full"]["out"]])
    if merged:
        assert _sx([runs["merged_run"]]) == _combine(_sx(runs["runs"]))


@pytest.mark.parametrize("merged", [False, True])
def test_reader_equals_rebuild(runs, merged):
    """For every literal: the tiered reader's first hits (path, owner, liveness, values) equal the rebuild's — so the
    hits under any drill path on any date are equal — and its per-bucket answers equal brute force on every date."""
    tiers = [runs["merged_run"]] if merged else runs["runs"]
    base = runs["base"]
    tiered = sa.TieredReader([_reader(base["out"], base["side"]), *(_reader(d, pq.read_table(d / "sidecar.parquet")) for d in tiers)])
    rebuilt = sa.TieredReader([_reader(runs["full"]["out"], runs["full"]["side"])])
    oracle = _oracle(runs["merged"])
    nonempty = 0
    for term in TERMS:
        if len(term) < 3:
            continue
        hits = tiered.hits(term)
        assert hits == rebuilt.hits(term), term
        nonempty += bool(hits)
        assert tiered.answer(term, DATES)["answers"] == {d: _brute_answer(oracle, term, d) for d in DATES}, term
    assert nonempty >= 8


def test_catalog_equals_rebuild(runs):
    """The base catalog merged with the runs' catalogs is the rebuilt catalog byte for byte; so is the base merged with
    the merged run's; and the tiered lookup answers every literal as the rebuilt catalog does."""
    con, tmp = runs["con"], runs["tmp"]
    full = runs["full"]["final"]
    for name, tiers in (("each", [d / "catalog" for d in runs["runs"]]), ("merged", [runs["merged_run"] / "catalog"])):
        out = tmp / "check" / name
        sa.merge_catalogs([runs["base"]["final"], *tiers], out)
        assert (out / "cells.parquet").read_bytes() == (full / "cells.parquet").read_bytes(), name
        assert (out / "index.parquet").read_bytes() == (full / "index.parquet").read_bytes(), name
    tiered = sa.TieredCatalog([_catalog(runs["base"]["final"]), *(_catalog(d / "catalog") for d in runs["runs"])])
    rebuilt = _catalog(full)
    members = sorted({r["q"] for r in pq.read_table(full / "cells.parquet").to_pylist() if r["bucket"] == ""})
    assert len(members) > 20
    for t in [*members, *TERMS]:
        assert tiered.answer(t, DATES) == rebuilt.answer(t, DATES), t


def test_runs_hold_only_new_cells(runs):
    """A run's catalog is new cells (dated at its scan, or a new member's whole history) and new or changed headers."""
    base = {(r["q"], r["bucket"], r["vf"]): r for r in pq.read_table(runs["base"]["final"] / "cells.parquet").to_pylist()}
    for run, scan in zip(runs["runs"], runs["scans"]["scans"][-K:]):
        rows = pq.read_table(run / "catalog" / "cells.parquet").to_pylist()
        cells = [r for r in rows if r["bucket"] != ""]
        heads = {r["q"] for r in rows if r["bucket"] == ""}
        assert {r["q"] for r in cells} <= heads
        for r in cells:
            assert (r["q"], r["bucket"], r["vf"]) not in base
            if r["vf"] != scan["ts"]:
                assert not any(k[0] == r["q"] for k in base), r  # an older cell only for a literal new to the catalog


def test_push_run_is_a_binary_counter():
    runs: list[dict] = []
    levels, merges = [], []
    days = [f"2026-10-{d:02d}" for d in range(9, 16)]
    for d in days:
        runs, m = sa.push_run(runs, {"key": sa.run_key(d, d), "first": d, "last": d, "scans": [d]})
        levels.append([r["level"] for r in runs])
        merges.append([(["/".join(x["key"] for x in ins)], out["key"]) for ins, out in m])
    assert levels == [[0], [1], [1, 0], [2], [2, 0], [2, 1], [2, 1, 0]]
    assert merges[3] == [(["deltas/2026-10-11/deltas/2026-10-12"], "deltas/2026-10-11_2026-10-12"),
                         (["deltas/2026-10-09_2026-10-10/deltas/2026-10-11_2026-10-12"], "deltas/2026-10-09_2026-10-12")]
    assert [r["scans"] for r in runs] == [days[:4], days[4:6], days[6:]]
    m = sa.manifest("g", ["2026-10-08"], runs)
    assert m["scans"] == ["2026-10-08", *days] and m["date"] == "2026-10-15" and m["base_scans"] == 1


def test_a_literal_crosses_v_on_an_append(runs):
    """The fixture exercises the hard case: a literal that became a member at a run's scan, its whole history in that
    run's catalog (cells dated before the scan)."""
    olds = 0
    for run, scan in zip(runs["runs"], runs["scans"]["scans"][-K:]):
        olds += sum(1 for r in pq.read_table(run / "catalog" / "cells.parquet").to_pylist() if r["bucket"] != "" and r["vf"] < scan["ts"])
    assert olds > 0


def test_compaction_equals_full_build(runs, tmp_path):
    """Base ⊕ runs compacted (the k-way merge cut to the full build's shard plan) is the full build byte for byte:
    every shard file, every sidecar; likewise through the merged run."""
    import json

    full = runs["full"]["out"]
    plan = json.loads((full / "shards.json").read_text()) if (full / "shards.json").exists() else None
    if plan is None:
        from dt_cloud import static_names as sn_

        plan = sn_.plan_shards(sorted((runs["full"]["build"] / "chist").glob("*.parquet")), target_rows=60, tasks=2)
    rg, sa.SX_RG = sa.SX_RG, 5  # the fixture's build row groups
    try:
        for name, tiers in (("each", runs["runs"]), ("merged", [runs["merged_run"]])):
            out = tmp_path / name
            sa.merge_shards([runs["base"]["out"], *tiers], out, plan=plan)
            want = sorted(p.name for p in (full / "sx").glob("*.parquet"))
            assert sorted(p.name for p in (out / "sx").glob("*.parquet")) == want
            for f in want:
                assert (out / "sx" / f).read_bytes() == (full / "sx" / f).read_bytes(), (name, f)
                assert (out / "sidecar" / f).read_bytes() == (full / "sidecar" / f).read_bytes(), (name, f)
    finally:
        sa.SX_RG = rg


def test_merge_cdeltas(runs, tmp_path):
    """Two days' version deltas merged: one row per version, the smallest `vt`, `op` 1 if it opened in either day."""
    files = [str(f) for d in runs["runs"] for f in sorted((d / "cdelta").glob("*.parquet"))]
    rows = [r for f in files for r in _read(Path(f))]
    want: dict[tuple, tuple] = {}
    for r in rows:
        k = r[:4]
        cur = want.get(k)
        want[k] = r if cur is None else (*r[:4], min(cur[4], r[4]), *r[5:7], max(cur[7], r[7]))
    n = sa.merge_cdeltas(files, tmp_path / "m.parquet")
    got = _read(tmp_path / "m.parquet")
    assert got == sorted(want.values(), key=lambda r: (r[0], r[1], r[2], r[3], r[7]))
    assert n == len(want) < len(rows)
