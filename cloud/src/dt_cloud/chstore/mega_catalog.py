"""One catalog over every scan in the consolidated store, kept by appending
each scan's changes (`ch-mega-catalog-build`; spec `specs/architecture/mega-index.md`).

A scan's catalog (`dated_hot_l1`) is its registry — every literal with at
least `threshold` direct name-substring paths, plus every literal of at most
`short` characters present at all (the complete length domain: an unlisted
literal of any length is provably below the threshold) — and, per registered
literal, its first-hit bucket bytes/objects. Here every scan's catalog lives
in four tables of one store, as versions: a row holds from its scan until the
next row for the same key.

- `{stem}_names (l, d, n)`: per lowercase basename, the change in its number
  of live paths at scan `d`; summed over a rebaseline epoch's scans through
  `D`, the vocabulary `D`'s census reads. A base scan (the store's first, or a
  source-format switch: no `changes` recorded) holds full counts.
- `{stem}_multi (depth, path, d)`: paths that had two or more live owner
  slices on some scan of the epoch (from `d`). Every other path has at most
  one, so a day that only closes its slices takes it from live to gone.
- `{stem}_terms (term, vf, member, paths)`: registry versions. Every literal
  ever registered is tracked; `member` and its count change with the census.
- `{stem}_cells (term, bucket, vf, b, o)`: first-hit totals per tracked
  literal and bucket, versioned when they change.

Appending scan `D` (`append`) costs the day's churn, not the scan:

1. counts: each path the day's `changes` touch moves its name by
   `[live after] − [live before]`; a path that only closes slices and never
   had two is −1, the rest are read exactly from `nodes`/`closures`;
2. registry: the native census (`native/hot_frequency.cpp`) over the
   maintained vocabulary;
3. answers: the day's signed changes through `native/catalog_delta.cpp`, all
   tracked literals at once, added to the previous totals (a directory's
   rollup is its own version: when anything under it changes, its old version
   closes and a new one opens, so first-hit sums move by exactly the day's
   signed first-hit rows);
4. literals registered for the first time: complete answers on `D` from the
   consolidated name index (`mega_names.answer`), or one pass over the scan
   when there are many.

A base scan computes all three from its live rows. Each scan is logged in
`name_index_log` (stem `{stem}`) once written; a rerun first drops an
unlogged scan's partial rows."""

from __future__ import annotations

from dataclasses import dataclass
from json import loads
from pathlib import Path
from subprocess import PIPE, Popen
from sys import stderr
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic
from typing import Callable
from uuid import uuid4

from .client import Ch, lit
from .coarse import CoarseRequest
from .mega_names import LOG, _log, _through, json_doc
from .schema import dt_lit, live, scan_epochs

THRESHOLD = 100_000
SHORT = 2
# What a literal's census count measures: `paths`, its direct matching paths live on the scan; or `rows`, what
# answering it on demand costs — the consolidated name index's postings rows of its names through the scan (versions
# opened by it plus closures by it, every name seen by then) plus `NAME_ROWS` per name (a name's postings occupy at
# least one granule in each table). A non-member's on-demand answer is then bounded by the threshold in these units,
# on every scan: the reader's vocabulary (`mega_names.span_live`) is a subset of the names seen by the scan, and it
# reads no version opened or closed after it.
WEIGHTS = ("paths", "rows")
NAME_ROWS = 256
# Entrants (literals registered for the first time) above this many are answered by one pass over the scan, not one
# consolidated-index query each (~1.4 s each, near the threshold: Aug 25's 272 took 387 s; a pass over that 220M-row
# scan takes ~70 s).
ENTRANT_QUERIES = 64
# Kernel key ranges per concurrent stream (`kernel`).
RANGES_PER_STREAM = 4
# `nodes` is sorted by `(depth, path, …)`: grouping its slices by path streams instead of hashing every path.
IN_ORDER = {"optimize_aggregation_in_order": 1}


def tables(stem: str) -> dict[str, str]:
    if not stem.isidentifier():
        raise ValueError("catalog stem must be an identifier")
    return {
        f"{stem}_names": "(l String, d DateTime('UTC'), n Int64) ENGINE = MergeTree PARTITION BY d ORDER BY (l, d)",
        f"{stem}_multi": "(depth UInt8, path String CODEC(ZSTD(3)), d DateTime('UTC')) ENGINE = MergeTree PARTITION BY d ORDER BY (depth, path)",
        f"{stem}_terms": "(term String, vf DateTime('UTC'), member UInt8, paths UInt64) ENGINE = MergeTree ORDER BY (term, vf)",
        f"{stem}_cells": "(term String, bucket String, vf DateTime('UTC'), b Int64, o Int64) ENGINE = MergeTree ORDER BY (term, bucket, vf)",
        f"{stem}_cost": "(l String, d DateTime('UTC'), n Int64) ENGINE = MergeTree PARTITION BY d ORDER BY (l, d)",
    }


@dataclass(frozen=True)
class Scan:
    date: str
    dt: str  # DateTime literal
    since: str  # its rebaseline epoch, a DateTime literal
    base: bool  # no `changes` recorded: the first scan or a source-format switch
    start: str = ""  # the newest base scan at or before it (a DateTime literal): where its counts start


def scans(ch: Ch) -> list[Scan]:
    """Published scans in order. `ingest` records `changes` only between scans of the same source format."""
    out, version, start = [], None, ""
    for _, dt, v, _, epoch in scan_epochs(ch):
        base = version is None or v != version
        start = dt_lit(dt) if base else start
        out.append(Scan(dt[:10], dt_lit(dt), dt_lit(epoch), base, start))
        version = v
    if len({s.date for s in out}) != len(out):
        raise ValueError("the consolidated catalog serves one scan per date")
    return out


@dataclass
class State:
    """The newest version of every tracked literal's registry entry and cells."""
    terms: dict[str, tuple[int, int]]  # term → (member, paths)
    cells: dict[str, dict[str, tuple[int, int]]]  # term → bucket → (b, o)

    @classmethod
    def load(cls, ch: Ch, stem: str, settings: dict | None = None) -> "State":
        terms = {t: (int(m), int(p)) for t, m, p in ch.json(
            f"SELECT term, argMax(member, vf), argMax(paths, vf) FROM {stem}_terms GROUP BY term", settings)}
        cells: dict[str, dict[str, tuple[int, int]]] = {}
        for t, bucket, b, o in ch.json(f"SELECT term, bucket, argMax(b, vf), argMax(o, vf) FROM {stem}_cells GROUP BY term, bucket", settings):
            cells.setdefault(t, {})[bucket] = (int(b), int(o))
        return cls(terms, cells)


def create(ch: Ch, stem: str, settings: dict | None = None) -> None:
    for table, ddl in tables(stem).items():
        ch.exec(f"CREATE TABLE IF NOT EXISTS {table} {ddl}", settings=settings)
    _log(ch, settings)


def drop(ch: Ch, stem: str, settings: dict | None = None) -> None:
    for table in tables(stem):
        ch.exec(f"DROP TABLE IF EXISTS {table}", settings=settings)
    _log(ch, settings)
    ch.exec(f"DELETE FROM {LOG} WHERE stem = {lit(stem)}", settings={**(settings or {}), "mutations_sync": 2})


def _discard(ch: Ch, stem: str, scan: Scan, settings: dict | None = None) -> None:
    """Drop what an interrupted run wrote for `scan` (it is not logged)."""
    for table in (f"{stem}_names", f"{stem}_multi", f"{stem}_cost"):
        ch.exec(f"ALTER TABLE {table} DROP PARTITION {lit(scan.dt.split(chr(39))[1])}", settings=settings)
    for table in (f"{stem}_terms", f"{stem}_cells"):
        ch.exec(f"DELETE FROM {table} WHERE vf = {scan.dt}", settings={**(settings or {}), "mutations_sync": 2})


# — counts ————————————————————————————————————————————————————————————————


def _live_paths(scan: Scan) -> str:
    """One row per path live on the scan (depth ≥ 1), with its name and live owner slices."""
    return f"""SELECT depth, path, any(name) AS name, count() AS slices FROM nodes
        WHERE depth >= 1 AND {live(scan.dt, 'depth >= 1', scan.since)} GROUP BY depth, path"""


def counts_base(ch: Ch, stem: str, scan: Scan, settings: dict | None = None) -> dict:
    """Full per-name live-path counts on a base scan, and its multi-slice paths."""
    tag = uuid4().hex[:12]
    paths = f"catalog_paths_{tag}"
    ch.tmp(paths, _live_paths(scan), {**(settings or {}), **IN_ORDER}, disk=True, order_by=("depth", "path"))
    ch.exec(f"INSERT INTO {stem}_names SELECT name, {scan.dt}, toInt64(count()) FROM {paths} GROUP BY name", settings=settings)
    ch.exec(f"INSERT INTO {stem}_multi SELECT depth, path, {scan.dt} FROM {paths} WHERE slices > 1", settings=settings)
    out = {"names": int(ch.scalar(f"SELECT count() FROM {stem}_names WHERE d = {scan.dt}", settings)),
           "paths": int(ch.scalar(f"SELECT count() FROM {paths}", settings)),
           "multi": int(ch.scalar(f"SELECT count() FROM {stem}_multi WHERE d = {scan.dt}", settings))}
    ch.exec(f"DROP TEMPORARY TABLE {paths}", settings=settings)
    ch._tmp.remove(paths)
    return out


def counts_append(ch: Ch, stem: str, scan: Scan, settings: dict | None = None) -> dict:
    """Per-name live-path changes from the scan's `changes`: a touched path moves its name by
    `[live after] − [live before]`. A path not in `{stem}_multi` never had two live slices, so it had at most one and
    the day's own events decide it: only closes → −1; closes and opens → 0, with its opened slices live after; only
    opens → +1 unless a slice was already live, read from the store along with every touched `{stem}_multi` path
    (live slices = versions opened minus closures, so no version join). A path left with two or more live slices
    joins `{stem}_multi`."""
    D, S = scan.dt, scan.since
    tag = uuid4().hex[:12]
    touched, lookup, read = (f"catalog_{part}_{tag}" for part in ("touched", "lookup", "read"))
    ch.tmp(touched, f"""SELECT depth, path, any(name) AS name, countIf(sign > 0) AS opens, countIf(sign < 0) AS closes,
            (depth, path) IN (SELECT depth, path FROM {stem}_multi WHERE d >= {scan.start} AND d < {D}) AS multi
        FROM changes WHERE at = {D} AND depth >= 1 GROUP BY depth, path""", {**(settings or {}), **IN_ORDER}, disk=True, order_by=("depth", "path"))
    ch.tmp(lookup, f"SELECT depth, path FROM {touched} WHERE multi OR closes = 0", settings, disk=True, order_by=("depth", "path"))
    keys = f"(depth, path) IN (SELECT depth, path FROM {lookup})"
    ch.tmp(read, f"""SELECT depth, path, any(name) AS name, sum(b) AS before, sum(a) AS after FROM (
            SELECT depth, path, name, toInt64(vf < {D}) AS b, toInt64(1) AS a FROM nodes WHERE {keys} AND vf >= {S} AND vf <= {D}
            UNION ALL SELECT depth, path, name, -toInt64(vt < {D}) AS b, toInt64(-1) AS a FROM closures WHERE {keys} AND vf >= {S} AND vt <= {D}
        ) GROUP BY depth, path""", settings, disk=True, order_by=("depth", "path"))
    ch.exec(f"""INSERT INTO {stem}_names SELECT name, {D}, sum(n) AS n FROM (
            SELECT name, if(opens = 0, toInt64(-1), toInt64(0)) AS n FROM {touched} WHERE NOT multi AND closes > 0
            UNION ALL SELECT name, toInt64(after > 0) - toInt64(before > 0) AS n FROM {read}
        ) GROUP BY name HAVING n != 0""", settings=settings)
    ch.exec(f"""INSERT INTO {stem}_multi SELECT depth, path, {D} FROM {read} WHERE after > 1
        UNION ALL SELECT depth, path, {D} FROM {touched} WHERE NOT multi AND closes > 0 AND opens > 1""", settings=settings)
    out = {"touched": int(ch.scalar(f"SELECT count() FROM {touched}", settings)),
           "read": int(ch.scalar(f"SELECT count() FROM {lookup}", settings)),
           "names": int(ch.scalar(f"SELECT count() FROM {stem}_names WHERE d = {D}", settings)),
           "net": int(ch.scalar(f"SELECT sum(n) FROM {stem}_names WHERE d = {D}", settings) or 0),
           "multi_new": int(ch.scalar(f"SELECT count() FROM {stem}_multi WHERE d = {D}", settings))}
    for table in (read, lookup, touched):
        ch.exec(f"DROP TEMPORARY TABLE {table}", settings=settings)
        ch._tmp.remove(table)
    return out


def vocabulary_sql(stem: str, scan: Scan) -> str:
    """`(l, c)`: every name live on the scan and its live paths — the census's weighted vocabulary."""
    return f"""SELECT l, toUInt64(sum(n)) AS c FROM {stem}_names WHERE d >= {scan.start} AND d <= {scan.dt}
        GROUP BY l HAVING sum(n) > 0"""


def cost_append(ch: Ch, stem: str, scan: Scan, settings: dict | None = None) -> dict:
    """Per-name postings rows the scan adds: the versions it opens and the closures it records (the store's `vf` /
    `vt` partitions, which the consolidated name index copies by name)."""
    D = scan.dt
    ch.exec(f"""INSERT INTO {stem}_cost SELECT name, {D}, toInt64(count()) FROM (
            SELECT name FROM nodes WHERE vf = {D} UNION ALL SELECT name FROM closures WHERE vt = {D}
        ) GROUP BY name""", settings=settings)
    return {"names": int(ch.scalar(f"SELECT count() FROM {stem}_cost WHERE d = {D}", settings)),
            "rows": int(ch.scalar(f"SELECT sum(n) FROM {stem}_cost WHERE d = {D}", settings) or 0)}


def cost_vocabulary_sql(stem: str, scan: Scan, name_rows: int = NAME_ROWS) -> str:
    """`(l, c)`: every name seen by the scan and its postings rows through it, plus `name_rows` — the cost census's
    vocabulary. Not epoch-bounded: a reader on the scan reads closures from every earlier epoch."""
    return f"""SELECT l, toUInt64(sum(n) + {int(name_rows)}) AS c FROM {stem}_cost WHERE d <= {scan.dt}
        GROUP BY l HAVING sum(n) > 0"""


# — registry ——————————————————————————————————————————————————————————————


def census(
    ch: Ch,
    vocabulary: str,
    binary: Path,
    *,
    threshold: int = THRESHOLD,
    short: int = SHORT,
    threads: int = 32,
    max_patterns: int = 2_000_000,
    settings: dict | None = None,
) -> tuple[dict[str, int], dict]:
    """The registry over a weighted vocabulary (`(l String, c UInt64)` rows): every literal with ≥ `threshold`
    direct paths at any length, and every literal of ≤ `short` characters present at all, with its count."""
    start = monotonic()
    proc = Popen([str(binary), str(threshold), "0", str(threads), str(max_patterns), str(short)], stdin=PIPE, stdout=PIPE, stderr=PIPE)
    out: list[bytes] = []
    err: list[bytes] = []
    readers = [Thread(target=lambda: out.append(proc.stdout.read())), Thread(target=lambda: err.append(proc.stderr.read()))]
    for reader in readers:
        reader.start()
    try:
        for chunk in ch.stream(vocabulary, fmt="RowBinary", settings={**(settings or {}), "max_bytes_before_external_group_by": 8 << 30}):
            proc.stdin.write(chunk)
        proc.stdin.close()
    except BaseException:
        proc.kill()
        raise
    finally:
        for reader in readers:
            reader.join()
    if proc.wait() != 0:
        raise RuntimeError("native census failed: " + b"".join(err).decode(errors="replace").strip()[-2000:])
    lines = out[0].splitlines()
    header, footer = loads(lines[0]), loads(lines[-1])
    if header != {"schema": "hot-frequency-queries-v1", "engine": "native", "threshold_paths": threshold, "max_chars": None,
                  **({"short_chars": short} if short else {})} or footer != {"complete": True, "patterns": len(lines) - 2}:
        raise RuntimeError("native census output is incomplete or disagrees with the request")
    stages = [loads(line) for line in b"".join(err).splitlines() if line.startswith(b"{")]
    read = next(stage for stage in stages if stage["stage"] == "read")
    registry = {row["pattern"]: row["direct_matching_paths"] for row in map(loads, lines[1:-1])}
    return registry, {"distinct_names": read["distinct_names"], "paths": read["paths"], "read_s": read["elapsed_s"],
                      "patterns": len(registry), "census_s": round(monotonic() - start, 3)}


# — answers ———————————————————————————————————————————————————————————————


def _varstring(value: str) -> bytes:
    raw = value.encode()
    n, out = len(raw), bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out) + raw


def kernel(
    ch: Ch,
    binary: Path,
    terms: list[str],
    source: Callable[[str], str],
    *,
    parallel: int = 16,
    settings: dict | None = None,
) -> dict[str, dict[str, tuple[int, int]]]:
    """Σ sign·(size, n_files) of each literal's first hits over `source(range)` — a FROM/WHERE body over one key
    range's rows with `path`, `depth`, `size`, `n_files` and a `sign` expression (`… AS sign` selectable) — per
    bucket, nonzero only.
    `parallel` concurrent streams over `RANGES_PER_STREAM`× as many disjoint `(depth, path)` key ranges (cut at
    `nodes`' primary marks, so each prunes by the primary key), each through its own `catalog-delta`: the marks span
    the whole history, so a scan's rows fall unevenly into them, and a pool of small ranges keeps every stream busy."""
    from concurrent.futures import ThreadPoolExecutor

    from .ingest import sample_bounds

    if not terms:
        return {}
    ranges = sample_bounds(ch, parallel * RANGES_PER_STREAM)
    with TemporaryDirectory(prefix="catalog-delta-") as tmp:
        terms_file = Path(tmp) / "terms"
        terms_file.write_bytes(b"".join(_varstring(t) for t in terms))
        results: list[tuple[list[str], list[str]] | BaseException] = [None] * len(ranges)  # type: ignore[list-item]

        def one(i: int) -> None:
            sql = f"""SELECT lowerUTF8(path), splitByChar('/', path)[1], toInt8(sign), size, n_files FROM {source(ranges[i])}
                AND depth >= 1 ORDER BY depth, path"""
            proc = Popen([str(binary), str(terms_file)], stdin=PIPE, stdout=PIPE, stderr=PIPE)
            try:
                sub = ch.fork()
                for chunk in sub.stream(sql, fmt="RowBinary", settings={**(settings or {}), "max_threads": 2}):
                    proc.stdin.write(chunk)
                proc.stdin.close()
                raw, err = proc.stdout.read(), proc.stderr.read()
                if proc.wait() != 0:
                    raise RuntimeError("catalog-delta failed: " + err.decode(errors="replace").strip())
                lines = raw.decode().splitlines()
                if loads(lines[-1]).get("complete") is not True:
                    raise RuntimeError("catalog-delta output is incomplete")
                results[i] = (loads(lines[0])["buckets"], lines[1:-1])
            except BaseException as e:
                proc.kill()
                results[i] = e

        with ThreadPoolExecutor(parallel) as pool:
            list(pool.map(one, range(len(ranges))))
    totals: dict[str, dict[str, list[int]]] = {}
    for result in results:
        if isinstance(result, BaseException):
            raise result
        buckets, lines = result
        for line in lines:
            t, b, size, files = line.split("\t")
            acc = totals.setdefault(terms[int(t)], {}).setdefault(buckets[int(b)], [0, 0])
            acc[0] += int(size)
            acc[1] += int(files)
    return {t: {b: (v[0], v[1]) for b, v in cells.items() if v != [0, 0]} for t, cells in totals.items()}


def live_source(scan: Scan) -> Callable[[str], str]:
    """The scan's live rows in one key range. The anti-join's closure set is limited to the range too: a scan of the
    v1 epoch has tens of millions of closures, one set per concurrent range otherwise (Sep 15: 221 GiB)."""
    def source(keys: str) -> str:
        restrict = f"depth >= 1 AND ({keys})"
        return f"(SELECT path, depth, size, n_files, toInt8(1) AS sign FROM nodes WHERE ({keys}) AND {live(scan.dt, restrict, scan.since)}) WHERE 1"
    return source


def changes_source(scan: Scan) -> Callable[[str], str]:
    return lambda keys: f"changes WHERE at = {scan.dt} AND ({keys})"


def entrant_answers(ch: Ch, scan: Scan, terms: list[str], postings: str, settings: dict | None = None) -> dict[str, dict[str, tuple[int, int]]]:
    """Complete answers on the scan for a few literals, from the consolidated name index."""
    from .mega_names import answer

    out = {}
    for t in terms:
        body = answer(ch, scan.date, t, postings=postings, settings=settings)
        out[t] = {row["path"]: (row["b"], row["o"]) for row in body["buckets"] if (row["b"], row["o"]) != (0, 0)}
    return out


# — versions ——————————————————————————————————————————————————————————————


def _write(ch: Ch, table: str, columns: str, rows: list[tuple], settings: dict | None = None) -> None:
    from json import dumps

    if not rows:
        return
    body = "\n".join(dumps(list(row)) for row in rows).encode()
    ch.insert(f"INSERT INTO {table} ({columns}) FORMAT JSONCompactEachRow", [body], settings)


def commit(
    ch: Ch,
    stem: str,
    scan: Scan,
    state: State,
    registry: dict[str, int],
    answers: dict[str, dict[str, tuple[int, int]]],
    *,
    full: set[str],
    settings: dict | None = None,
) -> dict:
    """Write the versions that differ from `state` and advance it. `answers[t]` is a complete answer for `t` in
    `full`, else a delta to add to its previous cells; tracked literals absent from `answers` keep their cells."""
    when = scan.dt.split("'")[1]
    term_rows, cell_rows = [], []
    for t in sorted(set(state.terms) | set(registry)):
        entry = (1, registry[t]) if t in registry else (0, 0)
        if state.terms.get(t) != entry:
            term_rows.append((t, when, *entry))
            state.terms[t] = entry
    for t in sorted(answers.keys() | full):
        before = state.cells.get(t, {})
        given = answers.get(t, {})
        after = dict(given) if t in full else {b: tuple(x + y for x, y in zip(before.get(b, (0, 0)), given.get(b, (0, 0)))) for b in before.keys() | given.keys()}
        for b in sorted(before.keys() | after.keys()):
            value = after.get(b, (0, 0))
            if value[0] < 0 or value[1] < 0:
                raise RuntimeError(f"negative first-hit totals for {t!r} in {b} on {scan.date}: the catalog and the store disagree")
            if before.get(b, (0, 0)) != value:
                cell_rows.append((t, b, when, *value))
        state.cells[t] = {b: v for b, v in after.items() if v != (0, 0)}
    _write(ch, f"{stem}_terms", "term, vf, member, paths", term_rows, settings)
    _write(ch, f"{stem}_cells", "term, bucket, vf, b, o", cell_rows, settings)
    return {"term_versions": len(term_rows), "cell_versions": len(cell_rows)}


# — build / append ————————————————————————————————————————————————————————


def process(
    ch: Ch,
    stem: str,
    scan: Scan,
    state: State,
    *,
    census_binary: Path,
    delta_binary: Path,
    postings: str | None,
    threshold: int = THRESHOLD,
    short: int = SHORT,
    weight: str = "paths",
    name_rows: int = NAME_ROWS,
    threads: int = 32,
    parallel: int = 16,
    settings: dict | None = None,
) -> dict:
    """One scan: counts (and, weighted by `rows`, postings costs), registry, answers, versions, log."""
    if weight not in WEIGHTS:
        raise ValueError(f"weight must be one of {WEIGHTS}")
    begin = monotonic()
    stages: dict[str, float] = {}
    _discard(ch, stem, scan, settings)
    counts = cost = None
    if weight == "paths":
        stage = monotonic()
        counts = counts_base(ch, stem, scan, settings) if scan.base else counts_append(ch, stem, scan, settings)
        stages["counts_s"] = round(monotonic() - stage, 3)
        log(f"catalog {scan.date}: counts {counts} ({stages['counts_s']} s)")
    else:
        stage = monotonic()
        cost = cost_append(ch, stem, scan, settings)
        stages["cost_s"] = round(monotonic() - stage, 3)
        log(f"catalog {scan.date}: postings rows {cost} ({stages['cost_s']} s)")
    stage = monotonic()
    vocabulary = cost_vocabulary_sql(stem, scan, name_rows) if weight == "rows" else vocabulary_sql(stem, scan)
    registry, census_stats = census(ch, vocabulary, census_binary, threshold=threshold, short=short, threads=threads, settings=settings)
    stages["census_s"] = round(monotonic() - stage, 3)
    log(f"catalog {scan.date}: census {census_stats}")
    tracked = sorted(state.terms)
    entrants = sorted(set(registry) - set(state.terms))
    stage = monotonic()
    if scan.base:
        full = set(tracked) | set(entrants)
        answers = kernel(ch, delta_binary, sorted(full), live_source(scan), parallel=parallel, settings=settings)
        entrant_plan = "scan"
    else:
        answers = kernel(ch, delta_binary, tracked, changes_source(scan), parallel=parallel, settings=settings)
        stages["delta_s"] = round(monotonic() - stage, 3)
        log(f"catalog {scan.date}: delta over {len(tracked)} tracked literals ({stages['delta_s']} s); {len(entrants)} entrants")
        stage = monotonic()
        full = set(entrants)
        if postings is not None and len(entrants) <= ENTRANT_QUERIES:
            answers.update(entrant_answers(ch, scan, entrants, postings, settings))
            entrant_plan = "postings"
        else:
            answers.update(kernel(ch, delta_binary, entrants, live_source(scan), parallel=parallel, settings=settings))
            entrant_plan = "scan"
    stages["answers_s"] = round(monotonic() - stage, 3)
    stage = monotonic()
    written = commit(ch, stem, scan, state, registry, answers, full=full, settings=settings)
    stages["commit_s"] = round(monotonic() - stage, 3)
    body = {"date": scan.date, "base": scan.base, "threshold": threshold, "short": short, "weight": weight,
            **({"name_rows": name_rows, "cost": cost} if weight == "rows" else {}), "counts": counts, "census": census_stats, "members": len(registry),
            "tracked": len(state.terms), "entrants": len(entrants), "entrant_plan": entrant_plan if entrants or scan.base else None,
            **written, "stages": stages, "scan_s": round(monotonic() - begin, 3)}
    ch.exec(f"INSERT INTO {LOG} (stem, through, op, doc) VALUES ({lit(stem)}, {scan.dt}, {lit('base' if scan.base else 'append')}, {lit(json_doc(body))})",
            settings=settings)
    return body


def build(
    ch: Ch,
    stem: str,
    *,
    census_binary: Path,
    delta_binary: Path,
    postings: str | None = None,
    through: str | None = None,
    threshold: int = THRESHOLD,
    short: int = SHORT,
    weight: str = "paths",
    name_rows: int = NAME_ROWS,
    threads: int = 32,
    parallel: int = 16,
    settings: dict | None = None,
    progress: Callable[[dict], None] | None = None,
) -> list[dict]:
    """Process every published scan after the stem's logged coverage, in order, through `through` (a date)."""
    create(ch, stem, settings)
    done = _through(ch, stem, settings)
    state = State.load(ch, stem, settings)
    out = []
    for scan in scans(ch):
        if done is not None and ch.scalar(f"SELECT {scan.dt} <= {done}", settings) == "1":
            continue
        if through is not None and scan.date > through:
            break
        body = process(ch, stem, scan, state, census_binary=census_binary, delta_binary=delta_binary, postings=postings,
                       threshold=threshold, short=short, weight=weight, name_rows=name_rows, threads=threads, parallel=parallel,
                       settings=settings)
        out.append(body)
        if progress is not None:
            progress(body)
    return out


# — reading ———————————————————————————————————————————————————————————————


def binding(ch: Ch, stem: str) -> dict:
    """What a server needs to read `stem`: its registry parameters and each covered scan's registered-literal count.
    Coverage must be every published scan from the first through the newest logged one."""
    rows = ch.json(f"SELECT toString(through), doc FROM {LOG} WHERE stem = {lit(stem)} ORDER BY through")
    if not rows:
        raise ValueError(f"catalog `{stem}` has no logged scans")
    members, params = {}, set()
    for through, doc in rows:
        body = loads(doc)
        members[through[:10]] = body["members"]
        weight = body.get("weight", "paths")
        params.add((body["threshold"], body["short"], weight, body.get("name_rows") if weight == "rows" else None))
    published = [s.date for s in scans(ch) if s.date <= max(members)]
    if len(params) != 1 or list(members) != published:
        raise ValueError(f"catalog `{stem}` does not cover every published scan through {max(members)} with one registry")
    (threshold, short, weight, name_rows), = params
    return {"schema": "mega-catalog-binding-v1", "target": ch.db, "stem": stem, "through": max(members),
            "threshold": threshold, "short": short, "weight": weight, **({"name_rows": name_rows} if weight == "rows" else {}),
            "members": members}


def view(ch: Ch, stem: str, date: str, term: str, settings: dict | None = None) -> tuple[int, dict[str, tuple[int, int]]] | None:
    """`term`'s registry entry and bucket totals on the scan dated `date`, or None if it isn't registered there."""
    D = dt_lit(f"{date} 00:00:00")
    t = lit(term)
    row = ch.json(f"SELECT argMax(member, vf), argMax(paths, vf) FROM {stem}_terms WHERE term = {t} AND vf <= {D} HAVING count() > 0", settings)
    if not row or row[0][0] != 1:
        return None
    cells = ch.json(f"SELECT bucket, argMax(b, vf), argMax(o, vf) FROM {stem}_cells WHERE term = {t} AND vf <= {D} GROUP BY bucket", settings)
    return int(row[0][1]), {b: (int(x), int(y)) for b, x, y in cells if (x, y) != (0, 0)}


def snapshot(ch: Ch, stem: str, date: str, settings: dict | None = None) -> dict[str, tuple[int, dict[str, tuple[int, int]]]]:
    """Every registered literal on the scan dated `date`: `{term: (paths, {bucket: (b, o)})}` (nonzero cells)."""
    D = dt_lit(f"{date} 00:00:00")
    members = {t: int(p) for t, m, p in ch.json(f"""SELECT term, argMax(member, vf) AS m, argMax(paths, vf) FROM {stem}_terms
        WHERE vf <= {D} GROUP BY term HAVING m = 1""", settings)}
    out: dict[str, tuple[int, dict]] = {t: (p, {}) for t, p in members.items()}
    for t, b, x, y in ch.json(f"""SELECT term, bucket, argMax(b, vf) AS x, argMax(o, vf) AS y FROM {stem}_cells
            WHERE vf <= {D} GROUP BY term, bucket HAVING x != 0 OR y != 0""", settings):
        if t in out:
            out[t][1][b] = (int(x), int(y))
    return out


def fresh(
    ch: Ch,
    date: str,
    *,
    census_binary: Path,
    delta_binary: Path,
    threshold: int = THRESHOLD,
    short: int = SHORT,
    weight: str = "paths",
    name_rows: int = NAME_ROWS,
    threads: int = 32,
    parallel: int = 16,
    settings: dict | None = None,
) -> tuple[dict[str, tuple[int, dict[str, tuple[int, int]]]], dict]:
    """The scan's catalog computed from its live rows alone (no tables written, no earlier scan; weighted by `rows`,
    the costs counted over the store's versions and closures through it): the reference an appended catalog must
    equal."""
    scan = next(s for s in scans(ch) if s.date == date)
    begin = monotonic()
    tag = uuid4().hex[:12]
    paths = f"catalog_fresh_{tag}"
    ch.tmp(paths, _live_paths(scan), {**(settings or {}), **IN_ORDER}, disk=True, order_by=("depth", "path"))
    vocabulary = f"SELECT name AS l, toUInt64(count()) AS c FROM {paths} GROUP BY name"
    if weight == "rows":
        vocabulary = f"""SELECT name AS l, toUInt64(count() + {int(name_rows)}) AS c FROM (
            SELECT name FROM nodes WHERE vf <= {scan.dt} UNION ALL SELECT name FROM closures WHERE vt <= {scan.dt}) GROUP BY name"""
    registry, census_stats = census(ch, vocabulary, census_binary, threshold=threshold, short=short, threads=threads, settings=settings)
    ch.exec(f"DROP TEMPORARY TABLE {paths}", settings=settings)
    ch._tmp.remove(paths)
    answers = kernel(ch, delta_binary, sorted(registry), live_source(scan), parallel=parallel, settings=settings)
    return {t: (p, answers.get(t, {})) for t, p in registry.items()}, {"census": census_stats, "fresh_s": round(monotonic() - begin, 3)}


def published(generation: Path, queries: Path, date: str) -> dict[str, tuple[int, dict[str, tuple[int, int]]]]:
    """A published dated L1 catalog's registry for the scan (aliases answer as their roots), with the direct-path
    counts of the census it was selected from, in `snapshot`'s shape."""
    from .dated_hot_l1_publish import load

    catalog = load(generation).catalogs[date]
    counts = {row["pattern"]: row["direct_matching_paths"] for row in map(loads, queries.read_text().splitlines()[1:-1])}
    out = {}
    for pattern in catalog.selection.patterns:
        body = catalog.view(date, pattern)
        out[pattern] = (counts[pattern], {row["path"]: (row["b"], row["o"]) for row in body["buckets"] if (row["b"], row["o"]) != (0, 0)})
    return out


def compare(got: dict, want: dict) -> dict:
    """Exactness of one scan's catalog against a reference: membership, counts, cells."""
    missing, extra = sorted(set(want) - set(got)), sorted(set(got) - set(want))
    counts = sorted(t for t in set(got) & set(want) if got[t][0] != want[t][0])
    cells = sorted(t for t in set(got) & set(want) if got[t][1] != want[t][1])
    return {"terms": len(want), "equal": not (missing or extra or counts or cells), "missing": missing[:20], "extra": extra[:20],
            "counts_differ": counts[:20], "cells_differ": cells[:20],
            "n_missing": len(missing), "n_extra": len(extra), "n_counts_differ": len(counts), "n_cells_differ": len(cells)}


def log(message: str) -> None:
    print(message, file=stderr, flush=True)
