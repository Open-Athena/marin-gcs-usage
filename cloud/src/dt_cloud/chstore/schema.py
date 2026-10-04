"""The store's tables (specs/ch-store.md §2).

- `nodes`: one row per version of a `(depth, path, usr)` owner slice — the
  path store's row (`size`, `n_files`, `kind`, …) plus its interval
  `[vf, vt)`. A scan D sees a version iff `vf <= D < vt`. Open versions
  (still present at the newest scan) have `vt = OPEN` and sit in their own
  partition (`toYYYYMM(OPEN)`), rewritten whole each ingest and swapped in by
  `REPLACE PARTITION`; closed versions are appended to the month partition
  of their `vt` and never touched again. Sorted `(depth, path, usr, vf)`:
  a subtree at one depth is one primary-key range. The `by_name` projection
  (the same rows sorted by the lowercase last segment) is the filter's
  name → nodes access path; `by_parent` (sorted `(depth, parent, size)`)
  makes "a level's children over a threshold" one short range per parent.
- `changes`: every opened (`sign = 1`, at its `vf`) and closed (`sign = −1`,
  at its `vt`) version, sorted `(at, depth, path, …)`: the change-keyed
  access path. Whatever moved between scans D1 and D2 is the rows with
  `at ∈ (D1, D2]`, and their signed sum is the net change.
- `names`: every lowercase segment name ever seen, with a text index — the
  filter's vocabulary.
- `scans`: the ingested scans (`id` as the site names them).

Nullable source fields are stored with sentinels (sort keys can't be
Nullable, and `Nullable` columns cost a null map): `usr = ''` (unattributed),
`n_children` / `n_desc` / `mtime` / `last_read` = −1, and a missing
`mtime_mean` = 0 with `mtime_w = 0` (the weight the Worker skips)."""

from __future__ import annotations

from .client import Ch

OPEN = "2106-01-01 00:00:00"
OPEN_PART = "210601"
NULL_USR = ""

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

NODES = f"""CREATE TABLE IF NOT EXISTS {{t}} (
    depth UInt8,
    path String CODEC(ZSTD(3)),
    usr LowCardinality(String),
    vf DateTime('UTC'),
    vt DateTime('UTC'),
    {VALUES_DDL},
    name String CODEC(ZSTD(3)),
    parent String DEFAULT if(position(path, '/') = 0, '', substring(path, 1, length(path) - position(reverse(path), '/'))) CODEC(ZSTD(3)),
    INDEX size_mm size TYPE minmax GRANULARITY 1,
    PROJECTION by_name (SELECT * ORDER BY name, depth, path, usr, vf),
    PROJECTION by_parent (SELECT * ORDER BY depth, parent, size)
) ENGINE = MergeTree
PARTITION BY toYYYYMM(vt)
ORDER BY (depth, path, usr, vf)
SETTINGS lightweight_mutation_projection_mode = 'rebuild', deduplicate_merge_projection_mode = 'rebuild'"""

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
PARTITION BY toYYYYMM(at)
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

# The stage: the new scan's rows (`s = 1`) beside the open rows (`s = 0`),
# sorted by key so one in-order GROUP BY pairs them.
STAGE = f"""CREATE TABLE {{t}} (
    depth UInt8,
    path String CODEC(ZSTD(1)),
    usr LowCardinality(String),
    s UInt8,
    vf DateTime('UTC'),
    {VALUES_DDL}
) ENGINE = MergeTree
ORDER BY (depth, path, usr, s)"""


def name_expr(path: str = "path") -> str:
    """The lowercase last segment of a path."""
    return f"lowerUTF8(splitByChar('/', {path})[-1])"


def create(ch: Ch) -> None:
    ch.exec(f"CREATE DATABASE IF NOT EXISTS {ch.db}", settings={"database": "default"})
    ch.exec(NODES.format(t="nodes"))
    for ddl in (CHANGES, NAMES, SCANS):
        ch.exec(ddl)


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
