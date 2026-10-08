"""Name-substring bucket totals for any scan, from the one consolidated store.

The append-only store (`ingest.py`: `nodes` versions opened at `vf`,
`closures` closing them at `vt`, `names` the lowercase-basename vocabulary
with its trigram index) already carries every published scan. Its `by_name`
projections (`nodes`: `name, depth, path, usr, vf`; `closures`: `name, depth,
path`) make it a name index over all of them at once: no per-scan index.

A literal (case-insensitive, no slash) answers on scan `D` as:

1. vocabulary: the lowercase basenames containing it whose live span covers
   `D` (`name_spans`, trigram index for three or more characters; without
   it, every name ever seen: `names`);
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

from json import dumps
from time import monotonic

from ..bench.ch import like_lit
from .client import Ch, lit
from .coarse import CoarseRequest
from .schema import dt_lit, live, scan_epochs

SCOPE = "case-insensitive substring within names; directory hits cover descendants; bytes/objects only"
FOREVER = "toDateTime('2106-01-01 00:00:00', 'UTC')"


SPANS_SCHEMA = f"""(l String, first SimpleAggregateFunction(min, DateTime('UTC')), versions SimpleAggregateFunction(sum, UInt64),
    closed SimpleAggregateFunction(sum, UInt64), closed_last SimpleAggregateFunction(max, DateTime('UTC')),
    INDEX tl l TYPE text(tokenizer = ngrams(3)) GRANULARITY 100000000) ENGINE = AggregatingMergeTree ORDER BY l"""
LOG = "name_index_log"


def _bound(column: str, start: str | None = None, end: str | None = None, *, day: str | None = None) -> str:
    """A `vf`/`vt` predicate: one scan (`day`), or a `(start, end]` window (either side open)."""
    if day is not None:
        return f"{column} = {day}"
    parts = [f"{column} > {start}"] if start else []
    parts += [f"{column} <= {end}"] if end else []
    return " AND ".join(parts) or "1"


def _span_rows(nodes: str, closures: str) -> str:
    """Per-name span aggregates of some versions and some closures, as rows `name_spans` sums: a version contributes
    its `vf` and a count, a closure a count and its `vt`. Neutral elements fill the other side, so the deltas of any
    set of scans add up to the spans of their union."""
    return f"""SELECT name AS l, min(vf) AS first, count() AS versions, toUInt64(0) AS closed, toDateTime(0, 'UTC') AS closed_last
        FROM nodes WHERE {nodes} GROUP BY name
        UNION ALL
        SELECT name AS l, {FOREVER} AS first, toUInt64(0) AS versions, count() AS closed, max(vt) AS closed_last
        FROM closures WHERE {closures} GROUP BY name"""


def span_live(D: str) -> str:
    """The HAVING clause over `name_spans` grouped by `l`: the name's span covers scan `D` (opened by it, and some
    version still open or last closed after it)."""
    return f"min(first) <= {D} AND if(sum(versions) > sum(closed), {FOREVER}, max(closed_last)) > {D}"


def _log(ch: Ch, settings: dict | None = None) -> None:
    ch.exec(f"""CREATE TABLE IF NOT EXISTS {LOG} (stem String, through DateTime('UTC'), op LowCardinality(String), at DateTime('UTC') DEFAULT now(),
        doc String) ENGINE = MergeTree ORDER BY (stem, through)""", settings=settings)


def _through(ch: Ch, stem: str, settings: dict | None = None) -> str | None:
    """The newest scan `stem` covers (a DateTime literal), from the log; None = never logged."""
    _log(ch, settings)
    row = ch.one(f"SELECT count(), toString(max(through)) FROM {LOG} WHERE stem = {lit(stem)}", settings)
    return dt_lit(row[1]) if int(row[0]) else None


def _newest(ch: Ch, end: str | None = None) -> str:
    """The newest published scan (at or before `end`, a date) as a DateTime literal."""
    scans = [dt for _, dt, _, _, _ in scan_epochs(ch) if end is None or dt[:10] <= end]
    if not scans:
        raise CoarseRequest(f"no published scan{f' on or before {end}' if end else ''}")
    return dt_lit(max(scans))


def build_spans(ch: Ch, settings: dict | None = None, *, end: str | None = None) -> dict:
    """`name_spans`: per name, its earliest version, version and closure counts and last closure, over every published
    scan (through `end`, a date). A name's live span (`span_live`) is a superset of the scans it is live on: opened at
    its earliest version, live until its last version closes, never closed while it has more versions than closures.
    The columns are sums/min/max, so daily upkeep (`append`) adds the day's rows and merges (or the readers' GROUP BY)
    combine them; a full build is one such row per name."""
    start = monotonic()
    E = _newest(ch, end)
    ch.exec("DROP TABLE IF EXISTS name_spans_build", settings=settings)
    ch.exec(f"CREATE TABLE name_spans_build {SPANS_SCHEMA}", settings=settings)
    ch.exec(f"""INSERT INTO name_spans_build SELECT l, min(first), sum(versions), sum(closed), max(closed_last)
        FROM ({_span_rows(_bound('vf', end=E), _bound('vt', end=E))}) GROUP BY l""", settings=settings)
    ch.exec("EXCHANGE TABLES name_spans_build AND name_spans" if ch.scalar("EXISTS TABLE name_spans") == "1"
            else "RENAME TABLE name_spans_build TO name_spans", settings=settings)
    ch.exec("DROP TABLE IF EXISTS name_spans_build", settings=settings)
    names, still_open = (int(x) for x in ch.one("SELECT count(), countIf(versions > closed) FROM name_spans", settings))
    body = {"names": names, "open": still_open, "through": E.split("'")[1], "build_s": round(monotonic() - start, 3)}
    if _through(ch, "name_spans", settings):
        ch.exec(f"DELETE FROM {LOG} WHERE stem = 'name_spans'", settings=settings)
    ch.exec(f"INSERT INTO {LOG} (stem, through, op, doc) VALUES ('name_spans', {E}, 'build', {lit(json_doc(body))})", settings=settings)
    return body


def json_doc(body: dict) -> str:
    return dumps(body, sort_keys=True)


def build_postings(
    ch: Ch,
    stem: str,
    start: str | None = None,
    settings: dict | None = None,
    *,
    end: str | None = None,
    optimize: bool = True,
) -> dict:
    """`{stem}_nodes` / `{stem}_closures`: the store's versions and closures re-sorted by name, in 256-row granules and
    without per-scan partitions, so one name costs a granule or two however many scans hold it. With `start` (a scan
    date), only versions still live on or after it, and the closures that can close them: the index of a span
    `[start, newest]`; with `end`, only scans through it (`append` adds later ones). `optimize` merges to one part."""
    if not stem.isidentifier() or stem == "name_spans":
        raise ValueError("postings stem must be an identifier other than `name_spans`")
    begin = monotonic()
    S = dt_lit(f"{start} 00:00:00") if start else None
    E = _newest(ch, end)
    nodes, closures = f"{stem}_nodes", f"{stem}_closures"
    for t in (nodes, closures):
        ch.exec(f"DROP TABLE IF EXISTS {t}", settings=settings)
    # Versions closed by the span's start drop out: a streaming anti-join (both sides sorted by the version key), not a
    # hash set of every earlier closure.
    source = (f"""SELECT n.name, n.vf, n.depth, n.path, n.usr, n.size, n.n_files FROM nodes AS n
        LEFT JOIN (SELECT depth, path, usr, vf, toUInt8(1) AS gone FROM closures WHERE vt <= {S}) AS c USING (depth, path, usr, vf)
        WHERE c.gone = 0 AND n.vf <= {E}"""
              if S else f"SELECT name, vf, depth, path, usr, size, n_files FROM nodes WHERE vf <= {E}")
    stage = monotonic()
    ch.exec(f"""CREATE TABLE {nodes} (name String, vf DateTime('UTC'), depth UInt8, path String CODEC(ZSTD(3)), usr LowCardinality(String),
        size Int64, n_files Int64) ENGINE = MergeTree ORDER BY (name, vf, depth, path, usr) SETTINGS index_granularity = 256
        AS {source}""", settings={**(settings or {}), "join_algorithm": "full_sorting_merge", "join_use_nulls": 0})
    nodes_s = round(monotonic() - stage, 3)
    stage = monotonic()
    ch.exec(f"""CREATE TABLE {closures} (name String, vt DateTime('UTC'), depth UInt8, path String CODEC(ZSTD(3)), usr LowCardinality(String),
        vf DateTime('UTC')) ENGINE = MergeTree ORDER BY (name, vt, depth, path, usr) SETTINGS index_granularity = 256
        AS SELECT name, vt, depth, path, usr, vf FROM closures WHERE {_bound('vt', S, E)}""", settings=settings)
    closures_s = round(monotonic() - stage, 3)
    if optimize:
        ch.exec(f"OPTIMIZE TABLE {nodes} FINAL", settings=settings)
        ch.exec(f"OPTIMIZE TABLE {closures} FINAL", settings=settings)
    body = {"stem": stem, "start": start, "through": E.split("'")[1], "sizes": sizes(ch, (nodes, closures), settings),
            "stages": {"nodes_s": nodes_s, "closures_s": closures_s}, "build_s": round(monotonic() - begin, 3)}
    if _through(ch, stem, settings):
        ch.exec(f"DELETE FROM {LOG} WHERE stem = {lit(stem)}", settings=settings)
    ch.exec(f"INSERT INTO {LOG} (stem, through, op, doc) VALUES ({lit(stem)}, {E}, 'build', {lit(json_doc(body))})", settings=settings)
    return body


def sizes(ch: Ch, tables, settings: dict | None = None) -> dict:
    return {t: dict(zip(("rows", "bytes", "parts"), (int(x) for x in ch.one(
        f"SELECT sum(rows), sum(bytes_on_disk), count() FROM system.parts WHERE active AND database = currentDatabase() AND table = {lit(t)}", settings))))
        for t in tables}


def append(ch: Ch, date: str, stems: list[str], settings: dict | None = None) -> dict:
    """Daily upkeep: add scan `date`'s opened versions and closures (`nodes`/`closures` partitions `vf`/`vt` = the
    scan, i.e. work proportional to the day's changes) to each postings stem, and their per-name span deltas to
    `name_spans`. Each target must be logged through an earlier scan (refused otherwise, so an append never repeats);
    the day's rows are staged in a side table and attached whole (`ATTACH PARTITION tuple() FROM`), then logged."""
    D, _ = scan_bound(ch, date)
    begin = monotonic()
    out = {"date": date, "targets": {}}
    for stem in ("name_spans", *stems):
        through = _through(ch, stem, settings)
        if through is None:
            raise CoarseRequest(f"`{stem}` has no build in `{LOG}`; build it before appending")
        if ch.scalar(f"SELECT {D} > {through}", settings) != "1":
            raise CoarseRequest(f"`{stem}` already covers {date} (logged through {through.split(chr(39))[1]})")
    for stem in ("name_spans", *stems):
        stage = monotonic()
        if stem == "name_spans":
            adds = {"name_spans": (f"CREATE TABLE {{t}} {SPANS_SCHEMA}",
                                   f"SELECT l, min(first), sum(versions), sum(closed), max(closed_last) FROM ({_span_rows(_bound('vf', day=D), _bound('vt', day=D))}) GROUP BY l")}
        else:
            adds = {f"{stem}_nodes": (f"CREATE TABLE {{t}} AS {stem}_nodes", f"SELECT name, vf, depth, path, usr, size, n_files FROM nodes WHERE vf = {D}"),
                    f"{stem}_closures": (f"CREATE TABLE {{t}} AS {stem}_closures", f"SELECT name, vt, depth, path, usr, vf FROM closures WHERE vt = {D}")}
        rows = {}
        for table, (create, select) in adds.items():
            side = f"{table}_add"
            ch.exec(f"DROP TABLE IF EXISTS {side}", settings=settings)
            ch.exec(create.format(t=side), settings=settings)
            ch.exec(f"INSERT INTO {side} {select}", settings=settings)
            rows[table] = int(ch.scalar(f"SELECT count() FROM {side}", settings))
        for table in adds:
            ch.exec(f"ALTER TABLE {table} ATTACH PARTITION tuple() FROM {table}_add", settings=settings)
            ch.exec(f"DROP TABLE {table}_add", settings=settings)
        body = {"rows": rows, "append_s": round(monotonic() - stage, 3)}
        ch.exec(f"INSERT INTO {LOG} (stem, through, op, doc) VALUES ({lit(stem)}, {D}, 'append', {lit(json_doc(body))})", settings=settings)
        out["targets"][stem] = body
    out["append_s"] = round(monotonic() - begin, 3)
    return out


def digest(ch: Ch, stem: str, settings: dict | None = None) -> dict:
    """Order- and part-insensitive content digests: postings rows as a multiset; `name_spans` per name after
    combining its rows (so an appended index equals a full build however its parts merged)."""
    if stem == "name_spans":
        q = {"name_spans": """SELECT count(), sum(cityHash64(l, f, v, c, cl)) FROM (SELECT l, min(first) AS f, sum(versions) AS v,
            sum(closed) AS c, max(closed_last) AS cl FROM name_spans GROUP BY l)"""}
    else:
        q = {f"{stem}_nodes": f"SELECT count(), sum(cityHash64(name, vf, depth, path, usr, size, n_files)) FROM {stem}_nodes",
             f"{stem}_closures": f"SELECT count(), sum(cityHash64(name, vt, depth, path, usr, vf)) FROM {stem}_closures"}
    return {t: [int(x) for x in ch.one(sql, settings)] for t, sql in q.items()}


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
    postings: str | None = None,
    settings: dict | None = None,
) -> dict:
    """Bucket bytes/objects of the first hits of `pattern` on `date`'s scan: from the store's own `by_name`
    projections, or the name-sorted `{postings}_nodes` / `{postings}_closures` (`build_postings`)."""
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
    # Names live on the scan (a superset, by `name_spans`), else every name ever seen.
    spans = ch.scalar("EXISTS TABLE name_spans") == "1"
    source = (f"name_spans WHERE l LIKE {like_lit(pattern)} GROUP BY l HAVING {span_live(D)}" if spans
              else f"names WHERE l LIKE {like_lit(pattern)}")
    ch.tmp(vocabulary, f"SELECT l FROM {source}{limit}", settings)
    n_names = int(ch.scalar(f"SELECT count() FROM {vocabulary}", settings))
    if max_names is not None and n_names > max_names:
        raise CoarseRequest(f"vocabulary exceeds its {max_names:,}-name work budget")
    stages["vocabulary_s"] = round(monotonic() - stage, 6)
    stage = monotonic()
    restrict = f"name IN (SELECT l FROM {vocabulary})"
    parent = "if(position(path, '/') = 0, '', substring(path, 1, length(path) - position(reverse(path), '/')))"
    first = f"position(lowerUTF8({parent}), {lit(pattern)}) = 0"
    if postings is None:
        where = f"nodes WHERE {restrict} AND depth >= 1 AND {live(D, restrict, since)}"
        run = settings
    else:
        # Both sides narrowed by name first (PREWHERE: the primary key), then the few closures joined as a hash table:
        # 1.3 s for Oct 6 `5418` against 2.9 s for a tuple `NOT IN`.
        where = f"""(SELECT depth, path, usr, vf, size, n_files FROM {postings}_nodes
                PREWHERE {restrict} AND vf >= {since} AND vf <= {D} WHERE depth >= 1) AS n
            LEFT JOIN (SELECT depth, path, usr, vf, toUInt8(1) AS gone FROM {postings}_closures
                PREWHERE {restrict} AND vt <= {D}) AS c USING (depth, path, usr, vf)
            WHERE c.gone = 0"""
        run = {**(settings or {}), "join_algorithm": "hash", "join_use_nulls": 0}
    rows = ch.json(f"""
        SELECT splitByChar('/', path)[1] AS bucket, count(), sumIf(size, {first}), sumIf(n_files, {first})
        FROM {where} GROUP BY bucket
    """, run) if n_names else []
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
            "vocabulary_names": n_names, "vocabulary": "name_spans" if spans else "names", "matching_slice_rows": postings, "stages": stages,
            "build_s": round(monotonic() - start, 6)}


def reference(ch: Ch, target: str, date: str, pattern: str, *, daily: bool) -> dict:
    """The per-scan (`daily`) or frozen-snapshot index's answer, as `{bucket: (b, o)}` plus timing."""
    from .hot_l1 import build

    start = monotonic()
    body = build(ch, target, date, pattern, daily=daily)
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
    postings: str | None = None,
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
                    body = answer(ch, date, pattern, postings=postings, settings=settings)
                finally:
                    ch.close()
                runs.append({"build_s": body["build_s"], **body["stages"]})
            record = {"date": date, "pattern": pattern.lower(), "threads": threads, "postings": postings, "runs": runs,
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
