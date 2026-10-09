#!/usr/bin/env python3
"""A markdown table per request: the box warm (median of trials ≥ 1), the box cold (caches dropped), the
Worker uncached (median), seconds.   table.py BOX_WARM.jsonl BOX_COLD.jsonl WORKER.jsonl"""
import json
import statistics
import sys


def load(p):
    return [json.loads(line) for line in open(p) if line.strip()]


warm, cold, worker = (load(p) for p in sys.argv[1:4])


def med(rs, name, trials=None):
    ms = [r["ms"] for r in rs if r["name"] == name and r["status"] == 200 and (trials is None or r["trial"] in trials)]
    return f"{statistics.median(ms) / 1000:.2f}" if ms else "—"


print("| request | box warm | box cold | Worker (uncached) |")
print("|---|---:|---:|---:|")
for name in dict.fromkeys(r["name"] for r in warm):
    print(f"| `{name}` | {med(warm, name, {1, 2})} | {med(cold, name)} | {med(worker, name)} |")
