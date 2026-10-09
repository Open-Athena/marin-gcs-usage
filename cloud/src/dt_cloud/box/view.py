"""The box's answers to the Worker's filtered reads, from a loaded `MemIndex`
(specs/filter-query-service.md §3, §6.3): `/api/subtree?q=` and
`/api/diff?q=` in the Worker's response shapes, exact and unflagged.

A port of `site/functions/_lib/view.ts`: `readView`'s filter branch (match
roots, NOT exclusions and their cuts, the forest threshold, the per-root
attenuated subtree), `buildView`'s tree assembly (`(other)` folds, `m`
flags, the `d`/`a`/`cb`/`us` fields) and `buildDiff`'s walk. The search is
`mem.evaluate` (complete: no budgets, so never `partial`); totals are the
index's exact integers; the shown fields come from the index's cold detail
(`mem.Detail`), owner slices included, so the owner (`o=`) and class (`cl=`)
scopes apply per slice as `scope.ts` does.

Where the box differs from the Worker, deliberately:

- **Every match is found** (no search budget): no `partial`, `approximate`,
  `firstPaint`; `truncated` is false.
- **Past `HARD_CAP` match roots** (50K), roots below the forest threshold
  fold into their parent's `(other)` (counted in `f`) instead of each being
  drawn; `folded` says how many. `matches` / `matched` still list every
  root, streamed. Below the cap every root is drawn, as the Worker does.
- **Ties** between siblings of equal bytes are ordered by path.
- **The diff's lookups** are exact point reads with no cap, and a match of
  the store root itself covers every path under it (the Worker's
  `inQuery` tests `p.startsWith(m + '/')`, which `''` never passes).
- **A root-hit query** (the view itself matches) goes through the filter
  branch: `matched` lists the view (the Worker's plain branch sends `[]`).
- No user lens (`lens=`): a 409, the Worker answers (claims live in D1).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Iterator

import numpy as np

from ..bench.mem import MemIndex, evaluate
from ..bench.query import Ast

MIN_AREA_DEFAULT = 12
ATTEN_DEFAULT = 2
QUANT = 128
HARD_CAP = 50_000
REGION_READS = 24
CHUNK = 1 << 21  # roots aggregated at a time
LIST_CHUNK = 1 << 16
CLASS_LETTERS = {"s": "1", "n": "2", "c": "3", "a": "4"}


class NotFound(Exception):
    pass


class BadRequest(ValueError):
    pass


def js_round(x: float) -> int:
    """JS `Math.round`: half up."""
    return int(math.floor(x + 0.5))


# --- scope ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """`o=` (`'owned'` / `'unowned'` / a frozenset: owned except these) and
    `cl=` (the classes kept, ⊂ {'1','2','3','4'}); None = no scope."""

    owner: object = None
    classes: frozenset | None = None

    @property
    def any(self) -> bool:
        return self.owner is not None or self.classes is not None


def parse_owner(raw: str | None):
    if raw in ("owned", "claimed"):
        return "owned"
    if raw in ("unowned", "unclaimed"):
        return "unowned"
    if raw and raw.startswith("!"):
        return frozenset(x for x in raw[1:].split(",") if x)
    return None


def parse_classes(raw: str | None) -> frozenset | None:
    if not raw:
        return None
    out = frozenset(CLASS_LETTERS[c] for c in raw if c in CLASS_LETTERS)
    return None if len(out) in (0, 4) else out


# --- aggregates -----------------------------------------------------------------------


@dataclass
class Agg:
    """A path's aggregate (`view.ts` `Agg`), for the nodes a response draws."""

    b: int = 0
    o: int = 0
    wts: float = 0.0
    wb: float = 0.0
    a: int | None = None
    cb: dict = field(default_factory=dict)
    ub: dict = field(default_factory=dict)
    kind: str | None = None
    nc: int | None = None

    def subtract(self, kids: list["Agg"]) -> "Agg":
        """`view.ts` `subtract`: the residual of a parent after its kids."""
        out = Agg(a=self.a)
        out.b = self.b - sum(k.b for k in kids)
        out.o = max(0, self.o - sum(k.o for k in kids))
        out.wts = self.wts - sum(k.wts for k in kids)
        out.wb = max(0.0, self.wb - sum(k.wb for k in kids))
        for key in ("cb", "ub"):
            mine = getattr(self, key)
            res = getattr(out, key)
            for k, v in mine.items():
                r = v - sum(getattr(kid, key).get(k, 0) for kid in kids)
                if r > 0:
                    res[k] = r
        return out

    def display(self) -> dict:
        out: dict = {}
        if self.wb:
            out["d"] = js_round(self.wts / self.wb / 86400)
        if self.a is not None:
            out["a"] = self.a
        if self.cb:
            out["cb"] = dict(sorted(self.cb.items(), key=lambda kv: -kv[1]))
        if self.ub:
            out["us"] = [[u, b] for u, b in sorted(self.ub.items(), key=lambda kv: -kv[1])]
        return out


@dataclass
class AggSet:
    """Aggregates of many nodes as columns; `ub` (per-user bytes) as COO
    triples (`ui` row, `uu` user code, `uv` bytes). `a` −1 = null, `kind`
    1 file / 0 dir / −1 null, `nc` −1 = null."""

    b: np.ndarray
    o: np.ndarray
    wts: np.ndarray
    wb: np.ndarray
    a: np.ndarray
    c: np.ndarray  # (n, 3): bytes in classes 2..4
    kind: np.ndarray
    nc: np.ndarray
    ui: np.ndarray
    uu: np.ndarray
    uv: np.ndarray

    @property
    def n(self) -> int:
        return len(self.b)

    @classmethod
    def zeros(cls, n: int) -> "AggSet":
        z = np.zeros(0, np.int64)
        return cls(np.zeros(n, np.int64), np.zeros(n, np.int64), np.zeros(n), np.zeros(n), np.full(n, -1, np.int64), np.zeros((n, 3), np.int64),
                   np.full(n, -1, np.int8), np.full(n, -1, np.int64), z, z, z)

    def take(self, idx: np.ndarray) -> "AggSet":
        idx = np.asarray(idx, np.int64)
        inv = np.full(self.n, -1, np.int64)
        inv[idx] = np.arange(len(idx))
        keep = inv[self.ui] >= 0 if len(self.ui) else np.zeros(0, bool)
        return AggSet(self.b[idx], self.o[idx], self.wts[idx], self.wb[idx], self.a[idx], self.c[idx], self.kind[idx], self.nc[idx],
                      inv[self.ui[keep]], self.uu[keep], self.uv[keep])

    def group_sum(self, g: np.ndarray, m: int) -> "AggSet":
        """`sumAgg` of rows into `m` groups (`g`: each row's group): bytes,
        counts and weights add, `a` maxes, per-user keys merge; the result has
        no kind / child count (a synthesized aggregate)."""
        out = AggSet.zeros(m)
        np.add.at(out.b, g, self.b)
        np.add.at(out.o, g, self.o)
        np.add.at(out.wts, g, self.wts)
        np.add.at(out.wb, g, self.wb)
        np.maximum.at(out.a, g, self.a)
        np.add.at(out.c, g, self.c)
        out.ui, out.uu, out.uv = _coo_sum(g[self.ui], self.uu, self.uv)
        return out

    def minus(self, cut: "AggSet", has: np.ndarray, lost: np.ndarray) -> "AggSet":
        """`view.ts` `minus` per row: rows with a cut (`has`) lose it
        (`subtract`), every row loses `lost` direct children from `nc`."""
        out = AggSet(self.b.copy(), self.o.copy(), self.wts.copy(), self.wb.copy(), self.a.copy(), self.c.copy(), self.kind.copy(), self.nc.copy(),
                     self.ui, self.uu, self.uv)
        if has.any():
            h = has
            out.b[h] = self.b[h] - cut.b[h]
            out.o[h] = np.maximum(0, self.o[h] - cut.o[h])
            out.wts[h] = self.wts[h] - cut.wts[h]
            out.wb[h] = np.maximum(0.0, self.wb[h] - cut.wb[h])
            out.c[h] = np.maximum(0, self.c[h] - cut.c[h])
            U = int(max(self.uu.max(initial=0), cut.uu.max(initial=0))) + 1
            kc = cut.ui * U + cut.uu
            o = np.argsort(kc, kind="stable")
            kc, cv = kc[o], cut.uv[o]
            kp = self.ui * U + self.uu
            pos = np.minimum(np.searchsorted(kc, kp), max(0, len(kc) - 1))
            match = (len(kc) > 0) & (kc[pos] == kp) if len(kc) else np.zeros(len(kp), bool)
            res = self.uv - np.where(match, cv[pos] if len(kc) else 0, 0)
            hp = h[self.ui]
            keep = ~hp | (res > 0)
            out.ui, out.uu, out.uv = self.ui[keep], self.uu[keep], np.where(hp, res, self.uv)[keep]
        lo = lost > 0
        if lo.any():
            m = lo & (self.nc >= 0)
            out.nc[m] = np.maximum(0, self.nc[m] - lost[m])
        return out

    def total(self) -> "AggSet":
        return self.group_sum(np.zeros(self.n, np.int64), 1)

    def agg(self, i: int, users: list[str]) -> Agg:
        cb = {str(k + 2): int(self.c[i, k]) for k in range(3) if self.c[i, k] > 0}
        sel = np.flatnonzero(self.ui == i)
        ub = {users[int(self.uu[j])]: int(self.uv[j]) for j in sel}
        return Agg(int(self.b[i]), int(self.o[i]), float(self.wts[i]), float(self.wb[i]), None if self.a[i] < 0 else int(self.a[i]), cb, ub,
                   None if self.kind[i] < 0 else ("file" if self.kind[i] else "dir"), None if self.nc[i] < 0 else int(self.nc[i]))

    def aggs(self, users: list[str]) -> list[Agg]:
        """Every row as an `Agg` (one pass over the COO)."""
        out = [Agg(int(self.b[i]), int(self.o[i]), float(self.wts[i]), float(self.wb[i]), None if self.a[i] < 0 else int(self.a[i]),
                   {str(k + 2): int(self.c[i, k]) for k in range(3) if self.c[i, k] > 0}, {},
                   None if self.kind[i] < 0 else ("file" if self.kind[i] else "dir"), None if self.nc[i] < 0 else int(self.nc[i])) for i in range(self.n)]
        for i, u, v in zip(self.ui.tolist(), self.uu.tolist(), self.uv.tolist()):
            out[i].ub[users[u]] = out[i].ub.get(users[u], 0) + v
        return out


def _coo_sum(i: np.ndarray, u: np.ndarray, v: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Duplicate (row, user) entries summed (zero sums kept: a key exists
    once any slice with that user contributed)."""
    if not len(i):
        z = np.zeros(0, np.int64)
        return z, z, z
    U = int(u.max()) + 1
    key, inv = np.unique(i.astype(np.int64) * U + u, return_inverse=True)
    out = np.zeros(len(key), np.int64)
    np.add.at(out, inv, v)
    return key // U, key % U, out


def node_aggs(ix: MemIndex, ids: np.ndarray, scope: Scope) -> AggSet:
    """Each node's scoped aggregate (`view.ts` `aggregate`'s `mine`: rows
    cut to the class scope, then filtered by the owner scope), from its
    owner slices: the detail row of a single-slice node (its bytes and
    objects are the index's), else `slices.parquet`. −1 = the store root
    (the buckets summed, `nc` = their count)."""
    ids = np.asarray(ids, np.int64)
    root = ids < 0
    if root.any():
        out = AggSet.zeros(len(ids))
        top = node_aggs(ix, ix.top, scope)
        rest = node_aggs(ix, ids[~root], scope)
        t = top.total()
        t.kind[:] = 0 if (top.kind >= 0).any() else -1
        t.nc[:] = ix.n_top
        g = np.empty(len(ids), np.int64)
        g[~root] = np.arange(len(rest.b))
        merged = _concat([rest, t])
        g[root] = rest.n
        return merged.take(g)
    n = len(ids)
    b, o = ix.b_of(ids), ix.o_of(ids)
    nc = ix.nc_of(ids)
    det = ix.detail
    if det is None:
        if scope.any:
            raise BadRequest("this index has no detail: owner / class scopes need it")
        out = AggSet.zeros(n)
        out.b, out.o, out.nc = b, o, nc
        return out
    d = det.take(ids)
    single = np.flatnonzero(~d["multi"])
    rows = {k: d[k][single] for k in ("usr", "kind", "mean", "lr", "c2", "c3", "c4")}
    rows["idx"], rows["size"], rows["nf"] = single, b[single], o[single]
    multi = np.flatnonzero(d["multi"])
    if len(multi):
        sl = det.slices
        mids = ids[multi]
        order = np.argsort(mids)
        pos = np.searchsorted(mids[order], sl["id"])
        hit = (pos < len(mids)) & (mids[order][np.minimum(pos, len(mids) - 1)] == sl["id"])
        src = np.flatnonzero(hit)
        ext = {k: sl[k][src] for k in ("usr", "kind", "mean", "lr", "c2", "c3", "c4")}
        ext["idx"] = multi[order][pos[src]]
        ext["size"], ext["nf"] = sl["size"][src], sl["n_files"][src]
        rows = {k: np.concatenate([rows[k], ext[k]]) for k in rows}
    size, nf = rows["size"].astype(np.int64), rows["nf"].astype(np.int64)
    c = np.stack([rows["c2"], rows["c3"], rows["c4"]], axis=1).astype(np.int64)
    mean = rows["mean"]
    w = np.where(np.isnan(mean), 0.0, size.astype(np.float64))
    if scope.classes is not None:
        cl = scope.classes
        c1 = np.maximum(0, size - c.sum(axis=1))
        new = (c1 if "1" in cl else 0) + sum(c[:, k] if str(k + 2) in cl else 0 for k in range(3))
        new = np.asarray(new, np.int64) + np.zeros(len(size), np.int64)
        f = np.where(size > 0, new / np.where(size > 0, size, 1), 0.0)
        nf = np.floor(nf * f + 0.5).astype(np.int64)
        w = w * f
        c = np.stack([c[:, k] if str(k + 2) in cl else np.zeros(len(size), np.int64) for k in range(3)], axis=1)
        size = new
    usr = rows["usr"]
    own = scope.owner
    if own is None:
        ok = np.ones(len(size), bool)
    elif own == "owned":
        ok = usr >= 0
    elif own == "unowned":
        ok = usr < 0
    else:
        bad = np.array([det.users[u] for u in own if u in det.users], np.int64)
        ok = (usr >= 0) & ~np.isin(usr, bad)
    idx = rows["idx"][ok]
    out = AggSet.zeros(n)
    np.add.at(out.b, idx, size[ok])
    np.add.at(out.o, idx, nf[ok])
    tw = ok & ~np.isnan(mean) & (w > 0)
    np.add.at(out.wts, rows["idx"][tw], mean[tw] * w[tw])
    np.add.at(out.wb, rows["idx"][tw], w[tw])
    np.maximum.at(out.a, idx, rows["lr"][ok])
    np.add.at(out.c, idx, c[ok])
    kd = rows["kind"][ok]
    out.kind[idx] = np.where(kd < 0, np.where(nc[idx] > 0, 0, 1), kd).astype(np.int8)
    out.nc[idx] = nc[idx]
    us = ok & (usr >= 0)
    out.ui, out.uu, out.uv = _coo_sum(rows["idx"][us], usr[us], size[us])
    return out


def _concat(parts: list[AggSet]) -> AggSet:
    off = np.cumsum([0] + [p.n for p in parts])
    return AggSet(*(np.concatenate([getattr(p, k) for p in parts]) for k in ("b", "o", "wts", "wb", "a", "c", "kind", "nc")),
                  np.concatenate([p.ui + off[j] for j, p in enumerate(parts)]), np.concatenate([p.uu for p in parts]), np.concatenate([p.uv for p in parts]))


# --- the filter view ------------------------------------------------------------------


@dataclass
class Read:
    """`view.ts` `Read` for the filter branch: what a view and a diff need."""

    path: str
    v: int
    dP: int
    root_agg: Agg
    kept: dict  # path → Agg (insertion order: by path)
    depth: dict  # path → depth
    folded_of: dict  # path → folded direct children (synthesized parents)
    threshold: float
    deepest: int  # the read roots' deepest depth (the `(other)` threshold)
    atten: float
    roots: np.ndarray  # ids, sorted
    net_b: np.ndarray  # per root: net bytes
    net_o: np.ndarray  # … and objects
    excluded: np.ndarray  # ids, sorted
    excl: AggSet  # per excluded
    hit: bool
    folded: int
    stats: dict
    root_paths: set  # the drawn match roots (`m`)

    def thr_at(self, d: int) -> float:
        return self.threshold * self.atten ** max(0, d - self.deepest - 1)


def _excl_cuts(ix: MemIndex, E: np.ndarray, ER: np.ndarray, EA: AggSet) -> tuple[np.ndarray, AggSet, np.ndarray, np.ndarray]:
    """`exclude()`'s bookkeeping: each excluded node's aggregate added to
    every node from its parent up to its match root (inclusive), and one
    lost child at its parent. Returns (cut node ids sorted, their summed
    cuts, lost-kid parent ids sorted, counts)."""
    anc, src = [], []
    cur = ix.parent[E].astype(np.int64) if len(E) else np.zeros(0, np.int64)
    idx = np.arange(len(E))
    while len(cur):
        anc.append(cur)
        src.append(idx)
        done = (cur == ER[idx]) | (cur < 0)
        cur, idx = cur[~done], idx[~done]
        cur = ix.parent[cur].astype(np.int64)
    if not anc:
        z = np.zeros(0, np.int64)
        return z, AggSet.zeros(0), z, z
    anc, src = np.concatenate(anc), np.concatenate(src)
    u, g = np.unique(anc, return_inverse=True)
    cuts = EA.take(src).group_sum(g, len(u))
    lp, lc = np.unique(ix.parent[E].astype(np.int64), return_counts=True)
    return u, cuts, lp, lc


def _lookup(keys: np.ndarray, vals_ids: np.ndarray, q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Positions of `q` in sorted `vals_ids` and whether present."""
    if not len(vals_ids):
        return np.zeros(len(q), np.int64), np.zeros(len(q), bool)
    pos = np.minimum(np.searchsorted(vals_ids, q), len(vals_ids) - 1)
    return pos, vals_ids[pos] == q


def _cut_for(ids: np.ndarray, cu: np.ndarray, cuts: AggSet, lp: np.ndarray, lc: np.ndarray) -> tuple[AggSet, np.ndarray, np.ndarray]:
    pos, has = _lookup(cu, cu, ids)
    c = cuts.take(np.where(has, pos, 0)) if cuts.n else AggSet.zeros(len(ids))
    lpos, lhas = _lookup(lp, lp, ids)
    lost = np.where(lhas, lc[lpos] if len(lc) else 0, 0).astype(np.int64)
    return c, has, lost


def _ancestor_sums(ix: MemIndex, ids: np.ndarray, net: AggSet, rd: np.ndarray, dP: int) -> tuple[np.ndarray, AggSet]:
    """Σ of `net` (rows of root `ids` at depths `rd`) into every ancestor
    strictly between the view root (depth `dP`) and the roots, bottom-up a
    depth at a time: (ancestor ids, their sums; an id can repeat across
    depths' rows only once)."""
    out_ids: list[np.ndarray] = []
    out_sets: list[AggSet] = []
    p_ids, p_set = np.zeros(0, np.int64), AggSet.zeros(0)
    for d in range(int(rd.max(initial=dP)), dP + 1, -1):
        at = np.flatnonzero(rd == d)
        ids_d = np.concatenate([ids[at], p_ids])
        if not len(ids_d):
            continue
        set_d = _concat([net.take(at), p_set])
        u, g = np.unique(ix.parent[ids_d].astype(np.int64), return_inverse=True)
        p_ids, p_set = u, set_d.group_sum(g, len(u))
        out_ids.append(p_ids)
        out_sets.append(p_set)
    if not out_ids:
        return np.zeros(0, np.int64), AggSet.zeros(0)
    return np.concatenate(out_ids), _concat(out_sets)


def _kids_over(ix: MemIndex, F: np.ndarray, thr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The children of each node of `F` whose raw bytes reach its `thr`
    (children are largest first, so a prefix of each range): (child ids,
    index into F)."""
    lo, hi = ix.kid_range(F)
    small = (hi - lo) <= 4096
    outs, owners = [], []
    if small.any():
        s = np.flatnonzero(small)
        c = MemIndex._ranges(lo[s], hi[s])
        own = np.repeat(s, (hi - lo)[s])
        ok = ix.b_of(c) >= thr[own]
        outs.append(c[ok])
        owners.append(own[ok])
    for i in np.flatnonzero(~small):
        a, b = int(lo[i]), int(hi[i])
        L, R = a, b  # first index with b < thr
        while L < R:
            m = (L + R) // 2
            if ix.b1(m) >= thr[i]:
                L = m + 1
            else:
                R = m
        outs.append(np.arange(a, L, dtype=np.int64))
        owners.append(np.full(L - a, i, np.int64))
    if not outs:
        z = np.zeros(0, np.int64)
        return z, z
    return np.concatenate(outs), np.concatenate(owners)


def filter_view(ix: MemIndex, path: str, ast: Ast, *, w: int, h: int, min_area: float, atten: float, scope: Scope = Scope(),
                max_depth: int | None = None, threshold: float | None = None, ev=None) -> Read | None:
    """`readView`'s filter branch on the box: None = nothing in scope matched."""
    t0 = time.monotonic()
    try:
        v = ix.find(path)
    except KeyError as e:
        raise NotFound(path) from e
    dP = ix.depth1(v)
    ev = ev if ev is not None else evaluate(ix, ast, path, v)
    stats = {"search_s": ev.stats.get("s")}
    users = ix.detail.user_names if ix.detail is not None else []
    roots = ev.roots.astype(np.int64)
    E = ev.excluded.astype(np.int64)
    ER = (ev.excl_root if ev.excl_root is not None else np.zeros(0, np.int64)).astype(np.int64)
    if len(E):
        o = np.argsort(E)
        E, ER = E[o], ER[o]
    EA = node_aggs(ix, E, scope)
    cu, cuts, lp, lc = _excl_cuts(ix, E, ER, EA)
    hit = ev.hit
    rd = np.array([dP], np.int64) if hit else ix.depth_of(roots)

    def net_of(ids: np.ndarray) -> AggSet:
        """Each root's scoped aggregate, net of the exclusions under it."""
        c, has, lost = _cut_for(ids, cu, cuts, lp, lc)
        return node_aggs(ix, ids, scope).minus(c, has, lost)

    # One pass over the roots, CHUNK at a time (a query can hold 20M): each
    # root's net bytes and objects, the total, and the synthesized ancestors'
    # partial sums; the working set stays a chunk's, not the whole set's.
    net_b = np.zeros(len(roots), np.int64)
    net_o = np.zeros(len(roots), np.int64)
    tot_parts: list[AggSet] = []
    anc_parts: list[tuple[np.ndarray, AggSet]] = []
    whole = None
    for i in range(0, len(roots), CHUNK):
        sl = slice(i, i + CHUNK)
        n = net_of(roots[sl])
        net_b[sl], net_o[sl] = n.b, n.o
        tot_parts.append(n.total())
        if not hit:
            anc_parts.append(_ancestor_sums(ix, roots[sl], n, rd[sl], dP))
        if len(roots) <= CHUNK:
            whole = n
    tot = _concat(tot_parts).total() if tot_parts else AggSet.zeros(1)
    if tot.b[0] <= 0:
        return None
    T = float(threshold) if threshold is not None else float(tot.b[0]) * min_area / (w * h)
    # The `(other)` threshold attenuates from the deepest of the read roots
    # (the Worker reads the REGION_READS heaviest).
    top = np.argsort(-net_b, kind="stable")[:REGION_READS]
    deepest = int(rd[top].max()) if len(top) else dP
    fold = (not hit) and len(roots) > HARD_CAP
    draw = np.zeros(len(roots), bool) if hit else (net_b >= T if fold else np.ones(len(roots), bool))

    # Synthesized ancestors between the view root and the roots: Σ net roots
    # under each (the chunks' partial sums merged).
    if anc_parts and sum(len(x) for x, _ in anc_parts):
        u, g = np.unique(np.concatenate([x for x, _ in anc_parts]), return_inverse=True)
        A_ids, A = u, _concat([y for _, y in anc_parts]).group_sum(g, len(u))
    else:
        A_ids, A = np.zeros(0, np.int64), AggSet.zeros(0)
    A_draw = (A.b >= T) if fold else np.ones(len(A_ids), bool)
    # Folded direct members per parent: roots and ancestors under the threshold.
    folded_of_id: dict[int, int] = {}
    for ids in (roots[~draw] if not hit else np.zeros(0, np.int64), A_ids[~A_draw]):
        for pid, cnt in zip(*np.unique(ix.parent[ids].astype(np.int64), return_counts=True)) if len(ids) else ():
            folded_of_id[int(pid)] = folded_of_id.get(int(pid), 0) + int(cnt)

    # Phase 2: under each drawn root (the view root, on a hit), thresholds
    # rebased on that root's depth.
    P2_ids: list[np.ndarray] = []
    P2_sets: list[AggSet] = []
    if not (max_depth is not None and max_depth <= 0):
        F = np.array([v], np.int64) if hit else roots[draw]
        Frd = np.array([dP], np.int64) if hit else rd[draw]
        while len(F):
            dch = np.where(F < 0, 0, ix.depth_of(np.maximum(F, 0))).astype(np.int64) + 1
            ok = np.ones(len(F), bool) if max_depth is None else dch <= Frd + max_depth
            F, Frd, dch = F[ok], Frd[ok], dch[ok]
            if not len(F):
                break
            thr = T * np.power(float(atten), np.maximum(0, dch - Frd - 1).astype(np.float64))
            kids, own = _kids_over(ix, F, thr)
            if len(E) and len(kids):
                _, ex = _lookup(E, E, kids)
                kids, own = kids[~ex], own[~ex]
            if not len(kids):
                break
            KA = node_aggs(ix, kids, scope)
            kc, khas, klost = _cut_for(kids, cu, cuts, lp, lc)
            KA = KA.minus(kc, khas, klost)
            sel = np.flatnonzero((KA.b > 0) & (KA.b >= thr[own]))
            P2_ids.append(kids[sel])
            P2_sets.append(KA.take(sel))
            F, Frd = kids[sel], Frd[own[sel]]
    K_ids = np.concatenate(P2_ids) if P2_ids else np.zeros(0, np.int64)
    K = _concat(P2_sets) if P2_sets else AggSet.zeros(0)

    # The kept map (`aggsF`): synthesized ancestors, drawn roots, phase-2
    # nodes; inserted in path order.
    sa = np.flatnonzero(A_draw)
    sr = np.flatnonzero(draw)
    all_ids = np.concatenate([A_ids[sa], roots[sr], K_ids])
    all_set = _concat([A.take(sa), whole.take(sr) if whole is not None else net_of(roots[sr]), K])
    kind_of = np.concatenate([np.zeros(len(sa), np.int8), np.ones(len(sr), np.int8), np.full(len(K_ids), 2, np.int8)])
    paths = ix.paths(all_ids) if len(all_ids) else []
    aggs = all_set.aggs(users)
    depths = ix.depth_of(all_ids).tolist() if len(all_ids) else []
    kept: dict = {}
    depth: dict = {}
    folded_of: dict = {}
    root_paths: set = set()
    for i in sorted(range(len(paths)), key=lambda i: paths[i]):
        p, a = paths[i], aggs[i]
        if kind_of[i] == 0:
            a.kind, a.nc = None, None
            if int(all_ids[i]) in folded_of_id:
                folded_of[p] = folded_of_id[int(all_ids[i])]
        elif kind_of[i] == 1:
            root_paths.add(p)
        kept[p] = a
        depth[p] = depths[i]
    if v in folded_of_id:
        folded_of[path] = folded_of_id[v]
    if hit:
        root_agg = whole.agg(0, users)
        root_paths.add(path)
    else:
        root_agg = tot.agg(0, users)
        root_agg.kind, root_agg.nc = None, None
    stats["s"] = round(time.monotonic() - t0, 4)
    stats["kept"] = len(kept)
    return Read(path, v, dP, root_agg, kept, depth, folded_of, T, deepest, float(atten), roots, net_b, net_o, E, EA, hit,
                int((~draw).sum()) if fold else 0, stats, root_paths)


# --- responses ------------------------------------------------------------------------


def parent_of(p: str) -> str:
    i = p.rfind("/")
    return "" if i < 0 else p[:i]


def kids_index(kept: dict, path: str) -> dict:
    out: dict = {}
    for p in kept:
        par = parent_of(p)
        key = par if par in kept else path
        out.setdefault(key, []).append(p)
    return out


def build_tree(r: Read, root_label: str) -> dict:
    """`buildView`'s assembly over a `Read`."""
    path = r.path
    kids_of = kids_index(r.kept, path)
    matched = r.root_paths  # `m`: the drawn match roots (the view root, on a hit)

    def name(p: str) -> str:
        return root_label if p == path and path == "" else p.rsplit("/", 1)[-1]

    def node_of(n: str, a: Agg) -> dict:
        return {"n": n, "k": a.kind or "dir", "b": js_round(a.b), "o": js_round(a.o), **a.display()}

    def build(p: str, a: Agg) -> dict:
        node = node_of(name(p), a)
        if p in matched:
            node["m"] = 1
        child_paths = kids_of.get(p, [])
        if not child_paths:
            return node
        kids = sorted((build(cp, r.kept[cp]) for cp in child_paths), key=lambda x: -x["b"])
        rest = a.subtract([r.kept[cp] for cp in child_paths])
        node["c"] = kids
        if rest.b > r.thr_at(r.depth.get(child_paths[0], r.dP + 1)):
            f = max(0, a.nc - len(child_paths)) if a.nc is not None else r.folded_of.get(p, 0)
            node["c"].append({**node_of("(other)", rest), "f": f})
        return node

    return build(path, r.root_agg)


def _sorted_paths(ix: MemIndex, ids: np.ndarray):
    """(the ids' paths as Arrow, the order that sorts them)."""
    import pyarrow.compute as pc

    arr = ix.paths_arrow(np.asarray(ids, np.int64))
    return arr, pc.array_sort_indices(arr).to_numpy()


def _json_list_chunks(items: Iterator[list]) -> Iterator[str]:
    yield "["
    first = True
    for chunk in items:
        if not chunk:
            continue
        s = json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))[1:-1]
        yield s if first else "," + s
        first = False
    yield "]"


def match_lists(ix: MemIndex, r: Read) -> dict:
    """The roots' and excluded paths' lists, as chunk generators (they can be
    millions long): `matches` (sorted), `matched` (net totals, heaviest
    first, ties by path), `excluded` (sorted)."""
    import pyarrow as pa

    if r.hit:
        roots = pa.array([r.path], pa.large_string())
        order = np.zeros(1, np.int64)
    else:
        roots, order = _sorted_paths(ix, r.roots)
    b, o = r.net_b, r.net_o
    rank = np.empty(len(order), np.int64)
    rank[order] = np.arange(len(order))
    morder = np.lexsort((rank, -b))

    def matches():
        for i in range(0, len(order), LIST_CHUNK):
            yield roots.take(pa.array(order[i : i + LIST_CHUNK])).to_pylist()

    def matched():
        for i in range(0, len(morder), LIST_CHUNK):
            sel = morder[i : i + LIST_CHUNK]
            ps = roots.take(pa.array(sel)).to_pylist()
            yield [{"path": p, "b": int(bb), "o": int(oo)} for p, bb, oo in zip(ps, b[sel].tolist(), o[sel].tolist())]

    out = {"matches": matches, "matched": matched, "n": len(order)}
    if len(r.excluded):
        ex, eo = _sorted_paths(ix, r.excluded)

        def excluded():
            for i in range(0, len(eo), LIST_CHUNK):
                yield ex.take(pa.array(eo[i : i + LIST_CHUNK])).to_pylist()

        out["excluded"] = excluded
    return out


def num(x: float) -> int | float:
    """A JS number's JSON: integral values without a fraction."""
    return int(x) if float(x).is_integer() else x


def owner_json(raw: str | None):
    own = parse_owner(raw)
    return {"not": [x for x in raw[1:].split(",") if x]} if isinstance(own, frozenset) else own


def subtree_body(ix: MemIndex, r: Read | None, *, date: str, path: str, w: int, h: int, min_area: float, atten: float, q: str,
                 owner_raw: str | None, root_label: str) -> Iterator[str]:
    """`/api/subtree?q=`'s body, streamed (key order as the Worker's)."""
    head = {"date": date, "path": path, "w": w, "h": h, "minArea": num(min_area), "atten": num(atten)}
    own = owner_json(owner_raw)
    if r is None:
        body = {**head, "tier": "none", "index": "none", "threshold": 0, "nodes": 0, "truncated": False, **({"owner": own} if own else {}),
                "q": q, "matches": [], "matched": [], "tree": {"n": root_label if path == "" else path.rsplit("/", 1)[-1], "k": "dir", "b": 0, "o": 0}}
        yield json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        return
    lists = match_lists(ix, r)
    tree = build_tree(r, root_label)
    pre = {**head, "tier": "box", "index": "mem", "threshold": js_round(r.threshold), "nodes": len(r.kept), "truncated": False,
           **({"folded": r.folded} if r.folded else {}), **({"owner": own} if own else {}), "q": q}
    s = json.dumps(pre, ensure_ascii=False, separators=(",", ":"))
    yield s[:-1] + ',"matches":'
    yield from _json_list_chunks(lists["matches"]())
    yield ',"matched":'
    yield from _json_list_chunks(lists["matched"]())
    if "excluded" in lists:
        yield ',"excluded":'
        yield from _json_list_chunks(lists["excluded"]())
    yield ',"tree":' + json.dumps(tree, ensure_ascii=False, separators=(",", ":")) + "}"


# --- the diff -------------------------------------------------------------------------


def _exists(ix: MemIndex, path: str) -> bool:
    try:
        ix.find(path)
        return True
    except KeyError:
        return False


def _has(sorted_ids: np.ndarray, x: int) -> bool:
    return bool(len(sorted_ids)) and bool(_lookup(sorted_ids, sorted_ids, np.array([x], np.int64))[1][0])


def in_query(ix: MemIndex, r: Read, node: int) -> bool:
    """`buildDiff`'s `inQuery` by node: at or under a match root and not at
    or under an excluded path. A match of the view root covers everything
    under it (the store root included)."""
    hit = r.hit
    cur = node
    while not hit and cur >= 0:
        hit = _has(r.roots, cur)
        cur = int(ix.parent[cur])
    if not hit:
        return False
    cur = node
    while cur >= 0:
        if _has(r.excluded, cur):
            return False
        cur = int(ix.parent[cur])
    return True


def lookup(ix: MemIndex, r: Read, p: str, scope: Scope) -> tuple[Agg | None, bool]:
    """A path's aggregate on a side where the walk didn't keep it (`lookup`
    / `lookupMany`): its scoped total less the side's excluded paths under
    it; None when it is outside the query, absent, or empty. Also whether it
    counts as a lookup (the Worker counts the asks inside the query)."""
    q, n = p, None
    while True:
        try:
            n = ix.find(q)
            break
        except KeyError:
            if q == "":
                return None, False
            q = parent_of(q)
    if not in_query(ix, r, n):
        return None, False
    if q != p:
        return None, True
    users = ix.detail.user_names if ix.detail is not None else []
    a = node_aggs(ix, np.array([n]), scope).agg(0, users)
    if len(r.excluded):
        under = ix.under(r.excluded, n, ix.depth1(n))
        if under.any():
            cut = r.excl.take(np.flatnonzero(under)).total().agg(0, users)
            if cut.b or cut.o:
                kind = a.kind
                a = a.subtract([cut])
                a.kind = kind
    return (a if a.b > 0 else None), True


def _side_lists(ix: MemIndex, r: Read | None):
    """(sorted root paths as Arrow, net bytes, net objects) of a side."""
    import pyarrow as pa

    if r is None:
        return pa.array([], pa.large_string()), np.zeros(0, np.int64), np.zeros(0, np.int64)
    if r.hit:
        return pa.array([r.path], pa.large_string()), r.net_b, r.net_o
    arr, order = _sorted_paths(ix, r.roots)
    return arr.take(pa.array(order)), r.net_b[order], r.net_o[order]


def _matched_union(ixa: MemIndex, va: Read | None, ixb: MemIndex, vb: Read | None) -> Iterator[list]:
    """Both sides' `matched` by path, the newer side's entry kept for a path
    in both."""
    import pyarrow as pa
    import pyarrow.compute as pc

    pa_, ba, oa = _side_lists(ixa, va)
    pb, bb, ob = _side_lists(ixb, vb)
    paths = pa.concat_arrays([pa_.cast(pa.large_string()), pb.cast(pa.large_string())])
    src = np.concatenate([np.zeros(len(pa_), np.int64), np.ones(len(pb), np.int64)])
    b, o = np.concatenate([ba, bb]), np.concatenate([oa, ob])
    order = pc.sort_indices(pa.table({"p": paths, "s": src}), sort_keys=[("p", "ascending"), ("s", "descending")]).to_numpy()
    ps = paths.take(pa.array(order))
    keep = np.ones(len(order), bool)
    if len(order) > 1:
        keep[1:] = pc.not_equal(ps.slice(1), ps.slice(0, len(ps) - 1)).to_numpy(zero_copy_only=False)
    sel = order[keep]
    for i in range(0, len(sel), LIST_CHUNK):
        s = sel[i : i + LIST_CHUNK]
        yield [{"path": p, "b": int(x), "o": int(y)} for p, x, y in zip(paths.take(pa.array(s)).to_pylist(), b[s].tolist(), o[s].tolist())]


def diff_body(ixa: MemIndex, ixb: MemIndex, *, prev: str, curr: str, path: str, w: int, h: int, min_area: float, atten: float, top: int,
              ast: Ast, q: str, scope: Scope = Scope(), summary: bool = False, depth: int | None = None, owner_raw: str | None = None) -> Iterator[str]:
    """`/api/diff?q=`'s body (`buildDiff` with a query), streamed."""
    dP = 0 if path == "" else path.count("/") + 1
    ra, rb = _exists(ixa, path), _exists(ixb, path)
    if not ra and not rb:
        raise NotFound(path)
    kw = dict(w=w, h=h, min_area=min_area, atten=atten, scope=scope, max_depth=depth)
    eva = evaluate(ixa, ast, path) if ra else None
    evb = evaluate(ixb, ast, path) if rb else None
    va = filter_view(ixa, path, ast, ev=eva, **kw) if ra else None
    vb = filter_view(ixb, path, ast, ev=evb, **kw) if rb else None
    if va and vb and va.threshold != vb.threshold:
        shared = max(va.threshold, vb.threshold)
        if va.threshold < shared:
            va = filter_view(ixa, path, ast, ev=eva, threshold=shared, **kw)
        else:
            vb = filter_view(ixb, path, ast, ev=evb, threshold=shared, **kw)
    own = owner_json(owner_raw)
    head = {"prev": prev, "curr": curr, "path": path, **({"owner": own} if own else {}), "q": q}
    if not va and not vb:
        body = {**head, "rows": [], "total_a": 0, "total_b": 0, "objects_a": 0, "objects_b": 0, "threshold": 0, "tier": "none", "matched": [],
                "expansions": 0, "truncated": False, "lookups": 0, "lookups_capped": False}
        yield json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        return
    totals = {
        "total_a": js_round(va.root_agg.b) if va else 0, "total_b": js_round(vb.root_agg.b) if vb else 0,
        "objects_a": js_round(va.root_agg.o) if va else 0, "objects_b": js_round(vb.root_agg.o) if vb else 0,
        "threshold": js_round((vb or va).threshold), "tier": "box",
    }
    rows: list[dict] = []
    expansions = lookups = 0
    frontier: list[dict] = []
    if not summary:
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

        def emit(p: str, d: int, a: Agg | None, b: Agg | None, x: bool, l: int | None = None) -> None:
            row = {"p": p, "d": d, "k": (a.kind if a else None) or (b.kind if b else None) or "dir", "s": status(a, b),
                   "a": js_round(a.b) if a else 0, "b": js_round(b.b) if b else 0, "oa": js_round(a.o) if a else 0, "ob": js_round(b.o) if b else 0}
            if x:
                row["x"] = True
            if l:
                row["l"] = l
            rows.append(row)

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
            for it, _, names in plans:
                for cp in names:
                    for side, ix_, v_ in ((1, ixa, va), (2, ixb, vb)):
                        if v_ and cp not in v_.kept:
                            found[(side, cp)], counted = lookup(ix_, v_, cp, scope)
                            lookups += counted
            for it, expand, names in plans:
                p, d, a, b = it["p"], it["d"], it["a"], it["b"]
                if p != path:
                    emit(rel(p), d - dP, a, b, expand, it["l"])
                if not expand:
                    continue
                expansions += 1
                sa, sb = [0, 0], [0, 0]
                for cp in names:
                    ca = (va.kept.get(cp) if va else None) or found.get((1, cp))
                    cb = (vb.kept.get(cp) if vb else None) or found.get((2, cp))
                    lk = 1 if ca and cp not in va.kept else 2 if cb and cp not in vb.kept else None
                    if ca:
                        sa[0] += ca.b
                        sa[1] += ca.o
                    if cb:
                        sb[0] += cb.b
                        sb[1] += cb.o
                    nxt.append({"p": cp, "d": d + 1, "a": ca, "b": cb, "l": lk})
                rest_a = Agg(b=max(0, (a.b if a else 0) - sa[0]), o=max(0, (a.o if a else 0) - sa[1]))
                rest_b = Agg(b=max(0, (b.b if b else 0) - sb[0]), o=max(0, (b.o if b else 0) - sb[1]))
                if rest_a.b > 0 or rest_b.b > 0:
                    key = "(other)" if p == path else f"{rel(p)}/(other)"
                    emit(key, d - dP + 1, rest_a if rest_a.b > 0 else None, rest_b if rest_b.b > 0 else None, False)
            level = nxt
        frontier = sorted((r for r in rows if not r.get("x") and r["s"] != "unchanged"), key=lambda r: -abs(r["b"] - r["a"]))
    skeleton = [r for r in rows if r.get("x")]
    pre = {**head, "rows": skeleton + frontier[:top], **totals}
    s = json.dumps(pre, ensure_ascii=False, separators=(",", ":"))
    yield s[:-1] + ',"matched":'
    yield from _json_list_chunks(_matched_union(ixa, va, ixb, vb))
    tail = {"expansions": expansions, "truncated": len(frontier) > top, "lookups": lookups, "lookups_capped": False}
    yield "," + json.dumps(tail, separators=(",", ":"))[1:]
