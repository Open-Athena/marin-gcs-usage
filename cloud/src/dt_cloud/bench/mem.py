"""The serving box's custom in-memory index (specs/filter-query-service.md
§4.2 engine 2, slimmed in §6.3): a generation's tree as id arrays, its
vocabulary as one concatenated lowercase blob, every filter answered without
touching a `path` string.

**Layout, format 2** (`build` writes it, `MemIndex.load` reads it; one `.npy`
per array, so a load is a read or an `mmap` of each file as is):

- nodes — every distinct path of the `path` sort (objects and dirs, owner
  slices summed), numbered breadth-first: by depth, then by parent, then
  largest first (ties by path). So a node's children are one contiguous id
  range, already in the drawing walk's order, and `parent` is non-decreasing:
  a node's children are found by binary search on it (no child arrays), and a
  depth is an id range (`dstart`, no depth array). The buckets are ids
  `0 … n_top − 1`, largest first.
  - `parent` (int32, −1 for a bucket), `nid` (int32, the last segment's
    vocabulary id);
  - `b32` / `o8` — bytes and objects narrowed to uint32 / uint8, the
    all-ones value meaning "see the overflow table" (`b_ov_id` / `b_ov`,
    `o_ov_id` / `o_ov`: sorted ids, int64 values);
- `name_nodes` / `name_off` — name id → its nodes (CSR);
- the vocabulary, lowercase only: `lower` (uint8 blob) + `lower_off`
  (int64); the original case is `upper` (one bit per blob byte: this ASCII
  letter is upper case) plus `case_ex*` (the names whose case isn't ASCII,
  stored whole); `vfwd` / `vrev` — ids by lowercase name and by its reversed
  bytes (anchored tests are binary searches);
- `detail.parquet` (cold, never loaded whole) — per node, in id order, what a
  response shows but a search never reads: owner, kind, mean write time, last
  read, storage-class bytes. `slices.parquet` holds the nodes with more than
  one owner slice, slice by slice. `Detail` reads the row groups holding the
  ids asked for.

**Why numpy + Arrow, not Rust:** the vocabulary scan is libc `memmem` and
vectorized numpy passes over the one blob, from threads (both release the
GIL); everything after it is a handful of vectorized gathers over candidate
ids, bounded by tree depth (§6.2).

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
from .terms import NameTest, regex_literal, regex_name_filter, seg_term

FORMAT = 2
TREE = ("parent", "nid", "b32", "o8", "b_ov_id", "b_ov", "o_ov_id", "o_ov", "dstart", "name_nodes", "name_off")
VOCAB = ("lower", "lower_off", "upper", "case_ex_id", "case_ex", "case_ex_off", "vfwd", "vrev")
ARRAYS = TREE + VOCAB
DETAIL = "detail.parquet"
SLICES = "slices.parquet"
META = "meta.json"
U32 = np.uint32(0xFFFFFFFF)
U8 = np.uint8(0xFF)
DETAIL_RG = 1 << 16


def err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def _memmem():
    """libc's `memmem` via ctypes (a foreign call releases the GIL)."""
    import ctypes

    f = ctypes.CDLL(None).memmem
    f.restype = ctypes.c_void_p
    f.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t]
    return f


class Unsupported(ValueError):
    """A query this engine can't answer exactly (an unplannable regex, a term
    whose last segment constrains nothing)."""


# --- the vocabulary ------------------------------------------------------------------


def _buffers(a) -> tuple[np.ndarray, np.ndarray]:
    """A large_string array's (bytes, offsets) as numpy, zero-copy."""
    _, offs, data = a.buffers()
    off = np.frombuffer(offs, np.int64)[a.offset : a.offset + len(a) + 1]
    buf = np.frombuffer(data, np.uint8) if data is not None else np.zeros(0, np.uint8)
    return buf, off


def vocab_arrays(names, chunk: int = 1 << 20) -> dict[str, np.ndarray]:
    """The vocabulary's arrays from its names (original case, a
    `large_string` array, row = id): the lowercase blob, the case bits, the
    names whose case isn't plain ASCII (stored whole), and the sort orders.

    A name's case is "bits" when it and its lowercase have the same byte
    length and every differing byte is an ASCII letter (`A`–`Z` → `a`–`z`);
    anything else (`İ`, `ẞ`, a lowercase that changes length) is an
    exception."""
    import pyarrow as pa
    import pyarrow.compute as pc

    names = names.cast(pa.large_string())
    if isinstance(names, pa.ChunkedArray):
        names = names.combine_chunks()
    lower = pc.utf8_lower(names)
    nb, noff = _buffers(names)
    lb, loff = _buffers(lower)
    nb0, lb0 = noff[0], loff[0]
    V = len(names)
    total = int(loff[-1] - lb0)
    upper = np.zeros((total + 7) // 8, np.uint8)
    ex: list[np.ndarray] = []
    for i0 in range(0, V, chunk):
        i1 = min(V, i0 + chunk)
        nl = noff[i0 + 1 : i1 + 1] - noff[i0:i1]
        ll = loff[i0 + 1 : i1 + 1] - loff[i0:i1]
        eq = nl == ll
        ex.append(np.flatnonzero(~eq) + i0)
        ids = np.flatnonzero(eq)
        lens = ll[ids]
        tot = int(lens.sum())
        if not tot:
            continue
        idx = np.repeat(np.arange(len(ids)), lens)
        intra = np.arange(tot) - np.repeat(np.cumsum(lens) - lens, lens)
        pl = loff[i0 + ids][idx] - lb0 + intra
        pn = noff[i0 + ids][idx] - nb0 + intra
        a, b = nb[pn + nb0], lb[pl + lb0]
        d = a != b
        if not d.any():
            continue
        ok = (a >= 65) & (a <= 90) & (b == a + 32)
        bad = np.unique(idx[d & ~ok])
        ex.append(ids[bad] + i0)
        isbad = np.zeros(len(ids), bool)
        isbad[bad] = True
        pos = pl[d & ok & ~isbad[idx]]
        np.bitwise_or.at(upper, pos >> 3, (1 << (pos & 7)).astype(np.uint8))
    ex_id = np.sort(np.concatenate(ex)).astype(np.int32) if ex else np.zeros(0, np.int32)
    exa = names.take(pa.array(ex_id)).cast(pa.large_string())
    eb, eoff = _buffers(exa)
    srt = sorts(lower)
    return {
        "lower": lb[lb0 : loff[-1]].copy(), "lower_off": (loff - lb0).astype(np.int64), "upper": upper,
        "case_ex_id": ex_id, "case_ex": eb[eoff[0] : eoff[-1]].copy(), "case_ex_off": (eoff - eoff[0]).astype(np.int64),
        **srt,
    }


def sorts(lower) -> dict[str, np.ndarray]:
    """The vocabulary's sort orders for anchored name tests: ids by lowercase
    name, and by its reversed bytes (both bytewise)."""
    import pyarrow as pa
    import pyarrow.compute as pc

    lb = lower.cast(pa.large_binary())
    return {
        "vfwd": pc.sort_indices(lb).to_numpy().astype(np.int32),
        "vrev": pc.sort_indices(pc.binary_reverse(lb)).to_numpy().astype(np.int32),
    }


# --- build ---------------------------------------------------------------------------


def narrow(a: np.ndarray, dtype, sentinel) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """`a` (int64, ≥ 0) as `dtype` with values ≥ `sentinel` moved to an
    overflow table: (narrow, overflow ids, overflow values)."""
    ov = np.flatnonzero(a >= int(sentinel))
    out = np.minimum(a, int(sentinel)).astype(dtype)
    return out, ov.astype(np.int32), a[ov].astype(np.int64)


def renumber(parent: np.ndarray, depth: np.ndarray, b: np.ndarray) -> np.ndarray:
    """New ids for nodes given in `(depth, path)` order: breadth-first, by
    depth, then the parent's new id, then bytes descending, then the old id
    (path order). Returns `old_of_new`."""
    N = len(parent)
    starts = np.concatenate([[0], np.flatnonzero(np.diff(depth.astype(np.int16))) + 1, [N]])
    if N and np.any(np.diff(depth.astype(np.int16)) < 0):
        raise ValueError("nodes aren't in depth order")
    new_of_old = np.empty(N, np.int64)
    old_of_new = np.empty(N, np.int64)
    nxt = 0
    for lo, hi in zip(starts[:-1], starts[1:]):
        ids = np.arange(lo, hi, dtype=np.int64)
        keys = [ids, -b[lo:hi].astype(np.int64)]
        if depth[lo] > 1:
            keys.append(new_of_old[parent[lo:hi]])
        order = np.lexsort(keys)
        new_of_old[lo + order] = np.arange(nxt, nxt + hi - lo)
        old_of_new[nxt : nxt + hi - lo] = lo + order
        nxt += hi - lo
    return old_of_new


def build(path_file: str, names_file: str, out: Path, *, threads: int = 16, mem: str = "90GB", tmp: str | None = None, detail_rg: int = DETAIL_RG) -> dict:
    """Build the index from a generation's `path` sort and v1 names file
    (local paths). DuckDB does the string work once (path hashes, the
    name join, the parent join, the detail sort); numpy renumbers.

    A node is the rows (owner slices) of one path, keyed by the md5 of the
    path within its depth; a second, independent hash must agree across a
    key's rows, so a collision is raised, never merged silently."""
    import duckdb
    import pyarrow as pa

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

    def save(name: str, a: np.ndarray) -> None:
        np.save(out / f"{name}.npy", a)

    t = time.monotonic()
    con.execute(f"CREATE TABLE vocab AS SELECT id, name FROM read_parquet({lit(names_file)})")
    V, id_min, id_max = con.execute("SELECT count(*), min(id), max(id) FROM vocab").fetchone()
    if V and (id_min != 0 or id_max != V - 1):
        raise ValueError(f"names ids aren't 0…{V - 1}: [{id_min}, {id_max}]")
    names = con.execute("SELECT name FROM vocab ORDER BY id").to_arrow_table().column(0)
    va = vocab_arrays(names)
    for k, a in va.items():
        save(k, a)
    n_case_ex = len(va["case_ex_id"])
    del names, va
    step("vocab", t)

    t = time.monotonic()
    cols = {c[0] for c in con.execute(f"DESCRIBE SELECT * FROM read_parquet({lit(path_file)})").fetchall()}

    def col(name: str, expr: str, default: str) -> str:
        return expr if name in cols else default

    src = f"read_parquet({lit(path_file)}, file_row_number = true)"
    con.execute(f"""CREATE TABLE r AS
        SELECT p.file_row_number AS rn, md5_number(p.path) AS h, hash(p.path) AS h2, md5_number(regexp_extract(p.path, '^(.*)/', 1)) AS ph,
               v.id AS nid, p.depth::UTINYINT AS depth, coalesce(p.size, 0) AS size, coalesce(p.n_files, 0) AS n_files,
               {col('usr', 'p.usr', 'NULL::VARCHAR')} AS usr,
               {col('kind', "p.kind = 'file'", 'NULL::BOOLEAN')} AS kind,
               {col('mtime_mean', 'p.mtime_mean', 'NULL::DOUBLE')} AS mm,
               {col('last_read', 'p.last_read', 'NULL::INTEGER')} AS lr,
               {', '.join(col(f'sum_storage_class_id_{k}', f'coalesce(p.sum_storage_class_id_{k}, 0)', '0::BIGINT') + f' AS c{k}' for k in (2, 3, 4))}
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
               coalesce(any_value(nid), -1)::INTEGER AS nid, sum(size)::BIGINT AS b, sum(n_files)::BIGINT AS o, count(*) AS ns,
               any_value(usr) AS usr, bool_or(kind) AS kind, any_value(mm) AS mm, max(lr) AS lr,
               sum(c2)::BIGINT AS c2, sum(c3)::BIGINT AS c3, sum(c4)::BIGINT AS c4
        FROM r GROUP BY depth, h""")
    clashes, split = con.execute("SELECT count(*) FILTER (clash), count(*) FILTER (gap != 0) FROM n").fetchone()
    if clashes:
        raise ValueError(f"{clashes} path-hash collisions; rebuild with a wider key")
    con.execute("CREATE TABLE n2 AS SELECT (row_number() OVER (ORDER BY f) - 1)::INTEGER AS id, * EXCLUDE (gap, clash) FROM n")
    con.execute("DROP TABLE n")
    # The nodes with several owner slices, slice by slice (keyed by old id).
    con.execute("""CREATE TABLE sl AS
        SELECT n2.id, r.usr, r.size, r.n_files, r.kind, r.mm, r.lr, r.c2, r.c3, r.c4
        FROM r JOIN n2 USING (depth, h) WHERE n2.ns > 1""")
    con.execute("DROP TABLE r")
    N, no_name, multi = con.execute("SELECT count(*), count(*) FILTER (nid < 0), count(*) FILTER (ns > 1) FROM n2").fetchone()
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

    def column(sql: str, dtype) -> np.ndarray:
        a = con.execute(sql).to_arrow_table().column(0).to_numpy()
        return np.ascontiguousarray(a, dtype=dtype)

    parent = column("SELECT parent FROM par ORDER BY id", np.int64)
    con.execute("DROP TABLE par")
    depth = column("SELECT depth FROM n2 ORDER BY id", np.uint8)
    b = column("SELECT b FROM n2 ORDER BY id", np.int64)
    perm = renumber(parent, depth, b)  # old_of_new
    new_of_old = np.empty(N, np.int64)
    new_of_old[perm] = np.arange(N)
    pn = np.where(parent[perm] >= 0, new_of_old[np.maximum(parent[perm], 0)], -1)
    if N and np.any(np.diff(pn) < 0):
        raise ValueError("renumbered parents aren't monotone")
    save("parent", pn.astype(np.int32))
    del parent, pn
    dn = depth[perm]
    D = int(dn.max(initial=0))
    save("dstart", np.searchsorted(dn, np.arange(1, D + 2), side="left").astype(np.int64))
    del depth, dn
    a32, bid, bov = narrow(b[perm], np.uint32, U32)
    save("b32", a32)
    save("b_ov_id", bid)
    save("b_ov", bov)
    n_b_ov = len(bid)
    del b, a32
    a8, oid, ov = narrow(column("SELECT o FROM n2 ORDER BY id", np.int64)[perm], np.uint8, U8)
    save("o8", a8)
    save("o_ov_id", oid)
    save("o_ov", ov)
    n_o_ov = len(oid)
    del a8
    nid = column("SELECT nid FROM n2 ORDER BY id", np.int32)[perm]
    save("nid", nid)
    has = nid >= 0
    order = np.argsort(np.where(has, nid, np.iinfo(np.int32).max), kind="stable")[: int(has.sum())]
    save("name_nodes", order.astype(np.int32))
    save("name_off", np.concatenate([[0], np.cumsum(np.bincount(nid[has], minlength=V))]).astype(np.int32))
    del nid, has, order
    step("arrays", t)

    t = time.monotonic()
    # The cold detail, in new-id order; DuckDB sorts it (out of core).
    con.register("m", pa.table({"id": pa.array(np.arange(N, dtype=np.int32)), "new": pa.array(new_of_old.astype(np.int32))}))
    con.execute("SET preserve_insertion_order = true")
    con.execute(f"""COPY (
        SELECT m.new AS id, CASE WHEN ns > 1 THEN NULL ELSE usr END AS usr, ns > 1 AS multi, kind,
               CASE WHEN ns > 1 THEN NULL ELSE mm END AS mtime_mean, lr AS last_read, c2, c3, c4
        FROM n2 JOIN m USING (id) ORDER BY m.new
    ) TO {lit(str(out / DETAIL))} (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {detail_rg})""")
    con.execute(f"""COPY (
        SELECT m.new AS id, usr, size, n_files, kind, mm AS mtime_mean, lr AS last_read, c2, c3, c4
        FROM sl JOIN m USING (id) ORDER BY m.new, usr NULLS FIRST
    ) TO {lit(str(out / SLICES))} (FORMAT parquet, COMPRESSION zstd)""")
    con.close()
    step("detail", t)

    meta = {
        "v": FORMAT, "path_file": path_file, "names_file": names_file, "rows": int(R), "nodes": int(N), "names": int(V),
        "no_name": int(no_name), "split_nodes": int(split), "multi_slice_nodes": int(multi), "case_exceptions": n_case_ex,
        "b_overflow": n_b_ov, "o_overflow": n_o_ov, "steps": steps,
        "bytes": {f.name: f.stat().st_size for f in sorted(out.iterdir()) if f.is_file()},
    }
    (out / META).write_text(json.dumps(meta, indent=1))
    return meta


# --- the index -----------------------------------------------------------------------


@dataclass
class MemIndex:
    parent: np.ndarray
    nid: np.ndarray
    b32: np.ndarray
    o8: np.ndarray
    b_ov_id: np.ndarray
    b_ov: np.ndarray
    o_ov_id: np.ndarray
    o_ov: np.ndarray
    dstart: np.ndarray  # dstart[k]: the first id at depth k + 1 (and N past the deepest)
    name_nodes: np.ndarray
    name_off: np.ndarray
    lower: np.ndarray  # uint8 blob
    lower_off: np.ndarray
    upper: np.ndarray
    case_ex_id: np.ndarray
    case_ex: np.ndarray
    case_ex_off: np.ndarray
    vfwd: np.ndarray | None = None  # ids by lowercase name (bytewise)
    vrev: np.ndarray | None = None  # ids by reversed lowercase name
    threads: int = 16
    chunk: int = 1 << 20
    fast: bool = True  # literal tests by `scan_literal` (False: Arrow's kernels)
    memmem: bool = True  # `contains` by libc memmem first (False: the numpy pass only)
    memmem_cap: int = 20_000  # hits per block past which the numpy pass takes over
    detail: "Detail | None" = None
    _found: dict = field(default_factory=dict)
    _freq: np.ndarray | None = None
    _arrow: object = None

    @classmethod
    def load(cls, d: Path, threads: int = 16, mmap: bool = False, detail: bool = True) -> "MemIndex":
        """Every array read into memory (a query must never page), or with
        `mmap` mapped (pages load as touched and are shared with the page
        cache: a tmpfs copy costs its RAM once)."""
        d = Path(d)
        meta = json.loads((d / META).read_text())
        if meta.get("v") != FORMAT:
            raise ValueError(f"{d}: index format {meta.get('v')}, want {FORMAT} (rebuild with `dt-cloud bench-index`)")
        arrays = {k: np.load(d / f"{k}.npy", mmap_mode="r" if mmap else None) for k in ARRAYS}
        det = Detail(str(d / DETAIL), str(d / SLICES)) if detail and (d / DETAIL).exists() else None
        return cls(**arrays, threads=threads, detail=det)

    @classmethod
    def from_names(cls, names, threads: int = 4, chunk: int = 1 << 20) -> "MemIndex":
        """A vocabulary-only index (no nodes): what the name tests need."""
        z = np.zeros(0, np.int32)
        va = vocab_arrays(names)
        return cls(parent=z, nid=z, b32=z.astype(np.uint32), o8=z.astype(np.uint8), b_ov_id=z, b_ov=z.astype(np.int64), o_ov_id=z, o_ov=z.astype(np.int64),
                   dstart=np.zeros(1, np.int64), name_nodes=z, name_off=np.zeros(len(names) + 1, np.int32), **va, threads=threads, chunk=chunk)

    @property
    def n(self) -> int:
        return len(self.parent)

    @property
    def V(self) -> int:
        return len(self.lower_off) - 1

    @property
    def n_top(self) -> int:
        return int(self.dstart[1]) if len(self.dstart) > 1 else 0

    @property
    def top(self) -> np.ndarray:
        """The buckets, largest first."""
        return np.arange(self.n_top, dtype=np.int64)

    def nbytes(self) -> dict[str, int]:
        return {k: int(getattr(self, k).nbytes) for k in ARRAYS if getattr(self, k) is not None}

    # columns

    def b_of(self, ids) -> np.ndarray:
        ids = np.asarray(ids, np.int64)
        v = self.b32[ids].astype(np.int64)
        m = v == int(U32)
        if m.any():
            v[m] = self.b_ov[np.searchsorted(self.b_ov_id, ids[m])]
        return v

    def o_of(self, ids) -> np.ndarray:
        ids = np.asarray(ids, np.int64)
        v = self.o8[ids].astype(np.int64)
        m = v == int(U8)
        if m.any():
            v[m] = self.o_ov[np.searchsorted(self.o_ov_id, ids[m])]
        return v

    def depth_of(self, ids) -> np.ndarray:
        return np.searchsorted(self.dstart, np.asarray(ids, np.int64), side="right").astype(np.int64)

    def depth1(self, i: int) -> int:
        return 0 if i < 0 else int(self.depth_of(np.array([i]))[0])

    def b1(self, i: int) -> int:
        return int(self.b_of(self.top).sum()) if i < 0 else int(self.b_of(np.array([i]))[0])

    def o1(self, i: int) -> int:
        return int(self.o_of(self.top).sum()) if i < 0 else int(self.o_of(np.array([i]))[0])

    def kid_range(self, nodes) -> tuple[np.ndarray, np.ndarray]:
        """Each node's children as an id range [lo, hi) (−1: the buckets)."""
        nodes = np.asarray(nodes, np.int64)
        return np.searchsorted(self.parent, nodes, side="left"), np.searchsorted(self.parent, nodes, side="right")

    def nc_of(self, nodes) -> np.ndarray:
        lo, hi = self.kid_range(nodes)
        return hi - lo

    # vocabulary

    def _blob(self) -> tuple[np.ndarray, np.ndarray]:
        """The lowercase vocabulary's (bytes, offsets)."""
        if self._freq is None:
            buf = self.lower
            # Bigram frequencies from a sample: the substring search starts at
            # the literal's rarest adjacent byte pair.
            n = min(len(buf), 64 << 20)
            step = max(1, len(buf) // max(1, n))
            smp = np.asarray(buf[::step][:n]).astype(np.int32)
            self._freq = np.bincount(smp[:-1] * 256 + smp[1:], minlength=1 << 16) if len(smp) > 1 else np.zeros(1 << 16, np.int64)
        return self.lower, self.lower_off

    @property
    def lower_arr(self):
        """The lowercase vocabulary as an Arrow `large_string` array over the
        blob (zero-copy)."""
        if self._arrow is None:
            import pyarrow as pa

            self._arrow = pa.LargeStringArray.from_buffers(self.V, pa.py_buffer(self.lower_off), pa.py_buffer(self.lower))
        return self._arrow

    def names_arrow(self, nids, lower: bool = False):
        """Names (original case unless `lower`) of vocabulary ids, as Arrow
        (a null id → a null name)."""
        import pyarrow as pa
        import pyarrow.compute as pc

        nids = np.asarray(nids, np.int64)
        null = nids < 0
        sel = self.lower_arr.take(pa.array(np.maximum(nids, 0)))
        if lower or not len(nids):
            return pc.if_else(pa.array(null), pa.scalar(None, pa.large_string()), sel) if null.any() else sel
        _, soff, sdata = sel.buffers()
        so = np.frombuffer(soff, np.int64)[sel.offset : sel.offset + len(sel) + 1]
        out = np.frombuffer(sdata, np.uint8)[so[0] : so[-1]].copy() if sdata is not None else np.zeros(0, np.uint8)
        lens = so[1:] - so[:-1]
        tot = int(lens.sum())
        if tot:
            src = np.repeat(self.lower_off[np.maximum(nids, 0)], lens) + (np.arange(tot) - np.repeat(np.cumsum(lens) - lens, lens))
            up = (self.upper[src >> 3] >> (src & 7).astype(np.uint8)) & 1
            out[up.astype(bool)] -= 32
        arr = pa.LargeStringArray.from_buffers(len(nids), pa.py_buffer((so - so[0]).astype(np.int64)), pa.py_buffer(out))
        if len(self.case_ex_id):
            pos = np.searchsorted(self.case_ex_id, np.maximum(nids, 0))
            hit = (pos < len(self.case_ex_id)) & (self.case_ex_id[np.minimum(pos, len(self.case_ex_id) - 1)] == nids)
            if hit.any():
                ex = pa.LargeStringArray.from_buffers(len(self.case_ex_id), pa.py_buffer(self.case_ex_off), pa.py_buffer(self.case_ex))
                arr = pc.replace_with_mask(arr, pa.array(hit), ex.take(pa.array(pos[hit])))
        if null.any():
            arr = pc.if_else(pa.array(null), pa.scalar(None, pa.large_string()), arr)
        return arr

    def case_exceptions(self) -> dict[str, int]:
        return {"n": int(len(self.case_ex_id)), "bytes": int(len(self.case_ex))}

    def _dense(self, raw: bytes, block: int = 1 << 27) -> float:
        """Expected hits of a literal per `scan_literal` block, from 16
        contiguous 4 MB samples of the blob (memmem pays a Python step per
        hit; past a few thousand per block the vectorized pass is cheaper)."""
        buf, _ = self._blob()
        n, w = 16, 4 << 20
        if len(buf) <= n * w:
            return bytes(buf).count(raw) * block / max(1, len(buf))
        hits = sum(bytes(buf[k : k + w]).count(raw) for k in np.linspace(0, len(buf) - w, n).astype(np.int64))
        return hits * block / (n * w)

    def _key(self, i: int, rev: bool) -> bytes:
        buf, off = self._blob()
        k = bytes(buf[off[i] : off[i + 1]])
        return k[::-1] if rev else k

    def _range(self, perm: np.ndarray, lo_key: bytes, prefix: bool, rev: bool) -> np.ndarray:
        """Ids whose (reversed) lowercase name starts with / equals `lo_key`:
        a binary search over a sorted permutation of the vocabulary."""

        def bisect(k: bytes, right: bool) -> int:
            a, b = 0, len(perm)
            while a < b:
                m = (a + b) // 2
                km = self._key(int(perm[m]), rev)
                if km < k or (right and km == k):
                    a = m + 1
                else:
                    b = m
            return a

        lo = bisect(lo_key, False)
        if not prefix:
            hi = bisect(lo_key, True)
        else:
            # The prefix's successor: bump its last byte below 0xff, drop the rest.
            k = lo_key.rstrip(b"\xff")
            hi = bisect(k[:-1] + bytes([k[-1] + 1]), False) if k else len(perm)
        return np.sort(perm[lo:hi]).astype(np.int32)

    def scan_literal(self, op: str, lit: str, block: int = 1 << 27) -> np.ndarray:
        """Name ids whose lowercase name `contains` / `starts` / `ends` /
        `equals` a literal, sorted. Anchored tests are binary searches over
        the vocabulary sorted forward (`vfwd`) and by reversed bytes
        (`vrev`); `contains` compares bytes over the one blob (numpy releases
        the GIL, so blocks run in parallel threads), starting from the
        literal's rarest byte pair; a hit straddling two names doesn't count."""
        buf, off = self._blob()
        raw = lit.encode()
        b = np.frombuffer(raw, np.uint8)
        L = len(b)
        if not L:
            raise Unsupported("an empty literal")
        if op in ("starts", "equals") and self.vfwd is not None:
            return self._range(self.vfwd, raw, op == "starts", False)
        if op == "ends" and self.vrev is not None:
            return self._range(self.vrev, raw[::-1], True, True)
        if op != "contains":
            lens = off[1:] - off[:-1]
            ids = np.flatnonzero(lens == L if op == "equals" else lens >= L)
            base = off[ids] if op in ("starts", "equals") else off[ids + 1] - L
            for j in range(L):
                keep = buf[base + j] == b[j]
                ids, base = ids[keep], base[keep]
            return ids.astype(np.int32)
        if L == 1:
            j0, pair = 0, None
        else:
            f = self._freq[b[:-1].astype(np.int32) * 256 + b[1:]]
            j0, pair = int(np.argmin(f)), True
        rest = [j for j in range(L) if j != j0 and not (pair and j == j0 + 1)]

        mm = _memmem() if self.memmem and self._dense(raw) < self.memmem_cap // 4 else None
        base = buf.ctypes.data

        def by_memmem(lo: int) -> np.ndarray | None:
            """libc `memmem` from hit to hit (it releases the GIL; glibc's is
            vectorized); None past `memmem_cap` hits in the block (then the
            vectorized pass is cheaper than a Python step per hit)."""
            hi = min(len(buf), lo + block + L - 1)
            out, p = [], lo
            while p < hi:
                r = mm(base + p, hi - p, raw, L)
                if not r:
                    break
                q = r - base
                if q >= lo + block:
                    break
                out.append(q)
                if len(out) > self.memmem_cap:
                    return None
                p = q + 1
            return np.array(out, np.int64)

        def one(lo: int) -> np.ndarray:
            if mm is not None:
                p = by_memmem(lo)
                if p is not None:
                    return p
            hi = min(len(buf), lo + block + (1 if pair else 0))
            c = buf[lo:hi]
            m = c == b[j0]
            if pair:
                m = m[:-1] & (c[1:] == b[j0 + 1])
            p = np.flatnonzero(m) + (lo - j0)
            p = p[(p >= 0) & (p + L <= len(buf))]
            for j in rest:
                p = p[buf[p + j] == b[j]]
            return p

        with ThreadPoolExecutor(self.threads) as ex:
            parts = list(ex.map(one, range(0, len(buf), block)))
        pos = np.concatenate(parts) if parts else np.zeros(0, np.int64)
        ids = np.searchsorted(off, pos, side="right") - 1
        ids = ids[pos + L <= off[ids + 1]]
        return np.unique(ids).astype(np.int32)

    def scan(self, t: NameTest, ignore_case: bool = False) -> np.ndarray:
        """Name ids passing `t` (lowercase names), sorted: a literal by
        `scan_literal`, a regex by Arrow's RE2 kernel over zero-copy slices of
        the blob in parallel threads."""
        import pyarrow as pa
        import pyarrow.compute as pc

        if t.op != "regex" and not ignore_case and self.fast:
            return self.scan_literal(t.op, t.arg)
        if t.op == "regex" and self.fast:
            lit = regex_literal(t.arg)
            if lit is not None:
                # RE2 only over the names holding the regex's literal run.
                ids = self.scan_literal(lit.op, lit.arg)
                if not len(ids):
                    return ids
                ok = pc.match_substring_regex(self.lower_arr.take(pa.array(ids)), t.arg, ignore_case=ignore_case)
                return ids[ok.to_numpy(zero_copy_only=False)]

        def one(lo: int) -> np.ndarray:
            a = self.lower_arr.slice(lo, self.chunk)
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

        starts = range(0, self.V, self.chunk)
        with ThreadPoolExecutor(self.threads) as ex:
            parts = list(ex.map(one, starts))
        return np.concatenate(parts).astype(np.int32) if parts else np.zeros(0, np.int32)

    # tree

    @staticmethod
    def _ranges(lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
        """The concatenated id ranges [lo, hi)."""
        lens = (hi - lo).astype(np.int64)
        tot = int(lens.sum())
        if not tot:
            return np.zeros(0, np.int64)
        return np.repeat(lo.astype(np.int64) - (np.cumsum(lens) - lens), lens) + np.arange(tot)

    @staticmethod
    def _csr(off: np.ndarray, vals: np.ndarray, ids: np.ndarray) -> np.ndarray:
        s = off[ids].astype(np.int64)
        e = off[ids + 1].astype(np.int64)
        return vals[MemIndex._ranges(s, e)] if len(ids) else np.zeros(0, vals.dtype)

    def nodes_of(self, name_ids: np.ndarray) -> np.ndarray:
        return self._csr(self.name_off, self.name_nodes, np.asarray(name_ids, np.int64))

    def children(self, nodes: np.ndarray) -> np.ndarray:
        lo, hi = self.kid_range(nodes)
        return self._ranges(lo, hi)

    def up(self, cur: np.ndarray) -> np.ndarray:
        out = np.full(len(cur), -1, np.int64)
        m = cur >= 0
        out[m] = self.parent[cur[m]]
        return out

    def segments(self, nodes: np.ndarray, k: int | None, lower: bool):
        """Each node's last `k + 1` segments (all, for None) joined by `/`."""
        import pyarrow as pa
        import pyarrow.compute as pc

        cols = []
        cur = np.asarray(nodes, np.int64)
        i = 0
        while (k is None or i <= k) and (cur >= 0).any():
            ids = np.where(cur >= 0, self.nid[np.maximum(cur, 0)], -1)
            cols.append(self.names_arrow(ids, lower=lower))
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

    def paths(self, nodes, sort: bool = False) -> list[str]:
        return self.paths_arrow(np.asarray(nodes, np.int64), sort).to_pylist()

    def path(self, node: int) -> str:
        return "" if node < 0 else self.paths([node])[0]

    def holds(self, mark: np.ndarray, x: np.ndarray, strict: bool = False) -> np.ndarray:
        """Per node of `x`: some ancestor-or-self (strict: ancestor) is marked."""
        acc = np.zeros(len(x), bool)
        cur = x.astype(np.int64)
        idx = np.arange(len(x))
        if not strict:
            hit = mark[cur]
            acc[hit] = True
            cur, idx = cur[~hit], idx[~hit]
        while len(cur):
            cur = self.parent[cur].astype(np.int64)
            keep = cur >= 0
            cur, idx = cur[keep], idx[keep]
            hit = mark[cur]
            acc[idx[hit]] = True
            cur, idx = cur[~hit], idx[~hit]
        return acc

    def outermost(self, mark: np.ndarray, x: np.ndarray, dv: int) -> np.ndarray:
        """The nodes of `x` with no marked strict ancestor deeper than `dv`."""
        bad = np.zeros(len(x), bool)
        cur = x.astype(np.int64)
        idx = np.arange(len(x))
        first = int(self.dstart[dv]) if dv < len(self.dstart) else self.n  # ids deeper than dv
        while len(cur):
            cur = self.parent[cur].astype(np.int64)
            keep = cur >= first
            cur, idx = cur[keep], idx[keep]
            hit = mark[cur]
            bad[idx[hit]] = True
            cur, idx = cur[~hit], idx[~hit]
        return x[~bad]

    def nearest(self, mark: np.ndarray, x: np.ndarray, dv: int) -> np.ndarray:
        """Per node of `x`: its nearest marked strict ancestor at depth ≥ `dv`
        (−1 if none)."""
        out = np.full(len(x), -1, np.int64)
        cur = x.astype(np.int64)
        idx = np.arange(len(x))
        first = int(self.dstart[dv - 1]) if dv >= 1 else 0
        while len(cur):
            cur = self.parent[cur].astype(np.int64)
            keep = cur >= first
            cur, idx = cur[keep], idx[keep]
            hit = mark[cur]
            out[idx[hit]] = cur[hit]
            cur, idx = cur[~hit], idx[~hit]
        return out

    def subtree_ranges(self, v: int, dv: int) -> tuple[np.ndarray, np.ndarray]:
        """Node `v`'s descendants at each depth as an id range: (lo, hi)
        indexed by depth (entries at or above `dv` are empty). Breadth-first
        ids make every depth of a subtree one range."""
        D = len(self.dstart) - 1
        lo = np.zeros(D + 2, np.int64)
        hi = np.zeros(D + 2, np.int64)
        a, b = v, v + 1
        for d in range(dv + 1, D + 1):
            if a >= b:
                break
            a, b = int(np.searchsorted(self.parent, a, side="left")), int(np.searchsorted(self.parent, b - 1, side="right"))
            lo[d], hi[d] = a, b
        return lo, hi

    def under(self, x: np.ndarray, v: int, dv: int) -> np.ndarray:
        """Mask: strictly under node `v` (depth `dv`; v = −1: the store root)."""
        if v < 0:
            return np.ones(len(x), bool)
        lo, hi = self.subtree_ranges(v, dv)
        d = self.depth_of(x)
        d = np.minimum(d, len(lo) - 1)
        return (x >= lo[d]) & (x < hi[d])

    def child_named(self, p: int, seg: str) -> int:
        """The child of node `p` (−1: the store root) named `seg` exactly;
        −1 if none."""
        key = seg.lower().encode()
        ids = self._range(self.vfwd, key, False, False) if self.vfwd is not None else self.scan_literal("equals", seg.lower())
        if not len(ids):
            return -1
        names = self.names_arrow(ids).to_pylist()
        ids = np.array([i for i, n in zip(ids, names) if n == seg], np.int64)
        if not len(ids):
            return -1
        nodes = self.nodes_of(ids)
        lo, hi = self.kid_range(np.array([p]))
        nodes = nodes[(nodes >= lo[0]) & (nodes < hi[0])]
        if len(nodes) > 1:
            raise ValueError(f"{len(nodes)} children of node {p} named {seg!r}")
        return int(nodes[0]) if len(nodes) else -1

    def find(self, path: str) -> int:
        """A path's node id (−1: the store root); KeyError if absent."""
        if path == "":
            return -1
        if path in self._found:
            return self._found[path]
        cur = -1
        for seg in path.split("/"):
            cur = self.child_named(cur, seg)
            if cur < 0:
                raise KeyError(path)
        self._found[path] = cur
        return cur

    def union(self, parts: list[np.ndarray]) -> np.ndarray:
        """Distinct node ids of several sets (each already distinct): one set
        as is, small ones by sort, big ones by a bitmap (a sort of tens of
        millions of ids took 16 s)."""
        parts = [p for p in parts if len(p)]
        if not parts:
            return np.zeros(0, np.int64)
        if len(parts) == 1:
            return parts[0]
        if sum(len(p) for p in parts) < 1 << 21:
            return np.unique(np.concatenate(parts))
        m = np.zeros(self.n, bool)
        for p in parts:
            m[p] = True
        return np.flatnonzero(m)

    def name_mask(self, t: NameTest) -> np.ndarray:
        """A bitmap over the vocabulary: the names passing `t`."""
        m = np.zeros(self.V, bool)
        m[self.scan(t)] = True
        return m

    def mark(self, nodes: np.ndarray) -> np.ndarray:
        m = np.zeros(self.n, bool)
        m[nodes] = True
        return m

    # matchers

    def start_set(self, m: Matcher, tm: "Timer | None" = None) -> tuple[np.ndarray, bool]:
        """(nodes whose last k+1 segments hold `m`, strict)."""
        tm = tm or Timer()
        st = seg_term(m)
        if st.trivial:
            raise Unsupported(f"term {m} constrains no segment")
        names = tm("scan", self.scan, st.name)
        nodes = tm("nodes", self.nodes_of, names)
        if st.k and len(nodes):
            if m.kind == "sub":
                nodes = tm("suffix", self._suffix_sub, nodes, m.text.removesuffix("/") if st.strict else m.text)
            else:
                import pyarrow.compute as pc

                ok = tm("suffix", lambda: pc.match_substring_regex(self.segments(nodes, st.k, lower=True), st.suffix_re).to_numpy(zero_copy_only=False))
                nodes = nodes[ok]
        return nodes, st.strict

    def _suffix_sub(self, nodes: np.ndarray, text: str) -> np.ndarray:
        """The nodes (whose name passed the last segment's test) where a
        substring `s0/s1/…/sk` ends: `k` ancestors up, the name ends with
        `s0` (an empty `s0` only needs the ancestor to exist), the ones
        between equal `s1 … s(k−1)`. Tests the vocabulary once per segment
        (a bitmap over names), then gathers up the tree; no strings built."""
        segs = text.split("/")
        k = len(segs) - 1
        cur = nodes.astype(np.int64)
        keep = np.ones(len(nodes), bool)
        for j in range(1, k + 1):
            cur = self.up(cur)
            keep &= cur >= 0
            seg = segs[k - j]
            if j < k:
                ok = self.name_mask(NameTest("equals", seg))
            elif seg:
                ok = self.name_mask(NameTest("ends", seg))
            else:
                continue
            idx = np.flatnonzero(keep)
            keep[idx] = ok[self.nid[cur[idx]]]
        return nodes[keep]

    def regex_set(self, source: str, tm: "Timer | None" = None) -> np.ndarray:
        import pyarrow.compute as pc

        tm = tm or Timer()
        plan = regex_name_filter(source)
        if plan is None:
            raise Unsupported(f"regex {source!r}: no name filter (its tail can cross a `/` or isn't `$`-anchored)")
        names = tm("scan", self.scan, NameTest("regex", plan.name_re), ignore_case=True)
        nodes = tm("nodes", self.nodes_of, names)
        if not len(nodes):
            return nodes
        ok = tm("verify", lambda: pc.match_substring_regex(self.segments(nodes, None, lower=False), source, ignore_case=True).to_numpy(zero_copy_only=False))
        return nodes[ok]


# --- the cold detail -----------------------------------------------------------------


class Detail:
    """`detail.parquet` (+ `slices.parquet`) read by node id: only the row
    groups holding the ids asked for, the latest few kept decoded. Local
    paths or `gs://` URLs (Arrow's filesystems; ranged reads)."""

    COLS = ("usr", "multi", "kind", "mtime_mean", "last_read", "c2", "c3", "c4")

    def __init__(self, src: str, slices: str | None = None, cache_groups: int = 256):
        import threading

        import pyarrow.parquet as pq

        self.src = src
        self.pf = pq.ParquetFile(_open(src), pre_buffer=True)
        md = self.pf.metadata
        self.starts = np.concatenate([[0], np.cumsum([md.row_group(i).num_rows for i in range(md.num_row_groups)])]).astype(np.int64)
        self.n = int(self.starts[-1])
        self.cache: dict[int, dict[str, np.ndarray]] = {}
        self.cache_groups = cache_groups
        self.lock = threading.Lock()
        self.users: dict[str, int] = {}
        self.user_names: list[str] = []
        sl = pq.read_table(_open(slices)) if slices and _exists(slices) else None
        self.slices = self._slice_arrays(sl) if sl is not None and sl.num_rows else None

    def user(self, name: str) -> int:
        c = self.users.get(name)
        if c is None:
            c = self.users[name] = len(self.user_names)
            self.user_names.append(name)
        return c

    def _codes(self, col) -> np.ndarray:
        """A string column → user codes (−1: null)."""
        import pyarrow as pa
        import pyarrow.compute as pc

        col = col.combine_chunks() if isinstance(col, pa.ChunkedArray) else col
        if pa.types.is_null(col.type):
            return np.full(len(col), -1, np.int64)
        if not pa.types.is_dictionary(col.type):
            col = pc.dictionary_encode(col)
        dic = col.dictionary.to_pylist()
        m = np.array([self.user(u) for u in dic] + [-1], np.int64)
        idx = col.indices.to_numpy(zero_copy_only=False)
        idx = np.where(col.is_null().to_numpy(zero_copy_only=False), len(dic), idx)
        return m[idx.astype(np.int64)]

    def _arrays(self, t) -> dict[str, np.ndarray]:
        def num(name: str, dtype, null) -> np.ndarray:
            c = t.column(name)
            return np.asarray(c.fill_null(null).to_numpy(), dtype)

        return {
            "usr": self._codes(t.column("usr")),
            "multi": num("multi", bool, False) if "multi" in t.column_names else np.zeros(t.num_rows, bool),
            "kind": np.where(t.column("kind").is_null().to_numpy(zero_copy_only=False), -1, num("kind", np.int8, False)).astype(np.int8),
            "mean": num("mtime_mean", np.float64, np.nan),
            "lr": num("last_read", np.int64, -1),
            "c2": num("c2", np.int64, 0), "c3": num("c3", np.int64, 0), "c4": num("c4", np.int64, 0),
        }

    def _slice_arrays(self, t) -> dict[str, np.ndarray]:
        a = self._arrays(t)
        a["id"] = np.asarray(t.column("id").to_numpy(), np.int64)
        a["size"] = np.asarray(t.column("size").to_numpy(), np.int64)
        a["n_files"] = np.asarray(t.column("n_files").to_numpy(), np.int64)
        return a

    def _split(self, t, groups: list[int]) -> dict[int, dict[str, np.ndarray]]:
        """A table of whole row groups (in `groups` order), per group."""
        a = self._arrays(t)
        ids = np.asarray(t.column("id").to_numpy(), np.int64)
        out, at = {}, 0
        for g in groups:
            n = int(self.starts[g + 1] - self.starts[g])
            if n and (ids[at] != self.starts[g] or ids[at + n - 1] != self.starts[g + 1] - 1):
                raise ValueError(f"{self.src}: row group {g} holds ids {ids[at]}…{ids[at + n - 1]}, want {self.starts[g]}…{self.starts[g + 1] - 1}")
            out[g] = {k: v[at : at + n] for k, v in a.items()}
            at += n
        return out

    def take(self, ids: np.ndarray, batch: int = 64) -> dict[str, np.ndarray]:
        """Detail columns for node ids (any order), aligned with `ids`. The
        row groups not cached are read `batch` at a time (Arrow decodes a
        batch's groups in parallel) and gathered from as they arrive; they
        are kept (LRU, `cache_groups`) only when the request's groups fit."""
        ids = np.asarray(ids, np.int64)
        kinds = (("usr", np.int64), ("multi", bool), ("kind", np.int8), ("mean", np.float64), ("lr", np.int64), ("c2", np.int64), ("c3", np.int64), ("c4", np.int64))
        out = {k: np.empty(len(ids), dt) for k, dt in kinds}
        if not len(ids):
            return out
        if ids.min() < 0 or ids.max() >= self.n:
            raise IndexError(f"detail ids outside 0…{self.n - 1}")
        g = np.searchsorted(self.starts, ids, side="right") - 1
        order = np.argsort(g, kind="stable")
        uniq, first = np.unique(g[order], return_index=True)
        bounds = np.append(first, len(g))
        where = {int(x): j for j, x in enumerate(uniq)}
        keep = len(uniq) <= self.cache_groups

        def gather(gi: int, a: dict) -> None:
            j = where[gi]
            sel = order[bounds[j] : bounds[j + 1]]
            off = ids[sel] - self.starts[gi]
            for k in out:
                out[k][sel] = a[k][off]

        with self.lock:
            missing = []
            for gi in where:
                a = self.cache.get(gi)
                if a is None:
                    missing.append(gi)
                else:
                    self.cache[gi] = self.cache.pop(gi)  # most recently used last
                    gather(gi, a)
            for i in range(0, len(missing), batch):
                part = missing[i : i + batch]
                for gi, a in self._split(self.pf.read_row_groups(part, columns=["id", *self.COLS], use_threads=True), part).items():
                    gather(gi, a)
                    if keep:
                        self.cache[gi] = a
            while len(self.cache) > self.cache_groups:
                self.cache.pop(next(iter(self.cache)))
        return out


def _open(uri: str):
    """A local path as is; a URL as an Arrow random-access file."""
    if "://" not in uri:
        return uri
    import pyarrow.fs as pafs

    fs, p = pafs.FileSystem.from_uri(uri)
    return fs.open_input_file(p)


def _exists(uri: str) -> bool:
    if "://" not in uri:
        return os.path.exists(uri)
    import pyarrow.fs as pafs

    fs, p = pafs.FileSystem.from_uri(uri)
    return fs.get_file_info(p).type != pafs.FileType.NotFound


# --- evaluation ----------------------------------------------------------------------


class Timer:
    """Accumulated seconds per step: `tm("step", f, *args)` runs and times `f`."""

    def __init__(self):
        self.s: dict[str, float] = {}

    def __call__(self, name: str, f, *a, **kw):
        t0 = time.monotonic()
        try:
            return f(*a, **kw)
        finally:
            self.s[name] = round(self.s.get(name, 0.0) + time.monotonic() - t0, 4)


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
    excl_root: np.ndarray | None = None  # per excluded node: its match root


def evaluate(ix: MemIndex, ast: Ast, view: str, v: int | None = None) -> MemResult:
    """One view's answer (`truth.view_truth`'s semantics on node ids)."""
    t0 = time.monotonic()
    tm = Timer()
    v = tm("find", ix.find, view) if v is None else v
    dv = ix.depth1(v)
    hit = compile_query(ast)(view)
    regex = [m for a in ast.alts for m in a if m.kind == "regex"] + [m for m in ast.neg if m.kind == "regex"]
    stats: dict = {}
    if regex:
        if len(ast.alts) != 1 or len(ast.alts[0]) != 1 or ast.neg:
            raise Unsupported("a regex mixed with other terms")
        B = ix.regex_set(regex[0].source, tm)
        stats["start"] = int(len(B))
        x = B[ix.under(B, v, dv)]
        posx, negx = np.ones(len(x), bool), np.zeros(len(x), bool)
    else:
        matchers = list(dict.fromkeys([m for a in ast.alts for m in a] + list(ast.neg)))
        start: dict[Matcher, tuple[np.ndarray, bool]] = {m: ix.start_set(m, tm) for m in matchers}
        stats["start"] = {str(m.text or m.pieces): int(len(start[m][0])) for m in matchers}
        cand = []
        for m in matchers:
            s, strict = start[m]
            c = ix.children(s) if strict else s
            cand.append(c[tm("under", ix.under, c, v, dv)])
        x = tm("cands", ix.union, cand)
        held: dict[Matcher, np.ndarray] = {}
        for m in matchers:
            s, strict = start[m]
            held[m] = tm("holds", lambda: ix.holds(ix.mark(s), x, strict))
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
        rb = np.array([ix.b1(v)], np.int64)
        ro = np.array([ix.o1(v)], np.int64)
    else:
        s = x[posx & ~negx]
        roots = tm("roots", lambda: np.sort(ix.outermost(ix.mark(s), s, dv).astype(np.int64)))
        rb, ro = ix.b_of(roots), ix.o_of(roots)
    e0 = x[negx]
    if len(e0):
        e = np.sort(ix.outermost(ix.mark(e0), e0, dv).astype(np.int64))
        if hit:
            r = np.full(len(e), v, np.int64)
            ok = np.ones(len(e), bool)
        else:
            r = ix.nearest(ix.mark(roots), e, dv)
            ok = r >= 0
        e, r = e[ok], r[ok]
        pos = np.zeros(len(r), np.int64) if hit else np.searchsorted(roots, r)
        rb = rb - _sum_at(pos, ix.b_of(e), len(roots))
        ro = ro - _sum_at(pos, ix.o_of(e), len(roots))
    else:
        e = r = np.zeros(0, np.int64)
    stats["steps"] = tm.s
    stats["s"] = round(time.monotonic() - t0, 4)
    return MemResult(bool(hit), roots, rb, ro, e, int(rb.sum()), int(ro.sum()), stats, excl_root=r)


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
