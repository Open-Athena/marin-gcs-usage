"""Match-set aliases end to end: a real complete census, its own-scan union
registry, and a dated L1 built over roots only, against an independent
first-hit oracle for every registered literal (aliases included)."""

from json import loads
from os import environ
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud.chstore import dated_hot_l1, hot_frequency_bench
from dt_cloud.chstore.daily_scalar import build, manifest_bytes
from dt_cloud.chstore.hot_frequency_union import union
from dt_cloud.chstore.hot_registry_selection import envelope, validate
from dt_cloud.chstore.match_sets import aliases
from test_chdaily_scalar import descriptor, fresh  # noqa: F401
from chserver import ch_url  # noqa: F401

DAY = '2026-10-06'
PATHS = ['a', 'a/run1', 'a/run1/model.npy', 'a/run1/model.npz', 'a/run2', 'a/run2/model.npy', 'a/run2/zarr.json',
         'b', 'b/data', 'b/data/model.npy', 'b/data/zarr.json', 'b/zarr.json', 'b/zarr.json/c0']


def own(path: str) -> tuple[int, int]:
    """Every path has one own object; bytes vary so buckets/roots differ."""
    return len(path), 1


def write_tree(path: Path) -> dict[str, tuple[int, int]]:
    totals = {name: tuple(map(sum, zip(*(own(child) for child in PATHS if child == name or child.startswith(name + '/')))))
              for name in PATHS}
    rows = [(name, 'u', 'dir' if any(child.startswith(name + '/') for child in PATHS) else 'file', name.count('/') + 1, *totals[name])
            for name in reversed(PATHS)]
    pq.write_table(pa.table(dict(zip(('path', 'usr', 'kind', 'depth', 'size', 'n_files'), zip(*rows), strict=True))), path, row_group_size=3)
    return totals


def oracle(totals: dict[str, tuple[int, int]], pattern: str) -> dict:
    """First hits: paths whose basename contains the literal, under no matching ancestor."""
    def hit(name: str) -> bool:
        return pattern in name.rsplit('/', 1)[-1].lower()

    buckets: dict[str, list[int]] = {}
    for name in PATHS:
        parts = name.split('/')
        if hit(name) and not any(hit('/'.join(parts[:depth])) for depth in range(1, len(parts))):
            pair = buckets.setdefault(parts[0], [0, 0])
            pair[0] += totals[name][0]
            pair[1] += totals[name][1]
    return {path: tuple(pair) for path, pair in sorted(buckets.items())}


def test_alias_answers_equal_an_independent_oracle(fresh: tuple, tmp_path: Path) -> None:
    census_binary, l1_binary = environ.get('HF_NATIVE_BINARY'), environ.get('HL1_NATIVE_BINARY')
    if not census_binary or not l1_binary:
        pytest.skip('HF_NATIVE_BINARY and HL1_NATIVE_BINARY are required')
    ch, target = fresh
    parquet, source = tmp_path / 'input.parquet', tmp_path / 'source.json'
    totals = write_tree(parquet)
    source.write_bytes(manifest_bytes(build(ch, target, parquet, descriptor(parquet), min_free_bytes=1)))
    census, queries, registry = tmp_path / 'census.json', tmp_path / 'queries.jsonl', tmp_path / 'registry.jsonl'
    hot_frequency_bench.bench(ch.url, target, DAY, 2, None, census, daily_source=source, queries_out=queries,
                              seconds=30, wall_seconds=120, native=Path(census_binary))
    union(((census, queries),), 2, None, registry)
    registry_raw, source_raw = registry.read_bytes(), source.read_bytes()
    selection = validate(manifest_bytes(envelope(registry_raw, source_raw, logical_store='gcs')), registry_raw, source_raw)

    found = aliases(registry_raw, DAY, target)
    roots = tuple(pattern for pattern in selection.patterns if pattern not in found)
    # Each alias matches exactly its root's paths (its count equals its shorter neighbour's).
    assert {alias: oracle(totals, alias) for alias in found} == {alias: oracle(totals, root) for alias, root in found.items()}
    assert 0 < len(found) < len(selection.patterns)
    assert all(root in roots for root in found.values())

    out = tmp_path / 'dated.json'
    body = dated_hot_l1.build(ch, selection, binary=Path(l1_binary), out=out)
    assert (body['schema'], body['aliases'], [result['pattern'] for result in body['results']], body['native']['registered_predicates']) == (
        'dated-hot-l1-native-v2', [[alias, root] for alias, root in found.items()], list(roots), len(roots))
    reader = dated_hot_l1.DatedHotL1Catalog.load(out)
    served = {}
    for pattern in selection.patterns:
        view = reader.view(DAY, pattern)
        assert view['pattern'] == pattern
        served[pattern] = {row['path']: (row['b'], row['o']) for row in view['buckets'] if row['b'] or row['o']}
    assert served == {pattern: oracle(totals, pattern) for pattern in selection.patterns}

    # The aliases are recomputed from the pinned registry: a dropped, invented
    # or rerouted alias is refused, not served.
    alias, root = body['aliases'][0]
    other = next(pattern for pattern in roots if pattern != root)
    for change in (lambda b: b['aliases'].pop(0), lambda b: b['aliases'].append([other, root]), lambda b: b['aliases'][0].__setitem__(1, other)):
        forged = loads(manifest_bytes(body))
        change(forged)
        with pytest.raises(ValueError, match="^dated L1 match-set aliases differ from the pinned registry's own-scan frequencies$"):
            dated_hot_l1.DatedHotL1Catalog.from_bytes(manifest_bytes(forged))


def test_no_aliases_when_the_registry_counted_another_snapshot(tmp_path: Path) -> None:
    from test_chhot_frequency_union import source

    census, queries = source(tmp_path / 'c', DAY, {'a': 9, 'b': 7, 'ab': 7}, max_chars=None)
    union(((census, queries),), 5, None, tmp_path / 'registry.jsonl')
    raw = (tmp_path / 'registry.jsonl').read_bytes()
    assert (aliases(raw, DAY, 'snapshot_20261006'), aliases(raw, DAY, 'other_snapshot'), aliases(raw, '2026-10-05', 'snapshot_20261006')) == (
        {'ab': 'b'}, {}, {})
