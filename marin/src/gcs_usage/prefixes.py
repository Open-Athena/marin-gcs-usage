"""Shared attribution-prefix loading + deepest-prefix lookup.

Used by ``report``/``gaps`` (cli) and ``webdata`` (viz). Attribution parquets
store raw users as of build time; loading re-resolves them against the
*current* identities.yaml, and folds in ``prefix_owners`` manual rows (a ``*``
in the bucket position fans out over the listing's buckets), so curation
takes effect without rebuilding any parquet.
"""

from __future__ import annotations

import sys
from fnmatch import fnmatch
from functools import partial
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    import duckdb

    from .identity import IdentityMap

err = partial(print, file=sys.stderr)


def load_prefix_map(
    con: "duckdb.DuckDBPyConnection",
    attributions: tuple[str, ...],
    identities: "IdentityMap",
    listing_src: str,
) -> dict[str, tuple]:
    """prefix -> (user, team, source); parquet rows win over manual rows."""
    by_prefix: dict[str, tuple] = {}
    for attribution in attributions:
        for prefix, user, team, source in con.execute(
            "SELECT prefix, user, team, source FROM read_parquet(?)", [attribution]
        ).fetchall():
            user = identities.resolve(user) if user else user
            by_prefix.setdefault(prefix, (user, identities.team_of(user) if user else team, source))
    buckets = [b for (b,) in con.execute(f"SELECT DISTINCT bucket FROM {listing_src}").fetchall()]
    for owner in identities.prefix_owners:
        bucket, _, rest = owner.prefix.removeprefix("gs://").partition("/")
        expanded = (f"gs://{b}/{rest}" for b in buckets if fnmatch(b, bucket))
        for prefix in expanded if "*" in bucket else (owner.prefix,):
            by_prefix.setdefault(prefix, (owner.user, owner.team, "manual"))
    err(f"{len(by_prefix)} attribution prefixes loaded")
    return by_prefix


MAX_DEEPEST_CACHE = 4_000_000


def deepest_lookup(by_prefix: dict[str, tuple]) -> Callable[[str], tuple | None]:
    """Cached ``dir_key -> attribution`` for gs-less 'bucket/a/b' dir keys:
    the attribution of the deepest attributed ancestor, or None.

    The cache (dir keys + their chopped ancestors) is epoch-cleared at
    ``MAX_DEEPEST_CACHE`` entries — unbounded it reaches tens of millions of
    string keys (several GB) on full-fleet listings."""
    cache: dict[str, tuple | None] = {}

    def deepest(dir_key: str) -> tuple | None:
        hit = cache.get(dir_key)
        if hit is not None or dir_key in cache:
            return hit
        probe = dir_key
        chopped = []
        result = None
        while True:
            row = by_prefix.get(f"gs://{probe}/")
            if row is not None:
                result = row
                break
            if "/" not in probe:
                break
            chopped.append(probe)
            probe = probe.rsplit("/", 1)[0]
        if len(cache) > MAX_DEEPEST_CACHE:
            cache.clear()
        for key in chopped:
            cache[key] = result
        cache[dir_key] = result
        return result

    return deepest
