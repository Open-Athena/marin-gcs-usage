#!/usr/bin/env python
"""Recursive diff between two layer-2 parquets -> site diff.json.

    cw-diff.py <prev.parquet> <curr.parquet> <out.json>
               [--prev-id 2026-08-19T1008] [--curr-id 2026-08-19T1201]
               [--budget 400] [--max-depth 8] [--top 500]

Runs disk-tree's best-first `recursive_diff` (spec: diff-and-search.md §3a) so
"what changed since the last scan" is localized server-side — a delta N levels
deep surfaces as its own row instead of an undifferentiated blob at depth 1.
Precomputed at publish time because the site is static: consecutive snapshot
pairs only, so there's no pair explosion.

Row keys are terse like tree.json: p=path d=depth k=kind s=status a/b=bytes
oa/ob=n_desc x=expanded pr=pruned (change may hide below a pruned dir).
"""
import json
from argparse import ArgumentParser

import pyarrow.parquet as pq

from disk_tree.diff import ScanSource, recursive_diff


def make_load(parquet_path: str):
    def load(blob_ref, max_depth=None, min_depth=None, follow_refs=False, path_prefix=None):
        filters = []
        if min_depth is not None:
            filters.append(('depth', '>=', min_depth))
        if max_depth is not None:
            filters.append(('depth', '<=', max_depth))
        if path_prefix:
            # Row-group pruning when the file is path-sorted; ScanSource
            # re-enforces the prefix mask, so this is purely an optimization.
            filters.append(('path', '>=', path_prefix + '/'))
            filters.append(('path', '<', path_prefix + '0'))
        cols = ['path', 'size', 'mtime', 'n_desc', 'n_children', 'kind', 'depth']
        return pq.read_table(parquet_path, columns=cols, filters=filters or None).to_pandas()
    return load


def root_stats(parquet_path: str) -> tuple[int, int]:
    t = pq.read_table(parquet_path, columns=['size', 'n_files'], filters=[('depth', '==', 0)])
    row = t.to_pylist()[0]
    return int(row['size']), int(row['n_files'])


def main():
    p = ArgumentParser(description=__doc__)
    p.add_argument('-b', '--budget', type=int, default=400, help='max directory expansions for the walk')
    p.add_argument('-c', '--curr-id', default=None, help='snapshot id of <curr> (recorded in the JSON)')
    p.add_argument('-d', '--max-depth', type=int, default=8, help='deepest level to descend to')
    p.add_argument('-p', '--prev-id', default=None, help='snapshot id of <prev> (recorded in the JSON)')
    p.add_argument('-t', '--top', type=int, default=500, help='max rows kept in the JSON (by |Δsize|)')
    p.add_argument('prev')
    p.add_argument('curr')
    p.add_argument('out')
    args = p.parse_args()

    src_a = ScanSource(blob=args.prev, scan_path='', uri='', load=make_load(args.prev))
    src_b = ScanSource(blob=args.curr, scan_path='', uri='', load=make_load(args.curr))
    result = recursive_diff(src_a, src_b, budget=args.budget, max_depth=args.max_depth)

    total_a, objects_a = root_stats(args.prev)
    total_b, objects_b = root_stats(args.curr)

    rows = [
        {
            'p': r.path, 'd': r.depth, 'k': r.kind, 's': r.status,
            'a': r.size_a, 'b': r.size_b, 'oa': r.n_desc_a, 'ob': r.n_desc_b,
            **({'x': True} if r.expanded else {}),
            **({'pr': True} if r.pruned else {}),
        }
        for r in result.rows[:args.top]
        if r.status != 'unchanged'
    ]
    out = {
        'prev': args.prev_id,
        'curr': args.curr_id,
        'total_a': total_a,
        'total_b': total_b,
        'objects_a': objects_a,
        'objects_b': objects_b,
        'expansions': result.expansions,
        'truncated': result.truncated or len(result.rows) > args.top,
        'rows': rows,
    }
    with open(args.out, 'w') as fh:
        json.dump(out, fh)
    print(f"{len(rows)} delta rows, {result.expansions} expansions, "
          f"Δtotal {total_b - total_a:+,} bytes -> {args.out}")


if __name__ == '__main__':
    main()
