#!/usr/bin/env python
"""Layer-2 parquet -> the site's layer-3 JSONs (tree/age/meta) for a non-GCS store.

    cw-webdata.py <outdir> <bucket>=<layer2.parquet>... [--label "Marin CoreWeave"]
                  [--asof 2026-08-15] [--min-frac 0.0002]

One `<bucket>=<layer-2>` pair per bucket of the scan, in the deployment's
order (specs/cw-multi-bucket.md §2): the store root wraps one node per bucket,
one byte floor (`min_frac` × the sum) folds each bucket's small dirs, the age
rows are the union, and meta.json carries per-bucket totals under `buckets`.

The site's TreeNode is {n, b, o, d?, cb?, c?}; the attribution fields (tm/sh/us)
are optional and their absence makes the app fall back to tree-coloring, so a
store with no ownership overlay renders fine. That's what makes piggybacking a
CoreWeave tree onto the existing app cheap -- no attribution pipeline needed.

Three shapes the site actually requires (an earlier cut of this script got the
latter two wrong, which blanked the age chart and crashed the cost panel):

- tree.json  root -> bucket -> d1 -> ...  The app indexes `kidPath[2]` for its
  top-level-dir color slots (Treemap.slotOf) and reads `root.c[].c[]` for the
  category order, so the store root must wrap *bucket* nodes, exactly like the
  GCS side's "marin GCS" -> "marin-us-east5" -> "checkpoints".
- age.json   AgeRow[] = {d, d1, b, o} where `d` is the created day in *epoch
  days* -- not a "YYYY-MM" string. AgeChart drops rows failing
  `Number.isFinite(r.d)` and buckets by day/week/month itself.
- meta.json  {asof, generated, total_bytes, total_objects, class_bytes,
  buckets}. The names matter: App.tsx reads meta.total_bytes / .total_objects,
  and iterates meta.class_bytes for the cost panel; `buckets` = {<name>:
  {total_bytes, total_objects}} is what the digest's quota line reads (the
  primary's share of the sum).

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


def bucket_tree(con, bucket: str, src: str, floor: int):
    """One bucket's subtree from its layer-2 parquet: the bucket node (the
    `.` row, renamed) with children folded under ``floor``; plus its total
    bytes/objects, root mean-mtime day, and age rows."""
    rp = f"read_parquet('{src}')"
    cols = {r[0] for r in con.execute(f'DESCRIBE SELECT * FROM {rp}').fetchall()}
    mm = 'mtime_mean' if 'mtime_mean' in cols else None
    d_expr = f'CAST({mm} / 86400 AS BIGINT)' if mm else 'NULL'

    rows = con.execute(f"""
        SELECT path, size, n_files, {d_expr} AS d
        FROM {rp}
        WHERE kind = 'dir' AND (size >= {floor} OR path = '.')
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

    # The '.' row is the *bucket* node under the store root.
    node = nodes['.']
    node['n'] = bucket

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
    max_mtime = con.execute(f"SELECT max(mtime) FROM {rp} WHERE kind = 'file'").fetchone()[0]
    return node, age, max_mtime


def main():
    p = ArgumentParser(description=__doc__)
    p.add_argument('-a', '--asof', help='scan date, YYYY-MM-DD (defaults to the max file mtime)')
    p.add_argument('-l', '--label', default='Marin CoreWeave', help='store root node label')
    p.add_argument('-m', '--min-frac', type=float, default=0.0002, help='drop dirs below this fraction of total bytes')
    p.add_argument('outdir')
    p.add_argument('sources', nargs='+', help='<bucket>=<layer2.parquet>, one per bucket, in deployment order')
    args = p.parse_args()

    outdir = args.outdir
    sources: list[tuple[str, str]] = []
    for s in args.sources:
        bucket, eq, src = s.partition('=')
        if not eq or not bucket or not src:
            p.error(f'expected <bucket>=<layer2.parquet>, got {s!r}')
        sources.append((bucket, src))
    if len({b for b, _ in sources}) != len(sources):
        p.error(f'duplicate bucket in {[b for b, _ in sources]}')

    con = duckdb.connect()
    # One floor across the scan: each bucket's share of the sum, as the site's
    # root view folds.
    totals = {
        b: con.execute(f"SELECT size, n_files FROM read_parquet('{src}') WHERE depth = 0").fetchone()
        for b, src in sources
    }
    total = sum(int(t[0]) for t in totals.values())
    total_objects = sum(int(t[1]) for t in totals.values())
    floor = int(total * args.min_frac)

    buckets = []
    age = []
    max_mtime = 0
    for b, src in sources:
        node, rows, mt = bucket_tree(con, b, src, floor)
        buckets.append(node)
        age += rows
        max_mtime = max(max_mtime, mt or 0)
    age.sort(key=lambda r: (r['d'], r['d1']))

    tree = {'n': args.label, 'b': int(total), 'o': int(total_objects), 'c': buckets}
    # the root's mean written day: bytes-weighted over the buckets that have one
    wd = [(n['d'], n['b']) for n in buckets if 'd' in n]
    if wd and sum(b for _, b in wd):
        tree['d'] = int(sum(d * b for d, b in wd) / sum(b for _, b in wd))

    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, 'tree.json'), 'w') as f:
        json.dump(tree, f, separators=(',', ':'))
    with open(os.path.join(outdir, 'age.json'), 'w') as f:
        json.dump(age, f, separators=(',', ':'))

    asof = args.asof or con.execute(f"SELECT strftime(to_timestamp({max_mtime}), '%Y-%m-%d')").fetchone()[0]
    meta = {
        'asof': asof,
        'generated': asof,
        'total_bytes': int(total),
        'total_objects': int(total_objects),
        'class_bytes': {},
        'buckets': {b: {'total_bytes': int(t[0]), 'total_objects': int(t[1])} for b, t in totals.items()},
    }
    with open(os.path.join(outdir, 'meta.json'), 'w') as f:
        json.dump(meta, f, separators=(',', ':'))

    def count(n):
        return 1 + sum(count(c) for c in n.get('c', []))

    days = [r['d'] for r in age]
    print(f'root {total / 1e12:,.1f} TB ({total / 2**40:,.1f} TiB) / {total_objects:,} objects over {len(buckets)} bucket(s)')
    for b, t in totals.items():
        print(f'  {b}: {t[0] / 2**40:,.1f} TiB / {t[1]:,} objects')
    print(f'tree nodes: {count(tree):,} (floor {floor / 1e9:,.1f} GB = {args.min_frac:.2%})')
    print(f'age rows: {len(age):,} over days {min(days)}..{max(days)}' if age else 'age rows: 0')
    print(f'asof {asof}; wrote {outdir}/{{tree,age,meta}}.json')


if __name__ == '__main__':
    main()
