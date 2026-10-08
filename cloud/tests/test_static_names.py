"""`dt_cloud.static_names`: intervals vs a sequential ingest oracle (the ClickHouse store's
semantics), append vs rebuild, suffix shards vs brute force, and the reader's per-bucket
first-hit answers vs brute force over every date."""
from __future__ import annotations

import json
import random
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import static_names as sn

DATES = ["2026-07-30", "2026-07-31", "2026-08-02", "2026-08-03", "2026-08-04"]
V2_FROM = 3  # scans from here on are store generations (v2); earlier ones v1 indexes


def _universe(rng: random.Random) -> list[str]:
    names = ["Gof.txt", "x5418y", "nk080", "48.parquet", "48.parquet.crc", "aGOFb", "pio", "a'b", "é5418", "sh", "ab"]
    dirs = ["b1", "b2", "b1/gof", "b1/d", "b2/e5418", "b2/f"]
    paths = list(dirs)
    for d in dirs[2:]:
        for n in rng.sample(names, 5):
            paths.append(f"{d}/{n}")
    return paths


def _scan_rows(rng: random.Random, paths: list[str], j: int) -> list[dict]:
    """One scan's rows: a few paths missing (absence gaps), values drifting, owner slices, and
    (v1) a duplicated unattributed row."""
    rows = []
    for p in paths:
        if rng.random() < 0.15:
            continue
        depth = p.count("/") + 1
        for usr in ([None, "alice"] if rng.random() < 0.3 else [None]):
            size = rng.choice([10, 10, 10, 20, 0])
            n = rng.choice([1, 1, 2])
            mean = rng.choice([1_700_000_000.4, 1_700_000_000.5, 1_700_000_001.5, 1_700_000_000.2])
            rows.append({"path": p, "depth": depth, "usr": usr, "size": size, "n_files": n, "mtime_mean": mean,
                         "last_read": rng.choice([5, 5, 6]), "kind": "file" if "." in p.rsplit("/", 1)[-1] and depth > 2 else "dir"})
            if j < V2_FROM and usr is None and rng.random() < 0.1:
                rows.append({**rows[-1]})
    return rows


def _write(rows: list[dict], path: Path, v: int) -> None:
    rows = sorted(rows, key=lambda r: (r["depth"], r["path"], r["usr"] or ""))
    if v == 2:
        t = pa.table({
            "path": [r["path"] for r in rows], "usr": [r["usr"] for r in rows], "size": [r["size"] for r in rows],
            "depth": pa.array([r["depth"] for r in rows], pa.int32()), "kind": [r["kind"] for r in rows],
            "n_files": [r["n_files"] for r in rows], "n_children": [1 for _ in rows], "n_desc": [2 for _ in rows],
            "mtime": [7 for _ in rows], "mtime_mean": [r["mtime_mean"] for r in rows], "created": [0 for _ in rows],
            "last_read": pa.array([r["last_read"] for r in rows], pa.int32()),
            "sum_storage_class_id_2": [0 for _ in rows], "sum_storage_class_id_3": [None for _ in rows], "sum_storage_class_id_4": [0 for _ in rows],
        })
    else:
        t = pa.table({
            "path": [r["path"] for r in rows], "depth": [r["depth"] for r in rows], "usr": [r["usr"] for r in rows],
            "b": [r["size"] for r in rows], "o": [r["n_files"] for r in rows],
            "wts": [r["mtime_mean"] * r["size"] for r in rows], "wb": [r["size"] for r in rows],
            "c2": [0 for _ in rows], "c3": [0 for _ in rows], "c4": [0 for _ in rows], "a": pa.array([r["last_read"] for r in rows], pa.int32()),
        })
    pq.write_table(t, path, row_group_size=7)


def _merged(rows: list[dict], v: int) -> dict:
    """The ingest's per-key merge (`chstore.ingest.MERGED`), values compared with the stamp rounded."""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["depth"], r["path"], r["usr"] or ""), []).append(r)
    out = {}
    for k, rs in groups.items():
        if v == 2:
            vals = [("file" if r["kind"] == "file" else "dir", r["size"], r["n_files"], 1, 2, 7, r["mtime_mean"], r["size"], r["last_read"], 0, 0, 0) for r in rs]
        else:
            vals = [("dir", r["size"], r["n_files"], -1, -1, -1, (r["mtime_mean"] * r["size"] / r["size"]) if r["size"] > 0 else 0.0, r["size"], r["last_read"], 0, 0, 0) for r in rs]
        if len(vals) == 1:
            mean = vals[0][6]
        else:
            mean = sum(x[6] * x[7] for x in vals) / max(sum(x[7] for x in vals), 1)
        out[k] = (vals[0][0], sum(x[1] for x in vals), sum(x[2] for x in vals), max(x[3] for x in vals), max(x[4] for x in vals),
                  max(x[5] for x in vals), float(round(mean)), sum(x[7] for x in vals), max(x[8] for x in vals), 0, 0, 0)
    return out


def _oracle(scans: list[tuple[int, dict]]) -> list[tuple]:
    """Sequential ingest: open versions vs each scan, closing gone/changed keys and opening new/changed ones."""
    open_: dict[tuple, tuple[int, tuple]] = {}
    done: list[tuple] = []
    for ts, rows in scans:
        for k in list(open_):
            if k not in rows or rows[k] != open_[k][1]:
                vf, vals = open_.pop(k)
                done.append((*k, vf, ts, *vals))
        for k, vals in rows.items():
            if k not in open_:
                open_[k] = (ts, vals)
    done += [(*k, vf, sn.OPEN, *vals) for k, (vf, vals) in open_.items()]
    return sorted(done)


@pytest.fixture(scope="module")
def fixture(tmp_path_factory):
    root = tmp_path_factory.mktemp("sn")
    rng = random.Random(7)
    paths = _universe(rng)
    scans, merged = [], []
    for j, d in enumerate(DATES):
        v = 2 if j >= V2_FROM else 1
        rows = _scan_rows(rng, paths, j)
        key = f"listing/{d}/path-index.parquet"
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        _write(rows, root / key, v)
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": v})
        merged.append((sn.scan_epoch(d), _merged(rows, v)))
    return root, {"bucket": "b", "scans": scans}, merged


def _read(path: Path) -> list[tuple]:
    t = pq.read_table(path)
    return [tuple(r.values()) for r in t.to_pylist()]


def _build(root: Path, scans: dict, ranges: dict, out: Path) -> list[tuple]:
    rows = []
    for r in ranges["ranges"]:
        sn.build_range(scans, ranges, r["i"], out, mount=str(root), threads=2, mem="1GB", tmp=out / "tmp")
        rows += _read(out / "intervals" / f"r{r['i']:04d}.parquet")
    return rows


@pytest.mark.parametrize("k", [1, 4])
def test_intervals_equal_sequential_ingest(fixture, tmp_path, k):
    root, scans, merged = fixture
    ranges = sn.plan_ranges(scans, k, str(root))
    rows = _build(root, scans, ranges, tmp_path)
    assert rows == sorted(rows)
    assert rows == _oracle(merged)
    # every range's file is sorted and ranges are disjoint, ordered
    assert len({(r[0], r[1], r[2], r[3]) for r in rows}) == len(rows)


def test_ranges_partition_keyspace():
    lo_hi = [((0, ""), (2, "b1/d")), ((2, "b1/d"), (2, "b2")), ((2, "b2"), (4, "")), ((4, ""), None)]
    keys = [(d, p) for d in range(0, 6) for p in ["", "a", "b1/d", "b1/z", "b2", "zz"]]
    for key in keys:
        hits = 0
        for lo, hi in lo_hi:
            for piece in sn.pieces(lo, hi):
                d, p = key
                if piece.dlo <= d <= piece.dhi and (piece.plo is None or p >= piece.plo) and (piece.phi is None or p < piece.phi):
                    hits += 1
        assert (key, hits) == (key, 1)


def test_append_equals_rebuild(fixture, tmp_path):
    root, scans, merged = fixture
    ranges = sn.plan_ranges(scans, 3, str(root))
    head = {"bucket": "b", "scans": scans["scans"][:-1]}
    full = tmp_path / "full"
    _build(root, scans, ranges, full)
    prev = tmp_path / "prev"
    _build(root, head, ranges, prev)
    app = tmp_path / "app"
    for r in ranges["ranges"]:
        name = f"r{r['i']:04d}"
        doc = sn.append_range(prev / "intervals" / f"{name}.parquet", scans["scans"][-1], ranges, r["i"], app,
                              bucket="b", mount=str(root), threads=2, mem="1GB", tmp=app / "tmp")
        assert (app / "intervals" / f"{name}.parquet").read_bytes() == (full / "intervals" / f"{name}.parquet").read_bytes()
        assert (app / "hist" / f"{name}.parquet").read_bytes() == (full / "hist" / f"{name}.parquet").read_bytes()
        full_doc = json.loads((full / "digest" / f"{name}.json").read_text())
        assert doc["digests"] == full_doc["digests"]
        delta = pq.read_table(app / "delta" / DATES[-1] / f"{name}.parquet").to_pylist()
        D = sn.scan_epoch(DATES[-1])
        assert sorted((d["op"], d["vf"] == D, d["vt"] == D) for d in delta) == sorted(
            [(1, True, False)] * doc["opened"] + [(-1, False, True)] * doc["closed"])


def test_digests_match_oracle(fixture, tmp_path):
    root, scans, merged = fixture
    ranges = sn.plan_ranges(scans, 2, str(root))
    _build(root, scans, ranges, tmp_path)
    import duckdb

    con = duckdb.connect()
    opened: dict[int, int] = {}
    closed: dict[int, int] = {}
    for depth, path, usr, vf, vt, _kind, size, n_files, *_ in _oracle(merged):
        opened[vf] = opened.get(vf, 0) + 1
        if vt != sn.OPEN:
            closed[vt] = closed.get(vt, 0) + 1
    got_open: dict[int, int] = {}
    got_close: dict[int, int] = {}
    for f in sorted((tmp_path / "digest").glob("*.json")):
        for ts, kinds in json.loads(f.read_text())["digests"].items():
            if "opened" in kinds:
                got_open[int(ts)] = got_open.get(int(ts), 0) + kinds["opened"][0]
            if "closed" in kinds:
                got_close[int(ts)] = got_close.get(int(ts), 0) + kinds["closed"][0]
    assert (got_open, got_close) == (opened, closed)
    # the digest string hashes as ClickHouse's reinterpretAsUInt64(MD5(...)) does: the md5's first 8 bytes, little-endian
    import hashlib

    s = "2|b1/gof|alice|1785369600|10|1"
    assert con.execute(f"SELECT md5_number_upper({sn.q(s)})").fetchone()[0] == int.from_bytes(hashlib.md5(s.encode()).digest()[:8], "little")


def _brute_answer(oracle: list[tuple], term: str, date: str) -> dict:
    D = sn.scan_epoch(date)
    totals: dict[str, list[int]] = {}
    for depth, path, usr, vf, vt, _kind, size, n_files, *_ in oracle:
        if not (vf <= D < vt) or depth < 1:
            continue
        name = path.rsplit("/", 1)[-1].lower()
        parent = path.rsplit("/", 1)[0].lower() if "/" in path else ""
        if term in name and term not in parent:
            b = totals.setdefault(path.split("/", 1)[0], [0, 0])
            b[0] += size
            b[1] += n_files
    return dict(sorted(totals.items()))


def test_shards_and_reader(fixture, tmp_path):
    root, scans, merged = fixture
    ranges = sn.plan_ranges(scans, 3, str(root))
    build = tmp_path / "build"
    _build(root, scans, ranges, build)
    plan = sn.plan_shards(sorted((build / "hist").glob("*.parquet")), target_rows=40, tasks=3)
    assert [s["lo"] for s in plan["shards"]] == sorted(s["lo"] for s in plan["shards"])
    files = [str(build / "intervals" / f"r{r['i']:04d}.parquet") for r in ranges["ranges"]]
    out = tmp_path / "out"
    for t in range(len(plan["tasks"])):
        sn.build_shards(files, plan, t, out, threads=2, mem="1GB", tmp=tmp_path / "tmp")
    # every suffix row, by brute force over the oracle's versions
    expected = []
    for depth, path, usr, vf, vt, _kind, size, n_files, *_ in _oracle(merged):
        name = path.rsplit("/", 1)[-1].lower()
        if depth >= 1:
            expected += [(name[p:], depth, path, usr, vf * 1000, vt * 1000, size, n_files) for p in range(len(name) - 2)]
    got = []
    for f in sorted((out / "sx").glob("*.parquet")):
        for r in pq.read_table(f).to_pylist():
            got.append((r["s"], r["depth"], r["path"], r["usr"], sn._ms(r["vf"]), sn._ms(r["vt"]), r["size"], r["n_files"]))
    assert got == sorted(expected, key=lambda r: (r[0].encode(), r[2].encode(), r[3].encode(), r[4]))
    side = pa.concat_tables([pq.read_table(f) for f in sorted((out / "sidecar").glob("*.parquet"))])
    assert sum(side.column("rows").to_pylist()) == len(expected)

    def fetch(file: str, lo: int, hi: int) -> bytes:
        with open(out / file, "rb") as fh:
            fh.seek(lo)
            return fh.read(hi - lo)

    reader = sn.Reader(fetch, lambda f: (out / f).stat().st_size, side)
    oracle = _oracle(merged)
    for term in ["gof", "5418", "nk080", "48.parquet", "48.parquet.crc", "pio", "a'b", "é54", "zzz", "par"]:
        body = reader.answer(term, DATES)
        assert body["answers"] == {d: _brute_answer(oracle, term, d) for d in DATES}, term


def test_islands_equal_pyrmts(fixture, tmp_path):
    """The restated kernel and pyrmts' own (`_intervals_sql`) give the same runs on the fixture."""
    msd = pytest.importorskip("pyrmts_engine.multiscan_duckdb")
    from pyrmts.types import Dim, Metric, Pyramid

    from dt_cloud.overtime import _NoStore

    root, scans, _ = fixture
    con = sn.connect(2, "1GB", tmp_path / "tmp")
    ps = sn.pieces((0, ""), None)
    srcs = [f"({sn.scan_sql(con, str(root / s['src']), ps, s['version'])})" for s in scans["scans"]]
    long = " UNION ALL ".join(f"SELECT {j}::BIGINT AS __scan, * FROM {src}" for j, src in enumerate(srcs))
    pyr = Pyramid(storage=_NoStore(), keyTemplate="", binCol="depth", dims=[Dim("path", "string"), Dim("usr", "string")],
                  metrics=[Metric(c, "count") for c in sn.VALUE_COLS], tiers=[])
    theirs = con.execute(msd._intervals_sql(msd._union_sql(srcs), sn.KEY_COLS, sn.VALUE_COLS, pyr)).fetchall()
    cols = ", ".join([*sn.KEY_COLS, *sn.VALUE_COLS, "__scan_lo", "__scan_hi"])
    mine = con.execute(f"SELECT {cols} FROM ({sn.islands_sql(long, sn.KEY_COLS, sn.VALUE_COLS)})").fetchall()
    assert sorted(mine) == sorted(theirs)
