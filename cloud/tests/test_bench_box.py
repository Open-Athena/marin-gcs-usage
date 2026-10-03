"""The serving box's engines (`dt_cloud.bench.mem`, `.duck`) against the
ground truth (`truth.compute`, both methods), on the site's v2-search fixture
and on a small generation built here with owner slices, mixed case, nested
matches and the anchors."""

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud.bench import duck, local, mem
from dt_cloud.bench import truth as bt
from dt_cloud.bench.query import glob, parse, regex, sub
from dt_cloud.bench.queryset import parse_set
from dt_cloud.bench.score import Truth, run, tally
from dt_cloud.bench.terms import NameTest, RegexPlan, SegTerm, regex_name_filter, seg_term

from test_bench_truth import NAMES_F, PATH_F, QUERIES


# --- terms ----------------------------------------------------------------------------


def test_seg_term():
    assert [seg_term(m) for m in (sub("tomat"), sub("/tomat"), sub("tomat/"), sub("checkpoints/step-"), sub("a/b/"))] == [
        SegTerm(0, False, NameTest("contains", "tomat"), "tomat", False),
        SegTerm(1, False, NameTest("starts", "tomat"), "/tomat", False),
        SegTerm(0, True, NameTest("ends", "tomat"), "tomat$", False),
        SegTerm(1, False, NameTest("starts", "step-"), "checkpoints/step-", False),
        SegTerm(1, True, NameTest("equals", "b"), "a/b$", False),
    ]
    assert [seg_term(m) for m in (glob("model-", "-of-00004"), glob("ckpt/", ".pt"), glob("tmp/", ""))] == [
        SegTerm(0, False, NameTest("regex", "model-[^/]*-of-00004"), "model-[^/]*-of-00004", False),
        SegTerm(1, False, NameTest("regex", "^[^/]*\\.pt"), "ckpt/[^/]*\\.pt", False),
        SegTerm(1, False, NameTest("regex", "^[^/]*"), "tmp/[^/]*", True),
    ]
    with pytest.raises(ValueError):
        seg_term(regex("x$"))


def test_regex_name_filter():
    assert [regex_name_filter(s) for s in ("\\.safetensors$", "/step-[0-9]+$", "^marin-us-central2/grug/[^/]*moe[^/]*$", "tokenizer\\.json$")] == [
        RegexPlan("\\.safetensors$", "\\.safetensors$"),
        RegexPlan("/step-[0-9]+$", "^step-[0-9]+$"),
        RegexPlan("^marin-us-central2/grug/[^/]*moe[^/]*$", "^[^/]*moe[^/]*$"),
        RegexPlan("tokenizer\\.json$", "tokenizer\\.json$"),
    ]
    # Unplannable: not end-anchored, a tail that can cross a `/`, a top-level
    # alternation, a lone `$`.
    assert [regex_name_filter(s) for s in ("step-1/.*json", "a/.*\\.json$", "x/[^a]+$", "a$|b$", "a/$", "(a/b)$", "\\w+\\W$")] == [None] * 7
    assert regex_name_filter("ckpt[^/]*/[a-z_]+\\.json\\Z") == RegexPlan("ckpt[^/]*/[a-z_]+\\.json\\Z", "^[a-z_]+\\.json\\Z")


# --- a generation ----------------------------------------------------------------------

FILES = [
    ("b/ckpts/checkpoints/step-100/model.pt", "a", 100),
    ("b/ckpts/checkpoints/step-100/opt.pt", "b", 50),
    ("b/ckpts/checkpoints/step-200/model.pt", "b", 200),
    ("b/ckpts/other/step-300/x.bin", "a", 30),
    ("b/Tomat/podcast.mp3", "a", 7),
    ("b/xtomat/inner/tomat-file", "b", 9),
    ("b/run/Checkpoints/step-1/eval/a.json", "a", 3),
    ("b/run/Checkpoints/step-1/b.json", "b", 4),
    ("c/data/x.safetensors", "a", 11),
    ("c/data/w.safetensors/inner.safetensors", "b", 13),
    ("c/data/W.SAFETENSORS/readme", "a", 1),
    ("c/tomat/k", "a", 5),
]


def write_gen(d: Path, files=FILES, rg_rows: int = 4) -> tuple[str, str]:
    """A `path` sort (dirs carry one row per owner slice, `(depth, path, usr)`
    order, `rg_rows`-row groups) and its v1 names file (`rgs`)."""
    agg: dict[tuple[str, str], list[int]] = {}
    for p, u, b in files:
        segs = p.split("/")
        for i in range(1, len(segs) + 1):
            a = agg.setdefault(("/".join(segs[:i]), u), [0, 0])
            a[0] += b
            a[1] += 1
    rows = sorted(((p.count("/") + 1, p, u, b, o) for (p, u), (b, o) in agg.items()), key=lambda r: (r[0], r[1].encode(), r[2]))
    t = pa.table({"path": [r[1] for r in rows], "usr": [r[2] for r in rows], "size": [r[3] for r in rows], "depth": pa.array([r[0] for r in rows], pa.int32()), "n_files": [r[4] for r in rows]})
    d.mkdir(parents=True, exist_ok=True)
    pf = d / "path-index.parquet"
    pq.write_table(t, pf, row_group_size=rg_rows)
    groups: dict[str, set[int]] = {}
    for i, r in enumerate(rows):
        groups.setdefault(r[1].rsplit("/", 1)[-1], set()).add(i // rg_rows)
    names = sorted(groups, key=lambda n: (-len(groups[n]), n))
    nf = d / "path-index.names.parquet"
    pq.write_table(pa.table({
        "id": pa.array(range(len(names)), pa.int32()), "name": names,
        "rgs": [",".join(map(str, sorted(groups[n]))) for n in names],
    }), nf)
    return str(pf), str(nf)


BOX = {
    "views": ["", "b", "b/ckpts/checkpoints"],
    "queries": [
        {"id": "ckpts-step", "q": "checkpoints/step-"},
        {"id": "tomat-end", "q": "tomat/"},
        {"id": "tomat-start", "q": "/tomat"},
        {"id": "tomat", "q": "tomat"},
        {"id": "step-not-eval", "q": "step- -eval"},
        {"id": "not-ckpts", "q": "-checkpoints"},
        {"id": "ckpts-not-other", "q": "ckpts -other"},
        {"id": "ckpts-dir", "q": "checkpoints/"},
        {"id": "ext-pt", "q": "*.pt"},
        {"id": "step-00", "q": "step-*00"},
        {"id": "pt-or-json", "q": "model|json -eval"},
        {"id": "re-st", "q": "\\.safetensors$", "qs": "regex", "views": ["", "c/data"]},
        {"id": "re-step", "q": "/step-[0-9]+$", "qs": "regex"},
        {"id": "re-ckpts-step", "q": "^b/ckpts/[^/]*/step-[0-9]+$", "qs": "regex"},
    ],
}


@pytest.fixture(scope="module")
def gen(tmp_path_factory):
    d = tmp_path_factory.mktemp("gen")
    pf, nf = write_gen(d)
    cases = parse_set(BOX)
    con = bt.connect(threads=2, mem="1GB")
    truths = bt.compute(cases, pf, nf, [c.id for c in cases], con)
    bt.write(truths, str(d / "truth"), {"date": "2026-10-01"})
    meta = mem.build(pf, nf, d / "mem", threads=2, mem="1GB")
    return {"pf": pf, "nf": nf, "d": d, "cases": cases, "truths": {t.id: t for t in truths}, "meta": meta}


def test_truth_methods_agree(gen):
    assert {k: (t.stats["method"], t.check and t.check["identical"]) for k, t in gen["truths"].items()} == {
        "ckpts-step": ("names-first", True), "tomat-end": ("scan", None), "tomat-start": ("names-first", True),
        "tomat": ("names-first", True), "step-not-eval": ("names-first", True), "not-ckpts": ("names-first", True),
        "ckpts-not-other": ("names-first", True), "ckpts-dir": ("scan", None), "ext-pt": ("names-first", True),
        "step-00": ("names-first", True), "pt-or-json": ("names-first", True), "re-st": ("scan", None), "re-step": ("scan", None),
        "re-ckpts-step": ("scan", None),
    }


def test_build(gen):
    m = gen["meta"]
    assert (m["rows"], m["nodes"], m["no_name"]) == (40, 31, 0)
    ix = mem.MemIndex.load(gen["d"] / "mem", threads=2)
    assert ix.paths(ix.top) == ["b", "c"]
    assert ix.paths(ix.children(ix.top[:1])) == ["b/ckpts", "b/xtomat", "b/Tomat", "b/run"]
    s = ix.find("b/ckpts/checkpoints")
    assert (ix.paths(ix.children(np.array([s]))), int(ix.b[s]), int(ix.o[s])) == (
        ["b/ckpts/checkpoints/step-200", "b/ckpts/checkpoints/step-100"], 350, 3,
    )


def test_mem_answers(gen):
    ix = mem.MemIndex.load(gen["d"] / "mem", threads=2)

    def ans(q, view, qs="simple"):
        r = mem.evaluate(ix, parse(q, qs), view)
        roots = [view] if r.hit else ix.paths(r.roots, sort=True)
        return roots, r.b, r.o, ix.paths(r.excluded, sort=True)

    assert ans("tomat/", "") == (["b/Tomat/podcast.mp3", "b/xtomat/inner", "c/tomat/k"], 21, 3, [])
    assert ans("checkpoints/", "b/ckpts/checkpoints") == (["b/ckpts/checkpoints/step-100", "b/ckpts/checkpoints/step-200"], 350, 3, [])
    assert ans("step- -eval", "b") == (["b/ckpts/checkpoints/step-100", "b/ckpts/checkpoints/step-200", "b/ckpts/other/step-300", "b/run/Checkpoints/step-1"], 384, 5, ["b/run/Checkpoints/step-1/eval"])
    assert ans("-checkpoints", "") == ([""], 433 - 357, 12 - 5, ["b/ckpts/checkpoints", "b/run/Checkpoints"])
    assert ans("\\.safetensors$", "", "regex") == (["c/data/W.SAFETENSORS", "c/data/w.safetensors", "c/data/x.safetensors"], 25, 3, [])
    with pytest.raises(mem.Unsupported):
        mem.evaluate(ix, parse("step-1/.*json", "regex"), "")


def score_engine(gen, kind, obj):
    e = local.LocalEngine(kind, obj)
    scores = run(e, gen["cases"], Truth(str(gen["d"] / "truth")))
    return tally(scores), e


@pytest.mark.parametrize("list_max", [50_000, 1])
def test_engines_exact(gen, list_max, monkeypatch):
    # list_max = 1: every multi-root answer carries its count + md5, not the list.
    monkeypatch.setattr(local, "LIST_MAX", list_max)
    ix = mem.MemIndex.load(gen["d"] / "mem", threads=2)
    n = sum(len(c.views) for c in gen["cases"])
    t, e = score_engine(gen, "mem", ix)
    assert t == {"exact": n}
    assert len(e.timings) == n
    t, _ = score_engine(gen, "duck", duck.DuckIndex(gen["pf"], gen["nf"], threads=2, mem="1GB"))
    assert t == {"exact": n}


def test_duck_unplannable_regex_scans(gen):
    ix = duck.DuckIndex(gen["pf"], gen["nf"], threads=2, mem="1GB")
    r = ix.evaluate(parse("step-1/.*json", "regex"), "")
    assert (r.roots, r.b, r.o, r.stats["map"], ix.roots()) == (2, 7, 2, "full-scan", ["b/run/Checkpoints/step-1/b.json", "b/run/Checkpoints/step-1/eval/a.json"])


def test_engines_exact_on_site_fixture(tmp_path):
    cases = parse_set(QUERIES)
    con = bt.connect(threads=2, mem="1GB")
    bt.write(bt.compute(cases, PATH_F, NAMES_F, [], con), str(tmp_path / "truth"), {"date": "x"})
    mem.build(PATH_F, NAMES_F, tmp_path / "mem", threads=2, mem="1GB")
    tr = Truth(str(tmp_path / "truth"))
    n = sum(len(c.views) for c in cases)
    assert tally(run(local.LocalEngine("mem", mem.MemIndex.load(tmp_path / "mem", threads=2)), cases, tr)) == {"exact": n}
    assert tally(run(local.LocalEngine("duck", duck.DuckIndex(PATH_F, NAMES_F, threads=2, mem="1GB")), cases, tr)) == {"exact": n}


def test_latency_summary():
    ts = [local.Timing("a", "", ms, mat, 1, {}) for ms, mat in [(10, 1), (20, 2), (30, 3), (40, 4)]] + [local.Timing("b", "", 0, 0, None, {"error": "x"})]
    assert local.latency_summary(ts) == {"n": 4, "p50_ms": 30, "p90_ms": 40, "max_ms": 40, "with_paths_p50_ms": 33, "with_paths_p90_ms": 44, "with_paths_max_ms": 44}
    assert json.loads(json.dumps(local.latency_summary([]))) == {"n": 0, "p50_ms": None, "p90_ms": None, "max_ms": None, "with_paths_p50_ms": None, "with_paths_p90_ms": None, "with_paths_max_ms": None}
