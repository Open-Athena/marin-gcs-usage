"""Experimental outer-root discovery without DFS positions.

IDs are opaque, persistent keys. A directory's structural ancestry includes
itself; each candidate carries its parent ID. Snapshot/version filtering must
happen before this query: the ever-seen dictionary cannot establish presence.
This primitive does not implement allocation, ingestion, or NOT subtraction.
"""

from __future__ import annotations

import json
import time

from ..bench.ch import literal_name_only, name_sql
from ..bench.duck import Unsupported
from ..bench.query import compile_query, parse
from ..bench.terms import seg_term
from .client import Ch, lit
from .narrow import disk_reserve, identifier, path_fingerprint


def outer_roots_sql(
    db: str,
    candidates: str,
    hierarchy: str,
    *,
    temporary: bool = False,
    temporary_hierarchy: bool = False,
) -> str:
    """Outermost positive candidates, joining only their selected parents.

    Candidate columns: id, parent_id, b, o; hierarchy: id, ancestors.
    No ordering or contiguity of IDs is assumed. Do not collect the candidate
    IDs in a single groupArray: use ClickHouse membership sets instead.
    """
    for value in (db, candidates, hierarchy):
        identifier(value)
    source = candidates if temporary else f"{db}.{candidates}"
    hierarchy_source = hierarchy if temporary_hierarchy else f"{db}.{hierarchy}"
    return f"""WITH positive AS (SELECT id, parent_id, b, o FROM {source}),
        blocked AS (
            SELECT id FROM (
                SELECT id, ancestors FROM {hierarchy_source} WHERE id IN (SELECT parent_id FROM positive)
            ) ARRAY JOIN ancestors AS ancestor
            WHERE ancestor IN (SELECT id FROM positive) GROUP BY id
        )
        SELECT id, b, o FROM positive WHERE parent_id NOT IN (SELECT id FROM blocked)"""


def session(
    url: str,
    target: str,
    *,
    threads: int = 2,
) -> Ch:
    identifier(target)
    if threads <= 0:
        raise ValueError("ancestry threads must be positive")
    return Ch(url, db=target, timeout=7200, max_threads=threads, max_block_size=8192, max_memory_usage=8 << 30,
              max_bytes_before_external_sort=256 << 20, max_bytes_ratio_before_external_sort=0,
              max_bytes_before_external_group_by=256 << 20, max_bytes_ratio_before_external_group_by=0)


def build(
    url: str,
    target: str,
    date: str,
    *,
    table: str = "ancestry_nodes",
    min_free_bytes: int = 0,
) -> dict:
    """Inline parent IDs in a new name-ordered scalar table for one selected date.

    The initial keys reuse the frozen dictionary, but the query algorithm never
    treats them as DFS positions. This build is not an incremental allocator.
    """
    identifier(table)
    if min_free_bytes < 0:
        raise ValueError("min_free_bytes cannot be negative")
    ch = session(url, target)
    try:
        if ch.scalar(f"EXISTS TABLE {table}") == "1":
            raise ValueError(f"experimental table already exists: {target}.{table}")
        manifest = json.loads(ch.scalar("SELECT doc FROM history_manifest"))
        if date not in manifest["dates"]:
            raise ValueError(f"date outside experimental history: {date}")
        db = identifier(manifest["dbs"][manifest["dates"].index(date)])
        disk_reserve(ch, table, min_free_bytes)
        start = time.monotonic()
        ch.exec(f"""CREATE TABLE {table} ENGINE = MergeTree ORDER BY (nid, id) AS
            SELECT n.pre AS id, m.parent_pre AS parent_id, n.nid AS nid, n.b AS b, n.o AS o
            FROM {db}.nodes n INNER JOIN {db}.metadata m ON n.pre = m.pre""",
                settings={"join_algorithm": "full_sorting_merge", "log_comment": f"narrow:{target}:{table}"})
        seconds = round(time.monotonic() - start, 3)
        actual, expected = int(ch.scalar(f"SELECT count() FROM {table}")), int(ch.scalar(f"SELECT count() FROM {db}.nodes"))
        if actual != expected:
            raise ValueError(f"ancestry scalar rows lost: {actual} != {expected}; partial table retained")
        ch.exec("CREATE VIEW IF NOT EXISTS ancestry_hierarchy AS SELECT pre AS id, ancestors FROM hierarchy")
        result = {"target": target, "date": date, "prefix": manifest["prefix"], "source_db": db,
                  "table": table, "nodes": actual, "seconds": seconds, "incremental": False}
        ch.exec(f"CREATE TABLE {table}_manifest (doc String) ENGINE = TinyLog")
        ch.exec(f"INSERT INTO {table}_manifest VALUES ({lit(json.dumps(result))})")
        return result
    finally:
        ch.close()


def evaluate(
    url: str,
    target: str,
    table: str,
    query: str,
    syntax: str = "simple",
    *,
    threads: int = 8,
) -> dict:
    """Literal discovery + uncapped root fingerprint; NOT a tree response."""
    identifier(table)
    ast = parse(query, syntax)
    if ast is None or not literal_name_only(ast):
        raise Unsupported("ancestry experiment only supports one unanchored literal without NOT")
    ch = session(url, target, threads=threads)
    try:
        manifest = json.loads(ch.scalar(f"SELECT doc FROM {table}_manifest"))
        db, prefix = identifier(manifest["source_db"]), manifest["prefix"]
        start = time.monotonic()
        if compile_query(ast)(prefix):
            roots = [prefix]
            n, b, o = ch.json(f"SELECT count(), sum(b), sum(o) FROM {db}.nodes WHERE pre = 0")[0]
            discovery_s, materialize_s, fingerprint = time.monotonic() - start, 0.0, None
        else:
            ch.tmp("cn", f"SELECT nid FROM names WHERE {name_sql(seg_term(ast.alts[0][0]).name, trigram=True)}")
            ch.tmp("ancestry_candidates", f"SELECT id, parent_id, b, o FROM {table} WHERE nid IN (SELECT nid FROM cn) AND id != 0")
            ch.tmp("ancestry_roots", outer_roots_sql(target, "ancestry_candidates", "ancestry_hierarchy", temporary=True))
            n, b, o = ch.json("SELECT count(), sum(b), sum(o) FROM ancestry_roots")[0]
            discovery_s = time.monotonic() - start
            source = f"SELECT path FROM {db}.nodes_by_name WHERE nid IN (SELECT nid FROM cn) AND pre IN (SELECT id FROM ancestry_roots)"
            start = time.monotonic()
            if n <= 50_000:
                roots, fingerprint = [row[0] for row in ch.json(source + " ORDER BY path")], None
            else:
                roots = None
                count, fingerprint = path_fingerprint(ch, source)
                if count != n:
                    raise ValueError(f"ancestry fingerprint lost rows: {count} != {n}")
            materialize_s = time.monotonic() - start
        return {"date": manifest["date"], "prefix": prefix, "roots": roots, "n": n, "md5": fingerprint, "b": b, "o": o,
                "discovery_s": round(discovery_s, 4), "materialize_s": round(materialize_s, 4), "renders_tree": False, "incremental": False}
    finally:
        ch.close()
