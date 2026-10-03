"""The serving box's custom in-memory index (specs/filter-query-service.md
§4.2 engine 2): a generation's tree as id arrays, its vocabulary as one
concatenated string blob, every filter answered without touching a `path`
string.

**Layout** (`build` writes it, `MemIndex.load` reads it; one `.npy` per
array, the vocabulary as an Arrow IPC file):

- nodes — every distinct path of the `path` sort (objects and dirs, owner
  slices summed), in the sort's `(depth, path)` order, so the buckets are
  ids `0 … n_top − 1`: `parent` (int32, −1 for a bucket), `nid` (int32,
  the last segment's vocabulary id), `depth` (uint8, a bucket is 1), `b` /
  `o` (int64 bytes, objects);
- `name_nodes` / `name_off` — name id → its nodes (CSR);
- `child` / `child_off` — node → its children, largest first (CSR; the
  drawing walk's order), and `top`, the buckets largest first;
- `vocab.arrow` — `name` (original case, for display) and `l` (lowercase,
  for matching), `large_string`, one contiguous array each, row = id.

**Why numpy + Arrow, not Rust:** the vocabulary scan is Arrow's compute
kernels (substring search, RE2) over zero-copy slices of the one blob, in
parallel threads (the kernels release the GIL); everything after it is a
handful of vectorized gathers over candidate ids, bounded by tree depth. Both
are memory-bandwidth-bound C loops already, the query AST and its semantics
stay the one Python port the ground truth uses (`query`), and the box ships
as the `dt-cloud` image (§4.4) with no second toolchain. A compiled core is
the lever if the gathers ever dominate (they don't: §6.2).

**Evaluation** (`terms`): each substring / glob matcher's *start set* —
nodes whose last `k + 1` segments hold it, prefiltered by a test on the
vocabulary — then, per candidate, whether it holds anywhere on the path (an
ancestor walk over a bitmap of the start set). Roots, outermost excluded
paths and net totals follow `truth.view_truth` exactly, with node ids for
paths.
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .query import Ast, Matcher, compile_query, lit, pos_everywhere
from .terms import NameTest, regex_name_filter, seg_term

ARRAYS = ("parent", "nid", "depth", "b", "o", "name_nodes", "name_off", "child", "child_off", "top")
VOCAB = "vocab.arrow"
META = "meta.json"


def err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


class Unsupported(ValueError):
    """A query this engine can't answer exactly (an unplannable regex, a term
    whose last segment constrains nothing)."""


# --- build ---------------------------------------------------------------------------


def build(path_file: str, names_file: str, out: Path, *, threads: int = 16, mem: str = "90GB", tmp: str | None = None) -> dict:
    """Build the index from a generation's `path` sort and v1 names file
    (local paths). DuckDB does the string work once (path hashes, the
    name join, the parent join, the sorts); the result is plain arrays.

    A node is the rows (owner slices) of one path, keyed by the md5 of the
    path within its depth; a second, independent hash must agree across a
    key's rows, so a collision is raised, never merged silently."""
    import duckdb
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.ipc as ipc

    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET threads = {threads}")
    con.execute(f"SET memory_limit = '{mem}'")
    con.execute("SET preserve_insertion_order = false")
    if tmp:
        con.execute(f"SET temp_directory = '{tmp}'")
    steps: dict[str, float] = {}

    def step(name: str, t0: float) -> None:
        steps[name] = round(time.monotonic() - t0, 2)
        err(f"build: {name} {steps[name]}s")

    t = time.monotonic()
    con.execute(f"CREATE TABLE vocab AS SELECT id, name FROM read_parquet({lit(names_file)})")
    V, id_min, id_max = con.execute("SELECT count(*), min(id), max(id) FROM vocab").fetchone()
    if V and (id_min != 0 or id_max != V - 1):
        raise ValueError(f"names ids aren't 0…{V - 1}: [{id_min}, {id_max}]")
    names = con.execute("SELECT name FROM vocab ORDER BY id").to_arrow_table().column(0).cast(pa.large_string()).combine_chunks()
    lower = pc.utf8_lower(names)
    with ipc.new_file(str(out / VOCAB), pa.schema([("name", pa.large_string()), ("l", pa.large_string())])) as w:
        w.write_table(pa.table({"name": names, "l": lower}))
    del names, lower
    step("vocab", t)

    t = time.monotonic()
    src = f"read_parquet({lit(path_file)}, file_row_number = true)"
    con.execute(f"""CREATE TABLE r AS
        SELECT p.file_row_number AS rn, md5_number(p.path) AS h, hash(p.path) AS h2, md5_number(regexp_extract(p.path, '^(.*)/', 1)) AS ph,
               v.id AS nid, p.depth::UTINYINT AS depth, coalesce(p.size, 0) AS size, coalesce(p.n_files, 0) AS n_files
        FROM {src} p LEFT JOIN vocab v ON v.name = regexp_extract(p.path, '[^/]*$')""")
    con.execute("DROP TABLE vocab")
    R = con.execute("SELECT count(*) FROM r").fetchone()[0]
    step("rows", t)

    t = time.monotonic()
    # A node is keyed by the 128-bit md5 of its path. DuckDB's 64-bit `hash`
    # is not that key: on gcs 2026-10-01 it merged ~690K distinct paths (the
    # first build's "split" nodes), e.g. sibling `model-0000{1,3}-of-00004.*`
    # files. It stays as an independent check: two paths sharing an md5
    # would differ in it. `split` counts keys whose rows aren't adjacent in
    # the `(depth, path, usr)` sort (0 unless something merged).
    con.execute("""CREATE TABLE n AS
        SELECT min(rn) AS f, max(rn) - min(rn) + 1 - count(*) AS gap, min(h2) != max(h2) AS clash, depth, h, any_value(ph) AS ph,
               coalesce(any_value(nid), -1)::INTEGER AS nid, sum(size)::BIGINT AS b, sum(n_files)::BIGINT AS o
        FROM r GROUP BY depth, h""")
    con.execute("DROP TABLE r")
    clashes, split = con.execute("SELECT count(*) FILTER (clash), count(*) FILTER (gap != 0) FROM n").fetchone()
    if clashes:
        raise ValueError(f"{clashes} path-hash collisions; rebuild with a wider key")
    con.execute("CREATE TABLE n2 AS SELECT (row_number() OVER (ORDER BY f) - 1)::INTEGER AS id, depth, h, ph, nid, b, o FROM n")
    con.execute("DROP TABLE n")
    N, no_name = con.execute("SELECT count(*), count(*) FILTER (nid < 0) FROM n2").fetchone()
    step("nodes", t)

    t = time.monotonic()
    # A pure equi-join (a non-key conjunct in the ON clause turns it into a
    # nested-loop join): a bucket's `ph` (the hash of '') matches no node.
    con.execute("""CREATE TABLE par AS
        SELECT c.id, coalesce(p.id, -1)::INTEGER AS parent, c.depth
        FROM n2 c LEFT JOIN (SELECT id, depth + 1 AS cd, h FROM n2) p ON p.cd = c.depth AND p.h = c.ph""")
    n_par, orphans = con.execute("SELECT count(*), count(*) FILTER (parent < 0 AND depth > 1) FROM par").fetchone()
    if n_par != N or orphans:
        raise ValueError(f"parent join: {n_par} rows for {N} nodes, {orphans} orphans")
    step("parents", t)

    t = time.monotonic()

    def col(sql: str, dtype) -> np.ndarray:
        a = con.execute(sql).to_arrow_table().column(0).to_numpy()
        return np.ascontiguousarray(a, dtype=dtype)

    def save(name: str, a: np.ndarray) -> None:
        np.save(out / f"{name}.npy", a)

    save("parent", col("SELECT parent FROM par ORDER BY id", np.int32))
    nid = col("SELECT nid FROM n2 ORDER BY id", np.int32)
    save("nid", nid)
    save("depth", col("SELECT depth FROM n2 ORDER BY id", np.uint8))
    save("b", col("SELECT b FROM n2 ORDER BY id", np.int64))
    o = col("SELECT o FROM n2 ORDER BY id", np.int64)
    if o.max(initial=0) >= 2**31:
        raise ValueError("an object count past int32")
    save("o", o.astype(np.int32))
    del o
    save("name_nodes", col("SELECT id FROM n2 WHERE nid >= 0 ORDER BY nid, id", np.int32))
    save("name_off", np.concatenate([[0], np.cumsum(np.bincount(nid[nid >= 0], minlength=V))]).astype(np.int32))
    del nid
    con.execute("CREATE TABLE pb AS SELECT par.id, par.parent, n2.b FROM par JOIN n2 USING (id)")
    con.execute("DROP TABLE n2")
    con.execute("DROP TABLE par")
    save("child", col("SELECT id FROM pb WHERE parent >= 0 ORDER BY parent, b DESC, id", np.int32))
    par = np.load(out / "parent.npy")
    save("child_off", np.concatenate([[0], np.cumsum(np.bincount(par[par >= 0], minlength=N))]).astype(np.int32))
    del par
    save("top", col("SELECT id FROM pb WHERE parent < 0 ORDER BY b DESC, id", np.int32))
    con.close()
    step("arrays", t)

    meta = {
        "v": 1, "path_file": path_file, "names_file": names_file, "rows": int(R), "nodes": int(N), "names": int(V),
        "no_name": int(no_name), "split_nodes": int(split), "steps": steps, "bytes": {f.name: f.stat().st_size for f in sorted(out.iterdir())},
    }
    (out / META).write_text(json.dumps(meta, indent=1))
    return meta


# --- the index -----------------------------------------------------------------------


@dataclass
class MemIndex:
    parent: np.ndarray
    nid: np.ndarray
    depth: np.ndarray
    b: np.ndarray
    o: np.ndarray
    name_nodes: np.ndarray
    name_off: np.ndarray
    child: np.ndarray
    child_off: np.ndarray
    top: np.ndarray
    names: object  # pa.LargeStringArray, original case
    lower: object  # pa.LargeStringArray, lowercase
    threads: int = 16
    chunk: int = 1 << 20
    _found: dict = field(default_factory=dict)

    @classmethod
    def load(cls, d: Path, threads: int = 16) -> "MemIndex":
        """Every array read into memory (not mapped: a query must never page)."""
        import pyarrow as pa
        import pyarrow.ipc as ipc

        arrays = {k: np.load(d / f"{k}.npy") for k in ARRAYS}
        with pa.OSFile(str(d / VOCAB)) as f:
            t = ipc.open_file(f).read_all()
        # One record batch (as `build` writes it): its arrays as is, no copy.
        one = lambda c: c.chunk(0) if c.num_chunks == 1 else c.combine_chunks()  # noqa: E731
        names, lower = one(t.column("name")), one(t.column("l"))
        return cls(**arrays, names=names, lower=lower, threads=threads)

    @property
    def n(self) -> int:
        return len(self.parent)

    def nbytes(self) -> dict[str, int]:
        out = {k: int(getattr(self, k).nbytes) for k in ARRAYS}
        out["vocab"] = int(self.names.nbytes + self.lower.nbytes)
        return out

    def case_exceptions(self) -> dict[str, int]:
        """Names whose lowercase differs (what a lowercase-only vocabulary
        plus an exceptions map would keep of `names`): count and bytes."""
        import pyarrow.compute as pc

        diff = pc.not_equal(self.names, self.lower)
        ex = self.names.filter(diff)
        return {"n": len(ex), "bytes": int(pc.sum(pc.binary_length(ex)).as_py() or 0)}

    # vocabulary

    def scan(self, t: NameTest, ignore_case: bool = False) -> np.ndarray:
        """Name ids passing `t` (lowercase names), sorted; parallel over
        zero-copy slices of the blob."""
        import pyarrow as pa
        import pyarrow.compute as pc

        def one(lo: int) -> np.ndarray:
            a = self.lower.slice(lo, self.chunk)
            if t.op == "contains":
                m = pc.match_substring(a, t.arg, ignore_case=ignore_case)
            elif t.op == "starts":
                m = pc.starts_with(a, t.arg, ignore_case=ignore_case)
            elif t.op == "ends":
                m = pc.ends_with(a, t.arg, ignore_case=ignore_case)
            elif t.op == "equals":
                m = pc.equal(a, pa.scalar(t.arg, pa.large_string()))
            else:
                m = pc.match_substring_regex(a, t.arg, ignore_case=ignore_case)
            return np.flatnonzero(m.to_numpy(zero_copy_only=False)) + lo

        V = len(self.lower)
        starts = range(0, V, self.chunk)
        with ThreadPoolExecutor(self.threads) as ex:
            parts = list(ex.map(one, starts))
        return np.concatenate(parts).astype(np.int32) if parts else np.zeros(0, np.int32)

    # tree

    @staticmethod
    def _csr(off: np.ndarray, vals: np.ndarray, ids: np.ndarray) -> np.ndarray:
        s = off[ids].astype(np.int64)
        lens = off[ids + 1].astype(np.int64) - s
        tot = int(lens.sum())
        if not tot:
            return np.zeros(0, vals.dtype)
        base = np.repeat(s - (np.cumsum(lens) - lens), lens)
        return vals[base + np.arange(tot)]

    def nodes_of(self, name_ids: np.ndarray) -> np.ndarray:
        return self._csr(self.name_off, self.name_nodes, name_ids.astype(np.int64))

    def children(self, nodes: np.ndarray) -> np.ndarray:
        return self._csr(self.child_off, self.child, nodes.astype(np.int64))

    def up(self, cur: np.ndarray) -> np.ndarray:
        out = np.full(len(cur), -1, np.int32)
        m = cur >= 0
        out[m] = self.parent[cur[m]]
        return out

    def segments(self, nodes: np.ndarray, k: int | None, lower: bool):
        """Each node's last `k + 1` segments (all, for None) joined by `/`."""
        import pyarrow as pa
        import pyarrow.compute as pc

        src = self.lower if lower else self.names
        cols = []
        cur = nodes.astype(np.int32)
        i = 0
        while (k is None or i <= k) and (cur >= 0).any():
            ids = np.where(cur >= 0, self.nid[np.maximum(cur, 0)], -1)
            cols.append(src.take(pa.array(ids, mask=ids < 0)))
            cur = self.up(cur)
            i += 1
        if not cols:
            return pa.array([""] * len(nodes), pa.large_string())
        if len(cols) == 1:
            return cols[0]
        return pc.binary_join_element_wise(*reversed(cols), pa.scalar("/", pa.large_string()), null_handling="skip")

    def paths_arrow(self, nodes: np.ndarray, sort: bool = False, chunk: int = 1 << 21):
        """Full paths (original case) as one Arrow array, optionally sorted
        (bytewise), built `chunk` nodes at a time."""
        import pyarrow as pa
        import pyarrow.compute as pc

        if not len(nodes):
            return pa.array([], pa.large_string())
        parts = [self.segments(nodes[i : i + chunk], None, lower=False) for i in range(0, len(nodes), chunk)]
        a = pa.concat_arrays(parts) if len(parts) > 1 else parts[0]
        if sort:
            a = a.take(pc.array_sort_indices(a))
        return a

    def paths(self, nodes: np.ndarray, sort: bool = False) -> list[str]:
        return self.paths_arrow(np.asarray(nodes), sort).to_pylist()

    def holds(self, mark: np.ndarray, x: np.ndarray, strict: bool = False) -> np.ndarray:
        """Per node of `x`: some ancestor-or-self (strict: ancestor) is marked."""
        acc = np.zeros(len(x), bool)
        cur = x.astype(np.int32)
        idx = np.arange(len(x))
        if not strict:
            hit = mark[cur]
            acc[hit] = True
            cur, idx = cur[~hit], idx[~hit]
        while len(cur):
            cur = self.parent[cur]
            keep = cur >= 0
            cur, idx = cur[keep], idx[keep]
            hit = mark[cur]
            acc[idx[hit]] = True
            cur, idx = cur[~hit], idx[~hit]
        return acc

    def outermost(self, mark: np.ndarray, x: np.ndarray, dv: int) -> np.ndarray:
        """The nodes of `x` with no marked strict ancestor deeper than `dv`."""
        bad = np.zeros(len(x), bool)
        cur = x.astype(np.int32)
        idx = np.arange(len(x))
        while len(cur):
            cur = self.parent[cur]
            keep = cur >= 0
            cur, idx = cur[keep], idx[keep]
            keep = self.depth[cur] > dv
            cur, idx = cur[keep], idx[keep]
            hit = mark[cur]
            bad[idx[hit]] = True
            cur, idx = cur[~hit], idx[~hit]
        return x[~bad]

    def nearest(self, mark: np.ndarray, x: np.ndarray, dv: int) -> np.ndarray:
        """Per node of `x`: its nearest marked strict ancestor at depth ≥ `dv`
        (−1 if none)."""
        out = np.full(len(x), -1, np.int64)
        cur = x.astype(np.int32)
        idx = np.arange(len(x))
        while len(cur):
            cur = self.parent[cur]
            keep = cur >= 0
            cur, idx = cur[keep], idx[keep]
            keep = self.depth[cur] >= dv
            cur, idx = cur[keep], idx[keep]
            hit = mark[cur]
            out[idx[hit]] = cur[hit]
            cur, idx = cur[~hit], idx[~hit]
        return out

    def under(self, x: np.ndarray, v: int, dv: int) -> np.ndarray:
        """Mask: strictly under node `v` (depth `dv`; v = −1: the store root)."""
        if v < 0:
            return np.ones(len(x), bool)
        cur = x.astype(np.int32).copy()
        while True:
            m = self.depth[cur] > dv
            if not m.any():
                break
            cur[m] = self.parent[cur[m]]
        return (self.depth[x] > dv) & (cur == v)

    def find(self, path: str) -> int:
        """A path's node id (−1: the store root); KeyError if absent."""
        if path == "":
            return -1
        if path in self._found:
            return self._found[path]
        import pyarrow as pa
        import pyarrow.compute as pc

        cur = -1
        for seg in path.split("/"):
            V = len(self.names)
            ids = np.concatenate([
                np.flatnonzero(pc.equal(self.names.slice(lo, self.chunk), pa.scalar(seg, pa.large_string())).to_numpy(zero_copy_only=False)) + lo
                for lo in range(0, V, self.chunk)
            ])
            nodes = self.nodes_of(ids.astype(np.int32))
            nodes = nodes[self.parent[nodes] == cur]
            if len(nodes) != 1:
                raise KeyError(path)
            cur = int(nodes[0])
        self._found[path] = cur
        return cur

    def mark(self, nodes: np.ndarray) -> np.ndarray:
        m = np.zeros(self.n, bool)
        m[nodes] = True
        return m

    # matchers

    def start_set(self, m: Matcher) -> tuple[np.ndarray, bool]:
        """(nodes whose last k+1 segments hold `m`, strict)."""
        st = seg_term(m)
        if st.trivial:
            raise Unsupported(f"term {m} constrains no segment")
        nodes = self.nodes_of(self.scan(st.name))
        if st.k and len(nodes):
            import pyarrow.compute as pc

            ok = pc.match_substring_regex(self.segments(nodes, st.k, lower=True), st.suffix_re).to_numpy(zero_copy_only=False)
            nodes = nodes[ok]
        return nodes, st.strict

    def regex_set(self, source: str) -> np.ndarray:
        import pyarrow.compute as pc

        plan = regex_name_filter(source)
        if plan is None:
            raise Unsupported(f"regex {source!r}: no name filter (its tail can cross a `/` or isn't `$`-anchored)")
        nodes = self.nodes_of(self.scan(NameTest("regex", plan.name_re), ignore_case=True))
        if not len(nodes):
            return nodes
        ok = pc.match_substring_regex(self.segments(nodes, None, lower=False), source, ignore_case=True).to_numpy(zero_copy_only=False)
        return nodes[ok]


@dataclass
class MemResult:
    hit: bool
    roots: np.ndarray  # node ids (hit: [v])
    root_b: np.ndarray  # net per root
    root_o: np.ndarray
    excluded: np.ndarray
    b: int
    o: int
    stats: dict


def evaluate(ix: MemIndex, ast: Ast, view: str, v: int | None = None) -> MemResult:
    """One view's answer (`truth.view_truth`'s semantics on node ids)."""
    t0 = time.monotonic()
    v = ix.find(view) if v is None else v
    dv = 0 if v < 0 else int(ix.depth[v])
    hit = compile_query(ast)(view)
    regex = [m for a in ast.alts for m in a if m.kind == "regex"] + [m for m in ast.neg if m.kind == "regex"]
    stats: dict = {}
    if regex:
        if len(ast.alts) != 1 or len(ast.alts[0]) != 1 or ast.neg:
            raise Unsupported("a regex mixed with other terms")
        B = ix.regex_set(regex[0].source)
        stats["start"] = int(len(B))
        x = B[ix.under(B, v, dv)]
        posx, negx = np.ones(len(x), bool), np.zeros(len(x), bool)
    else:
        matchers = list(dict.fromkeys([m for a in ast.alts for m in a] + list(ast.neg)))
        start: dict[Matcher, tuple[np.ndarray, bool]] = {m: ix.start_set(m) for m in matchers}
        stats["start"] = {str(m.text or m.pieces): int(len(start[m][0])) for m in matchers}
        cand = []
        for m in matchers:
            s, strict = start[m]
            cand.append(ix.children(s) if strict else s)
        x = np.unique(np.concatenate(cand)) if cand else np.zeros(0, np.int32)
        x = x[ix.under(x, v, dv)]
        held: dict[Matcher, np.ndarray] = {}
        for m in matchers:
            s, strict = start[m]
            held[m] = ix.holds(ix.mark(s), x, strict)
        if pos_everywhere(ast):
            posx = np.ones(len(x), bool)
        else:
            posx = np.zeros(len(x), bool)
            for a in ast.alts:
                acc = np.ones(len(x), bool)
                for m in a:
                    acc &= held[m]
                posx |= acc
        negx = np.zeros(len(x), bool)
        for m in ast.neg:
            negx |= held[m]
    stats["cands"] = int(len(x))
    if hit:
        roots = np.array([v], np.int64)
        rb = np.array([ix.b[ix.top].sum() if v < 0 else ix.b[v]], np.int64)
        ro = np.array([ix.o[ix.top].sum() if v < 0 else ix.o[v]], np.int64)
    else:
        s = x[posx & ~negx]
        roots = ix.outermost(ix.mark(s), s, dv).astype(np.int64)
        rb, ro = ix.b[roots].astype(np.int64), ix.o[roots].astype(np.int64)
    e0 = x[negx]
    if len(e0):
        e = ix.outermost(ix.mark(e0), e0, dv)
        if hit:
            r = np.full(len(e), v, np.int64)
            ok = np.ones(len(e), bool)
        else:
            r = ix.nearest(ix.mark(roots), e, dv)
            ok = r >= 0
        e, r = e[ok], r[ok]
        if hit:
            pos = np.zeros(len(r), np.int64)
        else:
            order = np.argsort(roots)
            roots, rb, ro = roots[order], rb[order], ro[order]
            pos = np.searchsorted(roots, r)
        rb = rb - _sum_at(pos, ix.b[e], len(roots))
        ro = ro - _sum_at(pos, ix.o[e], len(roots))
    else:
        e = np.zeros(0, np.int32)
    stats["s"] = round(time.monotonic() - t0, 4)
    return MemResult(bool(hit), roots, rb, ro, e, int(rb.sum()), int(ro.sum()), stats)


def _sum_at(pos: np.ndarray, vals: np.ndarray, n: int) -> np.ndarray:
    out = np.zeros(n, np.int64)
    np.add.at(out, pos, vals.astype(np.int64))
    return out


def rss() -> dict[str, int]:
    """This process's current and peak resident set (bytes; Linux)."""
    out = {}
    try:
        with open("/proc/self/status") as f:
            for line in f:
                k, _, v = line.partition(":")
                if k in ("VmRSS", "VmHWM"):
                    out[k] = int(v.split()[0]) * 1024
    except FileNotFoundError:
        import resource

        out["ru_maxrss"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return out


def evict(d: Path) -> None:
    """Drop a dir's files from the page cache (so the next read is from disk)."""
    for f in d.iterdir():
        if f.is_file():
            fd = os.open(f, os.O_RDONLY)
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            except (AttributeError, OSError):
                pass
            finally:
                os.close(fd)
