"""The serving box (`dt_cloud.box`, `dt-cloud serve-query`): the format-2
index's layout invariants, the filtered `/api/subtree` and `/api/diff`
bodies against the Worker's (`box-parity.json`, written by
`site/functions/_lib/boxParity.test.ts` over the `v2-search` fixture), the
owner / class scopes and a diff with changes on a generation built here,
and the HTTP server."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud.bench import mem
from dt_cloud.bench.query import parse, parse_simple
from dt_cloud.box import server as bs
from dt_cloud.box import view as bv

from test_bench_truth import FIX, NAMES_F, PATH_F

GOLDEN = json.loads((FIX / "box-parity.json").read_text())
DAY = 86400


# --- generations built here -------------------------------------------------------------


def write_v2(d: Path, files: list[tuple], rg_rows: int = 64) -> tuple[str, str]:
    """A store generation's `path` sort (layer-2 names: owner slices per path,
    `kind`, `mtime_mean`, `last_read`, class bytes) and its v1 names file,
    from `(path, usr, size, mtime_day, last_read_day | None, cls | None)`
    objects (`cls` 2..4: the object's storage class; None = Standard)."""
    agg: dict[tuple[str, str | None], dict] = {}
    for p, u, size, mt, lr, cls in files:
        segs = p.split("/")
        for i in range(1, len(segs) + 1):
            a = agg.setdefault(("/".join(segs[:i]), u), {"size": 0, "n": 0, "wts": 0.0, "lr": None, "c": [0, 0, 0], "file": i == len(segs)})
            a["size"] += size
            a["n"] += 1
            a["wts"] += size * mt * DAY
            if lr is not None:
                a["lr"] = max(a["lr"] or lr, lr)
            if cls:
                a["c"][cls - 2] += size
    kids: dict[str, set] = {}
    for p, _ in agg:
        if "/" in p:
            kids.setdefault(p.rsplit("/", 1)[0], set()).add(p)
    rows = sorted(agg.items(), key=lambda kv: (kv[0][0].count("/"), kv[0][0].encode(), kv[0][1] or "￿"))
    t = pa.table({
        "path": [p for (p, _), _ in rows], "usr": [u for (_, u), _ in rows], "size": [a["size"] for _, a in rows],
        "depth": pa.array([p.count("/") + 1 for (p, _), _ in rows], pa.int32()), "kind": ["file" if a["file"] else "dir" for _, a in rows],
        "n_files": [a["n"] for _, a in rows], "n_children": [len(kids.get(p, ())) for (p, _), _ in rows],
        "mtime_mean": [a["wts"] / a["size"] if a["size"] else None for _, a in rows],
        "last_read": pa.array([a["lr"] for _, a in rows], pa.int32()),
        **{f"sum_storage_class_id_{k}": [a["c"][k - 2] for _, a in rows] for k in (2, 3, 4)},
    })
    d.mkdir(parents=True, exist_ok=True)
    pf = d / "path-index.parquet"
    pq.write_table(t, pf, row_group_size=rg_rows)
    names = sorted({p.rsplit("/", 1)[-1] for (p, _) in agg})
    nf = d / "path-index.names.parquet"
    pq.write_table(pa.table({"id": pa.array(range(len(names)), pa.int32()), "name": names, "rgs": ["0"] * len(names)}), nf)
    return str(pf), str(nf)


OWN = [
    ("b/u1/ckpt/a.bin", "alice", 100, 20000, 20010, None),
    ("b/u1/ckpt/b.bin", "alice", 50, 20002, None, None),
    ("b/u1/logs/x.txt", "alice", 10, 20004, 20011, None),
    ("b/u2/ckpt/c.bin", "bob", 200, 20006, None, 2),
    ("b/u2/tmp/d.bin", None, 30, 20008, None, 3),
    ("c/Big/HUGE.bin", None, 5 << 30, 20000, None, None),
    *[(f"c/many/f{i:03d}", None, 1, 20000, None, None) for i in range(300)],
]


@pytest.fixture(scope="module")
def own(tmp_path_factory):
    d = tmp_path_factory.mktemp("own")
    pf, nf = write_v2(d, OWN)
    meta = mem.build(pf, nf, d / "mem", threads=2, mem="1GB", detail_rg=16)
    return {"meta": meta, "ix": mem.MemIndex.load(d / "mem", threads=2)}


def test_layout(own):
    m, ix = own["meta"], own["ix"]
    assert {k: m[k] for k in ("v", "rows", "nodes", "names", "multi_slice_nodes", "case_exceptions", "b_overflow", "o_overflow")} == {
        "v": 2, "rows": 319, "nodes": 316, "names": 315, "multi_slice_nodes": 2, "case_exceptions": 0, "b_overflow": 3, "o_overflow": 2,
    }
    # Breadth-first ids: parents never decrease, a depth is an id range, the
    # buckets come first, largest first; a node's children are one range,
    # largest first.
    assert (np.diff(ix.parent) >= 0).all()
    assert ix.dstart.tolist() == [0, 2, 6, 311, 316]
    assert ix.paths(ix.top) == ["c", "b"]
    assert ix.paths(ix.children(np.array([ix.find("b")]))) == ["b/u2", "b/u1"]
    # Narrowed columns read back exactly: `c` and `c/Big/HUGE.bin` past 2³²
    # bytes, `c` and `c/many` past 255 objects.
    big, many = ix.find("c/Big/HUGE.bin"), ix.find("c/many")
    assert [ix.b1(ix.find("c")), ix.b1(big), ix.o1(ix.find("c")), ix.o1(many), ix.o1(-1), ix.b1(-1)] == [
        (5 << 30) + 300, 5 << 30, 301, 300, 306, (5 << 30) + 300 + 390,
    ]
    # Original case from the lowercase blob + case bits.
    assert ix.path(big) == "c/Big/HUGE.bin"
    assert ix.names_arrow(ix.nid[[big]], lower=True).to_pylist() == ["huge.bin"]
    # The cold detail by id, any order: the same through a one-group cache
    # read two groups at a time.
    ids = np.array([300, 0, 150, 1, 299, 0])
    cols = lambda d: {k: [None if x != x else x for x in v.tolist()] for k, v in d.items()}  # noqa: E731 — NaN (no mean) as None
    want = cols(ix.detail.take(ids))
    assert cols(mem.Detail(ix.detail.src, None, cache_groups=1).take(ids, batch=2)) == want
    # (`b`, id 1, holds three owner slices: its detail row defers to `slices.parquet`.)
    assert (ix.paths([1]), want["kind"], want["multi"]) == (["b"], [1, 0, 1, 0, 1, 0], [False, False, False, True, False, False])
    # Mapped, the same answers.
    mm = mem.MemIndex.load(Path(own["ix"].detail.src).parent, threads=2, mmap=True)
    assert (type(mm.parent).__name__, mm.paths(mm.children(np.array([mm.find("b/u1")]))), mm.b1(mm.find("c"))) == ("memmap", ["b/u1/ckpt", "b/u1/logs"], (5 << 30) + 300)


def test_vocab_case_exceptions():
    # A name whose lowercase isn't its ASCII-folded self (Kelvin sign, Turkish
    # dotted I, a length change) is stored whole; the rest come back from the
    # lowercase blob and the case bits.
    names = ["Key", "Key", "İstanbul", "ẞ", "plain", "MiXeD.Bin", ""]
    ix = mem.MemIndex.from_names(pa.array(names, pa.large_string()), threads=1)
    assert ix.case_ex_id.tolist() == [1, 2, 3]
    assert ix.names_arrow(np.arange(len(names))).to_pylist() == names
    assert ix.names_arrow(np.array([5, -1, 0]), lower=True).to_pylist() == ["mixed.bin", None, "key"]


def test_node_aggs_scopes(own):
    ix = own["ix"]
    ids = np.array([ix.find(p) for p in ("b", "b/u2", "b/u1/ckpt")] + [-1])

    def rows(scope):
        a = bv.node_aggs(ix, ids, scope)
        return [(int(a.b[i]), int(a.o[i]), {k: v for k, v in sorted(bv.AggSet.agg(a, i, ix.detail.user_names).ub.items())}, a.c[i].tolist()) for i in range(len(ids))]

    big = (5 << 30) + 300
    assert rows(bv.Scope()) == [
        (390, 5, {"alice": 160, "bob": 200}, [200, 30, 0]),
        (230, 2, {"bob": 200}, [200, 30, 0]),
        (150, 2, {"alice": 150}, [0, 0, 0]),
        (big + 390, 306, {"alice": 160, "bob": 200}, [200, 30, 0]),
    ]
    assert rows(bv.Scope(owner="unowned")) == [(30, 1, {}, [0, 30, 0]), (30, 1, {}, [0, 30, 0]), (0, 0, {}, [0, 0, 0]), (big + 30, 302, {}, [0, 30, 0])]
    assert rows(bv.Scope(owner=frozenset({"alice"}))) == [(200, 1, {"bob": 200}, [200, 0, 0]), (200, 1, {"bob": 200}, [200, 0, 0]), (0, 0, {}, [0, 0, 0]), (200, 1, {"bob": 200}, [200, 0, 0])]
    # `cl=n` (Nearline): every slice cut to its class-2 bytes, objects scaled.
    assert rows(bv.Scope(classes=frozenset({"2"}))) == [(200, 1, {"alice": 0, "bob": 200}, [200, 0, 0]), (200, 1, {"bob": 200}, [200, 0, 0]), (0, 0, {"alice": 0}, [0, 0, 0]), (200, 1, {"alice": 0, "bob": 200}, [200, 0, 0])]


def body(ix, path, q, **kw):
    w, h = kw.pop("w", 1280), kw.pop("h", 896)
    ma, at = kw.pop("min_area", 12), kw.pop("atten", 2)
    owner = kw.pop("owner_raw", None)
    date = kw.pop("date", "2026-10-01")
    r = bv.filter_view(ix, path, parse_simple(q, min_term=1), w=w, h=h, min_area=ma, atten=at, scope=bv.Scope(bv.parse_owner(owner), bv.parse_classes(kw.pop("cl", None))), **kw)
    return json.loads("".join(bv.subtree_body(ix, r, date=date, path=path, w=w, h=h, min_area=ma, atten=at, q=q, owner_raw=owner, root_label="root")))


def test_subtree_scoped(own):
    ix = own["ix"]
    head = {"date": "2026-10-01", "w": 1280, "h": 896, "minArea": 12, "atten": 2, "tier": "box", "index": "mem", "truncated": False}
    assert body(ix, "b", "ckpt") == {
        **head, "path": "b", "threshold": 0, "nodes": 7, "q": "ckpt",
        "matches": ["b/u1/ckpt", "b/u2/ckpt"],
        "matched": [{"path": "b/u2/ckpt", "b": 200, "o": 1}, {"path": "b/u1/ckpt", "b": 150, "o": 2}],
        # `d`: the size-weighted mean write day ((100·20000 + 50·20002 + 200·20006) / 350 = 20003.7).
        "tree": {"n": "b", "k": "dir", "b": 350, "o": 3, "d": 20004, "a": 20010, "cb": {"2": 200}, "us": [["bob", 200], ["alice", 150]], "c": [
            {"n": "u2", "k": "dir", "b": 200, "o": 1, "d": 20006, "cb": {"2": 200}, "us": [["bob", 200]], "c": [
                {"n": "ckpt", "k": "dir", "b": 200, "o": 1, "d": 20006, "cb": {"2": 200}, "us": [["bob", 200]], "m": 1, "c": [
                    {"n": "c.bin", "k": "file", "b": 200, "o": 1, "d": 20006, "cb": {"2": 200}, "us": [["bob", 200]]}]}]},
            {"n": "u1", "k": "dir", "b": 150, "o": 2, "d": 20001, "a": 20010, "us": [["alice", 150]], "c": [
                {"n": "ckpt", "k": "dir", "b": 150, "o": 2, "d": 20001, "a": 20010, "us": [["alice", 150]], "m": 1, "c": [
                    {"n": "a.bin", "k": "file", "b": 100, "o": 1, "d": 20000, "a": 20010, "us": [["alice", 100]]},
                    {"n": "b.bin", "k": "file", "b": 50, "o": 1, "d": 20002, "us": [["alice", 50]]}]}]}]},
    }
    # Owner pool: owned, but not by Alice. Her root stays a (zero) node and a
    # `matched` entry, as the Worker keeps every root.
    got = body(ix, "", "ckpt", owner_raw="!alice")
    assert (got["owner"], got["matched"], got["tree"]["b"], got["tree"]["c"][0]["c"]) == (
        {"not": ["alice"]}, [{"path": "b/u2/ckpt", "b": 200, "o": 1}, {"path": "b/u1/ckpt", "b": 0, "o": 0}], 200, [
            {"n": "u2", "k": "dir", "b": 200, "o": 1, "d": 20006, "cb": {"2": 200}, "us": [["bob", 200]], "c": [
                {"n": "ckpt", "k": "dir", "b": 200, "o": 1, "d": 20006, "cb": {"2": 200}, "us": [["bob", 200]], "m": 1, "c": [
                    {"n": "c.bin", "k": "file", "b": 200, "o": 1, "d": 20006, "cb": {"2": 200}, "us": [["bob", 200]]}]}]},
            {"n": "u1", "k": "dir", "b": 0, "o": 0, "c": [{"n": "ckpt", "k": "dir", "b": 0, "o": 0, "m": 1}]},
        ],
    )
    # Nothing in scope: the empty view.
    assert body(ix, "b", "ckpt", owner_raw="unowned") == {**head, "path": "b", "tier": "none", "index": "none", "threshold": 0, "nodes": 0, "owner": "unowned", "q": "ckpt",
                                                          "matches": [], "matched": [], "tree": {"n": "b", "k": "dir", "b": 0, "o": 0}}


@pytest.mark.parametrize("i", range(len(GOLDEN["subtree"])))
def test_subtree_parity(i, fixture_ix):
    """Every case's body equals the Worker's, less the fields only one side
    has (`tier` / `index`, the Worker's coverage flags). (A NOT-only query at
    the store root agrees too since the Worker's `rootFor('')` fix, 10f553e:
    both subtract the six `ttl` roots.)"""
    e = GOLDEN["subtree"][i]
    c, want = e["case"], e["body"]
    got = body(fixture_ix, c["path"], c["q"], date=want["date"], w=c.get("w", 1280), h=c.get("h", 896), min_area=c.get("minArea", 12), atten=c.get("atten", 2), max_depth=c.get("depth"))
    drop = {"tier", "index", "partial", "partialReason", "approximate", "approximateReason"}
    got, want = ({k: v for k, v in b.items() if k not in drop and v is not None} for b in (got, want))
    assert got == want


@pytest.fixture(scope="module")
def fixture_ix(tmp_path_factory):
    d = tmp_path_factory.mktemp("v2search")
    mem.build(PATH_F, NAMES_F, d, threads=2, mem="1GB", detail_rg=1000)
    return mem.MemIndex.load(d, threads=2)


@pytest.mark.parametrize("i", range(len(GOLDEN["diff"])))
def test_diff_parity(i, fixture_ix):
    e = GOLDEN["diff"][i]
    c, want = e["case"], e["body"]
    got = json.loads("".join(bv.diff_body(fixture_ix, fixture_ix, prev=want["prev"], curr=want["curr"], path=c["path"], w=1280, h=896, min_area=12, atten=2, top=500,
                                          ast=parse_simple(c["q"], min_term=1), q=c["q"], depth=c.get("depth"))))
    assert {k: v for k, v in got.items() if k != "tier"} == {k: v for k, v in want.items() if k != "tier" and v is not None}


# --- a diff with changes ----------------------------------------------------------------


@pytest.fixture(scope="module")
def two(tmp_path_factory):
    d = tmp_path_factory.mktemp("two")
    a = [f[:2] + (f[2],) + f[3:] for f in OWN if not f[0].startswith("c/many")]
    b = [f for f in a if f[0] != "b/u1/ckpt/b.bin"] + [("b/u1/ckpt/z.bin", "alice", 70, 20009, None, None), ("b/u3/ckpt/n.bin", None, 5, 20009, None, None)]
    b = [(p, u, 260, mt, lr, c) if p == "b/u2/ckpt/c.bin" else (p, u, s, mt, lr, c) for p, u, s, mt, lr, c in b]
    out = []
    for k, files in (("a", a), ("b", b)):
        pf, nf = write_v2(d / k, files)
        mem.build(pf, nf, d / k / "mem", threads=2, mem="1GB")
        out.append(mem.MemIndex.load(d / k / "mem", threads=2))
    return out


def test_diff_changes(two):
    ixa, ixb = two
    got = json.loads("".join(bv.diff_body(ixa, ixb, prev="2026-09-30", curr="2026-10-01", path="b", w=1280, h=896, min_area=12, atten=2, top=500,
                                          ast=parse_simple("ckpt"), q="ckpt")))
    assert got == {
        "prev": "2026-09-30", "curr": "2026-10-01", "path": "b", "q": "ckpt",
        # The expanded skeleton (`x`), then the changed frontier by |Δ|.
        "rows": [
            {"p": "u1", "d": 1, "k": "dir", "s": "changed", "a": 150, "b": 170, "oa": 2, "ob": 2, "x": True},
            {"p": "u2", "d": 1, "k": "dir", "s": "changed", "a": 200, "b": 260, "oa": 1, "ob": 1, "x": True},
            {"p": "u3", "d": 1, "k": "dir", "s": "added", "a": 0, "b": 5, "oa": 0, "ob": 1, "x": True},
            {"p": "u1/ckpt", "d": 2, "k": "dir", "s": "changed", "a": 150, "b": 170, "oa": 2, "ob": 2, "x": True},
            {"p": "u2/ckpt", "d": 2, "k": "dir", "s": "changed", "a": 200, "b": 260, "oa": 1, "ob": 1, "x": True},
            {"p": "u3/ckpt", "d": 2, "k": "dir", "s": "added", "a": 0, "b": 5, "oa": 0, "ob": 1, "x": True},
            {"p": "u1/ckpt/z.bin", "d": 3, "k": "file", "s": "added", "a": 0, "b": 70, "oa": 0, "ob": 1},
            {"p": "u2/ckpt/c.bin", "d": 3, "k": "file", "s": "changed", "a": 200, "b": 260, "oa": 1, "ob": 1},
            {"p": "u1/ckpt/b.bin", "d": 3, "k": "file", "s": "removed", "a": 50, "b": 0, "oa": 1, "ob": 0},
            {"p": "u3/ckpt/n.bin", "d": 3, "k": "file", "s": "added", "a": 0, "b": 5, "oa": 0, "ob": 1},
        ],
        "total_a": 350, "total_b": 435, "objects_a": 3, "objects_b": 4, "threshold": 0, "tier": "box",
        "matched": [{"path": "b/u1/ckpt", "b": 170, "o": 2}, {"path": "b/u2/ckpt", "b": 260, "o": 1}, {"path": "b/u3/ckpt", "b": 5, "o": 1}],
        # Lookups: the names one side lacks inside its query (`z.bin`, `b.bin`).
        "expansions": 7, "truncated": False, "lookups": 2, "lookups_capped": False,
    }


# --- the server -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server(own, tmp_path_factory):
    root = tmp_path_factory.mktemp("root")
    (root / "2026-10-01").symlink_to(Path(own["ix"].detail.src).parent)
    box = bs.Box(root=str(root), threads=2, root_label="root")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), bs.make_handler(box, "s3cret"))
    box.start()
    box.done.wait(30)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def get(url: str, token: str | None = "s3cret") -> tuple[int, str, str]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers["x-query-engine"].split(";")[0], r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers["x-query-engine"].split(";")[0] if e.headers["x-query-engine"] else "", e.read().decode()


def test_server(server, own):
    st, eng, b = get(f"{server}/healthz", token=None)
    assert (st, eng, json.loads(b)["state"], [s["date"] for s in json.loads(b)["scans"]]) == (200, "box", "ready", ["2026-10-01"])
    assert get(f"{server}/api/subtree?date=2026-10-01&path=b&q=ckpt", token=None) == (401, "box", "unauthorized")
    assert get(f"{server}/api/subtree?date=2026-10-01&path=b&q=ckpt", token="wrong") == (401, "box", "unauthorized")
    st, eng, b = get(f"{server}/api/subtree?date=2026-10-01&path=b&q=ckpt&w=1200&h=850")
    assert (st, eng) == (200, "box")
    assert json.loads(b) == body(own["ix"], "b", "ckpt", w=1280, h=896)
    assert [get(f"{server}{u}") for u in (
        "/api/subtree?date=2026-09-01&path=b&q=ckpt",
        "/api/diff?from=2026-09-30&to=2026-10-01&q=ckpt",
        "/api/subtree?date=2026-10-01&path=nope&q=ckpt",
        "/api/subtree?date=2026-10-01&path=b",
        "/api/subtree?date=2026-10-01&path=b&q=ab",
        "/api/subtree?date=2026-10-01&q=a/.*json&qs=regex",
        "/api/subtree?date=2026-10-01&q=ckpt&lens=user:alice",
        "/api/subtree?date=bad&q=ckpt",
        "/nope",
    )] == [
        (409, "box", "scan 2026-09-01 not loaded (loaded: 2026-10-01)"),
        (409, "box", "scan 2026-09-30 not loaded (loaded: 2026-10-01)"),
        (404, "box", "path not found"),
        (400, "box", "the box answers filtered reads only (q=)"),
        (400, "box", "bad query: type at least 3 characters (“ab”)"),
        (501, "box", "not supported by the box: regex 'a/.*json': no name filter (its tail can cross a `/` or isn't `$`-anchored)"),
        (409, "box", "a user lens isn't served by the box"),
        (400, "box", "bad date"),
        (404, "box", "not found"),
    ]


def test_fold_past_hard_cap(own, monkeypatch):
    # Past HARD_CAP match roots, the ones under the forest threshold fold
    # into their parent (here: all 100 one-byte `f0xx` objects, into
    # `c/many`, left a leaf); `matches` / `matched` still list every root.
    monkeypatch.setattr(bv, "HARD_CAP", 10)
    g = body(own["ix"], "c", "f0", min_area=5000, w=128, h=128)
    assert ({k: g[k] for k in ("threshold", "nodes", "folded", "truncated")}, len(g["matches"]), g["matched"][:2], g["tree"]) == (
        {"threshold": 31, "nodes": 1, "folded": 100, "truncated": False}, 100, [{"path": "c/many/f000", "b": 1, "o": 1}, {"path": "c/many/f001", "b": 1, "o": 1}],
        {"n": "c", "k": "dir", "b": 100, "o": 100, "d": 20000, "c": [{"n": "many", "k": "dir", "b": 100, "o": 100, "d": 20000}]},
    )


def test_regex_verify_on_lowercase(fixture_ix):
    # A case-insensitive regex tested on the lowercase paths (and a path
    # holding a case exception, the Kelvin-sign `Key`, on its original case)
    # holds exactly where it holds on the original paths.
    import pyarrow.compute as pc

    ix = fixture_ix
    nodes = np.arange(ix.n)
    orig = ix.segments(nodes, None, lower=False)
    for src in ("^bk/key", "^bk/Key/", "[A-Z]ey/", "\\.BIN$", "ttl"):
        assert ix._verify_ci(nodes, src).tolist() == pc.match_substring_regex(orig, src, ignore_case=True).to_numpy(zero_copy_only=False).tolist()


@pytest.mark.parametrize("cap", [50_000, 10])
def test_chunked_roots_same_answers(fixture_ix, own, monkeypatch, cap):
    # Roots aggregated two at a time (what bounds a 20M-root query's working
    # set) give the bodies the whole set at once gives, folding or not.
    monkeypatch.setattr(bv, "HARD_CAP", cap)
    cases = [(fixture_ix, e["case"]["path"], e["case"]["q"], {"min_area": e["case"].get("minArea", 12)}) for e in GOLDEN["subtree"]]
    cases += [(own["ix"], "", "ckpt", {"owner_raw": "!alice"}), (own["ix"], "c", "f0", {"min_area": 5000, "w": 128, "h": 128})]
    want = [body(ix, p, q, **kw) for ix, p, q, kw in cases]
    monkeypatch.setattr(bv, "CHUNK", 2)
    assert [body(ix, p, q, **kw) for ix, p, q, kw in cases] == want
