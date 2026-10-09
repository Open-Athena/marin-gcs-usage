"""`dt_cloud.static_anchors`: anchored name search (`^q`, `q$`, `^q$`) over a base generation and per-scan runs —
every key's view at every directory on every scan, through the reader's dispatch (light, scoped roots, rollups,
stacked over the tiers; each run apart and merged), equals brute force from the versions; the name index holds
exactly one row per version; the scan-file brute force SQL equals the version brute force."""
from __future__ import annotations

import json
import random
from pathlib import Path

import fsspec
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import static_anchors as an
from dt_cloud import static_append as sa
from dt_cloud import static_catalog as sc
from dt_cloud import static_names as sn
from dt_cloud import static_roots as sr

from test_static_catalog import _gen
from test_static_names import _coalesced_oracle, _merged, _oracle, _scan_rows, _write

V = 3
R = 3
K = 2
IDS = ["2026-07-30", "2026-08-01", "2026-08-15", "2026-09-01", "2026-09-15", "2026-10-01"]
BASE = 4


def _paths() -> list[str]:
    """Names that exercise the anchors: a starts-with root below a contains root (`big-tomato/tomato.txt` for
    `^tomat`), a matching directory covering matches (`tomat/tomato.json`, `sub.json/in.json`, `config.json/config.json`),
    suffixes that are not the key (`.jsonl`, `.json.gz`), case, buckets that match, and enough `.json` files at
    several depths for heavy directories at R = 3."""
    p = ["b1", "b2", "tomato-bkt",
         "b1/big-tomato", "b1/big-tomato/tomato.txt", "b1/big-tomato/tomatoes.json",
         "b1/tomat", "b1/tomat/tomato.json", "b1/tomat/x",
         "b1/data", "b1/data/c.jsonl", "b1/data/x.json.gz", "b1/data/E.JSON", "b1/data/sub.json", "b1/data/sub.json/in.json",
         "b1/data/deep", "b1/data/deep/er",
         "b2/cfg", "b2/cfg/config.json", "b2/config.json", "b2/config.json/config.json", "b2/x", "b2/x/config.json", "b2/x/Config.JSON",
         "b2/train", "b2/train/train-0.bin", "b2/train-1.bin", "b2/xtrain", "b2/xtrain/train-2.bin", "b2/Train-3.BIN",
         "tomato-bkt/a.json", "tomato-bkt/tomato.json",
         # a parent holding the key inside a segment that doesn't match it: a contains-style test would drop these
         "b1/a.jsonx", "b1/a.jsonx/k.json", "b2/config.json.bak", "b2/config.json.bak/config.json"]
    p += [f"b1/data/f{i}.json" for i in range(6)]
    p += [f"b1/data/deep/g{i}.json" for i in range(4)]
    p += [f"b1/data/deep/er/h{i}.json" for i in range(4)]
    p += [f"b2/json/j{i}.json" for i in range(3)]
    p += ["b2/json"]
    p += [f"b2/cfg/c{i}/config.json" for i in range(4)] + [f"b2/cfg/c{i}" for i in range(4)]
    return p


KEYS = [an.term_key(t, m) for t, m in [
    (".json", "end"), ("json", "end"), ("son", "end"), (".bin", "end"), ("bin", "end"), ("ato", "end"), ("-bkt", "end"), ("zzz", "end"),
    ("tomat", "start"), ("train", "start"), ("conf", "start"), ("tr", "start"), ("b1", "start"), ("zz", "start"),
    ("config.json", "exact"), ("x", "exact"), ("train", "exact"), ("tomato.json", "exact"), ("b2", "exact"), ("nope", "exact"),
]]


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return build_world(tmp_path_factory.mktemp("anchors"))


def build_world(tmp: Path) -> dict:
    """The scans (`_paths` over `IDS`), the base generation (the first `BASE` scans) with its name index and
    anchors, a run per later scan (`names/`, `anchors/`, catalog), and the runs merged (also the TS fixture's,
    `site/functions/_lib/fixtures/static-anchors/gen.py`)."""
    root = tmp / "data"
    rng = random.Random(11)
    paths = _paths()
    scans, merged = [], []
    for j, d in enumerate(IDS):
        v = 2 if j >= 2 else 1
        rows = _scan_rows(rng, paths, j)
        key = f"listing/{d}/path-index.parquet"
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        _write(rows, root / key, v)
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": v})
        merged.append((sn.scan_epoch(d), _merged(rows, v)))
    base = _gen(root, {"bucket": "b", "scans": scans[:BASE]}, tmp / "base", V)
    con = base["con"]
    bdir = base["out"]
    plan = sn.plan_shards(sorted((base["build"] / "chist").glob("*.parquet")), target_rows=60, tasks=2)  # `_gen`'s plan
    (bdir / "shards.json").write_text(json.dumps(plan) + "\n")
    pq.write_table(base["side"], bdir / "sidecar.parquet")
    cint = sorted(str(f) for f in (base["build"] / "cintervals").glob("*.parquet"))
    rg = (sa.SX_RG, an.SX_RG, an.ROLLUP_RG, sr.IDX_RG)
    # several row groups per shard (the dispatch slack is the larger of the two), per rollup file and per index file
    sa.SX_RG, an.SX_RG, an.ROLLUP_RG, sr.IDX_RG = 4, 5, 4, 3
    try:
        an.write_names(con, an.files_sql(cint, "depth, path, usr, vf, vt, size, n_files"), bdir / an.NAMES, target_rows=11)
        an.build_tier_local(con, an.Tier(bdir).files("end"), an.Tier(bdir).files("exact"), bdir / an.ANCHORS, R, K)
        shards = sc.BaseShards(str(bdir), base["side"])
        ranges = base["ranges"]["ranges"]
        prev = {r["i"]: f"SELECT * FROM read_parquet({sn.q(str(base['build'] / 'cintervals' / f'r{r['i']:04d}.parquet'))})" for r in ranges}
        runs: list[an.Tier] = []
        deltas = []
        for j in range(BASE, len(IDS)):
            scan = scans[j]
            run = tmp / "runs" / scan["id"]
            for r in ranges:
                name = f"r{r['i']:04d}"
                sa.append_open(con, prev[r["i"]], scan, r, name, run, bucket="b", mount=str(root))
                prev[r["i"]] = f"SELECT * FROM read_parquet({sn.q(str(run / 'copen' / f'{name}.parquet'))})"
            files = sorted(str(f) for f in (run / "cdelta").glob("*.parquet"))
            deltas.append(files)
            sa.delta_shards(con, files, run, target_rows=7)
            sa.catalog_delta(con, [base["final"], *(t.root / "catalog" for t in runs)], shards, deltas, V, run / "catalog", tmp / "work" / scan["id"])
            an.build_run_local(con, [an.Tier(bdir), *runs], an.Tier(run), scan["ts"], R, K, [scan["id"]],
                               versions_sql=an.files_sql(files, "depth, path, usr, vf, vt, size, n_files"), names_rows=7)
            runs.append(an.Tier(run))
        merged_dir = tmp / "runs" / "merged"
        sa.merge_shards([t.root for t in runs], merged_dir, target_rows=9)
        an.merge_run_local(con, runs, an.Tier(merged_dir), R, K, IDS[BASE:], names_rows=9)
    finally:
        sa.SX_RG, an.SX_RG, an.ROLLUP_RG, sr.IDX_RG = rg
    oracle = _oracle(merged)
    versions = [(path, usr, vf * 1000, vt * 1000, size, n) for depth, path, usr, vf, vt, _k, size, n, *_ in oracle if depth >= 1]
    return {"root": root, "scans": scans, "base": bdir, "runs": [t.root for t in runs], "merged": merged_dir, "oracle": oracle,
            "versions": versions, "paths": paths, "con": con, "cint": cint, "catalog": base["final"]}


def _reader(dirs: list[Path], **kw) -> an.AnchoredReader:
    fs = fsspec.filesystem("file")
    return an.AnchoredReader([an.reader_tier(fs, str(d)) for d in dirs], **kw)


def _dirs(paths: list[str]) -> list[str]:
    out = {""}
    for p in paths:
        parts = p.split("/")
        for k in range(1, len(parts)):
            out.add("/".join(parts[:k]))
        out.add(p)
    return sorted(out) + ["nope"]


def _views(world, stack: list[Path], dates: list[str], keys=KEYS, **kw) -> tuple[list, list, dict]:
    reader = _reader(stack, **kw)
    got, want, sources = [], [], {}
    for key in keys:
        for P in _dirs(world["paths"]):
            v = reader.view(key, P, dates)
            sources[v["source"]] = sources.get(v["source"], 0) + 1
            if v["source"] == "plain":
                assert an.term_in_path(key, P)
                continue
            if v["source"] == "declined":
                continue
            for d in dates:
                D = sn.scan_epoch(d) * 1000
                b = an.brute_view(world["versions"], key, P, D)
                if v["source"] == "rollup":
                    kept = v["answers"][d]
                    rest = [sum(x[0] for c, x in b.items() if c not in kept), sum(x[1] for c, x in b.items() if c not in kept)]
                    got.append((key, P, d, kept, v["rest"][d]))
                    want.append((key, P, d, {c: b[c] for c in kept if c in b}, rest))
                else:
                    got.append((key, P, d, v["answers"][d]))
                    want.append((key, P, d, b))
    return got, want, sources


def test_name_index_is_one_row_per_version(world):
    rows = sorted((r["s"], r["path"], r["usr"], r["vf"], r["vt"]) for f in sorted((world["base"] / an.NAMES / "sx").glob("*.parquet"))
                  for r in pq.read_table(f).to_pylist())
    end = sn.scan_epoch(IDS[BASE - 1])
    want = sorted(("/" + path.rsplit("/", 1)[-1].lower(), path, usr, vf, vt if vt <= end else sn.OPEN)
                  for depth, path, usr, vf, vt, *_ in _coalesced_oracle(world["oracle"]) if depth >= 1 and vf <= end)
    as_s = [(s, p, u, int(vf.timestamp()), int(vt.timestamp())) for s, p, u, vf, vt in rows]
    assert as_s == want


def test_base_views_equal_brute_force(world):
    got, want, sources = _views(world, [world["base"]], IDS[:BASE])
    assert got == want
    assert {k for k, n in sources.items() if n} == {"plain", "light"}  # at the fixture's scale every range is light


def test_base_views_at_a_small_light_bound(world):
    """With the whole-range bound at 0 (and 8) rows the `q$` / `^q$` views with rows are scoped (roots or a rollup), the
    `^q` ones declined."""
    got, want, sources = _views(world, [world["base"]], IDS[:BASE], max_rows=0)
    assert got == want
    assert sources["rollup"] > 0 and sources["roots"] > 0 and sources["declined"] > 0  # light: only the keys with no rows
    got, want, sources = _views(world, [world["base"]], IDS[:BASE], max_rows=8)
    assert got == want
    assert min(sources[k] for k in ("light", "roots", "rollup", "declined", "plain")) > 0


@pytest.mark.parametrize("stack", ["run1", "run2", "merged"])
def test_tiered_views_equal_brute_force(world, stack):
    tiers = {"run1": [world["base"], world["runs"][0]], "run2": [world["base"], *world["runs"]], "merged": [world["base"], world["merged"]]}[stack]
    dates = IDS[:BASE + (1 if stack == "run1" else 2)]
    for mr in (an.MAX_ROWS, 8, 0):
        got, want, sources = _views(world, tiers, dates, max_rows=mr)
        assert got == want
        if mr != an.MAX_ROWS:
            assert sources["rollup"] > 0 and sources["roots"] > 0


def test_runs_make_directories_heavy_and_write_deltas(world):
    """The fixture's runs hit both run cases: a delta header on a heavy directory and a directory made heavy."""
    kinds = []
    for d in world["runs"]:
        for f in sorted((d / an.ANCHORS / "rollups").glob("*.parquet")):
            kinds += [r["kind"] for r in pq.read_table(f).to_pylist()]
    assert {-1, 0} <= set(kinds)


def test_brute_sql_equals_brute_force(world):
    con = world["con"]
    cases = [(key, *an.parse_key(key), P) for key in KEYS for P in ["", "b1", "b1/data", "b2", "b2/cfg"]]
    con.execute("CREATE OR REPLACE TABLE cases (key VARCHAR, k VARCHAR, m VARCHAR, P VARCHAR)")
    con.executemany("INSERT INTO cases VALUES (?, ?, ?, ?)", cases)
    for s in world["scans"]:
        got: dict = {(key, P): {} for key, _, _, P in cases}
        for key, P, child, b, o in con.execute(an.brute_sql(str(world["root"] / s["src"]), s["version"], "cases")).fetchall():
            if b or o:
                got[(key, P)][child] = [b, o]
        D = sn.scan_epoch(s["id"]) * 1000
        want = {(key, P): an.brute_view(world["versions"], key, P, D) for key, _, _, P in cases}
        # the plain views (P holds the key) are the reader's business, not brute force's: compare the rest
        assert {c: v for c, v in got.items() if not an.term_in_path(c[0], c[1])} == {c: v for c, v in want.items() if not an.term_in_path(c[0], c[1])}


def test_a_mutated_reader_fails(world, monkeypatch):
    """Mutation checks: a contains-style parent test, a stack that ignores the runs' cells and a largest-`vt` combine
    each make some view differ from brute force."""
    tiers = [world["base"], *world["runs"]]
    dates = IDS[:BASE + 2]

    def differs() -> bool:
        return any(got != want for got, want, _ in (_views(world, tiers, dates, max_rows=mr) for mr in (an.MAX_ROWS, 0)))

    assert not differs()
    with monkeypatch.context() as m:
        orig = an.fold

        def contains_parent(rows, key, under=None):
            """The contains literal's parent test (the lowercase parent holds the literal) on top of the per-segment one."""
            text = an.parse_key(key)[0]
            return [r for r in orig(rows, key, under) if text not in (r["path"].rsplit("/", 1)[0].lower() if "/" in r["path"] else "")]
        m.setattr(an, "fold", contains_parent)
        assert differs()
    with monkeypatch.context() as m:
        m.setattr(an, "stack", lambda parts: (lambda ps: (ps[0][0], ps[0][1:]) if ps else None)([p for p in parts if p]))
        assert differs()
    with monkeypatch.context() as m:
        def worst(parts):
            best: dict = {}
            for p in parts:
                for h in p:
                    k = (h["path"], h["usr"], h["vf"])
                    if k not in best or h["vt"] > best[k]["vt"]:
                        best[k] = h
            return list(best.values())
        m.setattr(an, "combine", worst)
        assert differs()
