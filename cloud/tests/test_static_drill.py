"""`dt_cloud.static_drill`: a base drill plus per-scan runs (each separately, and merged as the binary counter merges them),
read as the Worker reads them, equals the drill rebuilt over every scan through D — every member's roots, every heavy
directory's rollup — and every filtered view at every directory and date equals brute force. The scans are designed so the
runs hit each hard case: classes splitting, a child entering the kept set (an existing one and a new one), a directory
becoming heavy, a literal becoming a member, probes either way, closes, resurrection and multi-owner paths."""
from __future__ import annotations

import random
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import static_append as sa
from dt_cloud import static_catalog as sc
from dt_cloud import static_drill as sd
from dt_cloud import static_names as sn
from dt_cloud import static_roots as sr

from test_static_catalog import _gen
from test_static_names import _coalesced_oracle, _merged, _oracle, _write

DAYS = ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05"]
BASE = 3  # scans in the base generation; the rest are runs
V = 2
CONFIGS = [{"R": 3, "K": 2, "floor": 2, "rg": 4, "run_rg": 2}, {"R": 5, "K": 1, "floor": 3, "rg": 3, "run_rg": 3}]
NOISE = ["gof.txt", "x5418y", "nk080", "48.parquet", "a'b", "é5418", "sh", "ab", "ckpt_old.bin", "data.jsonl"]


def _files(j: int) -> dict[str, list[tuple[str | None, int]]]:
    """Scan `j`'s files `{path: [(owner, bytes)]}`; each a case for the runs (scans BASE, BASE + 1)."""
    rng = random.Random(100 + j)
    f: dict[str, list[tuple[str | None, int]]] = {}

    def add(p: str, size: int, usr: str | None = None) -> None:
        f.setdefault(p, []).append((usr, size))

    for r, (n, s) in {"run1": (3, 100), "run2": (2, 80), "run3": (1, 10), "run4": (1, 5), "run5": (1, 3)}.items():
        for i in range(n):
            add(f"b1/runs/{r}/ckpt-{i}.bin", s)
    if j >= 3:  # an existing directory child outside the kept set grows past it
        for i in range(1, 4):
            add(f"b1/runs/run4/ckpt-{i}.bin", 100)
    if j >= 4:  # a new directory child with more than any kept child: one leaves
        for i in range(2):
            add(f"b1/runs/run6/ckpt-{i}.bin", 500)
    add("b1/runs/runx/notes.txt", 7)
    if j >= 3:  # a directory child with no earlier `ckpt` root: probed, new
        add("b1/runs/runx/ckpt-0.bin", 1)
    for i in range(6):
        if (i == 3 and j == 2) or (i == 1 and j == 4):  # f3 gone for a scan and back (resurrection); f1 deleted (closes)
            continue
        add(f"b1/data/f{i}.json", 1000 if (i == 5 and j >= 3) else 10 + i)  # f5 re-versioned past the kept set
    if j >= 3:
        add("b1/data/x.js", 4)  # `.js` leaves `.json`'s class
    add("b2/new/ckpt-a.bin", 2)
    if j >= 3:  # b2/new becomes heavy
        for c in "bcdef":
            add(f"b2/new/ckpt-{c}.bin", 2)
    if j >= 3:  # `zebra` becomes a member
        for i in range(4):
            add(f"b2/zoo/zebra{i}.txt", 1)
    if j >= 4:
        add("b2/zoo/zebra9.txt", 1)
    add("b2/mix/ckpt-m.bin", 30)
    add("b2/mix/ckpt-m.bin", 20 if j < 3 else 25, "alice")  # one owner's slice changes
    for d in ("b3/p", "b3/q", "b3/p/r"):
        for n in NOISE:
            if rng.random() < 0.6:
                add(f"{d}/{n}", rng.choice([1, 2, 3, 50]))
    return f


def _rows(files: dict) -> list[dict]:
    """A scan's rows: the files and every directory (per owner: Σ bytes and files under it), as a layer-2 rolls them up."""
    acc: dict[tuple[str, str | None], list] = {}
    for p, slices in files.items():
        parts = p.split("/")
        for usr, size in slices:
            for k in range(1, len(parts) + 1):
                e = acc.setdefault(("/".join(parts[:k]), usr), [0, 0, k == len(parts)])
                e[0] += size
                e[1] += 1
    return [{"path": p, "depth": p.count("/") + 1, "usr": usr, "size": s, "n_files": n, "mtime_mean": 1.0, "last_read": 5,
             "kind": "file" if leaf else "dir"} for (p, usr), (s, n, leaf) in sorted(acc.items(), key=lambda x: (x[0][0], x[0][1] or ""))]


def _sx_files(d: Path) -> list[str]:
    return [str(p) for p in sorted((d / "sx").glob("*.parquet"))]


def _reader(d: Path) -> sn.Reader:
    def fetch(file: str, lo: int, hi: int) -> bytes:
        with open(d / file, "rb") as fh:
            fh.seek(lo)
            return fh.read(hi - lo)

    return sn.Reader(fetch, lambda f: (d / f).stat().st_size, pq.read_table(d / "sidecar.parquet"))


def _drill(con, g: dict, out: Path, R: int, K: int, floor: int) -> dict[str, str]:
    """A generation's whole drill (`build_drill`, aliases by exact root set) in `out`, and its measurement files
    `{kind: path}` (`(q, k, dir, rows)` at ≥ `floor` rows, as `roots measure`)."""
    members = sorted({r["q"] for r in pq.read_table(g["final"] / "cells.parquet").to_pylist() if r["bucket"] == "" and len(r["q"]) >= 3})
    con.execute("CREATE OR REPLACE TABLE mem (q VARCHAR)")
    con.executemany("INSERT INTO mem VALUES (?)", [(m,) for m in members])
    con.execute("DROP TABLE IF EXISTS lall; DROP TABLE IF EXISTS sall")
    sx = "[" + ", ".join(sn.q(f) for f in _sx_files(g["out"])) + "]"
    sr.member_roots(con, sr.sx_rows_sql(f"read_parquet({sx})"), "mem", "lall")
    acc: dict[str, list] = {m: [] for m in members}
    for qq, *row in con.execute("SELECT q, path, usr, vf, vt, size, n_files FROM lall ORDER BY ALL").fetchall():
        acc[qq].append(tuple(row))
    sets = {m: tuple(rows) for m, rows in acc.items()}
    canon: dict[tuple, str] = {}
    for m in members:
        canon.setdefault(sets[m], m)
    aliases = pa.table({"q": members, "canonical": [canon[sets[m]] for m in members]})
    con.execute("CREATE OR REPLACE TABLE canon (q VARCHAR)")
    con.executemany("INSERT INTO canon VALUES (?)", [(c,) for c in sorted(set(canon.values()))])
    con.execute("CREATE OR REPLACE TABLE lcan AS SELECT * FROM lall SEMI JOIN canon USING (q)")
    ci = "[" + ", ".join(sn.q(str(p)) for p in sorted((g["build"] / "cintervals").glob("*.parquet"))) + "]"
    sr.short_roots(con, f"SELECT * FROM read_parquet({ci})", "sall")
    sd.build_drill(con, out, long_roots="lcan", short_roots="sall", aliases=aliases, R=R, K=K)
    meas = {}
    for kind, table in (("long", "lall"), ("short", "sall")):
        con.execute(f"""CREATE OR REPLACE TABLE rp AS SELECT q, depth, path, count(*) AS n, count(*) FILTER (WHERE vt = {sn.OPEN}) AS n_open
            FROM {table} GROUP BY q, depth, path""")
        dirs, _, _ = sr.dir_stats(con, "rp", floor)
        meas[kind] = str(out.parent / f"meas-{kind}.parquet")
        pq.write_table(dirs.select(["q", "k", "dir", "rows"]), meas[kind])
    return meas


@pytest.fixture(scope="module", params=range(len(CONFIGS)), ids=lambda i: "R{R}-K{K}".format(**CONFIGS[i]))
def world(request, tmp_path_factory):
    cfg = CONFIGS[request.param]
    R, K, floor = cfg["R"], cfg["K"], cfg["floor"]
    root = tmp_path_factory.mktemp("drill")
    scans, merged = [], []
    for j, d in enumerate(DAYS):
        rows = _rows(_files(j))
        key = f"listing/{d}/path-index.parquet"
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        _write(rows, root / key, 2)
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": 2})
        merged.append((sn.scan_epoch(d), _merged(rows, 2)))
    gens = {j: _gen(root, {"bucket": "b", "scans": scans[:j + 1]}, root / f"gen{j}", V) for j in range(BASE - 1, len(DAYS))}
    base = gens[BASE - 1]
    pq.write_table(base["side"], base["out"] / "sidecar.parquet")
    con = base["con"]
    old_rg, sr.ROOT_RG = sr.ROOT_RG, cfg["rg"]
    old_idx, sr.IDX_RG = sr.IDX_RG, 3
    try:
        drills, meas = {}, None
        for j, g in gens.items():
            m = _drill(con, g, root / f"drill{j}" / "drill", R, K, floor)
            drills[j] = sd.Tier(root / f"drill{j}" / "drill", f"rebuild-{j}")
            meas = m if j == BASE - 1 else meas
        # the light runs (static_append), then each run's drill
        shards = sc.BaseShards(str(base["out"]), base["side"])
        prev = {r["i"]: f"SELECT * FROM read_parquet({sn.q(str(base['build'] / 'cintervals' / f'r{r['i']:04d}.parquet'))})" for r in base["ranges"]["ranges"]}
        runs, deltas, prior, metas = [], [], [sd.Tier(root / f"drill{BASE - 1}" / "drill", "base")], []
        cintervals = [str(p) for p in sorted((base["build"] / "cintervals").glob("*.parquet"))]
        for j in range(BASE, len(DAYS)):
            scan = scans[j]
            run = root / "runs" / scan["id"]
            for r in base["ranges"]["ranges"]:
                name = f"r{r['i']:04d}"
                sa.append_open(con, prev[r["i"]], scan, r, name, run, bucket="b", mount=str(root))
                prev[r["i"]] = f"SELECT * FROM read_parquet({sn.q(str(run / 'copen' / f'{name}.parquet'))})"
            cdelta = [str(p) for p in sorted((run / "cdelta").glob("*.parquet"))]
            sa.delta_shards(con, cdelta, run, target_rows=7)
            sa.catalog_delta(con, [base["final"], *(d / "catalog" for d in runs)], shards, [*deltas, cdelta], V, run / "catalog", root / "work" / scan["id"])
            con.execute(f"CREATE OR REPLACE TABLE pnew AS {sd.pnew_sql(cdelta, cintervals + [f for d in deltas for f in d])}")
            heads = {r["q"] for r in pq.read_table(run / "catalog" / "cells.parquet").to_pylist() if r["bucket"] == "" and len(r["q"]) >= 3}
            new = sorted(heads - set(prior[-1].alias_table().column("q").to_pylist()))
            history = sd.history_rows([_reader(base["out"]), *(_reader(d) for d in runs), _reader(run)], new)
            metas.append(sd.build_day(con, prior, run / "drill", D=scan["ts"], R=R, K=K, floor=floor, sx_files=_sx_files(run), cdelta_files=cdelta,
                                      new_members=new, history=history, meas={k: sd.meas_sql(k, [v]) for k, v in meas.items()},
                                      tier={"first": scan["id"], "last": scan["id"], "level": 0, "scans": [scan["id"]]}, rg=cfg["run_rg"],
                                      log=lambda *_: None))
            runs.append(run)
            deltas.append(cdelta)
            prior.append(sd.Tier(run / "drill", scan["id"]))
        sd.merge_tiers(con, prior[1:], root / "merged" / "drill", tier={"first": DAYS[BASE], "last": DAYS[-1], "level": 1, "scans": DAYS[BASE:]})
        merged_tier = sd.Tier(root / "merged" / "drill", "merged")
    finally:
        sr.ROOT_RG, sr.IDX_RG = old_rg, old_idx
    return {"cfg": cfg, "base_out": base["out"], "run_dirs": runs, "scans": scans, "merged": merged, "drills": drills, "base": prior[0], "runs": prior[1:], "merged_tier": merged_tier,
            "metas": metas, "con": con}


# (name, last scan index, tiers)
STACKS = [("run1", BASE, lambda w: [w["base"], w["runs"][0]]),
          ("run1+run2", BASE + 1, lambda w: [w["base"], *w["runs"]]),
          ("merged", BASE + 1, lambda w: [w["base"], w["merged_tier"]])]


def _members(tier: sd.Tier, kind: str) -> list[str]:
    if kind == "long":
        return sorted(tier.alias_table().column("q").to_pylist())
    return sorted({r for f in tier.files("short", "roots") for r in pq.read_table(f, columns=["q"]).column("q").to_pylist()})


def _rollup(tiers: list[sd.Tier], kind: str, t: str, d: str) -> tuple | None:
    """The reader's rollup of `(t, d)` over `tiers`: `(header (kept, rows, children), sorted cells)`, or None."""
    header, cells = None, []
    for tier in reversed(tiers):
        gf = tier.group_file(kind, "rollups")
        c = tier.canon(t, kind)
        rows = gf.read((c, d), (c, d + "\x00"))[0] if gf is not None else []
        if not rows:
            continue
        header = header or rows[0]
        cells += rows[1:]
        if rows[0]["kind"] == sd.KIND_FULL:
            break
    if header is None:
        return None
    return ((header["vf"], header["b"], header["o"]), sorted((r["kind"], r["child"], r["vf"], r["b"], r["o"]) for r in cells))


def _heavy(tiers: list[sd.Tier], kind: str, members: list[str]) -> set[tuple[str, str]]:
    """Every `(member, dir)` with a rollup header in some tier."""
    out = set()
    for tier in tiers:
        heads = {(r["q"], r["dir"]) for f in tier.files(kind, "rollups") for r in pq.read_table(f).to_pylist() if r["kind"] in (sd.KIND_FULL, sd.KIND_DELTA)}
        by_canon: dict[str, list[str]] = {}
        for m in members:
            by_canon.setdefault(tier.canon(m, kind), []).append(m)
        out |= {(m, d) for c, d in heads for m in by_canon.get(c, [])}
    return out


@pytest.mark.parametrize("stack", STACKS, ids=[s[0] for s in STACKS])
@pytest.mark.parametrize("kind", sd.KINDS)
def test_roots_equal_rebuild(world, stack, kind):
    """Every member's roots over base ⊕ runs (each tier's own canonical, combined: smallest `vt`) are the rebuild's."""
    _, j, tiers_of = stack
    tiers, rebuilt = tiers_of(world), world["drills"][j]
    members = _members(rebuilt, kind)
    if kind == "long":
        assert _members(tiers[-1], kind) == members
    got, want = sd.TierReads(tiers, kind), sd.TierReads([rebuilt], kind)
    nonempty = 0
    for t in members:
        a = sorted(tuple(r[k] for k in ("path", "usr", "vf", "vt", "size", "n_files")) for r in got.rows(t, "", "\U0010ffff"))
        assert a == sorted(tuple(r[k] for k in ("path", "usr", "vf", "vt", "size", "n_files")) for r in want.rows(t, "", "\U0010ffff")), t
        nonempty += bool(a)
    assert nonempty > 20


@pytest.mark.parametrize("stack", STACKS, ids=[s[0] for s in STACKS])
@pytest.mark.parametrize("kind", sd.KINDS)
def test_rollups_equal_rebuild(world, stack, kind):
    """Every rollup the rebuild holds, the tiers hold too, equal (header, kept children's cells, remainder's). A rollup only the
    tiers hold is a directory whose stored rows over the tiers (close records counted per tier) exceed R while its roots don't."""
    _, j, tiers_of = stack
    tiers, rebuilt = tiers_of(world), world["drills"][j]
    R = world["cfg"]["R"]
    members = _members(rebuilt, kind)
    want_heavy, got_heavy = _heavy([rebuilt], kind, members), _heavy(tiers, kind, members)
    assert want_heavy <= got_heavy
    for t, d in sorted(want_heavy):
        assert _rollup(tiers, kind, t, d) == _rollup([rebuilt], kind, t, d), (t, d)
    built = [world["base"], *world["runs"][:j - BASE + 1]]  # the tiers as each run was built (a merge only collapses rows)
    for t, d in sorted(got_heavy - want_heavy):
        stored = sum(len(sd.TierReads([tier], kind).rows(t, d + "/", d + "0")) for tier in built)
        assert len(sd.TierReads([rebuilt], kind).rows(t, d + "/", d + "0")) <= R < stored, (t, d)
    assert len(want_heavy) > 20


def _brute_view(versions: list[tuple], t: str, P: str, date: str) -> dict[str, list[int]]:
    D = sn.scan_epoch(date)
    acc: dict[str, list[int]] = {}
    for depth, path, usr, vf, vt, size, n_files in versions:
        name = path.rsplit("/", 1)[-1].lower()
        parent = path.rsplit("/", 1)[0].lower() if "/" in path else ""
        if depth >= 1 and vf <= D < vt and path.startswith(P + "/") and t in name and t not in parent:
            e = acc.setdefault(path[len(P) + 1:].split("/", 1)[0], [0, 0])
            e[0] += size
            e[1] += n_files
    return {k: v for k, v in sorted(acc.items()) if v != [0, 0]}


@pytest.mark.parametrize("stack", STACKS, ids=[s[0] for s in STACKS])
def test_views_equal_brute_force(world, stack):
    """Every member's filtered view at every directory on every date, read over base ⊕ runs as the Worker reads it: a roots
    answer is brute force child for child; a rollup's kept children are brute force and its remainder the rest. Where the
    rebuild dispatches the same way, its answer is the same."""
    _, j, tiers_of = stack
    tiers, rebuilt = tiers_of(world), world["drills"][j]
    versions = _coalesced_oracle(_oracle(world["merged"][:j + 1]))
    dates = DAYS[:j + 1]
    dirs = sorted({p.rsplit("/", 1)[0] for _, p, *_ in versions if "/" in p} | {"nope"})
    K = world["cfg"]["K"]
    sources = {"roots": 0, "rollup": 0, "plain": 0, "same": 0}
    for kind in sd.KINDS:
        got, want = sd.TieredDrill(tiers, kind), sd.TieredDrill([rebuilt], kind)
        for t in _members(rebuilt, kind):
            for P in dirs:
                a = got.view(t, P, dates)
                sources[a["source"]] += 1
                if a["source"] == "plain":
                    continue
                for d in dates:
                    exp = _brute_view(versions, t, P, d)
                    if a["source"] == "roots":
                        assert a["answers"][d] == exp, (t, P, d)
                    else:
                        kept = a["answers"][d]
                        assert kept == {c: v for c, v in exp.items() if c in kept}, (t, P, d)
                        assert a["rest"][d] == [sum(v[i] for c, v in exp.items() if c not in kept) for i in (0, 1)], (t, P, d)
                        assert a["header"]["kept"] <= K
                b = want.view(t, P, dates)
                if b["source"] == a["source"]:
                    sources["same"] += 1
                    assert {k: a[k] for k in ("answers", "rest", "header", "kept") if k in a} == {k: b[k] for k in ("answers", "rest", "header", "kept") if k in b}, (t, P)
                if a["source"] == "roots":
                    assert a["rows"] <= got.thr
    assert sources["roots"] > 300 and sources["rollup"] > 20 and sources["same"] > 300, sources


def test_the_runs_hit_every_case(world):
    """The fixture exercises each hard case at least once across the two runs."""
    long = [m["long_day"] for m in world["metas"]]
    short = [m["short_day"] for m in world["metas"]]
    cls = [m["classes"] for m in world["metas"]]
    assert any(c["classes"] > c["classes_before"] for c in cls)  # a class split
    assert sum(c["members_new"] for c in cls) >= 1  # a literal became a member
    assert sum(x["restated"] for x in long + short) >= 1  # an existing child entered or left the kept set
    assert sum(x["heavy_new"] for x in long + short) >= 1  # a directory became heavy
    assert sum(x["probes_existed"] for x in long + short) >= 1  # a probe finding an earlier root under the child
    assert sum(x["probes"] - x["probes_existed"] for x in long + short) >= 1  # and one finding none: a new child
    assert sum(x["candidates"] for x in long + short) >= 1
    assert sum(x["base_counted"] for x in long + short) >= 1  # a base count below the measurement's floor


def test_run_roots_are_deltas(world):
    """A run's roots are only rows opened or closed at its scan, except a new member's whole history."""
    for tier, meta in zip(world["runs"], world["metas"]):
        D = meta["D"]
        new = set()
        for kind in sd.KINDS:
            for f in tier.files(kind, "roots"):
                for r in pq.read_table(f).to_pylist():
                    if not (r["vf"] == D or r["vt"] == D):
                        new.add(r["q"])
        assert new <= {q for q in tier.alias_table().column("q").to_pylist() if q not in set(world["base"].alias_table().column("q").to_pylist())}


def test_history_table_equals_rows(world, tmp_path):
    """The build's DuckDB read of new members' tiered suffix rows equals the reference reader's, row for row."""
    con = world["con"]
    base_out = world["base_out"]
    run = world["run_dirs"][0]
    terms = world["metas"][0]["new_members_list"]
    assert terms
    want = sd.history_rows([_reader(base_out), _reader(run)], terms)
    got = sd.history_table(con, [str(base_out), str(run)], terms)
    key = lambda r: (r["s"], r["path"], r["usr"], r["vf"])  # noqa: E731
    assert sorted(got.to_pylist(), key=key) == sorted(want.to_pylist(), key=key)
