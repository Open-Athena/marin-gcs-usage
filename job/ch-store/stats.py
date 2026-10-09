#!/usr/bin/env python3
"""Latency percentiles of `ch-bench` records, per trial: n, statuses, p50 / p90 / max ms, how many over 10 s,
and the slowest few.   stats.py RUN.jsonl [TOP]"""
import json
import statistics
import sys

rs = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
top = int(sys.argv[2]) if len(sys.argv) > 2 else 6
for t in sorted({r["trial"] for r in rs}):
    x = [r for r in rs if r["trial"] == t]
    ms = sorted(r["ms"] for r in x)
    q90 = ms[min(len(ms) - 1, int(0.9 * len(ms)))]
    print(f"trial {t}: n={len(ms)} statuses={sorted({r['status'] for r in x})} p50={statistics.median(ms):.0f} p90={q90} max={ms[-1]} "
          f"over10s={sum(m > 10000 for m in ms)} cold={x[0]['cold']}")
    for r in sorted(x, key=lambda r: -r["ms"])[:top]:
        print(f"   {r['ms']:>7} ms {r['bytes']:>10} B {r['name']}")
