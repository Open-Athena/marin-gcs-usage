"""`mega_catalog`: the consolidated catalog, built by appending each scan's
changes, equals on every scan a brute-force catalog of that scan's source rows
— its registry over every substring of every name (≥ T direct paths, or ≤ S
characters and present) with each literal's direct-path count, and every
registered literal's first-hit bucket totals — and equals a fresh build from
that scan's live rows alone. The fixture days open a second owner slice on a
live path and then close it (the path stays live), close the last slice of a
multi-owner directory's owner, and change sizes under shared ancestors."""

from pathlib import Path
from shutil import which
from subprocess import run

import pytest

from dt_cloud.chstore import ingest as ci
from dt_cloud.chstore import mega_catalog as mc
from dt_cloud.chstore import mega_names
from dt_cloud.chstore.client import Ch

from chserver import ch_db, ch_url  # noqa: F401 — fixtures
from test_box import write_v2
from test_chmega_names import brute
from test_chstore import DAYS as BASE_DAYS, src_rows

NATIVE = Path(__file__).parents[1] / "src/dt_cloud/chstore/native"
C = BASE_DAYS["2026-10-01"]
D4 = [f for f in C if f[0] != "b/u2/tmp/d.bin"] + [("b/u1/logs/y.txt", "bob", 7, 20012, None, None)]
D5 = [f for f in D4 if f[0] != "b/u1/logs/y.txt"]
DAYS = {**BASE_DAYS, "2026-10-02": D4, "2026-10-03": D5}
THRESHOLD, SHORT = 3, 1


@pytest.fixture(scope="module")
def binaries(tmp_path_factory) -> dict[str, Path]:
    cxx = which("c++") or which("g++")
    if cxx is None:
        pytest.skip("no C++ compiler")
    d = tmp_path_factory.mktemp("native")
    out = {}
    for name, source in (("census", "hot_frequency.cpp"), ("delta", "catalog_delta.cpp")):
        out[name] = d / name
        run([cxx, "-O2", "-std=c++17", "-Wall", "-Wextra", "-Werror", str(NATIVE / source), "-o", str(out[name])], check=True)
    return out


@pytest.fixture(scope="module")
def store(ch_url, ch_db, tmp_path_factory):  # noqa: F811
    d = tmp_path_factory.mktemp("days")
    files = {day: write_v2(d / day, fs)[0] for day, fs in DAYS.items()}
    ch = Ch(ch_url, db=ch_db)
    for day in DAYS:
        ci.Ingest(ch, day, files[day], threads=2, log=lambda *a: None).run()
    mega_names.build_spans(ch)
    mega_names.build_postings(ch, "all")
    return {"ch": ch, "rows": {day: src_rows(files[day]) for day in DAYS}}


def expected(rows: list[tuple]) -> dict[str, tuple[int, dict[str, tuple[int, int]]]]:
    """The scan's catalog by brute force: every substring of every live name, counted over distinct paths."""
    names = {(depth, path): path.rsplit("/", 1)[-1].lower() for depth, path, *_ in rows if depth >= 1}
    candidates = {n[i:j] for n in names.values() for i in range(len(n)) for j in range(i + 1, len(n) + 1)}
    out = {}
    for t in candidates:
        paths = sum(t in n for n in names.values())
        if paths >= THRESHOLD or (len(t) <= SHORT and paths):
            body = brute(rows, t)
            out[t] = (paths, {row["path"]: (row["b"], row["o"]) for row in body["buckets"] if (row["b"], row["o"]) != (0, 0)})
    return out


def build(store, binaries, stem: str, *, postings: str | None, through: str | None = None) -> list[dict]:
    return mc.build(store["ch"], stem, census_binary=binaries["census"], delta_binary=binaries["delta"], postings=postings,
                    through=through, threshold=THRESHOLD, short=SHORT, threads=2, parallel=3)


@pytest.mark.parametrize("postings", [None, "all"])
def test_every_scan_equals_brute_force(store, binaries, postings):
    stem = f"cat_{postings or 'scan'}"
    mc.drop(store["ch"], stem)
    log = build(store, binaries, stem, postings=postings)
    assert [(row["date"], row["base"]) for row in log] == [(day, day == "2026-09-29") for day in DAYS]
    for day, rows in store["rows"].items():
        assert mc.snapshot(store["ch"], stem, day) == expected(rows), day


def test_multi_slice_paths_are_read_exactly(store, binaries):
    """`b/u1/logs` gains a second owner slice on 10-02 (an open while alice's is live: no count change) and loses
    it on 10-03 (a close that leaves it live). Only the exact reads see both."""
    stem = "cat_multi"
    mc.drop(store["ch"], stem)
    log = build(store, binaries, stem, postings=None)
    ch = store["ch"]
    assert ch.json(f"SELECT depth, path, toString(d) FROM {stem}_multi WHERE path = 'b/u1/logs'") == [[3, "b/u1/logs", "2026-10-02 00:00:00"]]
    paths = {day: len({(depth, path) for depth, path, *_ in rows if depth >= 1}) for day, rows in store["rows"].items()}
    days = list(DAYS)
    assert {row["date"]: row["counts"]["net"] for row in log if not row["base"]} == {
        day: paths[day] - paths[prev] for prev, day in zip(days, days[1:])
    }


def test_resumed_build_equals_one_build(store, binaries):
    mc.drop(store["ch"], "cat_resume")
    first = build(store, binaries, "cat_resume", postings="all", through="2026-09-30")
    second = build(store, binaries, "cat_resume", postings="all")
    assert [row["date"] for row in first] == ["2026-09-29", "2026-09-30"]
    assert [row["date"] for row in second] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    for day, rows in store["rows"].items():
        assert mc.snapshot(store["ch"], "cat_resume", day) == expected(rows), day


def test_a_mid_history_base_equals_brute_force(store, binaries, monkeypatch):
    """A source-format switch (no `changes`) recomputes every tracked literal from the scan's live rows, over the
    versions earlier scans wrote."""
    real = mc.scans
    def scans(ch):
        out = real(ch)
        switch = next(s for s in out if s.date == "2026-10-02")
        return [mc.Scan(s.date, s.dt, s.since, s.base or s is switch, switch.dt if s.date >= switch.date else s.start) for s in out]

    monkeypatch.setattr(mc, "scans", scans)
    mc.drop(store["ch"], "cat_rebase")
    log = build(store, binaries, "cat_rebase", postings="all")
    assert [row["date"] for row in log if row["base"]] == ["2026-09-29", "2026-10-02"]
    for day, rows in store["rows"].items():
        assert mc.snapshot(store["ch"], "cat_rebase", day) == expected(rows), day


def test_fresh_build_equals_appended(store, binaries):
    mc.drop(store["ch"], "cat_fresh")
    build(store, binaries, "cat_fresh", postings="all")
    for day in DAYS:
        got, _ = mc.fresh(store["ch"], day, census_binary=binaries["census"], delta_binary=binaries["delta"],
                          threshold=THRESHOLD, short=SHORT, threads=2, parallel=3)
        assert got == mc.snapshot(store["ch"], "cat_fresh", day), day


def test_view_reads_one_scan(store, binaries):
    mc.drop(store["ch"], "cat_view")
    build(store, binaries, "cat_view", postings="all")
    snap = mc.snapshot(store["ch"], "cat_view", "2026-10-02")
    assert mc.view(store["ch"], "cat_view", "2026-10-02", "bin") == snap["bin"] == (4, {"b": (430, 3), "c": (5 << 30, 1)})
    assert "ckpt" not in snap  # 2 paths, below the fixture's threshold
    assert mc.view(store["ch"], "cat_view", "2026-10-02", "ckpt") is None
    assert mc.view(store["ch"], "cat_view", "2026-10-02", "zzz") is None


# — weighted by on-demand cost (`weight="rows"`) ——————————————————————————

ROWS_THRESHOLD, NAME_ROWS = 6, 1


def name_rows(ch: Ch, day: str) -> dict[str, int]:
    """Each name seen by `day`'s scan and its postings rows through it (versions opened, closures recorded)."""
    D = f"toDateTime('{day} 00:00:00', 'UTC')"
    return {n: int(c) for n, c in ch.json(f"""SELECT name, count() FROM (SELECT name FROM nodes WHERE vf <= {D}
        UNION ALL SELECT name FROM closures WHERE vt <= {D}) GROUP BY name""")}


def expected_rows(ch: Ch, day: str, rows: list[tuple]) -> dict[str, tuple[int, dict[str, tuple[int, int]]]]:
    """The scan's cost-weighted catalog by brute force: every substring of every name seen by the scan, weighted by
    its names' postings rows plus `NAME_ROWS` each; registered at `ROWS_THRESHOLD`, or at most `SHORT` characters."""
    seen = name_rows(ch, day)
    candidates = {n[i:j] for n in seen for i in range(len(n)) for j in range(i + 1, len(n) + 1)}
    out = {}
    for t in candidates:
        weight = sum(c + NAME_ROWS for n, c in seen.items() if t in n)
        if weight >= ROWS_THRESHOLD or len(t) <= SHORT:
            body = brute(rows, t)
            out[t] = (weight, {row["path"]: (row["b"], row["o"]) for row in body["buckets"] if (row["b"], row["o"]) != (0, 0)})
    return out


def build_rows(store, binaries, stem: str, through: str | None = None) -> list[dict]:
    return mc.build(store["ch"], stem, census_binary=binaries["census"], delta_binary=binaries["delta"], postings="all", through=through,
                    threshold=ROWS_THRESHOLD, short=SHORT, weight="rows", name_rows=NAME_ROWS, threads=2, parallel=3)


def test_rows_weighted_every_scan_equals_brute_force(store, binaries):
    mc.drop(store["ch"], "cat_rows")
    log = build_rows(store, binaries, "cat_rows")
    assert [(row["date"], row["weight"], row["counts"]) for row in log] == [(day, "rows", None) for day in DAYS]
    for day, rows in store["rows"].items():
        assert mc.snapshot(store["ch"], "cat_rows", day) == expected_rows(store["ch"], day, rows), day
    assert {k: v for k, v in mc.binding(store["ch"], "cat_rows").items() if k != "members"} == {
        "schema": "mega-catalog-binding-v1", "target": store["ch"].db, "stem": "cat_rows", "through": "2026-10-03",
        "threshold": ROWS_THRESHOLD, "short": SHORT, "weight": "rows", "name_rows": NAME_ROWS}


def test_rows_weighted_non_members_read_less_than_the_threshold(store, binaries):
    """The guarantee itself, against what the on-demand reader reads: for every literal not registered on a scan,
    the postings rows of its vocabulary there (`span_live` names; versions opened by the scan, closures by it) plus
    `NAME_ROWS` per name stay below the threshold."""
    mc.drop(store["ch"], "cat_bound")
    build_rows(store, binaries, "cat_bound")
    ch = store["ch"]
    for day in DAYS:
        D = f"toDateTime('{day} 00:00:00', 'UTC')"
        live = {l for (l,) in ch.json(f"SELECT l FROM name_spans GROUP BY l HAVING {mega_names.span_live(D)}")}
        read = {n: int(c) for n, c in ch.json(f"""SELECT name, count() FROM (SELECT name FROM all_nodes WHERE vf <= {D}
            UNION ALL SELECT name FROM all_closures WHERE vt <= {D}) GROUP BY name""")}
        members = set(mc.snapshot(ch, "cat_bound", day))
        candidates = {n[i:j] for n in live for i in range(len(n)) for j in range(i + 1, len(n) + 1)}
        over = {t: cost for t in candidates - members
                if (cost := sum(read.get(n, 0) + NAME_ROWS for n in live if t in n)) >= ROWS_THRESHOLD}
        assert over == {}, day


def test_rows_weighted_fresh_build_equals_appended(store, binaries):
    mc.drop(store["ch"], "cat_rows_fresh")
    build_rows(store, binaries, "cat_rows_fresh")
    for day in DAYS:
        got, _ = mc.fresh(store["ch"], day, census_binary=binaries["census"], delta_binary=binaries["delta"], threshold=ROWS_THRESHOLD,
                          short=SHORT, weight="rows", name_rows=NAME_ROWS, threads=2, parallel=3)
        assert got == mc.snapshot(store["ch"], "cat_rows_fresh", day), day


def test_rows_weighted_resumed_build_equals_one_build(store, binaries):
    mc.drop(store["ch"], "cat_rows_resume")
    build_rows(store, binaries, "cat_rows_resume", through="2026-10-01")
    build_rows(store, binaries, "cat_rows_resume")
    for day, rows in store["rows"].items():
        assert mc.snapshot(store["ch"], "cat_rows_resume", day) == expected_rows(store["ch"], day, rows), day
