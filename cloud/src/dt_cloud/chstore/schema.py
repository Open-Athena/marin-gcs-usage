"""The store's tables (specs/ch-store.md §2). Everything is append-only.

- `nodes`: one row per version of a `(depth, path, usr)` owner slice — the
  path store's row (`size`, `n_files`, `kind`, …) and `vf`, the scan that
  opened it. Written once, when it opens; never touched again. Partitioned
  by `vf` (one partition per scan: a crashed ingest's rows are one
  `DROP PARTITION`), sorted `(depth, path, usr, vf)`. The `by_name`
  projection (sorted by the lowercase last segment) is the filter's name →
  nodes access path; `by_parent` (sorted `(depth, parent, size)`) makes "a
  level's children over a threshold" one short range per parent.
- `closures`: the version's end, `vt` — the scan that first didn't see it —
  one row per closed version, keyed like `nodes` (`… , vf`) and partitioned
  by `vt`. A scan D sees a version iff `vf <= D` and no closure of it has
  `vt <= D` (`live`): an anti-join on a churn-sized table, restricted to the
  rows a read touches (it carries `parent` and `name` for that, and the
  same two projections).
- `changes`: every opened (`sign = 1`, at its `vf`) and closed (`sign = −1`,
  at its `vt`) version of a day-to-day ingest, sorted `(at, depth, path, …)`:
  the change-keyed access path (what moved between D1 and D2 is `at ∈
  (D1, D2]`). A first scan or a source-format switch records none.
- `names`: every lowercase segment name ever seen, with a text index — the
  filter's vocabulary.
- `scans`: the ingested scans (`id` as the site names them).

Nullable source fields are stored with sentinels (sort keys can't be
Nullable, and `Nullable` columns cost a null map): `usr = ''` (unattributed),
`n_children` / `n_desc` / `mtime` / `last_read` = −1, and a missing
`mtime_mean` = 0 with `mtime_w = 0` (the weight the Worker skips)."""

from __future__ import annotations

from .client import Ch

OPEN = "2106-01-01 00:00:00"  # a version's `vt` while no closure ends it

# The version's values: a change in any of these opens a new version.
VALUE_COLS = ["kind", "size", "n_files", "n_children", "n_desc", "mtime", "mtime_mean", "mtime_w", "last_read", "c2", "c3", "c4"]
KEY_COLS = ["depth", "path", "usr"]

VALUES_DDL = """kind Enum8('dir' = 0, 'file' = 1),
    size Int64,
    n_files Int64,
    n_children Int64,
    n_desc Int64,
    mtime Int64,
    mtime_mean Float64,
    mtime_w Int64,
    last_read Int32,
    c2 Int64,
    c3 Int64,
    c4 Int64"""

PARENT_DEFAULT = "if(position(path, '/') = 0, '', substring(path, 1, length(path) - position(reverse(path), '/')))"

NODES = f"""CREATE TABLE IF NOT EXISTS nodes (
    depth UInt8,
    path String CODEC(ZSTD(3)),
    usr LowCardinality(String),
    vf DateTime('UTC'),
    {VALUES_DDL},
    name String CODEC(ZSTD(3)),
    parent String DEFAULT {PARENT_DEFAULT} CODEC(ZSTD(3)),
    INDEX size_mm size TYPE minmax GRANULARITY 1,
    PROJECTION by_name (SELECT * ORDER BY name, depth, path, usr, vf),
    PROJECTION by_parent (SELECT * ORDER BY depth, parent, size)
) ENGINE = MergeTree
PARTITION BY vf
ORDER BY (depth, path, usr, vf)
SETTINGS deduplicate_merge_projection_mode = 'rebuild'"""

CLOSURES = f"""CREATE TABLE IF NOT EXISTS closures (
    depth UInt8,
    path String CODEC(ZSTD(3)),
    usr LowCardinality(String),
    vf DateTime('UTC'),
    vt DateTime('UTC'),
    name String CODEC(ZSTD(3)),
    parent String DEFAULT {PARENT_DEFAULT} CODEC(ZSTD(3)),
    PROJECTION by_name (SELECT * ORDER BY name, depth, path),
    PROJECTION by_parent (SELECT * ORDER BY depth, parent, path)
) ENGINE = MergeTree
PARTITION BY vt
ORDER BY (depth, path, usr, vf)
SETTINGS deduplicate_merge_projection_mode = 'rebuild'"""

CHANGES = """CREATE TABLE IF NOT EXISTS changes (
    at DateTime('UTC'),
    sign Int8,
    depth UInt8,
    path String CODEC(ZSTD(3)),
    usr LowCardinality(String),
    vf DateTime('UTC'),
    kind Enum8('dir' = 0, 'file' = 1),
    size Int64,
    n_files Int64,
    c2 Int64,
    c3 Int64,
    c4 Int64,
    name String CODEC(ZSTD(3))
) ENGINE = MergeTree
PARTITION BY at
ORDER BY (at, depth, path, usr, sign)"""

NAMES = """CREATE TABLE IF NOT EXISTS names (
    l String,
    INDEX tl l TYPE text(tokenizer = ngrams(3))
) ENGINE = ReplacingMergeTree
ORDER BY l"""

SCANS = """CREATE TABLE IF NOT EXISTS scans (
    scan DateTime('UTC'),
    id String,
    version UInt8,
    rows UInt64,
    opened UInt64,
    closed UInt64,
    s Float64,
    src String,
    at DateTime('UTC') DEFAULT now()
) ENGINE = ReplacingMergeTree(at)
ORDER BY scan"""

def name_expr(path: str = "path") -> str:
    """The lowercase last segment of a path."""
    return f"lowerUTF8(splitByChar('/', {path})[-1])"


def create(ch: Ch) -> None:
    ch.exec(f"CREATE DATABASE IF NOT EXISTS {ch.db}", settings={"database": "default"})
    for ddl in (NODES, CLOSURES, CHANGES, NAMES, SCANS):
        ch.exec(ddl)


def scan_epochs(ch: Ch) -> list[tuple[str, str, int, int, str]]:
    """Published scans with their proven live-version lower bounds.

    A format switch can leave unchanged slices open. Only a full rebaseline
    (every live row opened at that scan) advances the epoch; ordinary churn
    inherits it. Ingest and serving must apply the same proof.
    """
    records = []
    epoch = ""
    for ident, dt, version, rows, opened in ch.json("SELECT id, toString(scan), version, rows, opened FROM scans FINAL ORDER BY scan"):
        if not epoch or opened == rows:
            epoch = dt
        records.append((ident, dt, version, rows, epoch))
    return records


def live(D: str, restrict: str = "1", since: str | None = None) -> str:
    """The versions scan `D` (a DateTime literal) sees: opened by then, and not closed by then. `restrict`
    (columns `nodes` and `closures` share: `depth`, `path`, `usr`, `parent`, `name`) bounds the closures
    the anti-join reads to the rows the query reads. `since` is the most recent complete rebaseline
    (every live row opened at that scan), so neither side needs earlier versions."""
    lower = f"vf >= {since} AND " if since is not None else ""
    return f"({lower}vf <= {D} AND (depth, path, usr, vf) NOT IN (SELECT depth, path, usr, vf FROM closures WHERE {lower}vt <= {D} AND ({restrict})))"


def dt_lit(d: str) -> str:
    """A `DateTime('UTC')` literal."""
    return f"toDateTime('{d}', 'UTC')"


def scan_dt(scan_id: str) -> str:
    """A scan id (`2026-10-01`, `2026-10-01T0003`) as a DateTime string."""
    import re

    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:T(\d{2})(\d{2}))?", scan_id)
    if not m:
        raise ValueError(f"bad scan id {scan_id!r}")
    return f"{m.group(1)} {m.group(2) or '00'}:{m.group(3) or '00'}:00"
