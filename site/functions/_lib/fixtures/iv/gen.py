"""The interval store fixture (specs/interval-store.md) for `intervalStore.test.ts`.

Six synthetic scans (three v1 indexes, then three v2 store generations: owner slices, a duplicated v1
row, paths that come and go, read days that move) from `cloud/tests/test_interval_store.py`'s
generator, built into one generation by `dt_cloud.interval_store` (`build_range` over three key
ranges, `write_served` at 4 rows per group) under `interval-store/<GEN>/` here, plus `expected.json`:
per `(date, path, w, h, depth)` the view computed from that scan's own rows by the per-scan reference
(`interval_verify.Scan.view`), flattened (`interval_read.flatten`). The generator asserts the Python
interval reader returns the same trees.

    cloud/.venv-or-root/bin/python site/functions/_lib/fixtures/iv/gen.py
"""
from __future__ import annotations

import importlib.util
import json
import random
import shutil
import sys
from pathlib import Path

import duckdb

HERE = Path(__file__).parent
ROOT = HERE.parents[4]
GEN = "g1"
PATHS = ["", "b1", "b2", "b1/gof", "b2/e"]
SIZES = [(8, 6), (30, 30)]


def main() -> None:
    from dt_cloud import interval_read as ir
    from dt_cloud import interval_store as ist
    from dt_cloud import static_names as sn
    from dt_cloud.interval_verify import Scan

    spec = importlib.util.spec_from_file_location("tis", ROOT / "cloud/tests/test_interval_store.py")
    t = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t)

    work = HERE / "work"
    shutil.rmtree(work, ignore_errors=True)
    rng = random.Random(11)
    paths = t._universe(rng)
    scans = []
    for j, d in enumerate(t.DATES):
        v = 2 if j >= t.V2_FROM else 1
        key = f"listing/{d}/path-index.parquet"
        (work / key).parent.mkdir(parents=True, exist_ok=True)
        t._write(t._scan_rows(rng, paths, j), work / key, v)
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": v})
    doc = {"bucket": "b", "scans": scans}
    ranges = {"k": 3, "ranges": [{"i": 0, "lo": [0, ""], "hi": [2, "b1/gof"]}, {"i": 1, "lo": [2, "b1/gof"], "hi": [3, ""]}, {"i": 2, "lo": [3, ""], "hi": None}]}
    con = duckdb.connect()
    for i in range(3):
        ist.build_range(doc, ranges, i, work / "out", con, mount=str(work))
    for i in range(3):
        ist.fold_range(str(work / "out"), i, work / "out", con)
    out = HERE / "interval-store" / GEN
    shutil.rmtree(out, ignore_errors=True)
    for sort, (sub, _, _) in ist.SORTS.items():
        schema = ist.SUB_SCHEMA[sub]
        ist.write_served(con, f"read_parquet('{work}/out/{sub}/r*.parquet')", sort, out / "served" / f"{sort}.parquet", schema, rg_rows=4,
                         stamps=[x["ts"] for x in scans])
    (out / "scans.json").write_text(json.dumps({"scans": [{"id": s["id"], "ts": s["ts"]} for s in scans]}, indent=1) + "\n")
    store = ir.Store(out / "served")
    expected = []
    for s in scans:
        scan = Scan(duckdb.connect(), str(work / s["src"]), s["version"], s["src"])
        for p in PATHS:
            for w, h in SIZES:
                for md in (None, 1):
                    want = scan.view(p, w, h, max_depth=md)
                    got = store.view(s["ts"], p, w, h, max_depth=md)
                    from dt_cloud.interval_verify import compare

                    assert compare(got["tree"], want["tree"], with_f=True) == [], (s["id"], p, w, h, md)
                    if want["tree"] is None:
                        continue
                    expected.append({"date": s["id"], "path": p, "w": w, "h": h, "depth": md, "v": s["version"], "tiles": ir.flatten(want["tree"])})
    (HERE / "expected.json").write_text(json.dumps(expected, indent=1, sort_keys=True) + "\n")
    shutil.rmtree(work)
    print(f"{len(expected)} views; files: {sorted(p.name for p in (out / 'served').iterdir())}", file=sys.stderr)
    slices_gen()


#: `g2`: the per-scan store fixtures `v2-slices` (multi-owner slices) and `v2-lens` as three scans —
#: slices, lens, slices again (every version closes and reopens) — so `intervalStore.test.ts` compares
#: sliced reads (lens, owner pools) of the interval store with the per-scan reader over the same files.
G2 = "g2"
G2_SCANS = [("2026-09-30T0007", "v2-slices"), ("2026-09-30T0008", "v2-lens"), ("2026-09-30T0009", "v2-slices")]


def slices_gen() -> None:
    from dt_cloud import interval_store as ist
    from dt_cloud import static_names as sn

    fx = HERE.parent
    work = HERE / "work2"
    shutil.rmtree(work, ignore_errors=True)
    scans = [{"id": d, "src": f"{name}/path-index.parquet", "ts": sn.scan_epoch(d), "version": 2} for d, name in G2_SCANS]
    doc = {"bucket": "b", "scans": scans}
    ranges = {"k": 1, "ranges": [{"i": 0, "lo": [0, ""], "hi": None}]}
    con = duckdb.connect()
    d = ist.build_range(doc, ranges, 0, work / "out", con, mount=str(fx))
    ist.fold_range(str(work / "out"), 0, work / "out", con)
    ds = ist.build_slices_range(doc, ranges, 0, work / "out", con, mount=str(fx))
    assert d["eq"] and ds["eq"], (d["eq"], ds["eq"])
    out = HERE / "interval-store" / G2
    shutil.rmtree(out, ignore_errors=True)
    for sort, (sub, _, _) in ist.SORTS.items():
        if sort == "reads":
            continue
        ist.write_served(con, f"read_parquet('{work}/out/{sub}/r*.parquet')", sort, out / "served" / f"{sort}.parquet", ist.SUB_SCHEMA[sub],
                         rg_rows=256, stamps=[x["ts"] for x in scans])
    (out / "scans.json").write_text(json.dumps({"scans": [{"id": x["id"], "ts": x["ts"]} for x in scans]}, indent=1) + "\n")
    shutil.rmtree(work)
    print(f"{G2}: {sorted(p.name for p in (out / 'served').iterdir())}", file=sys.stderr)


if __name__ == "__main__":
    slices_gen() if sys.argv[1:] == ["g2"] else main()
