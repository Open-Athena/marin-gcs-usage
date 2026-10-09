#!/usr/bin/env python3
"""cw's static-index verification terms (deterministic): named terms from cw's buckets, one- and two-character
literals, literals inside bucket names, hash-sampled catalog members across lengths, and hash-sampled non-members just
under V (the static reader's worst cases), from a generation's census.

    cw-catalog-terms.py CENSUS_DIR V > cw-catalog-terms.txt   # CENSUS_DIR: a local copy of `catalog/census/`
"""
import hashlib
import sys

import duckdb

census, V = sys.argv[1], int(sys.argv[2])
named = [".safetensors", "model-0000", ".json", "step-", "checkpoint", "tmp", "ttl=", "glm52", "codecontests", "rollouts",
         "qwen", "trace_jobs", "eval_sessions", ".parquet", "_success", "shard", ".jsonl.gz", "optimizer", "iris", "skyrl"]
short = ["a", "e", "0", "1", "_", ".", "-", "=", "zz", "ab", "_s", ".j", "q", "x9", "~"]
buckets = ["marin-us-east-02a", "east-02", "hero", "checkpoints", "east-06a", "rhoarnet", "west-04a", "-us-", "marin"]


def h(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


con = duckdb.connect()
nodes = con.execute(f"SELECT q, rows FROM read_parquet('{census}/*.parquet')").fetchall()
members = sorted((q for q, n in nodes if n > V), key=h)
near = sorted((q for q, n in nodes if 0.8 * V < n <= V), key=h)
by_len: dict[int, list[str]] = {}
for q in members:
    by_len.setdefault(min(len(q), 12), []).append(q)
sampled = [q for L in sorted(by_len) for q in by_len[L][:4]]
out, seen = [], set()
for t in named + short + buckets + sampled + near[:15]:
    if t.lower() not in seen and "\n" not in t and "/" not in t:
        seen.add(t.lower())
        out.append(t)
print("\n".join(out))
