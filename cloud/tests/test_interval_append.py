"""`dt_cloud.interval_append`: a base generation plus per-scan runs is, version for version, a full rebuild through the
same scan — for the folded path versions (`pvl`), the owner slices (`sv`) and the slices with their path's total
(`svt`) — with each run read alone and with runs merged on the binary counter; every view at every path and scan read
over base + runs is the per-scan reference's; and the comparison catches a broken run (mutations)."""
from __future__ import annotations

import json
import random
import threading
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from test_interval_store import V2_FROM, _scan_rows, _slice_oracle, _universe, _write

from dt_cloud import interval_append as ia
from dt_cloud import interval_store as ist
from dt_cloud import static_names as sn
from dt_cloud.static_append import push_run, run_key
from dt_cloud.static_profile import Profile

#: Four scans in the base (three v1 indexes, one v2 store generation), then four appended (v2, a timed id among them).
DATES = ["2026-07-30", "2026-07-31", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-04T1236", "2026-08-05", "2026-08-07"]
BASE = 4
RANGES = {"k": 3, "ranges": [{"i": 0, "lo": [0, ""], "hi": [2, "b1/gof"]}, {"i": 1, "lo": [2, "b1/gof"], "hi": [3, ""]},
                             {"i": 2, "lo": [3, ""], "hi": None}]}
NAMES = [f"r{i:04d}" for i in range(RANGES["k"])]


def build_full(scans: list[dict], root: Path, out: Path) -> Path:
    """`interval-store build` + `fold` + `build -S` + `fold -S` over `scans`: the range files a generation is cut from."""
    con = duckdb.connect()
    doc = {"bucket": "b", "scans": scans}
    for i in range(RANGES["k"]):
        assert ist.build_range(doc, RANGES, i, out, con, mount=str(root))["eq"]
        ist.fold_range(str(out), i, out, con)
        assert ist.build_slices_range(doc, RANGES, i, out, con, mount=str(root))["eq"]
        ist.slice_totals_range(str(out), i, out, con)
    return out


def append_scan(scan: dict, root: Path, base: Path, prev: Path | None, out: Path) -> list[dict]:
    """Every key range of one scan's run (`runs ranges`): from the base's range files, or the previous run's state."""
    con = duckdb.connect()
    docs = []
    for i, name in enumerate(NAMES):
        state = ia.run_state_sql(str(prev / "state"), name) if prev else ia.base_state_sql(str(base), name)
        docs.append(ia.append_range(con, state, scan, RANGES["ranges"][i], name, out, bucket="b", mount=str(root)))
    return docs


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("iva")
    rng = random.Random(11)
    paths = _universe(rng)
    scans = []
    for j, d in enumerate(DATES):
        v = 2 if j >= V2_FROM else 1
        key = f"listing/{d}/path-index.parquet"
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        _write(_scan_rows(rng, paths, j), root / key, v)
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": v})
    base = build_full(scans[:BASE], root, root / "base")
    # A full rebuild through each appended scan: what base + runs must equal.
    full = {s["id"]: build_full(scans[:j + 1], root, root / f"full-{s['id']}") for j, s in enumerate(scans) if j >= BASE}
    runs, prev, docs = {}, None, {}
    for s in scans[BASE:]:
        out = root / "runs" / s["id"]
        docs[s["id"]] = append_scan(s, root, base, prev, out)
        runs[s["id"]] = out
        prev = out
    # The binary counter over the appended scans: after each, the runs a manifest would list (merges made as it says).
    stacks, live = {}, []
    dirs = {f"deltas/{s}": d for s, d in runs.items()}
    for s in scans[BASE:]:
        live, merges = push_run(live, {"key": run_key(s["id"], s["id"]), "first": s["id"], "last": s["id"], "scans": [s["id"]]})
        for ins, m in merges:
            out = root / "merged" / m["key"].replace("/", "_")
            for name in NAMES:
                ia.merge_range([str(dirs[r["key"]]) for r in ins], name, out)
            dirs[m["key"]] = out
        stacks[s["id"]] = [(r, dirs[r["key"]]) for r in live]
    return {"root": root, "scans": scans, "paths": paths, "base": base, "full": full, "runs": runs, "stacks": stacks, "docs": docs}


# ── Base + runs, combined: the versions ────────────────────────────────────


def _rows(d: Path, t: str) -> list[dict]:
    """A dir's `t` rows in the served columns, `usr` '' where no owner is named (the base's NULL, a run's '')."""
    cols = ist.SUB_SCHEMA[t].names
    tb = pa.concat_tables([pq.read_table(p, columns=cols) for p in sorted((d / t).glob("r*.parquet"))])
    rows = tb.to_pylist()
    for r in rows:
        if "usr" in r and r["usr"] is None:
            r["usr"] = ""
    return rows


def combine(tiers: list[list[dict]], t: str) -> list[dict]:
    """Tiers' rows (base first): one per version (`IDENT`), the smallest `vt` (the reader's `interval_read.combine`)."""
    best: dict[tuple, dict] = {}
    for rows in tiers:
        for r in rows:
            k = tuple(r[c] for c in ia.IDENT[t])
            if k not in best or r["vt"] < best[k]["vt"]:
                best[k] = r
    return sorted(best.values(), key=lambda r: tuple(r[c] for c in ia.IDENT[t]))


def versions(base: Path, run_dirs: list[Path], t: str) -> list[dict]:
    return combine([_rows(base, t), *(_rows(d, t) for d in run_dirs)], t)


@pytest.mark.parametrize("t", ia.TABLES)
def test_each_run_alone_plus_the_base_is_a_full_rebuild(world, t):
    """Base + the level-0 runs through scan D (no merges) equals `build`/`fold` over every scan through D, row for
    row (the exact `wts` carried as the build carries it)."""
    appended = [s["id"] for s in world["scans"][BASE:]]
    for j, d in enumerate(appended):
        got = versions(world["base"], [world["runs"][x] for x in appended[:j + 1]], t)
        want = combine([_rows(world["full"][d], t)], t)
        assert got == want, (t, d)


@pytest.mark.parametrize("t", ia.TABLES)
def test_merged_runs_plus_the_base_are_a_full_rebuild(world, t):
    """…and with the runs merged on the binary counter, as each scan's manifest lists them."""
    shapes = {}
    for d, stack in world["stacks"].items():
        shapes[d] = [(r["key"], r["level"]) for r, _ in stack]
        got = versions(world["base"], [p for _, p in stack], t)
        assert got == combine([_rows(world["full"][d], t)], t), (t, d)
    assert shapes == {
        "2026-08-04": [("deltas/2026-08-04", 0)],
        "2026-08-04T1236": [("deltas/2026-08-04_2026-08-04T1236", 1)],
        "2026-08-05": [("deltas/2026-08-04_2026-08-04T1236", 1), ("deltas/2026-08-05", 0)],
        "2026-08-07": [("deltas/2026-08-04_2026-08-07", 2)],
    }


def test_a_merged_run_is_its_inputs_combined(world):
    """A merge keeps one row per version (the smallest `vt`, `op` 1 if any input opened it): a version opened in one
    run and closed in a later one is a single closed row; nothing else changes."""
    stack = world["stacks"]["2026-08-07"]
    (_, merged), = stack
    ins = [world["runs"][s["id"]] for s in world["scans"][BASE:]]
    for t in ia.TABLES:
        cols = ia.DELTA_SCHEMA[t].names
        rows = lambda d: pa.concat_tables([pq.read_table(p) for p in sorted((d / t).glob("r*.parquet"))]).select(cols).to_pylist()  # noqa: E731
        want: dict[tuple, dict] = {}
        for d in ins:
            for r in rows(d):
                k = tuple(r[c] for c in ia.IDENT[t])
                w = want.get(k)
                want[k] = r if w is None else {**w, "vt": min(w["vt"], r["vt"]), "op": max(w["op"], r["op"])}
        assert rows(merged) == [want[k] for k in sorted(want)], t


def test_runs_hold_the_scans_changes_only(world):
    """A run's delta: the versions opened at its scan (`op` 1, `vf` = D, open) and close records (`op` −1, `vt` = D),
    and its counts say so; the open state is the scan's rows (`append_range` checks it, or raises)."""
    for s in world["scans"][BASE:]:
        D = s["ts"]
        for t in ia.TABLES:
            rows = pa.concat_tables([pq.read_table(p) for p in sorted((world["runs"][s["id"]] / t).glob("r*.parquet"))]).to_pylist()
            assert sorted({(r["op"], r["vf"] == D, r["vt"] == D, r["vt"] == sn.OPEN) for r in rows}) == [(-1, False, True, False), (1, True, False, True)]
            doc = [x[t] for x in world["docs"][s["id"]]]
            assert [sum(x["opened"] for x in doc), sum(x["closed"] for x in doc), sum(x["delta"] for x in doc)] == \
                [sum(r["op"] == 1 for r in rows), sum(r["op"] == -1 for r in rows), len(rows)]


def test_slices_live_at_each_scan_are_the_scans_owner_slices(world):
    """Over base + runs, each appended scan's live owner slices are its rows summed per `(depth, path, usr)`, and each
    slice piece carries its path's total then."""
    root = world["root"]
    for s in world["scans"][BASE:]:
        stack = [p for _, p in world["stacks"][s["id"]]]
        D = s["ts"]
        sv = [r for r in versions(world["base"], stack, "sv") if r["vf"] <= D < r["vt"]]
        got = {(r["depth"], r["path"], r["usr"] or None): {c: r[c] for c in ("kind", "size", "n_files", "wb", "c4", "last_read")} for r in sv}
        want = {k: {c: a[c] for c in ("kind", "size", "n_files", "wb", "c4", "last_read")} for k, a in _slice_oracle(root, s).items()}
        assert got == want, s["id"]
        pv = {(r["depth"], r["path"]): r["size"] for r in versions(world["base"], stack, "pvl") if r["vf"] <= D < r["vt"]}
        svt = [r for r in versions(world["base"], stack, "svt") if r["vf"] <= D < r["vt"]]
        assert sorted((r["depth"], r["path"], r["usr"]) for r in svt) == sorted((r["depth"], r["path"], r["usr"]) for r in sv)
        assert [r for r in svt if r["tot"] != pv[(r["depth"], r["path"])]] == []


# ── Mutations: the comparison catches a broken run ─────────────────────────


def _mutate(rows: list[dict], how: str, D: int) -> list[dict]:
    rows = [dict(r) for r in rows]
    if how == "drop-a-close-record":
        i = next(i for i, r in enumerate(rows) if r["vt"] == D)
        return rows[:i] + rows[i + 1:]
    if how == "drop-an-opened-version":
        i = next(i for i, r in enumerate(rows) if r["vf"] == D)
        return rows[:i] + rows[i + 1:]
    if how == "close-a-day-late":
        i = next(i for i, r in enumerate(rows) if r["vt"] == D)
        rows[i]["vt"] = D + 86_400
        return rows
    if how == "wts-of-the-new-scan":
        # The carry rule broken: an opened version's exact `wts` nudged in its last bits, as a fresh sum would be.
        i = next(i for i, r in enumerate(rows) if r["vf"] == D and r["wts"])
        rows[i]["wts"] = rows[i]["wts"] * (1 + 2 ** -50)
        return rows
    raise ValueError(how)


@pytest.mark.parametrize("how", ["drop-a-close-record", "drop-an-opened-version", "close-a-day-late", "wts-of-the-new-scan"])
@pytest.mark.parametrize("t", ia.TABLES)
def test_a_mutated_run_is_caught(world, t, how):
    s = world["scans"][BASE + 1]
    appended = [x["id"] for x in world["scans"][BASE:BASE + 2]]
    good = [_rows(world["runs"][x], t) for x in appended]
    want = combine([_rows(world["full"][s["id"]], t)], t)
    assert combine([_rows(world["base"], t), *good], t) == want
    bad = [good[0], _mutate(good[1], how, s["ts"])]
    assert combine([_rows(world["base"], t), *bad], t) != want


# ── Views over base + runs (the reference reader, as the Worker plans) ─────


def _cut(rel_root: Path, out: Path) -> Path:
    con = duckdb.connect()
    for sort in ia.RUN_SORTS:
        sub, _, _ = ist.SORTS[sort]
        ist.write_served(con, ia.delta_relation(str(rel_root), sub), sort, out / f"{sort}.parquet", ist.SUB_SCHEMA[sub], rg_rows=4)
    return out


@pytest.fixture(scope="module")
def served(world):
    """The base's served sorts (dyadic segments, 4-row groups) and each run's (`cut_run`'s, 4-row groups)."""
    root = world["root"]
    base_scans = world["scans"][:BASE]
    con = duckdb.connect()
    base = root / "served-base"
    for sort in ("path", "bysize"):
        sub, _, _ = ist.SORTS[sort]
        ist.write_served(con, f"read_parquet('{world['base']}/{sub}/r*.parquet')", sort, base / f"{sort}.parquet", ist.SUB_SCHEMA[sub], rg_rows=4,
                         stamps=[s["ts"] for s in base_scans])
    cut = {}
    for d, stack in world["stacks"].items():
        for r, p in stack:
            if r["key"] not in cut:
                cut[r["key"]] = _cut(p, root / "served-runs" / r["key"].replace("/", "_"))
    single = {s: _cut(p, root / "served-single" / s) for s, p in world["runs"].items()}
    return {"base": base, "cut": cut, "single": single}


def _views_equal(world, store_for, paths: list[str]) -> int:
    from dt_cloud.interval_verify import Scan, compare

    root = world["root"]
    checked = 0
    for s in world["scans"]:
        store = store_for(s)
        scan = Scan(duckdb.connect(), str(root / s["src"]), s["version"], s["src"])
        for path in paths:
            for w, h in ((4, 4), (8, 6), (30, 30)):
                for md in (None, 1):
                    want = scan.view(path, w, h, max_depth=md)
                    got = store.view(s["ts"], path, w, h, max_depth=md)
                    assert compare(got["tree"], want["tree"], with_f=True) == [], (s["id"], path, w, h, md)
                    checked += want["tree"] is not None
    return checked


def test_every_view_at_every_path_and_scan_over_base_and_runs(world, served):
    """The newest manifest's stack (one merged run) and the runs read alone: at every scan, base or appended, every
    path's view (three sizes, whole and one level deep) is the per-scan reference's, tile for tile."""
    from dt_cloud import interval_read as ir

    paths = ["", *sorted({p for p in world["paths"] if "." not in p.rsplit("/", 1)[-1]})]
    last = [served["cut"][r["key"]] for r, _ in world["stacks"]["2026-08-07"]]
    singles = [served["single"][s["id"]] for s in world["scans"][BASE:]]
    n1 = _views_equal(world, lambda s: ir.Store(served["base"], last), paths)
    n2 = _views_equal(world, lambda s: ir.Store(served["base"], singles), paths)
    assert n1 == n2 > 0


def test_a_view_over_runs_missing_a_close_record_differs(world, served, tmp_path):
    """The reader's half of the mutation check: a run whose close records are dropped leaves versions live past their
    end, and the views at that scan change."""
    from dt_cloud import interval_read as ir
    from dt_cloud.interval_verify import Scan, compare

    s = world["scans"][BASE]
    bad = tmp_path / "bad"
    for t in ia.TABLES:
        for p in sorted((world["runs"][s["id"]] / t).glob("r*.parquet")):
            tb = pq.read_table(p)
            (bad / t).mkdir(parents=True, exist_ok=True)
            pq.write_table(tb.filter(pa.compute.equal(tb["op"], 1)), bad / t / p.name)
    store = ir.Store(served["base"], [_cut(bad, tmp_path / "served-bad")])
    scan = Scan(duckdb.connect(), str(world["root"] / s["src"]), s["version"], s["src"])
    paths = ["", *sorted({p for p in world["paths"] if "." not in p.rsplit("/", 1)[-1]})]
    differ = [p for p in paths if compare(store.view(s["ts"], p, 30, 30)["tree"], scan.view(p, 30, 30)["tree"], with_f=True)]
    assert differ == ["", "b1", "b1/d", "b1/d/e", "b1/gof", "b1/gof/e", "b2/e", "b2/e/e", "b2/f", "b2/f/c"]


# ── The chain: order, stages, jobs ─────────────────────────────────────────


def test_prune_plan_keeps_the_newest_complete_state_only():
    pre = "interval-store/g"
    objs = [(f"{pre}/state/{s}/{t}/r{i:04d}.parquet", 10) for s in ("2026-08-04", "2026-08-05") for t in ia.TABLES for i in range(2)]
    plan = ia.prune_plan(objs, pre, 2, True, "2026-08-05")
    assert {k: plan[k] for k in ("keep", "delete")} == {"keep": ["2026-08-05"], "delete": [{"scan": "2026-08-04", "objects": 6, "bytes": 60}]}
    assert plan["names"] == sorted(n for n, _ in objs if "/2026-08-04/" in n)
    with pytest.raises(ia.StateIncomplete, match=r"state/2026-08-05 incomplete: 1 of 2 ranges without svt"):
        ia.prune_plan(objs[:-1], pre, 2, True, "2026-08-05")
    with pytest.raises(ia.StateIncomplete, match="no manifest"):
        ia.prune_plan(objs, pre, 2, False, "2026-08-05")


def test_manifest_lists_the_runs_and_their_scans_stamps():
    base = {"scans": [{"id": "2026-08-03"}]}
    runs = [{"key": "deltas/2026-08-04_2026-08-04T1236", "first": "2026-08-04", "last": "2026-08-04T1236", "level": 1,
             "scans": ["2026-08-04", "2026-08-04T1236"], "stamps": {"2026-08-04T1236": 20, "2026-08-04": 10}, "rows": 5, "bytes": 9}]
    assert ia.manifest("g", base, runs) == {
        "gen": "g", "date": "2026-08-04T1236", "base_scans": 1, "scans": ["2026-08-03", "2026-08-04", "2026-08-04T1236"],
        "stamps": {"2026-08-04": 10, "2026-08-04T1236": 20},
        "runs": [{"key": "deltas/2026-08-04_2026-08-04T1236", "first": "2026-08-04", "last": "2026-08-04T1236", "level": 1,
                  "scans": ["2026-08-04", "2026-08-04T1236"], "rows": 5, "bytes": 9}],
    }
    assert ia.missing_files([{"key": "deltas/x"}], lambda k: not k.endswith("slices.groups.parquet")) == ["deltas/x/served/slices.groups.parquet"]


def _cfg(**kw) -> ia.Config:
    p = Profile(name="t", layouts=("listing/{id}/path-index.parquet",), bucket="data", scratch="scr", gen="g1", r2_bucket="r2b",
                r2_secrets={"key_id": "kid", "secret": "sec"}, r2_endpoint="https://r2", project="proj", region="us-east1", image="img",
                sa="sa@x", append_tasks=2, **kw)
    return ia.Config(p, "g0")


class Fake:
    """The data bucket as a dict of keys, Batch as a log of submitted jobs (each stage's outputs appear when it runs)."""

    def __init__(self, published: list[str], **cfg):
        self.cfg = cfg
        self.keys: dict[str, dict] = {
            "interval-store/g1/scans.json": {"scans": [{"id": "2026-08-03"}]},
            "interval-store/g0/ranges.json": {"k": 4},
        }
        self.published = published
        self.jobs: list[tuple[str, dict]] = []
        self.log: list[str] = []

    def run_job(self, name: str, spec: dict) -> None:
        self.jobs.append((name, spec))
        stage = spec["labels"]["stage"]
        d = name.split("-", 2)[2].rsplit("-", 1)[0]
        d = next(x for x in self.published if x.lower().replace("t", "-") == d)
        if stage == "ranges":
            for i in range(4):
                self.keys[f"interval-store/g1/deltas/{d}/ranges/r{i:04d}.json"] = {}
        elif stage == "publish":
            have = [k for k in self.keys if k.startswith("interval-store/g1/manifests/")]
            prev = self.keys[sorted(have)[-1]]["runs"] if have else []
            self.keys[f"interval-store/g1/manifests/{d}.json"] = {"runs": [*prev, {"scans": [d]}]}

    def runner(self, dry_run: bool = False) -> ia.Runner:
        return ia.Runner(
            cfg=_cfg(**self.cfg), exists=lambda k: k in self.keys,
            count=lambda prefix, suffix: sum(1 for k in self.keys if k.startswith(prefix) and k.endswith(suffix)),
            read_json=lambda k: self.keys[k], list_keys=lambda prefix: [k for k in self.keys if k.startswith(prefix)],
            published=lambda layouts, start: [s for s in self.published if s >= start], run_job=self.run_job,
            prepare=lambda d: self.keys.__setitem__(f"interval-store/g1/deltas/{d}/scans.json", {}),
            prune=lambda d: self.log.append(f"prune {d}"), log=self.log.append, dry_run=dry_run,
            now=lambda: datetime(2026, 8, 5, 1, 2, 3, tzinfo=timezone.utc),
        )


def test_the_chain_appends_strictly_in_scan_id_order():
    from dt_cloud.static_runner import NotNext

    f = Fake(["2026-08-03", "2026-08-04", "2026-08-04T1236"])
    with pytest.raises(NotNext, match=r"1 earlier published scan\(s\) pending: 2026-08-04"):
        f.runner().run("2026-08-04T1236")
    with pytest.raises(NotNext, match="2026-08-05 is not published"):
        f.runner().run("2026-08-05")
    assert f.runner().run("2026-08-04T1236", catch_up=True) == ["2026-08-04", "2026-08-04T1236"]
    assert [n for n, _ in f.jobs] == [
        "iv-ranges-2026-08-04-010203", "iv-publish-2026-08-04-010203",
        "iv-ranges-2026-08-04-1236-010203", "iv-publish-2026-08-04-1236-010203",
    ]
    assert [x for x in f.log if x.startswith("prune")] == ["prune 2026-08-04", "prune 2026-08-04T1236"]
    # Done: a rerun only re-copies and prunes.
    f.jobs.clear()
    assert f.runner().run("2026-08-04T1236") == []
    assert [n for n, _ in f.jobs] == ["iv-r2-2026-08-04-1236-010203"]


def test_the_chain_resumes_at_the_first_missing_stage():
    f = Fake(["2026-08-03", "2026-08-04"])
    f.keys["interval-store/g1/deltas/2026-08-04/scans.json"] = {}
    for i in range(4):
        f.keys[f"interval-store/g1/deltas/2026-08-04/ranges/r{i:04d}.json"] = {}
    f.runner().run("2026-08-04")
    assert [spec["labels"]["stage"] for _, spec in f.jobs] == ["publish"]
    # Published but the copy not known done (a rerun): the copy alone, its own job.
    f.jobs.clear()
    f.runner().run("2026-08-04")
    assert [spec["labels"]["stage"] for _, spec in f.jobs] == ["r2"]


def test_jobs_are_the_profiles_and_carry_the_cost_label(monkeypatch):
    monkeypatch.setenv("DISKY_LABELS", "app=disky,deployment=t")
    f = Fake(["2026-08-03", "2026-08-04"])
    f.runner().run("2026-08-04")
    f.runner().run("2026-08-04")  # published: the copy's own job
    (_, ranges), (_, publish), (_, r2) = f.jobs
    want = {"app": "disky", "deployment": "t", "component": "interval-store"}
    for spec in (ranges, publish, r2):
        assert spec["allocationPolicy"]["labels"] == want
        assert {k: spec["labels"][k] for k in ("purpose", "component", "gen")} == {"purpose": "interval-store", "component": "interval-store", "gen": "g1"}
    cmd = lambda s: s["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1]  # noqa: E731
    assert [s["taskGroups"][0]["taskCount"] for s in (ranges, publish, r2)] == [2, 1, 1]
    assert cmd(ranges) == ("set -euo pipefail; mkdir -p /stage/tmp /stage/out && cd /stage && python3 -u -m dt_cloud.interval_append ranges "
                           "-d 2026-08-04 -n 2 -M 90GB -p 16 -b data -g g1 -R g0 -S scr -m /gcs/data")
    assert cmd(r2) == ("set -euo pipefail; mkdir -p /stage/tmp /stage/out && cd /stage && python3 -u -m dt_cloud.interval_append r2 "
                       "-d 2026-08-04 -b data -g g1 -R g0 -S scr")
    assert r2["taskGroups"][0]["taskSpec"]["environment"] == {
        "variables": {"STATIC_NAMES_BUCKET": "data", "STATIC_NAMES_SCRATCH": "scr", "R2_BUCKET": "r2b", "R2_ENDPOINT": "https://r2"},
        "secretVariables": {"R2_ACCESS_KEY_ID": "projects/proj/secrets/kid/versions/latest", "R2_SECRET_ACCESS_KEY": "projects/proj/secrets/sec/versions/latest"},
    }


def test_the_profile_drives_the_append(tmp_path):
    doc = {"bucket": "data", "append": {"gen": "g1", "ranges_gen": "g0", "scratch": "scr", "layouts": ["listing/{id}/path-index.parquet"],
                                        "region": "r", "image": "img", "sa": "sa", "r2_bucket": "r2b"}}
    p = tmp_path / "x.json"
    p.write_text(json.dumps(doc))
    cfg = ia.load_config(str(p), {"INTERVAL_STORE_SRC": "/gcs/data/src/a, /gcs/data/src/b", "INTERVAL_STORE_GEN": "g2"})
    assert [cfg.ranges_gen, cfg.p.gen, cfg.p.bucket, cfg.p.layouts, cfg.p.src] == ["g0", "g2", "data", ("listing/{id}/path-index.parquet",), ("/gcs/data/src/a", "/gcs/data/src/b")]
    p.write_text(json.dumps({"bucket": "data", "append": {k: v for k, v in doc["append"].items() if k != "image"}}))
    with pytest.raises(SystemExit, match=r"no image in the profile's `append` section \(or \$INTERVAL_STORE_IMAGE\)"):
        ia.load_config(str(p), {})
    with pytest.raises(SystemExit, match=r"no image"):  # the gcs profile pins none: the caller passes its job image
        ia.load_config("gcs", {})
    gcs = ia.load_config("gcs", {"INTERVAL_STORE_IMAGE": "img"})
    assert [gcs.p.gen, gcs.ranges_gen, gcs.p.bucket] == ["2026-10-09b", "2026-10-09", "oa-gcs-usage-dvx"]


PREP = "set -euo pipefail; mkdir -p /stage/tmp /stage/out && cd /stage && python3 -u -m dt_cloud.interval_append"
R2_ENV = {
    "variables": {"STATIC_NAMES_BUCKET": "data", "STATIC_NAMES_SCRATCH": "scr", "R2_BUCKET": "r2b", "R2_ENDPOINT": "https://r2"},
    "secretVariables": {"R2_ACCESS_KEY_ID": "projects/proj/secrets/kid/versions/latest", "R2_SECRET_ACCESS_KEY": "projects/proj/secrets/sec/versions/latest"},
}


def _runnables(spec: dict) -> list[str]:
    return [r["container"]["commands"] for r in spec["taskGroups"][0]["taskSpec"]["runnables"]]


def test_publish_and_the_r2_copy_are_one_task_in_order():
    f = Fake(["2026-08-03", "2026-08-04"])
    f.runner().run("2026-08-04")
    assert [n for n, _ in f.jobs] == ["iv-ranges-2026-08-04-010203", "iv-publish-2026-08-04-010203"]
    _, spec = f.jobs[1]
    tg = spec["taskGroups"][0]
    # Runnables run in order and stop at a failure: the publish (its runs, its check, the manifest last), then the
    # copy (the runs, its check, the manifest last). A retried task skips the written manifest (`-s`).
    assert _runnables(spec) == [
        ["-c", f"{PREP} publish -d 2026-08-04 -M 90GB -p 16 -s -b data -g g1 -R g0 -S scr -m /gcs/data"],
        ["-c", f"{PREP} r2 -d 2026-08-04 -b data -g g1 -R g0 -S scr"],
    ]
    assert [r["container"]["imageUri"] for r in tg["taskSpec"]["runnables"]] == ["img", "img"]
    assert [tg["taskCount"], tg["taskSpec"]["maxRetryCount"], tg["taskSpec"]["environment"]] == [1, 3, R2_ENV]
    assert [spec["labels"]["stage"], spec["allocationPolicy"]["serviceAccount"]["email"],
            spec["allocationPolicy"]["instances"][0]["policy"]["machineType"]] == ["publish", "sa@x", "n2-highmem-16"]


def test_publish_and_r2_stay_two_jobs_when_the_r2_account_differs():
    f = Fake(["2026-08-03", "2026-08-04"], r2_sa="r2@x")
    f.runner().run("2026-08-04")
    (_, ranges), (_, publish), (_, r2) = f.jobs
    assert [s["labels"]["stage"] for s in (ranges, publish, r2)] == ["ranges", "publish", "r2"]
    assert [s["allocationPolicy"]["serviceAccount"]["email"] for s in (publish, r2)] == ["sa@x", "r2@x"]
    assert _runnables(publish) == [["-c", f"{PREP} publish -d 2026-08-04 -M 90GB -p 16 -b data -g g1 -R g0 -S scr -m /gcs/data"]]
    assert "secretVariables" not in publish["taskGroups"][0]["taskSpec"]["environment"]


def test_ranges_are_assigned_longest_first_to_the_least_loaded_task():
    #           r0 r1 r2 r3 r4 r5 r6 r7
    counts = [8, 1, 1, 7, 2, 2, 3, 6]
    plan = ia.assign_ranges(8, 3, counts)
    assert plan == [[0, 5], [1, 3, 4], [2, 6, 7]]
    assert [sum(counts[i] for i in p) for p in plan] == [10, 10, 10]
    # The measured shape: the big ranges contiguous (r0 and r1 of 8 over 4 tasks). Blocks would put both in task 0.
    counts = [8, 8, 1, 1, 1, 1, 1, 1]
    assert ia.assign_ranges(8, 4, counts) == [[0], [1], [2, 4, 6], [3, 5, 7]]
    # Ties go to the lower range, then the lower task: equal counts are the interleave.
    assert ia.assign_ranges(7, 3, [5] * 7) == [[0, 3, 6], [1, 4], [2, 5]]
    # No counts (the base, a run before them): interleaved.
    assert ia.assign_ranges(10, 4, None) == [[0, 4, 8], [1, 5, 9], [2, 6], [3, 7]]
    # Every range exactly once, more tasks than ranges included.
    assert ia.assign_ranges(2, 3, [1, 2]) == [[1], [0], []]
    with pytest.raises(ValueError, match="3 counts for 4 ranges"):
        ia.assign_ranges(4, 2, [1, 2, 3])


def test_a_ranges_cost_is_its_open_rows_from_the_previous_scans_docs():
    doc = lambda n: {t: {"opened": 0, "closed": 0, "delta": 0, "open": n * (j + 1)} for j, t in enumerate(ia.TABLES)}  # noqa: E731
    docs = {"r0000": doc(1), "r0001": doc(10), "r0002": doc(100)}
    assert ia.open_counts(docs, 3) == [6, 60, 600]
    assert ia.open_counts({k: v for k, v in docs.items() if k != "r0001"}, 3) is None
    assert ia.open_counts({**docs, "r0002": {"pvl": {"open": 1}}}, 3) is None

    class Blob:
        def __init__(self, name):
            self.name = name

        def download_as_bytes(self):
            return json.dumps(docs[Path(self.name).stem]).encode()

    class Gcs:
        listed: list[tuple[str, str]] = []

        def list_blobs(self, bucket, prefix):
            self.listed.append((bucket, prefix))
            return [Blob(f"{prefix}{n}.json") for n in reversed(sorted(docs))]

        def bucket(self, name):
            assert name == "data"
            return type("B", (), {"blob": lambda _, n: Blob(n)})()

    g = Gcs()
    assert ia.prev_open_counts(g, "data", "interval-store/g1", "2026-08-04T1236", 3) == [6, 60, 600]
    assert g.listed == [("data", "interval-store/g1/deltas/2026-08-04T1236/ranges/")]
    assert ia.prev_open_counts(g, "data", "interval-store/g1", "2026-08-04T1236", 4) is None


class PruneGcs:
    """The data bucket's manifests and the scratch bucket's state objects; deletes recorded per bucket. The first
    `barrier` deletes wait for each other: run one at a time, they time out."""

    def __init__(self, objects: list[str], manifests: list[str], barrier: int, gone: set[str] = frozenset(), timeout: float = 5):
        self.objects, self.manifests, self.gone, self.timeout = objects, manifests, set(gone), timeout
        self.deleted: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(barrier)
        self.n = 0

    def list_blobs(self, bucket, prefix):
        assert bucket == "scr"
        return [type("O", (), {"name": n, "size": 10})() for n in self.objects if n.startswith(prefix)]

    def bucket(self, bucket):
        gcs = self

        class Blob:
            def __init__(self, name):
                self.name = name

            def exists(self):
                assert bucket == "data"
                return self.name in gcs.manifests

            def delete(self):
                from google.api_core.exceptions import NotFound

                with gcs.lock:
                    i = gcs.n
                    gcs.n += 1
                if i < gcs.barrier.parties:
                    gcs.barrier.wait(timeout=gcs.timeout)
                if self.name in gcs.gone:
                    raise NotFound(self.name)
                with gcs.lock:
                    gcs.deleted.append((bucket, self.name))

        return type("B", (), {"blob": lambda _, n: Blob(n)})()


def test_prune_deletes_the_earlier_states_in_parallel():
    pre = "interval-store/g1"
    objs = [f"{pre}/state/{s}/{t}/r{i:04d}.parquet" for s in ("2026-08-03", "2026-08-04", "2026-08-05") for t in ia.TABLES for i in range(4)]
    g = PruneGcs(objs, [f"{pre}/manifests/2026-08-05.json"], barrier=4, gone={f"{pre}/state/2026-08-03/sv/r0002.parquet"})
    doc = ia.prune_state(g, "g1", "2026-08-05", 4, bucket="data", scratch="scr", workers=4)
    old = [n for n in objs if "/2026-08-05/" not in n]
    assert sorted(g.deleted) == sorted(("scr", n) for n in old if n not in g.gone)
    assert doc == {"scan": "2026-08-05", "keep": ["2026-08-05"], "deleted": 24,
                   "delete": [{"scan": "2026-08-03", "objects": 12, "bytes": 120}, {"scan": "2026-08-04", "objects": 12, "bytes": 120}]}
    # One at a time, the deletes the barrier holds never meet.
    g = PruneGcs(objs, [f"{pre}/manifests/2026-08-05.json"], barrier=2, timeout=0.2)
    with pytest.raises(threading.BrokenBarrierError):
        ia.prune_state(g, "g1", "2026-08-05", 4, bucket="data", scratch="scr", workers=1)
    # Incomplete (no manifest): nothing deleted.
    g = PruneGcs(objs, [], barrier=4)
    with pytest.raises(ia.StateIncomplete, match="no manifest"):
        ia.prune_state(g, "g1", "2026-08-05", 4, bucket="data", scratch="scr", workers=4)
    assert g.deleted == []
