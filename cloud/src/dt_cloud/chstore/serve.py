"""The store's answers in the Worker's response shapes (specs/ch-store.md §4):
`/api/subtree` and `/api/diff`, plain and filtered (`q=`), at any ingested
scan, and `/api/series` — what `dt-cloud serve-query -e ch` serves.

Every answer is a few statements on one ClickHouse session (temporary tables
for a filter's candidates, roots and exclusions). A scan is "as of" its
datetime `D`: the versions with `vf <= D < vt`.

- **Plain view** (`view.ts` `readView`'s plain branch): P's own slices, the
  threshold `P.b · minArea / (w·h)` attenuated per level, then one read per
  depth of the children of the paths kept one level up (each a primary-key
  range on `(depth, path)`), grouped per path with `sum(size) ≥ thr(depth)`.
  Exact: bytes are monotone up the tree, so a path over the threshold has its
  parent over the shallower one.
- **Filter view** (the box's `filter_view`, `dt_cloud.box.view`): candidates
  by name (`names` → the `by_name` projection), roots = the outermost of
  `pos ∧ ¬neg`, exclusions = the outermost of `neg` charged to the root
  holding them — both one window pass in `k` order (the path with `/` as
  `\\0`, so a subtree is contiguous) — then the per-root attenuated subtree
  drawn level by level as above.
- **Diff** (`buildDiff`): both sides' views at one threshold, the same walk,
  and exact point lookups for names one side lacks.
- **Series**: every version of P's slices, summed per scan.

Deliberate differences from the Worker, beyond the box's (`box.view`):

- Per-path aggregates always sum every owner slice; the Worker's `bysize`
  read drops a multi-slice path's slices under the threshold.
- Lookups are uncapped (`lookups_capped` false).
- On a v1 (dirs-only) scan, `(other)`'s `f` counts every folded child; the
  Worker counts the ones its coarse tier holds.
- No owner / class scope or user lens (501 / 409: the Worker answers)."""

from __future__ import annotations

import codecs
import json
import re
import time
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from ..bench.ch import name_sql, neg_sql, pos_sql
from ..bench.mem import Unsupported
from ..bench.query import Ast, compile_query
from ..bench.terms import regex_name_filter, seg_term
from ..box.view import HARD_CAP, REGION_READS, Agg, NotFound, build_tree, js_round, kids_index, num, owner_json, parent_of
from .client import Ch, lit
from .schema import dt_lit

ENGINE = "ch"
MAX_STEMS = 5000
SLICE = "toString(usr), toString(kind), size, n_files, n_children, mtime_mean, mtime_w, last_read, c2, c3, c4"
# A path's aggregate over its slices, as columns (`ub`: per-user bytes, the unattributed '' included).
AGG = ("sum(size) AS b, sum(n_files) AS o, sum(mtime_mean * mtime_w) AS wts, sum(mtime_w) AS wb, max(last_read) AS a, "
       "sum(c2) AS c2, sum(c3) AS c3, sum(c4) AS c4, sumMap(map(toString(usr), size)) AS ub, any(toString(kind)) AS kind, max(n_children) AS nc")
AGG_COLS = "b, o, wts, wb, a, c2, c3, c4, ub, kind, nc"
SUM_AGG = ("sum(b) AS b, sum(o) AS o, sum(wts) AS wts, sum(wb) AS wb, max(a) AS a, sum(c2) AS c2, sum(c3) AS c3, sum(c4) AS c4, "
           "sumMap(ub) AS ub")
SUM_COLS = "b, o, wts, wb, a, c2, c3, c4, ub"


def depth_of(path: str) -> int:
    return 0 if path == "" else path.count("/") + 1


def under(p: str) -> str:
    """Strictly under `p` ('0' sorts just past '/')."""
    return "1" if p == "" else f"(path >= {lit(p + '/')} AND path < {lit(p + '0')})"


def prefix_sql(i: str) -> str:
    return f"arrayStringConcat(arraySlice(splitByChar('/', path), 1, {i}), '/')"


PARENT = "if(position(path, '/') = 0, '', substring(path, 1, length(path) - position(reverse(path), '/')))"
# A path's bytes are split over at most this many owner slices (gcs 2026-10-03: 25), so a path over a
# threshold has a slice over `thr / MAX_SLICES`: the `size` minmax index prunes a level's read to the
# granules holding one (the Worker's `bysize` read, in effect) before the path's slices are summed.
MAX_SLICES = 64


def over(depth_cond: str, asof: str, cond: str, thr: float) -> str:
    """The paths at one depth (`depth_cond`), inside `cond`, that can clear `thr`: a set for `path IN`."""
    return f"(SELECT DISTINCT path FROM nodes WHERE {depth_cond} AND {asof} AND {cond} AND size >= {thr / MAX_SLICES!r})"


# --- aggregates ---------------------------------------------------------------------


def merge_slice(a: Agg, s: list) -> None:
    """`view.ts` `merge` of one owner slice (`SLICE`'s columns)."""
    usr, kind, size, nf, nc, mean, w, lr, c2, c3, c4 = s
    a.b += size
    a.o += nf
    if w > 0:
        a.wts += mean * w
        a.wb += w
    if lr >= 0:
        a.a = lr if a.a is None else max(a.a, lr)
    for k, v in (("2", c2), ("3", c3), ("4", c4)):
        if v:
            a.cb[k] = a.cb.get(k, 0) + v
    if usr:
        a.ub[usr] = a.ub.get(usr, 0) + size
    a.kind = kind
    if nc >= 0:
        a.nc = nc


def slice_order(slices: list) -> list:
    """The `path` sort's order within a path: owners ascending, unattributed last."""
    return sorted(slices, key=lambda s: (s[0] == "", s[0]))


def agg_of(slices: list) -> Agg:
    a = Agg()
    for s in slice_order(slices):
        merge_slice(a, s)
    return a


def agg_row(r: list, synth: bool = False) -> Agg:
    """An `AGG` row (`b, o, wts, wb, a, c2, c3, c4, ub[, kind, nc]`) as an Agg;
    `synth`: a synthesized aggregate (no kind / child count)."""
    b, o, wts, wb, a, c2, c3, c4, ub = r[:9]
    out = Agg(b=b, o=o, wts=float(wts), wb=float(wb), a=None if a < 0 else a,
              cb={k: v for k, v in (("2", c2), ("3", c3), ("4", c4)) if v > 0}, ub={u: v for u, v in sorted(ub.items()) if u})
    if not synth and len(r) > 9:
        out.kind = r[9]
        out.nc = None if r[10] < 0 else r[10]
    return out


def minus(a: Agg, cut: Agg | None, lost: int = 0) -> Agg:
    """`view.ts` `minus`: `a` less an exclusion cut, `lost` direct children gone."""
    if cut is None and not lost:
        return a
    out = a.subtract([cut]) if cut is not None else Agg(a.b, a.o, a.wts, a.wb, a.a, dict(a.cb), dict(a.ub))
    out.kind = a.kind
    out.nc = None if a.nc is None else max(0, a.nc - lost)
    return out


# --- the store ----------------------------------------------------------------------


@dataclass(frozen=True)
class Scan:
    id: str
    dt: str
    version: int

    @property
    def lit(self) -> str:
        return dt_lit(self.dt)

    @property
    def asof(self) -> str:
        return f"vf <= {self.lit} AND vt > {self.lit}"


class Store:
    """A store database behind one ClickHouse server."""

    def __init__(self, url: str, *, db: str = "default", threads: int = 8, root_label: str = "marin GCS", syntax: str = "simple", timeout: float = 300):
        self.url, self.db, self.threads = url, db, threads
        self.root_label, self.syntax, self.timeout = root_label, syntax, timeout
        self._scans: dict[str, Scan] | None = None
        self._at = 0.0

    def session(self) -> Ch:
        return Ch(self.url, db=self.db, timeout=self.timeout, max_threads=self.threads, output_format_json_escape_forward_slashes=0,
                  output_format_json_quote_64bit_integers=0, prefer_column_name_to_alias=1, max_query_size=64 << 20, max_ast_elements=5_000_000, max_expanded_ast_elements=5_000_000)

    def scans(self, refresh: bool = False) -> dict[str, Scan]:
        if refresh or self._scans is None or time.monotonic() - self._at > 60:
            rows = Ch(self.url, db=self.db, session=False, timeout=30).json("SELECT id, toString(scan), version FROM scans FINAL ORDER BY scan")
            self._scans = {i: Scan(i, d, v) for i, d, v in rows}
            self._at = time.monotonic()
        return self._scans

    def scan(self, scan_id: str) -> Scan | None:
        return self.scans().get(scan_id) or self.scans(refresh=True).get(scan_id)


# --- views --------------------------------------------------------------------------


@dataclass
class View:
    """What `build_tree`, the diff's walk and a body need from one side."""

    path: str
    dP: int
    root_agg: Agg
    kept: dict
    depth: dict
    folded_of: dict
    threshold: float
    deepest: int
    atten: float
    root_paths: set = field(default_factory=set)
    truncated: bool = False
    folded: int = 0
    prep: "Prep | None" = None

    def thr_at(self, d: int) -> float:
        return self.threshold * self.atten ** max(0, d - self.deepest - 1)


def root_read(ch: Ch, s: Scan, path: str) -> Agg | None:
    """P's aggregate over its own slices (the store root: every depth-1 row,
    `nc` = their count); None = not in the scan."""
    if path == "":
        rows = ch.json(f"SELECT path, groupArray(tuple({SLICE})) FROM nodes WHERE depth = 1 AND {s.asof} GROUP BY path ORDER BY path")
        if not rows:
            return None
        a = Agg()
        for _, sl in rows:
            for x in slice_order(sl):
                merge_slice(a, x)
        a.nc = len(rows)
        return a
    rows = ch.json(f"SELECT groupArray(tuple({SLICE})) FROM nodes WHERE depth = {depth_of(path)} AND path = {lit(path)} AND {s.asof}")
    return agg_of(rows[0][0]) if rows and rows[0][0] else None


# Up to this many parents a level's read is an OR of their `path` ranges (primary-key pruning); past it,
# a parent-set test per row (evaluating thousands of ranges per row cost seconds), the granules then
# pruned by depth and the `size` index alone.
RANGE_PARENTS = 32


def _level_cond(parents: list[str], d: int, path: str) -> str:
    if path == "" and d == 1:
        return "1"
    if len(parents) > RANGE_PARENTS:
        return f"{PARENT} IN ({', '.join(lit(p) for p in parents)})"
    return "(" + " OR ".join(under(p) for p in parents) + ")"


def plain_view(ch: Ch, s: Scan, path: str, *, w: int, h: int, min_area: float, atten: float, max_depth: int | None = None,
               threshold: float | None = None, root: Agg | None = None) -> View | None:
    """`readView`'s plain branch; None = nothing under P."""
    dP = depth_of(path)
    root = root or root_read(ch, s, path)
    if root is None:
        raise NotFound(path)
    if root.b <= 0:
        return None
    T = float(threshold) if threshold is not None else root.b * min_area / (w * h)
    v = View(path, dP, root, {}, {}, {}, T, dP, float(atten))
    frontier = [path]
    d = dP + 1
    while frontier and (max_depth is None or d <= dP + max_depth) and len(v.kept) <= 4 * HARD_CAP:
        thr = v.thr_at(d)
        cond = _level_cond(frontier, d, path)
        rows = ch.json(f"""SELECT path, groupArray(tuple({SLICE})) FROM nodes WHERE depth = {d} AND {s.asof}
            AND path IN {over(f"depth = {d}", s.asof, cond, thr)} GROUP BY path HAVING sum(size) >= {thr!r} ORDER BY path""")
        nxt = []
        for p, sl in rows:
            a = agg_of(sl)
            v.kept[p], v.depth[p] = a, d
            if a.kind != "file" and a.nc != 0:
                nxt.append(p)
        if s.version < 2 and frontier:
            # A v1 row has no child count: `(other)`'s `f` is the folded children counted.
            for par, n in ch.json(f"""SELECT {PARENT} AS par, count() FROM (SELECT path FROM nodes WHERE depth = {d} AND {s.asof} AND {cond}
                    GROUP BY path HAVING sum(size) > 0 AND sum(size) < {thr!r}) GROUP BY par"""):
                v.folded_of[par] = v.folded_of.get(par, 0) + n
        frontier = nxt
        d += 1
    if len(v.kept) > HARD_CAP:
        top = sorted(v.kept, key=lambda p: -v.kept[p].b)[:HARD_CAP]
        v.kept = {p: v.kept[p] for p in sorted(top)}
        v.truncated = True
    return v


# --- the filter ---------------------------------------------------------------------


@dataclass
class Prep:
    """One side's filter evaluation, held in session tables suffixed `sfx`:
    `cr` candidates, `rn` roots with their net aggregates, `ex` exclusions
    (with their root `rp` / `rd`), `cut` / `lost` the exclusions' effect on
    the nodes between them and their root."""

    sfx: str
    scan: Scan
    path: str
    dP: int
    hit: bool
    n_roots: int
    n_ex: int
    total: Agg
    root: Agg | None
    stats: dict


def _window_outer(src: str, cols: str) -> str:
    """The outermost rows of `src` (which has `path`) — a row is nested iff a
    preceding row's subtree, in `k` order, holds it."""
    return f"""SELECT {cols} FROM (
        SELECT *, max(e) OVER (ORDER BY k ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS mp
        FROM (SELECT *, concat(replaceAll(path, '/', '\\0'), '\\0') AS k, concat(k, '\\xff') AS e FROM ({src})))
    WHERE mp <= k"""


def filter_prepare(ch: Ch, s: Scan, path: str, ast: Ast, sfx: str = "a") -> Prep | None:
    """Candidates, roots and exclusions of `ast` under P in scan `s`; None =
    nothing matched (or nothing left net of the exclusions)."""
    t0 = time.monotonic()
    st: dict = {}
    dP = depth_of(path)
    hit = bool(compile_query(ast)(path))
    root = root_read(ch, s, path)
    if root is None:
        raise NotFound(path)
    regex = [m for a in ast.alts for m in a if m.kind == "regex"] + [m for m in ast.neg if m.kind == "regex"]
    strict = []
    if regex:
        if len(ast.alts) != 1 or len(ast.alts[0]) != 1 or ast.neg:
            raise Unsupported("a regex mixed with other terms")
        src = regex[0].source
        plan = regex_name_filter(src)
        if plan is None:
            raise Unsupported(f"regex {src!r}: no name filter (its tail can cross a `/` or isn't `$`-anchored)")
        name_cond = f"match(l, {lit('(?i)' + plan.name_re)})"
        flags = f"match(path, {lit('(?i)' + src)}) AS p, 0 AS n"
    else:
        matchers = list(dict.fromkeys([m for a in ast.alts for m in a] + list(ast.neg)))
        terms = {m: seg_term(m) for m in matchers}
        if any(t.trivial for t in terms.values()):
            raise Unsupported("a term that constrains no segment")
        name_cond = "(" + " OR ".join(name_sql(t.name) for t in terms.values()) + ")"
        flags = f"{pos_sql(ast)} AS p, {neg_sql(ast)} AS n"
        strict = [t for t in terms.values() if t.strict]
    in_view = "depth >= 1" if path == "" else f"startsWith(path, {lit(path + '/')})"
    base = "path, depth, usr, kind, size, n_files, n_children, mtime_mean, mtime_w, last_read, c2, c3, c4, lowerUTF8(path) AS lp"
    agg_cands = lambda src: f"SELECT path, any(depth) AS depth, any(p) AS p, any(n) AS n, {AGG} FROM (SELECT *, {flags} FROM ({src})) GROUP BY path"  # noqa: E731
    ch.tmp(f"cr0_{sfx}", agg_cands(f"SELECT {base} FROM nodes WHERE name IN (SELECT l FROM names WHERE {name_cond}) AND {s.asof} AND {in_view}"))
    cr = f"cr0_{sfx}"
    st["cands_s"] = round(time.monotonic() - t0, 3)
    if strict:
        cond = " OR ".join(f"match(lowerUTF8(path), {lit(t.suffix_re)})" for t in strict)
        stems = [(p, d) for p, d in ch.json(f"SELECT path, depth FROM cr0_{sfx} WHERE {cond}")]
        if path and any(re.search(t.suffix_re, path.lower()) for t in strict):
            stems.append((path, dP))
        st["stems"] = len(stems)
        if len(stems) > MAX_STEMS:
            raise Unsupported(f"{len(stems)} stems")
        if stems:
            rng = " OR ".join(f"(depth = {d + 1} AND {under(p)})" for p, d in stems)
            ch.tmp(f"cr1_{sfx}", agg_cands(f"SELECT {base} FROM nodes WHERE {s.asof} AND ({rng})"))
            ch.tmp(f"cr_{sfx}", f"SELECT path, any(depth) AS depth, any(p) AS p, any(n) AS n, any(b) AS b, any(o) AS o, any(wts) AS wts, "
                   f"any(wb) AS wb, any(a) AS a, any(c2) AS c2, any(c3) AS c3, any(c4) AS c4, any(ub) AS ub, any(kind) AS kind, any(nc) AS nc "
                   f"FROM (SELECT * FROM cr0_{sfx} UNION ALL SELECT * FROM cr1_{sfx}) GROUP BY path")
            cr = f"cr_{sfx}"
    st["cands"] = int(ch.scalar(f"SELECT count() FROM {cr}") or 0)
    cols = f"path, depth, k, {AGG_COLS}"
    # Roots: the view itself on a hit, else the outermost of `pos ∧ ¬neg`.
    if hit:
        ch.tmp(f"rt_{sfx}", f"SELECT {lit(path)} AS path, toUInt8({dP}) AS depth, concat(replaceAll({lit(path)}, '/', '\\0'), '\\0') AS k")
    else:
        ch.tmp(f"rt_{sfx}", _window_outer(f"SELECT * FROM {cr} WHERE p AND NOT n", cols))
    # Exclusions: the outermost of `neg`, each charged to the root holding it.
    xcols = ", ".join("x." + c + " AS " + c for c in AGG_COLS.split(", "))
    if ast.neg:
        ch.tmp(f"exo_{sfx}", _window_outer(f"SELECT * FROM {cr} WHERE n", cols))
    if ast.neg and hit:
        # Every exclusion is strictly under the view, the one root.
        ch.tmp(f"ex_{sfx}", f"SELECT x.path AS path, x.depth AS depth, {lit(path)} AS rp, toUInt8({dP}) AS rd, {xcols} FROM exo_{sfx} AS x")
    elif ast.neg:
        ch.tmp(f"ex_{sfx}", f"""SELECT x.path AS path, x.depth AS depth, r.rr.2 AS rp, r.rr.3 AS rd, {xcols}
            FROM exo_{sfx} AS x INNER JOIN (
                SELECT path, rr FROM (
                    SELECT path, k, r, max(if(r = 1, tuple(k, path, depth), tuple('', '', toUInt8(0)))) OVER (ORDER BY k, r DESC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS rr
                    FROM (SELECT path, depth, k, 1 AS r FROM rt_{sfx} UNION ALL SELECT path, depth, k, 0 AS r FROM exo_{sfx}))
                WHERE r = 0 AND rr.1 != '' AND startsWith(k, rr.1) AND k != rr.1) AS r ON r.path = x.path""")
    else:
        ch.tmp(f"ex_{sfx}", f"SELECT '' AS path, toUInt8(0) AS depth, '' AS rp, toUInt8(0) AS rd, {AGG_COLS} FROM {cr} WHERE 0")
    n_ex = int(ch.scalar(f"SELECT count() FROM ex_{sfx}") or 0)
    # Cuts: each exclusion's aggregate on every node from its parent up to its root; lost children per parent.
    ch.tmp(f"cut_{sfx}", f"""SELECT anc AS path, n, {SUM_COLS} FROM (SELECT anc, count() AS n, {SUM_AGG} FROM ex_{sfx}
        ARRAY JOIN arrayMap(i -> {prefix_sql('i')}, range(rd, depth)) AS anc GROUP BY anc)""")
    ch.tmp(f"lost_{sfx}", f"SELECT par AS path, lost FROM (SELECT {PARENT} AS par, count() AS lost FROM ex_{sfx} GROUP BY par)")
    # Each root's net aggregate (`minus`): less its cut, less its lost children.
    if hit:
        cut = _cut_of(ch, sfx, path)
        lost = int(ch.scalar(f"SELECT lost FROM lost_{sfx} WHERE path = {lit(path)}") or 0)
        net = minus(root, cut, lost)
        ch.tmp(f"rn_{sfx}", f"SELECT {lit(path)} AS path, toUInt8({dP}) AS depth, toInt64({js_round(net.b)}) AS b, toInt64({js_round(net.o)}) AS o")
        total, n_roots = net, 1
    else:
        ch.tmp(f"rn_{sfx}", f"""SELECT r.path AS path, r.depth AS depth, r.b - c.b AS b, greatest(0, r.o - c.o) AS o, r.wts - c.wts AS wts,
                greatest(0, r.wb - c.wb) AS wb, r.a AS a, r.c2 - c.c2 AS c2, r.c3 - c.c3 AS c3, r.c4 - c.c4 AS c4,
                if(c.n > 0, mapFilter((u, x) -> x > 0, mapSubtract(r.ub, c.ub)), r.ub) AS ub, r.kind AS kind,
                if(r.nc < 0, r.nc, greatest(0, r.nc - l.lost)) AS nc
            FROM rt_{sfx} AS r LEFT JOIN cut_{sfx} AS c ON c.path = r.path LEFT JOIN lost_{sfx} AS l ON l.path = r.path""")
        row = ch.json(f"SELECT count(), {SUM_AGG} FROM rn_{sfx}")[0]
        n_roots = row[0]
        if not n_roots:
            return None
        total = agg_row(row[1:], synth=True)
    st["roots"], st["excluded"] = n_roots, n_ex
    st["s"] = round(time.monotonic() - t0, 3)
    if total.b <= 0:
        return None
    return Prep(sfx, s, path, dP, hit, n_roots, n_ex, total, root, st)


def _cut_of(ch: Ch, sfx: str, p: str) -> Agg | None:
    r = ch.json(f"SELECT n, b, o, wts, wb, a, c2, c3, c4, ub FROM cut_{sfx} WHERE path = {lit(p)}")
    return agg_row(r[0][1:], synth=True) if r else None


def filter_view(ch: Ch, pr: Prep, *, w: int, h: int, min_area: float, atten: float, max_depth: int | None = None,
                threshold: float | None = None) -> View:
    """The box's `filter_view` drawing over a prepared side: synthesized
    ancestors, drawn roots, the per-root attenuated subtree."""
    sfx, s, path, dP = pr.sfx, pr.scan, pr.path, pr.dP
    T = float(threshold) if threshold is not None else pr.total.b * min_area / (w * h)
    hit = pr.hit
    if hit:
        deepest = dP
    else:
        deepest = int(ch.scalar(f"SELECT max(depth) FROM (SELECT depth FROM rn_{sfx} ORDER BY b DESC, path LIMIT {REGION_READS})") or dP)
    fold = (not hit) and pr.n_roots > HARD_CAP
    v = View(path, dP, Agg(), {}, {}, {}, T, deepest, float(atten), prep=pr)
    kept: dict[str, Agg] = {}
    depth: dict[str, int] = {}
    if hit:
        v.root_agg = pr.total
        v.root_paths.add(path)
        F = [(path, dP)]
    else:
        root_agg = Agg(pr.total.b, pr.total.o, pr.total.wts, pr.total.wb, pr.total.a, dict(pr.total.cb), dict(pr.total.ub))
        v.root_agg = root_agg
        drawn = ch.json(f"SELECT path, depth, {AGG_COLS} FROM rn_{sfx}" + (f" WHERE b >= {T!r}" if fold else ""))
        F = []
        for p, d, *r in drawn:
            kept[p], depth[p] = agg_row(r), d
            v.root_paths.add(p)
            F.append((p, d))
        # Synthesized ancestors between P and the roots: Σ net roots under each.
        anc = f"""SELECT anc AS path, toUInt8(length(splitByChar('/', anc))) AS depth, {SUM_COLS} FROM (SELECT anc, {SUM_AGG} FROM rn_{sfx}
            ARRAY JOIN arrayMap(i -> {prefix_sql('i')}, range({dP + 1}, depth)) AS anc GROUP BY anc)"""
        ch.tmp(f"anc_{sfx}", anc)
        for p, d, *r in ch.json(f"SELECT path, depth, b, o, wts, wb, a, c2, c3, c4, ub FROM anc_{sfx}" + (f" WHERE b >= {T!r}" if fold else "")):
            kept[p], depth[p] = agg_row(r, synth=True), d
        if fold:
            v.folded = pr.n_roots - len(drawn)
            for par, n in ch.json(f"""SELECT par, count() FROM (SELECT {PARENT} AS par FROM rn_{sfx} WHERE b < {T!r}
                    UNION ALL SELECT {PARENT} AS par FROM anc_{sfx} WHERE b < {T!r}) GROUP BY par"""):
                v.folded_of[par] = n
    # Phase 2: under each drawn root (the view, on a hit), thresholds rebased on its depth.
    if not (max_depth is not None and max_depth <= 0):
        rd_of = {p: d for p, d in F}  # frontier node → its root's depth
        while F:
            want = [(p, d) for p, d in F if max_depth is None or d + 1 <= rd_of[p] + max_depth]
            if not want:
                break
            thr = {p: T * atten ** max(0, d + 1 - rd_of[p] - 1) for p, d in want}
            by_depth: dict[int, list[str]] = {}
            for p, d in want:
                by_depth.setdefault(d + 1, []).append(p)
            cond = " OR ".join(f"(depth = {d} AND {_level_cond(ps, d, '-')})" for d, ps in by_depth.items())
            rows = ch.json(f"""SELECT k.path, k.depth, {', '.join('k.' + c for c in AGG_COLS.split(', '))}, c.n, c.b, c.o, c.wts, c.wb, c.a, c.c2, c.c3, c.c4, c.ub,
                    l.lost, e.x
                FROM (SELECT path, any(depth) AS depth, {AGG} FROM nodes WHERE {s.asof} AND depth IN ({", ".join(str(x) for x in sorted({d + 1 for _, d in want}))})
                      AND path IN {over("1", s.asof, f"({cond})", min(thr.values()))} GROUP BY path HAVING b >= {min(thr.values())!r}) AS k
                LEFT JOIN cut_{sfx} AS c ON c.path = k.path LEFT JOIN lost_{sfx} AS l ON l.path = k.path
                LEFT JOIN (SELECT path, 1 AS x FROM ex_{sfx}) AS e ON e.path = k.path ORDER BY k.path""")
            nxt = []
            for p, d, *r in rows:
                agg_part, cut_part, lost, x = r[:11], r[11:21], r[21], r[22]
                if x:
                    continue
                par = parent_of(p)
                if par not in thr or agg_part[0] < thr[par]:
                    continue
                a = minus(agg_row(agg_part), agg_row(cut_part[1:], synth=True) if cut_part[0] else None, lost)
                if a.b <= 0 or a.b < thr[par]:
                    continue
                kept[p], depth[p] = a, d
                rd_of[p] = rd_of[par]
                nxt.append((p, d))
            F = nxt
    v.kept = {p: kept[p] for p in sorted(kept)}
    v.depth = depth
    return v


# --- bodies -------------------------------------------------------------------------


def _jdump(x: object) -> str:
    return json.dumps(x, ensure_ascii=False, separators=(",", ":"))


def lines_as_list(chunks: Iterable[bytes]) -> Iterator[str]:
    """Newline-terminated JSON values (ClickHouse `TSVRaw` of `toJSONString`s)
    as one JSON array."""
    dec = codecs.getincrementaldecoder("utf-8")()
    yield "["
    pend = b""
    for c in chunks:
        c = pend + c
        if c.endswith(b"\n"):
            c, pend = c[:-1], b"\n"
        else:
            pend = b""
        if c:
            yield dec.decode(c.replace(b"\n", b","))
    tail = dec.decode(b"", final=True)
    yield tail + "]"


def _list(ch: Ch, sql: str) -> Iterator[str]:
    return lines_as_list(ch.stream(sql))


def matched_sql(sfx: str) -> str:
    return f"SELECT concat('{{\"path\":', toJSONString(path), ',\"b\":', toString(b), ',\"o\":', toString(o), '}}') FROM rn_{sfx} ORDER BY b DESC, path"


def tree_name(path: str, root_label: str) -> str:
    return root_label if path == "" else path.rsplit("/", 1)[-1]


def subtree_body(ch: Ch, v: View | None, *, date: str, path: str, w: int, h: int, min_area: float, atten: float, q: str | None,
                 root_label: str) -> Iterator[str]:
    """`/api/subtree`'s body (key order as `api/subtree.ts`), streamed."""
    head = {"date": date, "path": path, "w": w, "h": h, "minArea": num(min_area), "atten": num(atten)}
    if v is None:
        body = {**head, "tier": "none", "index": "none", "threshold": 0, "nodes": 0, "truncated": False,
                **({"q": q, "matches": [], "matched": []} if q is not None else {}), "tree": {"n": tree_name(path, root_label), "k": "dir", "b": 0, "o": 0}}
        yield _jdump(body)
        return
    tree = build_tree(v, root_label)
    pre = {**head, "tier": ENGINE, "index": ENGINE, "threshold": js_round(v.threshold), "nodes": len(v.kept), "truncated": v.truncated,
           **({"folded": v.folded} if v.folded else {})}
    if q is None:
        yield _jdump({**pre, "tree": tree})
        return
    pr = v.prep
    yield _jdump({**pre, "q": q})[:-1] + ',"matches":'
    yield from _list(ch, f"SELECT toJSONString(path) FROM rn_{pr.sfx} ORDER BY path")
    yield ',"matched":'
    yield from _list(ch, matched_sql(pr.sfx))
    if pr.n_ex:
        yield ',"excluded":'
        yield from _list(ch, f"SELECT toJSONString(path) FROM ex_{pr.sfx} ORDER BY path")
    yield ',"tree":' + _jdump(tree) + "}"


# --- the diff -----------------------------------------------------------------------


def _lookups(ch: Ch, s: Scan, v: View, asks: list[tuple[str, int]]) -> tuple[dict, int]:
    """One side's point lookups for names its view didn't keep: each path's
    aggregate (net of the side's exclusions under it), None when outside the
    side's query, absent or empty; and how many asks were inside the query."""
    out: dict = {cp: None for cp, _ in asks}
    pr = v.prep
    todo = asks
    if pr is not None and not pr.hit:
        pre = sorted({"/".join(cp.split("/")[:i]) for cp, _ in asks for i in range(1, cp.count("/") + 2)})
        plist = "[" + ",".join(lit(p) for p in pre) + "]"
        roots = {r[0] for r in ch.json(f"SELECT path FROM rn_{pr.sfx} WHERE path IN {plist}")}
        excl = {r[0] for r in ch.json(f"SELECT path FROM ex_{pr.sfx} WHERE path IN {plist}")} if pr.n_ex else set()

        def inq(p: str) -> bool:
            ps = ["/".join(p.split("/")[:i]) for i in range(1, p.count("/") + 2)]
            return any(x in roots for x in ps) and not any(x in excl for x in ps)

        todo = [(cp, d) for cp, d in asks if inq(cp)]
    elif pr is not None and pr.n_ex:
        excl_rows = set()
        pre = sorted({"/".join(cp.split("/")[:i]) for cp, _ in asks for i in range(1, cp.count("/") + 2)})
        plist = "[" + ",".join(lit(p) for p in pre) + "]"
        excl_rows = {r[0] for r in ch.json(f"SELECT path FROM ex_{pr.sfx} WHERE path IN {plist}")}
        todo = [(cp, d) for cp, d in asks if not any("/".join(cp.split("/")[:i]) in excl_rows for i in range(1, cp.count("/") + 2))]
    if not todo:
        return out, 0
    keys = ",".join(f"({d}, {lit(cp)})" for cp, d in todo)
    found = {p: agg_of(sl) for p, sl in ch.json(f"SELECT path, groupArray(tuple({SLICE})) FROM nodes WHERE {s.asof} AND (depth, path) IN ({keys}) GROUP BY path")}
    cuts: dict = {}
    if pr is not None and pr.n_ex and found:
        alist = "[" + ",".join(lit(p) for p in found) + "]"
        for a, *r in ch.json(f"SELECT q, {SUM_AGG} FROM ex_{pr.sfx} ARRAY JOIN {alist} AS q WHERE startsWith(path, concat(q, '/')) GROUP BY q"):
            cuts[a] = agg_row(r, synth=True)
    for cp, a in found.items():
        c = cuts.get(cp)
        if c is not None and (c.b or c.o):
            kind = a.kind
            a = a.subtract([c])
            a.kind = kind
        out[cp] = a if a.b > 0 else None
    return out, len(todo)


def diff_body(ch: Ch, sa: Scan, sb: Scan, *, path: str, w: int, h: int, min_area: float, atten: float, top: int, ast: Ast | None,
              q: str | None, summary: bool = False, depth: int | None = None) -> Iterator[str]:
    """`/api/diff`'s body (`buildDiff`), plain or filtered, streamed."""
    dP = depth_of(path)
    kw = dict(w=w, h=h, min_area=min_area, atten=atten, max_depth=depth)
    head = {"prev": sa.id, "curr": sb.id, "path": path, **({"q": q} if q is not None else {})}
    if ast is None:
        ra, rb = root_read(ch, sa, path), root_read(ch, sb, path)
        if ra is None and rb is None:
            raise NotFound(path)
        T = max(ra.b if ra else 0, rb.b if rb else 0) * min_area / (w * h)
        va = plain_view(ch, sa, path, threshold=T, root=ra, **kw) if ra else None
        vb = plain_view(ch, sb, path, threshold=T, root=rb, **kw) if rb else None
    else:
        ra, rb = _exists(ch, sa, path), _exists(ch, sb, path)
        if not ra and not rb:
            raise NotFound(path)
        pa_ = filter_prepare(ch, sa, path, ast, "a") if ra else None
        pb_ = filter_prepare(ch, sb, path, ast, "b") if rb else None
        va = filter_view(ch, pa_, **kw) if pa_ else None
        vb = filter_view(ch, pb_, **kw) if pb_ else None
        if va and vb and va.threshold != vb.threshold:
            shared = max(va.threshold, vb.threshold)
            if va.threshold < shared:
                va = filter_view(ch, pa_, threshold=shared, **kw)
            else:
                vb = filter_view(ch, pb_, threshold=shared, **kw)
        T = (vb or va).threshold if (va or vb) else 0
    if not va and not vb:
        body = {**head, "rows": [], "total_a": 0, "total_b": 0, "objects_a": 0, "objects_b": 0, "threshold": 0, "tier": "none",
                **({"matched": []} if ast is not None else {}), "expansions": 0, "truncated": False, "lookups": 0, "lookups_capped": False}
        yield _jdump(body)
        return
    if va and vb and (sa.version >= 2) != (sb.version >= 2):
        # One side dirs-only: the other side's objects fold into `(other)`.
        for v in (va, vb):
            v.kept = {p: a for p, a in v.kept.items() if a.kind != "file"}
    totals = {"total_a": js_round(va.root_agg.b) if va else 0, "total_b": js_round(vb.root_agg.b) if vb else 0,
              "objects_a": js_round(va.root_agg.o) if va else 0, "objects_b": js_round(vb.root_agg.o) if vb else 0,
              "threshold": js_round(T), "tier": ENGINE}
    rows: list[dict] = []
    expansions = lookups = 0
    if not summary:
        rows, expansions, lookups = _walk(ch, sa, sb, va, vb, path, dP, depth)
    frontier = sorted((r for r in rows if not r.get("x") and r["s"] != "unchanged"), key=lambda r: -abs(r["b"] - r["a"]))
    skeleton = [r for r in rows if r.get("x")]
    pre = {**head, "rows": skeleton + frontier[:top], **totals}
    tail = {"expansions": expansions, "truncated": len(frontier) > top, "lookups": lookups, "lookups_capped": False}
    if ast is None:
        yield _jdump({**pre, **tail})
        return
    yield _jdump(pre)[:-1] + ',"matched":'
    yield from _list(ch, _matched_union_sql(va, vb))
    yield "," + _jdump(tail)[1:]


def _exists(ch: Ch, s: Scan, path: str) -> bool:
    if path == "":
        return bool(ch.scalar(f"SELECT count() FROM nodes WHERE depth = 1 AND {s.asof}") not in (None, "0"))
    return bool(ch.scalar(f"SELECT count() FROM nodes WHERE depth = {depth_of(path)} AND path = {lit(path)} AND {s.asof}") not in (None, "0"))


def _matched_union_sql(va: View | None, vb: View | None) -> str:
    """Both sides' `matched`, by path; the newer side's entry for a path in both."""
    obj = "concat('{\"path\":', toJSONString(path), ',\"b\":', toString(b), ',\"o\":', toString(o), '}')"
    if va and vb:
        a, b = va.prep.sfx, vb.prep.sfx
        src = f"SELECT path, b, o FROM rn_{b} UNION ALL SELECT path, b, o FROM rn_{a} WHERE path NOT IN (SELECT path FROM rn_{b})"
    else:
        src = f"SELECT path, b, o FROM rn_{(va or vb).prep.sfx}"
    return f"SELECT {obj} FROM ({src}) ORDER BY path"


def _walk(ch: Ch, sa: Scan, sb: Scan, va: View | None, vb: View | None, path: str, dP: int, depth: int | None) -> tuple[list, int, int]:
    """`buildDiff`'s level-by-level walk (the box's `diff_body`)."""
    kids_a = kids_index(va.kept, path) if va else {}
    kids_b = kids_index(vb.kept, path) if vb else {}

    def rel(p: str) -> str:
        return p if path == "" else p[len(path) + 1 :]

    def status(a: Agg | None, b: Agg | None) -> str:
        if not a:
            return "added"
        if not b:
            return "removed"
        return "changed" if js_round(a.b) != js_round(b.b) or js_round(a.o) != js_round(b.o) else "unchanged"

    rows: list[dict] = []

    def emit(p: str, d: int, a: Agg | None, b: Agg | None, x: bool, l: int | None = None) -> None:
        row = {"p": p, "d": d, "k": (a.kind if a else None) or (b.kind if b else None) or "dir", "s": status(a, b),
               "a": js_round(a.b) if a else 0, "b": js_round(b.b) if b else 0, "oa": js_round(a.o) if a else 0, "ob": js_round(b.o) if b else 0}
        if x:
            row["x"] = True
        if l:
            row["l"] = l
        rows.append(row)

    expansions = lookups = 0
    level = [{"p": path, "d": dP, "a": va.root_agg if va else None, "b": vb.root_agg if vb else None, "l": None}]
    while level:
        nxt = []
        plans = []
        for it in level:
            p, a, b = it["p"], it["a"], it["b"]
            ka, kb = kids_a.get(p, []), kids_b.get(p, [])
            same = bool(a and b and js_round(a.b) == js_round(b.b) and js_round(a.o) == js_round(b.o))
            expand = bool((a or b) and not same and (ka or kb)) and (depth is None or it["d"] - dP < depth)
            plans.append((it, expand, sorted(set(ka) | set(kb)) if expand else []))
        found: dict = {}
        for side, s, v in ((1, sa, va), (2, sb, vb)):
            if not v:
                continue
            asks = [(cp, it["d"] + 1) for it, _, names in plans for cp in names if cp not in v.kept]
            if asks:
                got, n = _lookups(ch, s, v, asks)
                lookups += n
                for cp, ag in got.items():
                    found[(side, cp)] = ag
        for it, expand, names in plans:
            p, d, a, b = it["p"], it["d"], it["a"], it["b"]
            if p != path:
                emit(rel(p), d - dP, a, b, expand, it["l"])
            if not expand:
                continue
            expansions += 1
            sa_, sb_ = [0, 0], [0, 0]
            for cp in names:
                ca = (va.kept.get(cp) if va else None) or found.get((1, cp))
                cb = (vb.kept.get(cp) if vb else None) or found.get((2, cp))
                lk = 1 if ca and cp not in va.kept else 2 if cb and cp not in vb.kept else None
                if ca:
                    sa_[0] += ca.b
                    sa_[1] += ca.o
                if cb:
                    sb_[0] += cb.b
                    sb_[1] += cb.o
                nxt.append({"p": cp, "d": d + 1, "a": ca, "b": cb, "l": lk})
            rest_a = Agg(b=max(0, (a.b if a else 0) - sa_[0]), o=max(0, (a.o if a else 0) - sa_[1]))
            rest_b = Agg(b=max(0, (b.b if b else 0) - sb_[0]), o=max(0, (b.o if b else 0) - sb_[1]))
            if rest_a.b > 0 or rest_b.b > 0:
                key = "(other)" if p == path else f"{rel(p)}/(other)"
                emit(key, d - dP + 1, rest_a if rest_a.b > 0 else None, rest_b if rest_b.b > 0 else None, False)
        level = nxt
    return rows, expansions, lookups


# --- the series ---------------------------------------------------------------------


def series_points(ch: Ch, scans: list[Scan], path: str, paths: list[str], split: bool) -> tuple[list[dict], dict | None]:
    """One point per scan where the path (or any of `paths`) exists: Σ over
    its slices valid then (`split`: per depth-1 root too)."""
    targets = paths or [path]
    if split or targets == [""]:
        q = "SELECT path, toString(vf), toString(vt), size, n_files FROM nodes WHERE depth = 1"
    else:
        keys = ",".join(f"({depth_of(p)}, {lit(p)})" for p in targets)
        q = f"SELECT path, toString(vf), toString(vt), size, n_files FROM nodes WHERE depth > 0 AND (depth, path) IN ({keys})"
    rows = ch.json(q)
    points: list[dict] = []
    by_date: dict[str, list] = {}
    for s in scans:
        live = [(p, b, o) for p, vf, vt, b, o in rows if vf <= s.dt < vt]
        if not live:
            continue
        points.append({"date": s.id, "b": sum(b for _, b, _ in live), "o": sum(o for _, _, o in live)})
        if split:
            per: dict[str, list] = {}
            for p, b, o in live:
                e = per.setdefault(p, [0, 0])
                e[0] += b
                e[1] += o
            by_date[s.id] = [{"path": p, "b": b, "o": o} for p, (b, o) in per.items()]
    return points, (by_date if split else None)


def root_points(by_date: dict[str, list]) -> list[dict]:
    """`series.ts` `rootPoints`."""
    dates = sorted(by_date)
    traces: dict[str, list] = {}
    for d in dates:
        for r in by_date[d]:
            traces.setdefault(r["path"], []).append({"date": d, "b": r["b"], "o": r["o"]})
    latest = {r["path"]: r["b"] for r in by_date.get(dates[-1], [])} if dates else {}
    out = [{"path": p, "points": pts} for p, pts in traces.items()]
    out.sort(key=lambda t: (t["points"][0]["date"], -latest.get(t["path"], 0), t["path"]))
    return out


def series_body(ch: Ch, scans: list[Scan], *, path: str, paths: list[str], split: bool) -> str:
    points, by_date = series_points(ch, scans, path, paths, split)
    body = {"path": path, **({"paths": paths} if paths else {}), "points": points, **({"roots": root_points(by_date)} if split else {})}
    return _jdump(body)


__all__ = ["Store", "Scan", "View", "plain_view", "filter_prepare", "filter_view", "subtree_body", "diff_body", "series_body", "owner_json"]
