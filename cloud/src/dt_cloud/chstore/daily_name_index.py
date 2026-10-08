"""Per-scan bounded name postings over one completed daily scalar source.

The frozen union's cold lane (`hot_l1.build` under the name-summary budget)
resolves a literal through a lowercase-basename vocabulary and per-date
`nodes_by_name` postings. A daily scalar target has neither, so a literal
below that scan's threshold had no answer. This adds both to the target
itself, keyed by the scan's own preorder geometry:

- `names(nid, l)`: each distinct lowercase basename once, trigram text index,
  `nid = sipHash64(l)`; the build refuses any hash collision, so a `nid`
  names exactly one basename (no 606M-row join to dense ids).
- `nodes_by_name(nid, pre, post, b, o)`: every node, sorted `(nid, pre)`, in
  256-row granules: a literal's names hash across the whole key range, so each
  costs a granule (Oct 6 `3p`: 6.7M rows read at 256, 194M at 8192).

`name_index_manifest` marks completion and binds the index to the exact
source-manifest bytes; the serving lane refuses an index bound to anything
else. Owned partial tables are retained on failure, never marked complete.
"""

from hashlib import sha256
from json import loads
from time import monotonic

from .client import Ch, lit
from .daily_scalar import manifest_bytes
from .hot_l1_catalog import _unique_object
from .narrow import identifier

SCHEMA = 'daily-name-index-v1'
TABLES = ('names', 'nodes_by_name', 'name_index_manifest')
BASENAME = "lowerUTF8(arrayElement(splitByChar('/', path), -1))"


def statements(target: str) -> dict[str, str]:
    identifier(target)
    return {
        'names': f"""CREATE TABLE {target}.names (nid UInt64, l String, INDEX tl l TYPE text(tokenizer = ngrams(3)))
            ENGINE = MergeTree ORDER BY l AS SELECT sipHash64(l) AS nid, l FROM (SELECT {BASENAME} AS l FROM {target}.nodes GROUP BY l)""",
        'nodes_by_name': f"""CREATE TABLE {target}.nodes_by_name (nid UInt64, pre UInt32, post UInt32, b UInt64, o UInt64)
            ENGINE = MergeTree ORDER BY (nid, pre) SETTINGS index_granularity = 256 AS SELECT sipHash64({BASENAME}) AS nid, pre, post, b, o FROM {target}.nodes""",
    }


def _source(ch: Ch, target: str) -> tuple[dict, bytes]:
    doc = ch.scalar(f'SELECT doc FROM {target}.source_manifest')
    if not isinstance(doc, str):
        raise ValueError('daily name index requires a completed daily scalar source manifest')
    body = loads(doc, object_pairs_hook=_unique_object)
    if (body.get('schema') != 'daily-scalar-source-v1' or body.get('complete') is not True or
            body.get('target') != target or body.get('snapshot_db') != target or body.get('prefix') != ''):
        raise ValueError('daily name index requires a complete global daily scalar source for this target')
    return body, manifest_bytes(body)


def build(
    ch: Ch,
    target: str,
    *,
    memory_bytes: int = 8 << 30,
    spill_bytes: int = 4 << 30,
    query_seconds: int = 3600,
) -> dict:
    """Fresh construction only: refuses when any owned table already exists."""
    identifier(target)
    source, raw = _source(ch, target)
    for table in TABLES:
        if ch.scalar(f'EXISTS TABLE {target}.{table}') != '0':
            raise ValueError(f'daily name index table {target}.{table} already exists; refusing to overwrite')
    settings = {'max_memory_usage': memory_bytes, 'max_bytes_before_external_group_by': spill_bytes,
                'max_bytes_ratio_before_external_group_by': 0, 'max_bytes_before_external_sort': spill_bytes,
                'max_bytes_ratio_before_external_sort': 0, 'max_execution_time': query_seconds}
    stages = {}
    for table, sql in statements(target).items():
        start = monotonic()
        ch.exec(sql, fmt=None, settings=settings)
        stages[table] = round(monotonic() - start, 6)
    names, unique = (int(v) for v in ch.json(f'SELECT count(), uniqExact(nid) FROM {target}.names', settings=settings)[0])
    if names != unique:
        raise ValueError(f'daily name index basename hash collision ({names - unique} duplicate ids); not marked complete')
    postings = int(ch.scalar(f'SELECT count() FROM {target}.nodes_by_name'))
    if postings != source['nodes']:
        raise ValueError('daily name index postings differ from the accepted source node count; not marked complete')
    body = {'schema': SCHEMA, 'complete': True, 'target': target, 'logical_store': source['logical_store'],
            'date': source['date'], 'prefix': '', 'names': names, 'postings': postings,
            'buckets': source['buckets'], 'source_manifest_sha256': sha256(raw).hexdigest(),
            'source_manifest_bytes': len(raw), 'stages': stages}
    ch.exec(f'CREATE TABLE {target}.name_index_manifest (doc String) ENGINE = TinyLog', fmt=None)
    ch.exec(f'INSERT INTO {target}.name_index_manifest VALUES ({lit(manifest_bytes(body).decode().rstrip())})', fmt=None)
    return body


def load(ch: Ch, target: str) -> dict:
    """The completed index manifest, re-bound to the target's current source manifest."""
    identifier(target)
    if ch.scalar(f'EXISTS TABLE {target}.name_index_manifest') != '1':
        raise ValueError(f'daily name index for {target} has no completion marker')
    body = loads(ch.scalar(f'SELECT doc FROM {target}.name_index_manifest'), object_pairs_hook=_unique_object)
    _, raw = _source(ch, target)
    if (body.get('schema') != SCHEMA or body.get('complete') is not True or body.get('target') != target or
            body.get('source_manifest_sha256') != sha256(raw).hexdigest()):
        raise ValueError('daily name index is incomplete or bound to a different source manifest')
    return body
