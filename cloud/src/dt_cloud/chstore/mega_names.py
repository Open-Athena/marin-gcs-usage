"""Name-substring bucket totals for any scan, from the one consolidated store.

The append-only store (`ingest.py`: `nodes` versions opened at `vf`,
`closures` closing them at `vt`, `names` the lowercase-basename vocabulary
with its trigram index) already carries every published scan. Its `by_name`
projections (`nodes`: `name, depth, path, usr, vf`; `closures`: `name, depth,
path`) make it a name index over all of them at once: no per-scan index.

A literal (case-insensitive, no slash) answers on scan `D` as:

1. vocabulary: the lowercase basenames containing it (`names`, trigram index
   for three or more characters);
2. postings: the rows with one of those names, live on `D` (`schema.live`:
   opened by `D`, not closed by `D`, both sides read by name). A path is one
   row per owner slice (`usr`; `''` = unowned); its totals are their sum;
3. first hits: a matching row whose parent path does not contain the literal.
   A slash-free literal matches within one segment, so "the parent contains
   it" is exactly "an ancestor directory's name matches", and that ancestor
   already covers this row. A directory hit contributes its recursive
   `size`/`n_files`, an object its own;
4. bucket totals: first hits grouped by their first path segment.

This is the same first-hit rule as `hot_l1.oracle`, read from versioned path
strings rather than a frozen preorder."""

from __future__ import annotations

from time import monotonic

from ..bench.ch import like_lit
from .client import Ch, lit
from .coarse import CoarseRequest
from .schema import dt_lit, live, scan_epochs

SCOPE = "case-insensitive substring within names; directory hits cover descendants; bytes/objects only"


def scan_bound(ch: Ch, date: str) -> tuple[str, str]:
    """`date`'s published scan as a DateTime literal, and its rebaseline epoch."""
    for _, dt, _, _, epoch in scan_epochs(ch):
        if dt[:10] == date:
            return dt_lit(dt), dt_lit(epoch)
    raise CoarseRequest(f"no published scan on {date}")


def buckets(ch: Ch, D: str, since: str) -> list[str]:
    """The depth-1 paths (buckets) live on the scan."""
    restrict = "depth = 1"
    return [row[0] for row in ch.json(f"SELECT DISTINCT path FROM nodes WHERE {restrict} AND {live(D, restrict, since)} ORDER BY path")]


def answer(
    ch: Ch,
    date: str,
    pattern: str,
    *,
    max_names: int | None = None,
    max_postings: int | None = None,
    settings: dict | None = None,
) -> dict:
    """Bucket bytes/objects of the first hits of `pattern` on `date`'s scan."""
    if not isinstance(pattern, str) or not pattern or "/" in pattern or "\0" in pattern or len(pattern) > 512:
        raise CoarseRequest("name totals need one nonempty literal without slashes or NUL, at most 512 characters")
    pattern = pattern.lower()
    start = monotonic()
    D, since = scan_bound(ch, date)
    paths = buckets(ch, D, since)
    stages = {}
    stage = monotonic()
    vocabulary = f"mega_names_{abs(hash((date, pattern, start))):x}"
    limit = f" LIMIT {max_names + 1}" if max_names is not None else ""
    ch.tmp(vocabulary, f"SELECT l FROM names WHERE l LIKE {like_lit(pattern)}{limit}", settings)
    n_names = int(ch.scalar(f"SELECT count() FROM {vocabulary}", settings))
    if max_names is not None and n_names > max_names:
        raise CoarseRequest(f"vocabulary exceeds its {max_names:,}-name work budget")
    stages["vocabulary_s"] = round(monotonic() - stage, 6)
    stage = monotonic()
    restrict = f"name IN (SELECT l FROM {vocabulary})"
    first = f"position(lowerUTF8(parent), {lit(pattern)}) = 0"
    rows = ch.json(f"""
        SELECT splitByChar('/', path)[1] AS bucket, count(), sumIf(size, {first}), sumIf(n_files, {first})
        FROM nodes WHERE {restrict} AND depth >= 1 AND {live(D, restrict, since)}
        GROUP BY bucket
    """, settings) if n_names else []
    postings = sum(int(row[1]) for row in rows)
    if max_postings is not None and postings > max_postings:
        raise CoarseRequest(f"matching slice rows exceed their {max_postings:,}-row work budget")
    totals = {bucket: (int(b), int(o)) for bucket, _, b, o in rows}
    stages["postings_s"] = round(monotonic() - stage, 6)
    unknown = set(totals) - set(paths)
    if unknown:
        raise RuntimeError(f"first hits outside the scan's buckets: {sorted(unknown)}")
    out = [{"path": p, "b": totals.get(p, (0, 0))[0], "o": totals.get(p, (0, 0))[1]} for p in paths]
    return {"schema": "mega-name-totals-v1", "date": date, "pattern": pattern, "exact": True, "scope": SCOPE,
            "root": {"b": sum(r["b"] for r in out), "o": sum(r["o"] for r in out)}, "buckets": out,
            "vocabulary_names": n_names, "matching_slice_rows": postings, "stages": stages,
            "build_s": round(monotonic() - start, 6)}


def reference(ch: Ch, target: str, date: str, pattern: str, *, daily: bool) -> dict:
    """The per-scan (`daily`) or frozen-snapshot index's answer, as `{bucket: (b, o)}` plus timing."""
    from .hot_l1 import build

    start = monotonic()
    body = build(ch.fork(db=target), target, date, pattern, daily=daily)
    return {"buckets": {row["path"]: (row["b"], row["o"]) for row in body["buckets"]}, "build_s": round(monotonic() - start, 6)}


def bench(
    url: str,
    db: str,
    dates: list[str],
    patterns: list[str],
    *,
    threads: int,
    trials: int = 1,
    references: dict[str, tuple[str, bool]] | None = None,
):
    """Per `(date, pattern)`: the consolidated answer `trials` times (first = coldest), and, where `references` names
    a target for the date, whether its buckets equal that index's. Yields one record each."""
    settings = {"max_threads": threads}
    for date in dates:
        for pattern in patterns:
            runs = []
            body = None
            for _ in range(trials):
                ch = Ch(url, db=db)
                try:
                    body = answer(ch, date, pattern, settings=settings)
                finally:
                    ch.close()
                runs.append({"build_s": body["build_s"], **body["stages"]})
            record = {"date": date, "pattern": pattern.lower(), "threads": threads, "runs": runs,
                      "vocabulary_names": body["vocabulary_names"], "matching_slice_rows": body["matching_slice_rows"],
                      "root": body["root"]}
            if references and date in references:
                target, daily = references[date]
                ch = Ch(url, db=target)
                try:
                    ref = reference(ch, target, date, pattern, daily=daily)
                finally:
                    ch.close()
                mine = {row["path"]: (row["b"], row["o"]) for row in body["buckets"]}
                record["reference"] = {"target": target, "build_s": ref["build_s"], "equal": mine == ref["buckets"]}
                if mine != ref["buckets"]:
                    record["reference"]["diff"] = {p: [mine.get(p), ref["buckets"].get(p)] for p in sorted(set(mine) | set(ref["buckets"]))
                                                   if mine.get(p) != ref["buckets"].get(p)}
            yield record
