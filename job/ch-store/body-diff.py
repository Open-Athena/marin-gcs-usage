#!/usr/bin/env python3
"""Two bodies of one request (the box's, the Worker's): where they differ — top-level fields and the tree,
node by node (by path), or diff rows (by `p`). Labels (`tier`, `index`, coverage) and `pv` are ignored.
    body-diff.py BOX.json WORKER.json"""
import json
import sys

a, b = (json.load(open(f)) for f in sys.argv[1:3])
skip = {"tier", "index", "partial", "partialReason", "approximate", "approximateReason", "firstPaint", "tree", "rows"}
for k in sorted(set(a) | set(b)):
    if k not in skip and a.get(k) != b.get(k):
        print(f"field {k}: box {str(a.get(k))[:200]} | worker {str(b.get(k))[:200]}")


def flat(n, p, out):
    out[p] = {k: v for k, v in n.items() if k not in ("c", "pv")}
    for c in n.get("c", []):
        flat(c, f"{p}/{c['n']}" if p else c["n"], out)
    return out


if "tree" in a or "tree" in b:
    ta, tb = flat(a.get("tree", {}), "", {}), flat(b.get("tree", {}), "", {})
    only_a, only_b = sorted(set(ta) - set(tb)), sorted(set(tb) - set(ta))
    print(f"tree: {len(ta)} vs {len(tb)} nodes; box only {len(only_a)}, worker only {len(only_b)}")
    for p in only_a[:8]:
        print("  box only", p, ta[p])
    for p in only_b[:8]:
        print("  worker only", p, tb[p])
    n = 0
    for p in sorted(set(ta) & set(tb)):
        if ta[p] != tb[p]:
            n += 1
            if n <= 12:
                print("  differ", p, {k: (ta[p].get(k), tb[p].get(k)) for k in set(ta[p]) | set(tb[p]) if ta[p].get(k) != tb[p].get(k)})
    print(f"  {n} common nodes differ")
if "rows" in a:
    ra, rb = {r["p"]: r for r in a["rows"]}, {r["p"]: r for r in b["rows"]}
    print(f"rows: {len(ra)} vs {len(rb)}; box only {sorted(set(ra) - set(rb))[:8]}; worker only {sorted(set(rb) - set(ra))[:8]}")
    d = [p for p in set(ra) & set(rb) if ra[p] != rb[p]]
    for p in sorted(d)[:10]:
        print("  differ", p, ra[p], rb[p])
