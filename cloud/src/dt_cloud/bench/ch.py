"""ClickHouse as a serving engine (specs/serving-options.md benchmark B):
the filter bench's names-first plan in SQL, over one generation exported as
interval-encoded nodes.

**Export** (`export`, from the `mem` index plus its generation's `path`
sort): every node gets `pre` (its rank in a depth-first order) and `post`
(`pre` + its subtree's node count − 1), so "under X" is `X.pre < pre ≤ X.post`
and "has an ancestor in a set" is a running max of `post` in `pre` order —
integer work, no path strings. Files: `nodes-NNNN.parquet` (`pre`, `post`,
`depth`, `nid`, `b`, `o`, `path`) and `names.parquet` (`nid`, `l`: the
lowercase name).

**Tables** (the loader is deployment-side, e.g. `job/serving-exp-ch.sh`):

- `names (nid, l)` `ORDER BY l`, with a text index on `l`;
- `nodes_by_name (nid, pre, post, depth, b, o, path)` `ORDER BY (nid, pre)`:
  a name's nodes are a primary-key range;
- `nodes (pre, post, depth, nid, b, o, path)` `ORDER BY pre`: a subtree is a
  primary-key range (children of a `strict` term's stems).

**Per query** (`truth.view_truth`'s semantics, as `duck.DuckIndex`): the
names passing each term's segment test → their nodes strictly under the
view → full-path `p` / `n` flags (`lowerUTF8(path)`) → roots = outermost of
`p ∧ ¬n`, exclusions = outermost of `n`, each exclusion charged to the root
holding it (one window pass over both, in `pre` order).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .duck import Unsupported
from .query import Ast, Matcher, compile_query, glob_re, pos_everywhere
from .terms import NameTest, regex_name_filter, seg_term

DEFAULT_URL = "http://localhost:8123"
# What a cold answer starts without: ClickHouse's caches, then the OS page cache.
DROP_CACHES = (
    "MARK CACHE", "UNCOMPRESSED CACHE", "INDEX MARK CACHE", "INDEX UNCOMPRESSED CACHE", "QUERY CONDITION CACHE",
    "PRIMARY INDEX CACHE", "TEXT INDEX TOKENS CACHE", "TEXT INDEX HEADER CACHE", "TEXT INDEX POSTINGS CACHE", "PAGE CACHE", "MMAP CACHE",
)

__all__ = ["ChIndex", "Unsupported", "export", "lit"]


def err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def lit(s: str) -> str:
    """A ClickHouse string literal (backslash escapes, unlike DuckDB's)."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def like_lit(s: str, pre: str = "%", post: str = "%") -> str:
    esc = s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return lit(pre + esc + post)


def name_sql(t: NameTest, col: str = "l") -> str:
    """A `terms.NameTest` on a lowercase-name column. Literal tests are
    `LIKE` patterns, which the text index can serve."""
    if t.op == "contains":
        return f"{col} LIKE {like_lit(t.arg)}"
    if t.op == "starts":
        return f"startsWith({col}, {lit(t.arg)})"
    if t.op == "ends":
        return f"endsWith({col}, {lit(t.arg)})"
    if t.op == "equals":
        return f"{col} = {lit(t.arg)}"
    return f"match({col}, {lit(t.arg)})"


def matcher_sql(m: Matcher, path: str = "path", lower: str = "lp") -> str:
    if m.kind == "sub":
        return f"position({lower}, {lit(m.text)}) > 0"
    if m.kind == "glob":
        return f"match({lower}, {lit(glob_re(m.pieces))})"
    return f"match({path}, {lit('(?i)' + m.source)})"


def pos_sql(ast: Ast) -> str:
    if pos_everywhere(ast):
        return "1"
    return "(" + " OR ".join("(" + " AND ".join(matcher_sql(m) for m in a) + ")" for a in ast.alts) + ")"


def neg_sql(ast: Ast) -> str:
    if not ast.neg:
        return "0"
    return "(" + " OR ".join(matcher_sql(m) for m in ast.neg) + ")"


@dataclass
class ChResult:
    hit: bool
    roots: int
    b: int
    o: int
    excluded: int
    stats: dict


class ChIndex:
    """The engine: a ClickHouse session over HTTP (temporary tables per
    answer), `max_threads` = `threads`."""

    def __init__(self, url: str = DEFAULT_URL, *, threads: int = 8, db: str = "default", max_stems: int = 5000, cold: bool = False):
        self.url = url.rstrip("/")
        self.cold = cold
        self.session = uuid.uuid4().hex
        self.settings = {"max_threads": str(threads), "database": db, "session_id": self.session, "session_timeout": "3600"}
        self.max_stems = max_stems
        self._views: dict[str, tuple[int, int, int, int, int]] = {}
        self._hit_view: str | None = None
        t = time.monotonic()
        self.n = int(self.one("SELECT count() FROM nodes")[0])
        sb, so = self.one("SELECT sum(b), sum(o) FROM nodes WHERE depth = 1")
        self._views[""] = (-1, self.n - 1, 0, int(sb), int(so))
        self.stats = {"init_s": round(time.monotonic() - t, 2), "nodes": self.n, "url": self.url}

    # transport

    def exec(self, sql: str, fmt: str = "TSV") -> str:
        q = sql.strip().rstrip(";")
        if fmt and q.split(None, 1)[0].upper() in ("SELECT", "WITH"):
            q += f" FORMAT {fmt}"
        req = urllib.request.Request(f"{self.url}/?{urllib.parse.urlencode(self.settings)}", data=q.encode(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"ClickHouse {e.code}: {e.read().decode()[:2000]}\n--- {q[:2000]}") from None

    def rows(self, sql: str) -> list[list[str]]:
        out = self.exec(sql)
        return [line.split("\t") for line in out.splitlines()]

    def one(self, sql: str) -> list[str]:
        r = self.rows(sql)
        return r[0] if r else []

    def tmp(self, name: str, sql: str) -> None:
        self.exec(f"DROP TEMPORARY TABLE IF EXISTS {name}")
        self.exec(f"CREATE TEMPORARY TABLE {name} ENGINE = Memory AS {sql}")

    def prepare(self) -> None:
        """Before each timed answer: when `cold`, drop ClickHouse's caches and
        the OS page cache (needs a privileged container / root)."""
        if not self.cold:
            return
        for c in DROP_CACHES:
            self.exec(f"SYSTEM DROP {c}")
        import os

        os.sync()
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("3\n")

    # views

    def view(self, view: str) -> tuple[int, int, int, int, int]:
        """(pre, post, depth, b, o) of a view root; the store root is
        (−1, n−1, 0, Σ buckets)."""
        if view not in self._views:
            name = view.rsplit("/", 1)[-1].lower()
            r = self.rows(f"""SELECT pre, post, depth, b, o FROM nodes_by_name
                WHERE nid IN (SELECT nid FROM names WHERE l = {lit(name)}) AND depth = {view.count('/') + 1} AND path = {lit(view)}""")
            if len(r) != 1:
                raise KeyError(f"view {view!r}: {len(r)} nodes")
            self._views[view] = tuple(int(x) for x in r[0])  # type: ignore[assignment]
        return self._views[view]

    # evaluation

    def _cands(self, cond: str, flags: str, lo: int, hi: int) -> None:
        self.tmp("cn", f"SELECT nid FROM names WHERE {cond}")
        self.tmp("cr", f"""SELECT pre, post, depth, b, o, path, {flags} FROM (
            SELECT pre, post, depth, b, o, path, lowerUTF8(path) AS lp FROM nodes_by_name
            WHERE nid IN (SELECT nid FROM cn) AND pre > {lo} AND pre <= {hi})""")

    def evaluate(self, ast: Ast, view: str) -> ChResult:
        t0 = time.monotonic()
        stats: dict = {}
        vpre, vpost, dv, vb, vo = self.view(view)
        regex = [m for a in ast.alts for m in a if m.kind == "regex"] + [m for m in ast.neg if m.kind == "regex"]
        if regex:
            if len(ast.alts) != 1 or len(ast.alts[0]) != 1 or ast.neg:
                raise Unsupported("a regex mixed with other terms")
            src = regex[0].source
            plan = regex_name_filter(src)
            flags = f"match(path, {lit('(?i)' + src)}) AS p, 0 AS n"
            if plan is None:
                stats["map"] = "full-scan"
                self.tmp("cr", f"""SELECT pre, post, depth, b, o, path, {flags} FROM nodes
                    WHERE pre > {vpre} AND pre <= {vpost} AND match(path, {lit('(?i)' + src)})""")
            else:
                self._cands(f"match(l, {lit('(?i)' + plan.name_re)})", flags, vpre, vpost)
        else:
            matchers = list(dict.fromkeys([m for a in ast.alts for m in a] + list(ast.neg)))
            terms = {m: seg_term(m) for m in matchers}
            if any(t.trivial for t in terms.values()):
                raise Unsupported("a term that constrains no segment")
            flags = f"{pos_sql(ast)} AS p, {neg_sql(ast)} AS n"
            self._cands("(" + " OR ".join(name_sql(t.name) for t in terms.values()) + ")", flags, vpre, vpost)
            strict = [t for t in terms.values() if t.strict]
            if strict:
                import re

                cond = " OR ".join(f"match(lowerUTF8(path), {lit(t.suffix_re)})" for t in strict)
                stems = [tuple(int(x) for x in r) for r in self.rows(f"SELECT DISTINCT pre, post, depth FROM cr WHERE {cond}")]
                if view and any(re.search(t.suffix_re, view.lower()) for t in strict):
                    stems.append((vpre, vpost, dv))
                stats["stems"] = len(stems)
                if len(stems) > self.max_stems:
                    raise Unsupported(f"{len(stems)} stems")
                if stems:
                    rng = " OR ".join(f"(depth = {d + 1} AND pre > {a} AND pre <= {b})" for a, b, d in stems)
                    self.exec(f"""INSERT INTO cr SELECT pre, post, depth, b, o, path, {flags} FROM (
                        SELECT pre, post, depth, b, o, path, lowerUTF8(path) AS lp FROM nodes WHERE {rng})""")
        stats["cands_s"] = round(time.monotonic() - t0, 3)
        hit = bool(compile_query(ast)(view))
        # A node is in `cr` twice only when a stem's child also passed a name test.
        src = "(SELECT pre, any(post) AS post, any(b) AS b, any(o) AS o, any(p) AS p, any(n) AS n FROM cr GROUP BY pre)" if stats.get("stems") else "cr"
        outer = """SELECT pre, post, b, o FROM (
            SELECT pre, post, b, o, max(toNullable(toInt64(post))) OVER (ORDER BY pre ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS mp
            FROM {src} WHERE {cond}) WHERE coalesce(mp, -1) < pre"""
        t1 = time.monotonic()
        self._hit_view = view if hit else None
        if hit:
            self.tmp("roots", f"SELECT toUInt32({max(vpre, 0)}) AS pre, toUInt32({vpost}) AS post, toInt64({vb}) AS b, toInt64({vo}) AS o")
        else:
            self.tmp("roots", outer.format(src=src, cond="p AND NOT n"))
        stats["roots_s"] = round(time.monotonic() - t1, 3)
        n, rb, ro = (int(x) for x in self.one("SELECT count(), sum(b), sum(o) FROM roots"))
        ne = eb = eo = 0
        if ast.neg:
            t1 = time.monotonic()
            self.tmp("ex", outer.format(src=src, cond="n"))
            if hit:
                # Every exclusion is strictly under the view, the one root.
                ne, eb, eo = (int(x) for x in self.one("SELECT count(), sum(b), sum(o) FROM ex"))
            else:
                ne, eb, eo = (int(x) for x in self.one("""SELECT countIf(cov), sumIf(b, cov), sumIf(o, cov) FROM (
                    SELECT b, o, r, coalesce(rp, -1) >= pre AND NOT r AS cov FROM (
                        SELECT pre, b, o, r, max(if(r, toNullable(toInt64(post)), NULL)) OVER (ORDER BY pre, r DESC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS rp
                        FROM (SELECT pre, post, 0 AS b, 0 AS o, 1 AS r FROM roots UNION ALL SELECT pre, post, b, o, 0 AS r FROM ex)))"""))
            stats["ex_s"] = round(time.monotonic() - t1, 3)
        stats["s"] = round(time.monotonic() - t0, 4)
        return ChResult(hit, n, rb - eb, ro - eo, ne, stats)

    def roots_summary(self, list_max: int) -> tuple[list[str] | None, int, str | None]:
        """The last answer's roots: (sorted list, n, None) up to `list_max`,
        else (None, n, md5 of the sorted, newline-joined list)."""
        if self._hit_view is not None:
            return [self._hit_view], 1, None
        paths = "(SELECT any(path) AS path FROM cr WHERE pre IN (SELECT pre FROM roots) GROUP BY pre)"
        n = int(self.one("SELECT count() FROM roots")[0])
        if n <= list_max:
            out = self.exec(f"SELECT path FROM {paths} ORDER BY path", fmt="JSONCompact")
            return [r[0] for r in json.loads(out)["data"]], n, None
        md5 = self.one(f"SELECT lower(hex(MD5(arrayStringConcat(arraySort(groupArray(path)), '\\n')))) FROM {paths}")[0]
        return None, n, md5


# --- export: the mem index as interval-encoded nodes ---------------------------------


def intervals(parent: np.ndarray, depth: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """`(pre, post)` per node for a forest given in depth order (ids sorted
    by `depth`, buckets at depth 1, `parent` −1 for them): subtree sizes
    bottom-up, then each node's `pre` = its parent's + 1 + the sizes of the
    siblings before it."""
    n = len(parent)
    dmax = int(depth.max())
    lo = np.searchsorted(depth, np.arange(1, dmax + 2)).astype(np.int64)  # lo[d-1] = first id at depth d
    size = np.ones(n, np.int64)
    for d in range(dmax, 1, -1):
        a, b = lo[d - 1], lo[d]
        pa, pb = lo[d - 2], lo[d - 1]
        size[pa:pb] += np.bincount(parent[a:b] - pa, weights=size[a:b], minlength=pb - pa).astype(np.int64)
    pre = np.empty(n, np.int64)
    s1 = size[lo[0] : lo[1]]
    pre[lo[0] : lo[1]] = np.cumsum(s1) - s1
    for d in range(2, dmax + 1):
        a, b = lo[d - 1], lo[d]
        p = parent[a:b]
        order = np.argsort(p, kind="stable")
        sp, ss = p[order], size[a:b][order]
        cs = np.cumsum(ss) - ss
        start = np.ones(len(sp), bool)
        start[1:] = sp[1:] != sp[:-1]
        first = np.maximum.accumulate(np.where(start, np.arange(len(sp)), 0))
        pre[a + order] = pre[sp] + 1 + (cs - cs[first])
    seen = np.zeros(n, bool)
    seen[pre] = True
    if not seen.all() or pre.max() != n - 1:
        raise ValueError("pre isn't a permutation of 0…n−1")
    return pre, pre + size - 1


def export(index: Path, path_file: str, out: Path, rows_per_file: int = 1 << 25) -> dict:
    """Write `nodes-NNNN.parquet` (in id order) and `names.parquet` from a
    `mem` index dir and the generation's `path` sort it was built from (a
    node is a run of equal `path`s in the sort: its owner slices)."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.ipc as ipc
    import pyarrow.parquet as pq

    out.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    st: dict = {}
    parent = np.load(index / "parent.npy")
    depth = np.load(index / "depth.npy")
    pre, post = intervals(parent, depth)
    del parent
    st["intervals_s"] = round(time.monotonic() - t0, 1)
    err(f"export: intervals {st['intervals_s']}s")
    with pa.OSFile(str(index / "vocab.arrow")) as f:
        lower = ipc.open_file(f).read_all().column("l")
    pq.write_table(pa.table({"nid": pa.array(np.arange(len(lower), dtype=np.uint32)), "l": lower}), out / "names.parquet", row_group_size=1 << 20)
    del lower
    nid = np.load(index / "nid.npy")
    b = np.load(index / "b.npy")
    o = np.load(index / "o.npy")
    n = len(nid)
    pf = pq.ParquetFile(path_file)
    i = 0
    prev = None
    part: list = []
    k = 0

    def flush() -> None:
        nonlocal part, k
        if not part:
            return
        paths = pa.concat_arrays(part)
        a, z = i - len(paths), i
        tbl = pa.table({
            "pre": pa.array(pre[a:z].astype(np.uint32)), "post": pa.array(post[a:z].astype(np.uint32)),
            "depth": pa.array(depth[a:z]), "nid": pa.array(nid[a:z].astype(np.uint32)),
            "b": pa.array(b[a:z]), "o": pa.array(o[a:z].astype(np.int64)), "path": paths,
        })
        # The path's segment count must be its node's depth (catches any drift between the sort and the ids).
        segs = pc.add(pc.count_substring(tbl["path"], "/"), 1)
        if not pc.all(pc.equal(pc.cast(segs, pa.int64()), pc.cast(tbl["depth"], pa.int64()))).as_py():
            raise ValueError(f"depth mismatch in ids {a}…{z}")
        pq.write_table(tbl, out / f"nodes-{k:04d}.parquet", row_group_size=1 << 20)
        err(f"export: nodes-{k:04d} ids {a}…{z} ({round(time.monotonic() - t0)}s)")
        k += 1
        part = []

    run = 0
    for batch in pf.iter_batches(columns=["path"], batch_size=1 << 21):
        p = batch.column(0)
        if prev is None:
            keep = np.ones(len(p), bool)
        else:
            keep = np.empty(len(p), bool)
            keep[0] = p[0].as_py() != prev
        if len(p) > 1:
            keep[1:] = pc.not_equal(p.slice(1), p.slice(0, len(p) - 1)).to_numpy(zero_copy_only=False)
        prev = p[len(p) - 1].as_py()
        kept = p.filter(pa.array(keep))
        part.append(kept.cast(pa.large_string()))
        i += len(kept)
        run += len(kept)
        if run >= rows_per_file:
            flush()
            run = 0
    flush()
    if i != n:
        raise ValueError(f"{i} distinct paths in the sort, {n} nodes in the index")
    st.update(nodes=n, files=k, s=round(time.monotonic() - t0, 1))
    (out / "export.json").write_text(json.dumps(st))
    return st
