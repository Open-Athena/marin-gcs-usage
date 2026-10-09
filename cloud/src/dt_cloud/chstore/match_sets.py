"""Registered literals with identical match sets on one scan.

A name containing a k-character literal contains its (k−1)-prefix and
(k−1)-suffix, so each of those matches a superset of its paths. When the exact
direct path count of the literal equals its prefix's (or suffix's), the two
match sets are equal (a subset with the same positive-weight total), and every
answer over matching paths (root/bucket coverage, child tiles) is the same.
An alias maps to its root: the shortest literal of its equivalence chain,
the one a catalog computes.

The counts must be the scan's own: only a registry whose source census for
that date was counted on the same snapshot qualifies; otherwise no aliases.
"""

from json import loads

from .hot_frequency_registry import UNION_SCHEMA
from .hot_l1_catalog import _unique_object


def aliases(
    registry_raw: bytes,
    date: str,
    snapshot_db: str,
) -> dict[str, str]:
    """`{alias: root}` over an already-validated union registry's records, in
    registry order (by length, so a prefix's root is resolved first)."""
    lines = registry_raw.splitlines()
    header = loads(lines[0], object_pairs_hook=_unique_object)
    if header.get('schema') != UNION_SCHEMA:
        raise ValueError('match-set aliases require a dated union registry')
    own = [source for source in header['sources'] if source['date'] == date]
    if not own or own[0]['snapshot_db'] != snapshot_db:
        return {}
    counts: dict[str, int] = {}
    for line in lines[1:-1]:
        row = loads(line, object_pairs_hook=_unique_object)
        count = row['direct_matching_paths'][date]
        if count is not None:
            counts[row['pattern']] = count
    roots: dict[str, str] = {}
    for pattern, count in counts.items():
        if len(pattern) < 2:
            continue
        for shorter in (pattern[:-1], pattern[1:]):
            if counts.get(shorter) == count:
                roots[pattern] = roots.get(shorter, shorter)
                break
    return roots
