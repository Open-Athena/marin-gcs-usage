#!/usr/bin/env python3
"""The catalog's verification terms (deterministic): the build's named terms (`terms.txt`), one- and
two-character literals, literals inside bucket names, hash-sampled catalog members across lengths, and
hash-sampled non-members just under V (the static reader's worst cases), from a generation's census.

    catalog-terms.py CENSUS_DIR V > catalog-terms.txt   # CENSUS_DIR: a local copy of `catalog/census/`
"""
import hashlib
import sys
from pathlib import Path

import duckdb

census, V = sys.argv[1], int(sys.argv[2])
here = Path(__file__).parent
named = [t for t in (here / "terms.txt").read_text().splitlines() if t]
short = ["a", "e", "0", "1", "_", ".", "-", "é", "3p", "6h", "zz", "ab", "_s", ".j", "q", "x9", "~"]
buckets = ["east5", "us-east1", "eu-west4", "central2", "us-central1", "marin-us-c", "west4", "-us-", "marin"]


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
