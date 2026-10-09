"""The serving box's DuckDB engine (specs/filter-query-service.md §4.2 engine
1): phase 0's names-first, as a long-lived process over a generation's
parquet with the vocabulary pinned.

- **Pinned:** `(l, rgs)` — each name lowercased, and the `path`-sort row
  groups holding it (v1 `rgs`). Display names come from the rows.
- **Per query:** the names passing each term's segment test (`terms`) → their
  row groups → those groups' rows whose name passed (pyarrow, parallel), with
  the full-path `pos` / `neg` flags (`query`'s SQL). A term ending in `/`
  adds the children of the dirs its stem ends (by `depth` / `path` pushdown);
  a regex goes through its name filter (`terms.regex_name_filter`). Past
  `scan_frac` of the file's groups, or a name over the writer's `rgs` cap, it
  scans the whole `path` sort instead (correct, slow).
- **Roots, exclusions, net totals:** in SQL, the ancestor anti-join, with
  `truth.view_truth`'s semantics.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass

from .query import Ast, compile_query, lit, neg_sql, pos_sql
from .terms import regex_name_filter, seg_term

NAME = "regexp_extract(path, '[^/]*$')"


def err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


class Unsupported(ValueError):
    pass


def _anc(dv: int, inclusive: bool = False) -> str:
    """A path's ancestors deeper than `dv` segments (inclusive: from depth
    `dv`, the view root itself), as a list."""
    lo = dv if inclusive else dv + 1
    return f"list_transform(range({lo}, len(string_split(path, '/'))), lambda i: array_to_string(string_split(path, '/')[1:i], '/'))"


@dataclass
class DuckResult:
    hit: bool
    roots: int
    b: int
    o: int
    excluded: int
    stats: dict


class DuckIndex:
    def __init__(self, path_file: str, names_file: str, *, threads: int = 16, mem: str = "48GB", tmp: str | None = None, scan_frac: float = 0.3, rg_batch: int = 512):
        import duckdb
        import pyarrow.parquet as pq

        self.path_file = path_file
        self.scan_frac = scan_frac
        self.rg_batch = rg_batch
        self.con = con = duckdb.connect()
        con.execute(f"SET threads = {threads}")
        con.execute(f"SET memory_limit = '{mem}'")
        con.execute("SET preserve_insertion_order = false")
        con.execute("SET parquet_metadata_cache = true")
        if tmp:
            con.execute(f"SET temp_directory = '{tmp}'")
        self.stats: dict = {}
        t = time.monotonic()
        self.pf = pq.ParquetFile(path_file)
        md = self.pf.metadata
        self.n_rg = md.num_row_groups
        names = [md.schema.column(i).name for i in range(md.num_columns)]
        ci, di = names.index("path"), names.index("depth")
        self.d_max, self.p_min, self.p_max = [], [], []
        for g in range(self.n_rg):
            rg = md.row_group(g)
            ps, ds = rg.column(ci).statistics, rg.column(di).statistics
            self.d_max.append(ds.max if ds is not None and ds.has_min_max else 1 << 30)
            self.p_min.append(ps.min if ps is not None and ps.has_min_max else "")
            self.p_max.append(ps.max if ps is not None and ps.has_min_max else "\U0010ffff")
        self.stats["footer_s"] = round(time.monotonic() - t, 2)
        t = time.monotonic()
        con.execute(f"CREATE TABLE vocab AS SELECT lower(name) AS l, rgs FROM read_parquet({lit(names_file)})")
        self.names = con.execute("SELECT count(*) FROM vocab").fetchone()[0]
        self.stats["vocab_s"] = round(time.monotonic() - t, 2)
        self._view_tot: dict[str, tuple[int, int]] = {}

    def memory(self) -> int:
        return int(self.con.execute("SELECT coalesce(sum(memory_usage_bytes), 0) FROM duckdb_memory()").fetchone()[0])

    def view_tot(self, view: str) -> tuple[int, int]:
        if view not in self._view_tot:
            where = "depth = 1" if view == "" else f"depth = {view.count('/') + 1} AND path = {lit(view)}"
            b, o = self.con.execute(f"SELECT coalesce(sum(size), 0), coalesce(sum(n_files), 0) FROM read_parquet({lit(self.path_file)}) WHERE {where}").fetchone()
            self._view_tot[view] = (int(b), int(o))
        return self._view_tot[view]

    def _in_view(self, g: int, view: str, dv: int) -> bool:
        """Row group `g` can hold rows strictly under `view` (by its stats)."""
        if self.d_max[g] <= dv:
            return False
        return view == "" or (self.p_max[g] >= view + "/" and self.p_min[g] < view + "0")

    def _rows(self, cond: str, stats: dict, view: str = "", dv: int = 0) -> None:
        """`rows(path, size, n_files)`: the rows strictly under `view` whose
        lowercase name passes `cond` (SQL over `l`), via the pinned `rgs`
        (pruned to the view by row-group stats) or a scan."""
        con = self.con
        con.execute(f"CREATE OR REPLACE TEMP TABLE cn AS SELECT l, rgs FROM vocab WHERE {cond}")
        n_names, n_null = con.execute("SELECT count(*), count(*) FILTER (rgs IS NULL) FROM cn").fetchone()
        rgs = [r for (r,) in con.execute("SELECT DISTINCT unnest(string_split(rgs, ','))::INTEGER AS g FROM cn WHERE rgs IS NOT NULL ORDER BY g").fetchall()]
        stats.update(names=int(n_names), null_rgs=int(n_null), rgs_all=len(rgs))
        rgs = [g for g in rgs if self._in_view(g, view, dv)]
        stats["rgs"] = len(rgs)
        con.execute("CREATE OR REPLACE TEMP TABLE rows (path VARCHAR, size BIGINT, n_files BIGINT)")
        keep = f"lower({NAME}) IN (SELECT l FROM cn)"
        if n_null or len(rgs) > self.scan_frac * self.n_rg:
            stats["map"] = "scan"
            vw = "" if view == "" else f" AND depth > {dv} AND path >= {lit(view + '/')} AND path < {lit(view + '0')}"
            con.execute(f"INSERT INTO rows SELECT path, size, n_files FROM read_parquet({lit(self.path_file)}) WHERE {keep}{vw}")
            return
        stats["map"] = "rgs"
        for i in range(0, len(rgs), self.rg_batch):
            tbl = self.pf.read_row_groups(rgs[i : i + self.rg_batch], columns=["path", "size", "n_files"], use_threads=True)
            con.register("rg_rows", tbl)
            con.execute(f"INSERT INTO rows SELECT path, size, n_files FROM rg_rows WHERE {keep}")
            con.unregister("rg_rows")

    def _children(self, stems: list[str]) -> int:
        """Append the children of these dirs to `rows` (depth / path-range
        pushdown on the `path` sort)."""
        n = 0
        for p in stems:
            d = p.count("/") + 2
            n += self.con.execute(f"""INSERT INTO rows SELECT path, size, n_files FROM read_parquet({lit(self.path_file)})
                WHERE depth = {d} AND path >= {lit(p + '/')} AND path < {lit(p + '0')}""").fetchone()[0]
        return n

    def candidates(self, ast: Ast, stats: dict, view: str = "", dv: int = 0) -> None:
        """`cr(path, b, o, p, n)`: candidate paths (slices summed) with their
        full-path flags."""
        con = self.con
        regex = [m for a in ast.alts for m in a if m.kind == "regex"] + [m for m in ast.neg if m.kind == "regex"]
        if regex:
            if len(ast.alts) != 1 or len(ast.alts[0]) != 1 or ast.neg:
                raise Unsupported("a regex mixed with other terms")
            src = regex[0].source
            plan = regex_name_filter(src)
            if plan is None:
                stats["map"] = "full-scan"
                con.execute(f"""CREATE OR REPLACE TEMP TABLE rows AS SELECT path, size, n_files FROM read_parquet({lit(self.path_file)})
                    WHERE regexp_matches(path, {lit(src)}, 'i')""")
            else:
                self._rows(f"regexp_matches(l, {lit(plan.name_re)}, 'i')", stats, view, dv)
            flags = f"regexp_matches(path, {lit(src)}, 'i') AS p, false AS n"
        else:
            matchers = list(dict.fromkeys([m for a in ast.alts for m in a] + list(ast.neg)))
            terms = {m: seg_term(m) for m in matchers}
            if any(t.trivial for t in terms.values()):
                raise Unsupported("a term that constrains no segment")
            self._rows("(" + " OR ".join(t.name.sql("l") for t in terms.values()) + ")", stats, view, dv)
            strict = [t for t in terms.values() if t.strict]
            if strict:
                cond = " OR ".join(f"regexp_matches(lower(path), {lit(t.suffix_re)})" for t in strict)
                stems = [p for (p,) in con.execute(f"SELECT DISTINCT path FROM rows WHERE {cond} ORDER BY path").fetchall()]
                # `rows` holds only paths under the view: the view itself may be a stem.
                if view and any(re.search(t.suffix_re, view.lower()) for t in strict):
                    stems.append(view)
                stats["stems"] = len(stems)
                stats["children"] = self._children(stems)
            flags = f"{pos_sql(ast)} AS p, {neg_sql(ast)} AS n"
        con.execute(f"""CREATE OR REPLACE TEMP TABLE cr AS
            SELECT path, sum(size)::BIGINT AS b, sum(n_files)::BIGINT AS o, any_value(p) AS p, any_value(n) AS n FROM (
                SELECT path, size, n_files, {flags} FROM (SELECT path, size, n_files, lower(path) AS lp FROM rows))
            GROUP BY path""")
        stats["cands"] = con.execute("SELECT count(*) FROM cr").fetchone()[0]

    def evaluate(self, ast: Ast, view: str) -> DuckResult:
        con = self.con
        t0 = time.monotonic()
        stats: dict = {}
        dv = 0 if view == "" else view.count("/") + 1
        self.candidates(ast, stats, view, dv)
        stats["cands_s"] = round(time.monotonic() - t0, 3)
        under = "true" if view == "" else f"starts_with(path, {lit(view + '/')})"
        con.execute(f"CREATE OR REPLACE TEMP TABLE u AS SELECT * FROM cr WHERE {under}")
        hit = compile_query(ast)(view)
        if hit:
            vb, vo = self.view_tot(view)
            con.execute(f"CREATE OR REPLACE TEMP TABLE roots AS SELECT {lit(view)} AS path, {vb}::BIGINT AS b, {vo}::BIGINT AS o")
        else:
            con.execute("CREATE OR REPLACE TEMP TABLE s AS SELECT path, b, o FROM u WHERE p AND NOT n")
            con.execute(f"""CREATE OR REPLACE TEMP TABLE roots AS SELECT s.* FROM s ANTI JOIN (
                SELECT DISTINCT x.path FROM (SELECT path, unnest({_anc(dv)}) AS a FROM s) x SEMI JOIN s ON s.path = x.a
            ) y ON s.path = y.path""")
        con.execute("CREATE OR REPLACE TEMP TABLE ng AS SELECT path, b, o FROM u WHERE n")
        con.execute(f"""CREATE OR REPLACE TEMP TABLE ex AS SELECT ng.* FROM ng ANTI JOIN (
            SELECT DISTINCT x.path FROM (SELECT path, unnest({_anc(dv)}) AS a FROM ng) x SEMI JOIN ng ON ng.path = x.a
        ) y ON ng.path = y.path""")
        con.execute(f"""CREATE OR REPLACE TEMP TABLE exr AS SELECT path, b, o, arg_max(r, len(r)) AS r FROM (
            SELECT ex.path, ex.b, ex.o, x.a AS r FROM (SELECT path, unnest({_anc(dv, inclusive=True)}) AS a FROM ex) x
            JOIN roots ON roots.path = x.a JOIN ex ON ex.path = x.path
        ) GROUP BY path, b, o""")
        con.execute("""CREATE OR REPLACE TEMP TABLE net AS SELECT roots.path, roots.b - coalesce(c.b, 0) AS b, roots.o - coalesce(c.o, 0) AS o
            FROM roots LEFT JOIN (SELECT r, sum(b) AS b, sum(o) AS o FROM exr GROUP BY r) c ON c.r = roots.path""")
        n, b, o = con.execute("SELECT count(*), coalesce(sum(b), 0), coalesce(sum(o), 0) FROM net").fetchone()
        ne = con.execute("SELECT count(*) FROM exr").fetchone()[0]
        stats["s"] = round(time.monotonic() - t0, 4)
        return DuckResult(bool(hit), int(n), int(b), int(o), int(ne), stats)

    def roots_arrow(self):
        """The last `evaluate`'s match roots, sorted (a chunked Arrow array)."""
        return self.con.execute("SELECT path FROM net ORDER BY path").to_arrow_table().column(0)

    def roots(self) -> list[str]:
        return self.roots_arrow().to_pylist()
