#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyarrow"]
# ///
"""The static name-search reader's fixture (`staticNames.test.ts`): suffix shards shaped like
`dt-cloud static-names shards` writes them (specs/architecture/static-name-search.md), over
made-up intervals, plus a brute-force first-hit oracle's answers.

- `sx/s0000.parquet`, `sx/s0001.parquet`: one row per (lowercase name suffix of ≥ 3 characters,
  version), depth ≥ 1, columns `s, depth, path, usr, vf, vt, size, n_files`, sorted
  `(s, path, usr, vf)`, zstd, `usr` dictionary-encoded, statistics on, timestamps in ms — the
  writer's settings, but `RG` rows per group (not 8192) so a term spans several groups. Shard 0
  holds suffixes whose first three characters sort below `SPLIT`, shard 1 the rest.
- `shards.json`: the shard plan (`i, lo, hi, rows`), as `plan-shards` writes it.
- `expected.json`: per (term, date) `{bucket: [bytes, objects]}` from the oracle: for each live
  `(path, usr)` slice on the date (depth ≥ 1) whose lowercase name contains the term and whose
  lowercase parent path does not, its bucket gets its size and objects. It shares no code with
  the suffix layout, so the reader is checked against the query's definition.

Regenerate: `site/functions/_lib/fixtures/static-names/gen.py` (uv runs it).
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).parent
RG = 4
SPLIT = "m"
OPEN = datetime(2106, 1, 1, tzinfo=timezone.utc)
D = {d: datetime.fromisoformat(d).replace(tzinfo=timezone.utc) for d in ("2026-08-01", "2026-09-01", "2026-10-01")}
SCHEMA = pa.schema([
    pa.field("s", pa.string(), nullable=False),
    pa.field("depth", pa.uint8(), nullable=False),
    pa.field("path", pa.string(), nullable=False),
    pa.field("usr", pa.string(), nullable=False),
    pa.field("vf", pa.timestamp("ms", tz="UTC"), nullable=False),
    pa.field("vt", pa.timestamp("ms", tz="UTC"), nullable=False),
    pa.field("size", pa.int64(), nullable=False),
    pa.field("n_files", pa.int64(), nullable=False),
])
A, S, O = D["2026-08-01"], D["2026-09-01"], D["2026-10-01"]

#: Versions `(path, usr, vf, vt, size, n_files)`. Exercises: a `foo` dir covering `foo` descendants
#: (first hit), case variants, a name holding the term twice (deduped), versions opening and closing
#: between the dates, two owner slices of one path, a non-ASCII name, a bucket-level row (depth 0,
#: never a suffix row), a term only in the parent (not a hit) and a long shared suffix (> 24 chars).
VERSIONS = [
    ("bkt-a", "", A, OPEN, 10_000, 100),
    ("bkt-a/Foo", "", A, S, 700, 7),
    ("bkt-a/Foo", "", S, OPEN, 900, 9),
    ("bkt-a/Foo/foo.txt", "", A, OPEN, 5, 1),
    ("bkt-a/Foo/bar", "", A, OPEN, 300, 3),
    ("bkt-a/data/foofoo.bin", "", A, OPEN, 40, 1),
    ("bkt-a/data/FOO-2.bin", "alice", S, OPEN, 60, 1),
    ("bkt-a/data/FOO-2.bin", "bob", A, O, 61, 1),
    ("bkt-b/xfoox", "", A, S, 1_000, 10),
    ("bkt-b/xfoox/inner-foo", "", A, S, 3, 1),
    ("bkt-b/zz/Ñandú-foo.json", "", S, OPEN, 77, 1),
    ("bkt-b/zz/barfoo", "", A, OPEN, 8, 1),
    ("bkt-b/zz/abcdefghijklmnopqrstuvwxyz-0001.parquet", "", A, OPEN, 11, 1),
    ("bkt-b/zz/abcdefghijklmnopqrstuvwxyz-0002.parquet", "", S, OPEN, 12, 1),
    ("bkt-b/qux", "", A, OPEN, 500, 5),
    ("bkt-b/qux/mmm.txt", "", A, OPEN, 13, 1),
    ("bkt-b/qux/plain", "", A, O, 14, 1),
]
TERMS = ["foo", "foo.", "foo-2", "oof", "fooz", "qux", "mmm", "andú", "abcdefghijklmnopqrstuvwxyz-0001", "bkt", "zzz"]


def suffix_rows() -> list[dict]:
    rows = []
    for path, usr, vf, vt, size, n in VERSIONS:
        depth = path.count("/")
        name = path.rsplit("/", 1)[-1].lower()
        if depth < 1 or len(name) < 3:
            continue
        for p in range(len(name) - 2):
            rows.append(dict(s=name[p:], depth=depth, path=path, usr=usr, vf=vf, vt=vt, size=size, n_files=n))
    # Code-point order, as DuckDB sorts (the writer's `ORDER BY s, path, usr, vf`).
    return sorted(rows, key=lambda r: (r["s"], r["path"], r["usr"], r["vf"]))


def write(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    t = pa.Table.from_pylist(rows, schema=SCHEMA)
    with pq.ParquetWriter(out, SCHEMA, compression="zstd", use_dictionary=["usr"], write_statistics=True,
                          coerce_timestamps="ms", allow_truncated_timestamps=False) as w:
        for off in range(0, t.num_rows, RG):
            w.write_table(t.slice(off, RG), row_group_size=RG)


def oracle(term: str, day: datetime) -> dict:
    totals: dict[str, list[int]] = {}
    for path, usr, vf, vt, size, n in VERSIONS:
        if path.count("/") < 1 or not (vf <= day < vt):
            continue
        parent, name = path.rsplit("/", 1)
        if term in name.lower() and term not in parent.lower():
            t = totals.setdefault(path.split("/", 1)[0], [0, 0])
            t[0] += size
            t[1] += n
    return dict(sorted(totals.items()))


def main() -> None:
    rows = suffix_rows()
    lo = [r for r in rows if r["s"][:3] < SPLIT]
    hi = [r for r in rows if r["s"][:3] >= SPLIT]
    write(lo, HERE / "sx" / "s0000.parquet")
    write(hi, HERE / "sx" / "s0001.parquet")
    shards = {"shards": [{"i": 0, "lo": "   ", "hi": SPLIT, "rows": len(lo), "prefixes": len({r["s"][:3] for r in lo})},
                         {"i": 1, "lo": SPLIT, "hi": None, "rows": len(hi), "prefixes": len({r["s"][:3] for r in hi})}]}
    (HERE / "shards.json").write_text(json.dumps(shards, indent=1) + "\n")
    expected = {t: {d: oracle(t, day) for d, day in D.items()} for t in TERMS}
    (HERE / "expected.json").write_text(json.dumps(expected, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(lo)} + {len(hi)} suffix rows")


if __name__ == "__main__":
    main()
