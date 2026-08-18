#!/usr/bin/env python
"""Layer-2 parquet -> the site's layer-3 JSONs (tree/age/meta) for a non-GCS store.

    cw-webdata.py <layer2.parquet> <outdir> [--bucket marin-us-east-02a]
                  [--label "Marin CoreWeave"] [--asof 2026-08-15] [--min-frac 0.0002]

The site's TreeNode is {n, b, o, d?, cb?, c?}; the attribution fields (tm/sh/us)
are optional and their absence makes the app fall back to tree-coloring, so a
store with no ownership overlay renders fine. That's what makes piggybacking a
CoreWeave tree onto the existing app cheap -- no attribution pipeline needed.

Three shapes the site actually requires (an earlier cut of this script got the
latter two wrong, which blanked the age chart and crashed the cost panel):

- tree.json  root -> bucket -> d1 -> ...  The app indexes `kidPath[2]` for its
  top-level-dir color slots (Treemap.slotOf) and reads `root.c[].c[]` for the
  category order, so the store root must wrap a *bucket* node, exactly like the
  GCS side's "marin GCS" -> "marin-us-east5" -> "checkpoints".
- age.json   AgeRow[] = {d, d1, b, o} where `d` is the created day in *epoch
  days* -- not a "YYYY-MM" string. AgeChart drops rows failing
  `Number.isFinite(r.d)` and buckets by day/week/month itself.
- meta.json  {asof, generated, total_bytes, total_objects, class_bytes}. The
  names matter: App.tsx reads meta.total_bytes / .total_objects, and iterates
  meta.class_bytes for the cost panel.

`d` on a tree node is the bytes-weighted mean mtime in epoch days (the age
lens' unit). Sub-threshold siblings fold into one "(other)" node.

class_bytes is emitted empty for stores with no GCS-style storage classes: the
site prices classes with GCS US list rates, and applying those to CoreWeave
would invent a number. The store descriptor's `prices: false` hides the panel.
"""
import json
import os
from argparse import ArgumentParser

import duckdb


def main():
    p = ArgumentParser(description=__doc__)
    p.add_argument('-a', '--asof', help='scan date, YYYY-MM-DD (defaults to the max file mtime)')
    p.add_argument('-b', '--bucket', help='bucket node name (defaults to the parquet stem)')
    p.add_argument('-l', '--label', default='Marin CoreWeave', help='store root node label')
    p.add_argument('-m', '--min-frac', type=float, default=0.0002, help='drop dirs below this fraction of total bytes')
    p.add_argument('src')
    p.add_argument('outdir')
    args = p.parse_args()

    src, outdir = args.src, args.outdir
    con = duckdb.connect()
    rp = f"read_parquet('{src}')"

    cols = {r[0] for r in con.execute(f'DESCRIBE SELECT * FROM {rp}').fetchall()}
    mm = 'mtime_mean' if 'mtime_mean' in cols else None
    d_expr = f'CAST({mm} / 86400 AS BIGINT)' if mm else 'NULL'

    total, total_objects, root_d = con.execute(f'SELECT size, n_files, {d_expr} FROM {rp} WHERE depth = 0').fetchone()
    floor = int(total * args.min_frac)

    rows = con.execute(f"""
        SELECT path, size, n_files, {d_expr} AS d
        FROM {rp}
        WHERE kind = 'dir' AND size >= {floor}
        ORDER BY depth, path
    """).fetchall()

    nodes: dict[str, dict] = {}
    for path, size, nfiles, d in rows:
        n = {'n': path.rsplit('/', 1)[-1], 'b': int(size), 'o': int(nfiles)}
        if d is not None:
            n['d'] = int(d)
        nodes[path] = n

    # Link children to parents; anything below the floor is folded per-parent.
    kids: dict[str, list] = {}
    for path, n in nodes.items():
        if path == '.':
            continue
        parent = path.rsplit('/', 1)[0] if '/' in path else '.'
        kids.setdefault(parent, []).append((path, n))

    for parent, children in kids.items():
        parent_node = nodes.get(parent)
        if not parent_node:
            continue
        children.sort(key=lambda kv: -kv[1]['b'])
        parent_node['c'] = [n for _, n in children]
        # Bytes not accounted for by the kept children = direct files + folded dirs.
        rest = parent_node['b'] - sum(n['b'] for n in parent_node['c'])
        rest_o = parent_node['o'] - sum(n['o'] for n in parent_node['c'])
        if rest > floor:
            parent_node['c'].append({'n': '(other)', 'b': int(rest), 'o': int(max(rest_o, 0))})

    # The '.' row is the *bucket*; the site expects a store root above it, so
    # that `[root, bucket, d1]` indexing and `root.c[].c[]` category scanning
    # line up with the GCS snapshots.
    bucket = nodes['.']
    bucket['n'] = args.bucket or os.path.splitext(os.path.basename(src))[0]
    tree = {'n': args.label, 'b': int(total), 'o': int(total_objects), 'c': [bucket]}
    if root_d is not None:
        tree['d'] = int(root_d)

    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, 'tree.json'), 'w') as f:
        json.dump(tree, f, separators=(',', ':'))

    # Age: bytes/objects per (created day, top-level dir). `d` is epoch days,
    # `d1` the first path component -- the key AgeChart colors by in tree mode.
    age = [
        {'d': int(d), 'd1': d1, 'b': int(b), 'o': int(o)}
        for d, d1, b, o in con.execute(f"""
            SELECT CAST(mtime / 86400 AS BIGINT) AS d,
                   split_part(path, '/', 1) AS d1,
                   SUM(size) AS b,
                   COUNT(*) AS o
            FROM {rp}
            WHERE kind = 'file' AND mtime > 0
            GROUP BY 1, 2
            ORDER BY 1, 2
        """).fetchall()
    ]
    with open(os.path.join(outdir, 'age.json'), 'w') as f:
        json.dump(age, f, separators=(',', ':'))

    asof = args.asof or con.execute(
        f"SELECT strftime(to_timestamp(max(mtime)), '%Y-%m-%d') FROM {rp} WHERE kind = 'file'"
    ).fetchone()[0]
    meta = {
        'asof': asof,
        'generated': asof,
        'total_bytes': int(total),
        'total_objects': int(total_objects),
        'class_bytes': {},
    }
    with open(os.path.join(outdir, 'meta.json'), 'w') as f:
        json.dump(meta, f, separators=(',', ':'))

    def count(n):
        return 1 + sum(count(c) for c in n.get('c', []))

    days = [r['d'] for r in age]
    print(f'root {total / 1e12:,.1f} TB ({total / 2**40:,.1f} TiB) / {total_objects:,} objects')
    print(f'tree nodes: {count(tree):,} (floor {floor / 1e9:,.1f} GB = {args.min_frac:.2%})')
    print(f'age rows: {len(age):,} over days {min(days)}..{max(days)}' if age else 'age rows: 0')
    print(f'asof {asof}; wrote {outdir}/{{tree,age,meta}}.json')


if __name__ == '__main__':
    main()
