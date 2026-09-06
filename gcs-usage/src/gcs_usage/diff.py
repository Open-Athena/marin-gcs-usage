"""Scan-over-scan diff for the site's "Changes since previous scan" section.

Adapts two dates' ``listing/<date>/path-index.parquet`` files to disk-tree's
best-first ``recursive_diff`` (the same walk cw-s3's ``job/cw-diff.py`` runs
over its L2 parquets) and writes the ``diff.json`` the site's ``DiffTreemap``
renders — the component shipped with the site but never mounted on gcs because
nothing produced its input (specs/branch-parity-discipline.md §diff).

The path-index has one row per ``(path, depth, team, usr)`` attribution slice,
descendant-inclusive, sorted ``(depth, path)`` — so one directory's children
are a single filtered read (row-group pruning on depth + path range) grouped
over the attribution axes. ``depth`` is the segment count, which matches
``ScanSource``'s ``count('/') + 1`` convention exactly. Dir-level only (the
index has no leaf objects): deltas bottom out at directories, which is what a
change treemap wants anyway.
"""

from __future__ import annotations

import json
from typing import Optional

import pandas as pd


_EMPTY_COLS = ["path", "depth", "size", "n_desc", "n_children", "kind", "mtime"]


def _make_load(path_index: str, fs=None):
    """`LoadFn` over one path-index parquet: children rows at one depth under
    one prefix, aggregated over the (team, usr) attribution slices.

    The footer is parsed ONCE (`ParquetFile` + a cached per-row-group stats
    list) — `read_table(filters=…)` re-fetches and re-parses the ~27k-group
    footer on every call, which turns a 400-expansion walk into footer I/O.
    The file is sorted ``(depth, path)``, so each query overlaps a handful of
    contiguous 8k-row groups."""
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path_index, filesystem=fs)
    md = pf.metadata
    names = pf.schema_arrow.names
    d_ix, p_ix = names.index("depth"), names.index("path")
    stats = []  # (depth_min, depth_max, path_min, path_max) per row group
    for i in range(md.num_row_groups):
        rg = md.row_group(i)
        ds, ps = rg.column(d_ix).statistics, rg.column(p_ix).statistics
        stats.append((ds.min, ds.max, ps.min, ps.max))

    def load(
        blob_ref,
        max_depth: Optional[int] = None,
        min_depth: Optional[int] = None,
        follow_refs: bool = False,
        path_prefix: Optional[str] = None,
    ) -> pd.DataFrame:
        lo = min_depth if min_depth is not None else 0
        hi = max_depth if max_depth is not None else 1 << 30
        # '0' is the successor of '/' in ASCII → [prefix+'/', prefix+'0') is
        # exactly the subtree's path range.
        p_lo = path_prefix + "/" if path_prefix else None
        p_hi = path_prefix + "0" if path_prefix else None
        groups = [
            i for i, (dmin, dmax, pmin, pmax) in enumerate(stats)
            if dmax >= lo and dmin <= hi and (p_lo is None or (pmax >= p_lo and pmin < p_hi))
        ]
        if not groups:
            return pd.DataFrame(columns=_EMPTY_COLS)
        df = pf.read_row_groups(groups, columns=["path", "depth", "b", "o"]).to_pandas()
        mask = (df["depth"] >= lo) & (df["depth"] <= hi)
        if p_lo is not None:
            mask &= (df["path"] >= p_lo) & (df["path"] < p_hi)
        df = df[mask]
        if df.empty:
            return pd.DataFrame(columns=_EMPTY_COLS)
        g = df.groupby(["path", "depth"], as_index=False).agg(size=("b", "sum"), n_desc=("o", "sum"))
        g["n_children"] = pd.NA  # unknown here; NaN==NaN in the prune trigger
        g["kind"] = "dir"
        g["mtime"] = pd.NA
        return g

    return load


def _root_stats(load) -> tuple[int, int]:
    """(total bytes, total objects) = the depth-1 (bucket) rows summed."""
    df = load(None, min_depth=1, max_depth=1)
    return int(df["size"].sum()), int(df["n_desc"].sum())


def compute_diff(
    prev_index: str,
    curr_index: str,
    prev_id: Optional[str] = None,
    curr_id: Optional[str] = None,
    budget: int = 400,
    max_depth: int = 8,
    top: int = 500,
    fs=None,
) -> dict:
    """Run the recursive diff and return the site's ``diff.json`` payload."""
    from disk_tree.diff import ScanSource, recursive_diff

    load_a, load_b = _make_load(prev_index, fs), _make_load(curr_index, fs)
    src_a = ScanSource(blob=prev_index, scan_path="", uri="", load=load_a)
    src_b = ScanSource(blob=curr_index, scan_path="", uri="", load=load_b)
    result = recursive_diff(src_a, src_b, budget=budget, max_depth=max_depth)

    total_a, objects_a = _root_stats(load_a)
    total_b, objects_b = _root_stats(load_b)

    rows = [
        {
            "p": r.path, "d": r.depth, "k": r.kind, "s": r.status,
            "a": r.size_a, "b": r.size_b, "oa": r.n_desc_a, "ob": r.n_desc_b,
            **({"x": True} if r.expanded else {}),
            **({"pr": True} if r.pruned else {}),
        }
        for r in result.rows[:top]
        if r.status != "unchanged"
    ]
    return {
        "prev": prev_id,
        "curr": curr_id,
        "total_a": total_a,
        "total_b": total_b,
        "objects_a": objects_a,
        "objects_b": objects_b,
        "expansions": result.expansions,
        "truncated": result.truncated or len(result.rows) > top,
        "rows": rows,
    }


def write_json(payload: dict, out: str) -> None:
    """Write ``payload`` to ``out`` (local path or ``gs://`` via fsspec)."""
    import fsspec

    with fsspec.open(out, "w") as fh:
        json.dump(payload, fh)
