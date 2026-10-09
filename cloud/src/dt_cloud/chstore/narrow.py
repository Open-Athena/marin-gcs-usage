"""Bounded experiment: frozen multi-scan path IDs, DFS intervals and late metadata.

This is NOT an incremental historical index. The selected scans share the
union's numbering; another scan requires rebuilding that union. Builds only
create new, explicitly named experimental databases and never change the store.
Run the build on a dev node, not on the laptop.
"""

from __future__ import annotations

import json
import re
import struct
import time
from hashlib import md5
from itertools import chain
from math import isfinite
from typing import Callable, Iterable, Iterator

import numpy as np

from ..bench.ch import ChIndex, intervals, literal_name_only
from ..bench.query import Ast, parse
from .client import Ch, lit, rowbinary_strings
from .coalesce import pair_query, window_query
from .ingest import sample_bounds
from .schema import OPEN, dt_lit, name_expr, scan_dt
from .serve import AGG, AGG_COLS, Store, depth_of, filter_prepare, under


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", value):
        raise ValueError(f"invalid experimental database name: {value!r}")
    return value


def rich_name_variant_identity(target: str, variant: str) -> dict[str, str]:
    """Checkpoint identity shared by index creation, benchmarks and serving."""
    identifier(target)
    identifier(variant)
    return {"target": target, "variant": variant, "view": f"metadata_by_name_{variant}",
            "table": f"metadata_history_by_name_{variant}"}


def path_fingerprint(ch: Ch, source: str) -> tuple[int, str]:
    """The benchmark's complete sorted/newline-joined MD5, in bounded memory."""
    digest, count = md5(), 0
    for path in rowbinary_strings(ch.stream(f"SELECT path FROM ({source}) ORDER BY path", "RowBinary")):
        if count:
            digest.update(b"\n")
        digest.update(path)
        count += 1
    return count, digest.hexdigest()


def disk_reserve(
    ch: Ch,
    label: str,
    min_free_bytes: int,
) -> None:
    """Check before a stage, not an in-flight disk quota; retain partial builds."""
    if min_free_bytes:
        available = int(ch.scalar("SELECT min(free_space) FROM system.disks"))
        if available < min_free_bytes:
            raise ValueError(f"before {label}: {available} free bytes < reserve {min_free_bytes}; partial build retained")


def interval_rows(pre: np.ndarray, post: np.ndarray) -> Iterator[bytes]:
    """RowBinary `(id, pre, post)` without materializing paths in Python."""
    for start in range(0, len(pre), 1 << 20):
        end = min(start + (1 << 20), len(pre))
        rows = np.empty(end - start, dtype=[("id", "<u4"), ("pre", "<u4"), ("post", "<u4")])
        rows["id"] = np.arange(start, end, dtype=np.uint32)
        rows["pre"], rows["post"] = pre[start:end], post[start:end]
        yield rows.tobytes()


def preorder_parents(
    chunks: Iterable[bytes],
    count: int,
    *,
    root_depth: int,
) -> Iterator[bytes]:
    """RowBinary `(pre, parent_pre)` from ordered `(pre, depth)`, O(depth) state.

    Frozen DFS keys only; this is not parent derivation for opaque stable IDs.
    Input records may straddle chunks; output batches stay below one MiB.
    """
    if not 0 < count < 2**32 or not 0 <= root_depth <= 255:
        raise ValueError("numeric parent count/depth exceed the frozen key domain")
    pending, output, stack, seen = b"", bytearray(), [], 0
    for chunk in chunks:
        data = pending + chunk
        complete = len(data) // 5 * 5
        for pre, depth in struct.iter_unpack("<IB", memoryview(data)[:complete]):
            if pre != seen:
                raise ValueError(f"noncontiguous preorder key: {pre} != {seen}")
            relative = depth - root_depth
            if seen == 0 and relative != 0:
                raise ValueError("first row must be the selected root")
            if seen and relative <= 0:
                raise ValueError("only one selected root is allowed")
            if relative > len(stack):
                raise ValueError(f"preorder depth has no parent: {depth}")
            while len(stack) > relative:
                stack.pop()
            output.extend(struct.pack("<Iq", pre, stack[-1] if stack else -1))
            stack.append(pre)
            seen += 1
            if len(output) >= 768 << 10:
                yield bytes(output)
                output.clear()
        pending = data[complete:]
    if pending:
        raise ValueError("truncated numeric parent input")
    if seen != count:
        raise ValueError(f"numeric parent rows lost: {seen} != {count}")
    if output:
        yield bytes(output)


def numeric_parent_index(
    url: str,
    target: str,
    *,
    min_free_bytes: int = 0,
) -> dict:
    """Build all frozen path parent links once, without decoding path strings."""
    identifier(target)
    if min_free_bytes < 0:
        raise ValueError("min_free_bytes cannot be negative")
    ch = Ch(url, db=target, timeout=7200, max_threads=2, max_block_size=8192, max_memory_usage=8 << 30,
            max_bytes_before_external_sort=256 << 20, max_bytes_ratio_before_external_sort=0)
    reader = ch.fork()
    try:
        if ch.scalar("EXISTS TABLE numeric_parents") == "1":
            raise ValueError(f"experimental table already exists: {target}.numeric_parents")
        manifest = json.loads(ch.scalar("SELECT doc FROM manifest"))
        count = manifest["union_nodes"]
        disk_reserve(ch, "numeric_parents", min_free_bytes)
        start = time.monotonic()
        ch.exec("CREATE TABLE numeric_parents (pre UInt32, parent_pre Int64) ENGINE = MergeTree ORDER BY pre")
        chunks = reader.stream("SELECT pre, toUInt8(depth) FROM dictionary ORDER BY pre", "RowBinary",
                               settings={"log_comment": f"narrow:{target}:numeric_parents"})
        rows = iter(preorder_parents(chunks, count, root_depth=depth_of(manifest["prefix"])))
        first = next(rows)
        ch.insert("INSERT INTO numeric_parents FORMAT RowBinary", chain((first,), rows))
        actual = int(ch.scalar("SELECT count() FROM numeric_parents"))
        if actual != count:
            raise ValueError(f"numeric parent upload lost rows: {actual} != {count}; partial table retained")
        result = {"target": target, "nodes": actual, "seconds": round(time.monotonic() - start, 3), "incremental": False}
        ch.exec("CREATE TABLE numeric_parent_manifest (doc String) ENGINE = TinyLog")
        ch.exec(f"INSERT INTO numeric_parent_manifest VALUES ({lit(json.dumps(result))})")
        return result
    finally:
        reader.close()
        ch.close()


def preorder_ancestors(
    chunks: Iterable[bytes],
    parent_rows: int,
    union_nodes: int,
    *,
    root_depth: int,
) -> Iterator[bytes]:
    """Sparse directory `(pre, depth)` → inclusive numeric ancestor arrays.

    Every directory ancestor is itself a parent, so the parent-only preorder
    remains ancestor-closed. Leaf gaps are valid; missing directories are not.
    Retain O(depth) stack state and bounded RowBinary payloads, not N arrays.
    """
    if not 0 <= parent_rows <= union_nodes < 2**32 or union_nodes == 0 or not 0 <= root_depth <= 255:
        raise ValueError("directory stream bounds exceed the frozen key domain")
    pending, output, stack, seen, previous = b"", bytearray(), [], 0, -1
    for chunk in chunks:
        data = pending + chunk
        complete = len(data) // 5 * 5
        for pre, depth in struct.iter_unpack("<IB", memoryview(data)[:complete]):
            if seen >= parent_rows:
                raise ValueError("directory stream exceeds its expected row count")
            if not previous < pre < union_nodes:
                raise ValueError("directory preorder keys must increase within the union")
            relative = depth - root_depth
            if seen == 0 and (pre != 0 or relative != 0):
                raise ValueError("directory stream must start at the selected root")
            if seen and relative <= 0:
                raise ValueError("directory stream has another root")
            if relative > len(stack):
                raise ValueError("directory stream lacks an ancestor")
            del stack[relative:]
            stack.append(pre)
            output.extend(struct.pack("<I", pre))
            length = len(stack)
            while length >= 128:
                output.append((length & 127) | 128)
                length >>= 7
            output.append(length)
            output.extend(struct.pack(f"<{len(stack)}I", *stack))
            seen, previous = seen + 1, pre
            if len(output) >= 1 << 20:
                yield bytes(output)
                output.clear()
        pending = data[complete:]
    if pending or seen != parent_rows:
        raise ValueError(f"incomplete directory stream: {seen} != {parent_rows} rows, {len(pending)} trailing bytes")
    if output:
        yield bytes(output)


def stream_hierarchy(
    ch: Ch,
    target: str,
    *,
    table: str = "hierarchy_stream",
    min_free_bytes: int = 0,
) -> dict:
    """One parent-only preorder pass; no repeated string joins or full-ID set.

    Requires completed `parent_paths` from history preparation. Creates only
    a new table and retains partial failures; this is a frozen hierarchy.
    """
    identifier(target)
    identifier(table)
    if min_free_bytes < 0:
        raise ValueError("min_free_bytes cannot be negative")
    if ch.scalar(f"EXISTS TABLE {target}.{table}") == "1":
        raise ValueError(f"experimental table already exists: {target}.{table}")
    manifest = json.loads(ch.scalar(f"SELECT doc FROM {target}.manifest"))
    count = int(ch.scalar(f"SELECT count() FROM {target}.parent_paths"))
    if count != int(ch.scalar(f"SELECT count() FROM {target}.parents")):
        raise ValueError("parent_paths row count differs from the frozen directory parents")
    disk_reserve(ch, table, min_free_bytes)
    start = time.monotonic()
    ch.exec(f"CREATE TABLE {target}.{table} (pre UInt32, ancestors Array(UInt32)) ENGINE = MergeTree ORDER BY pre")
    reader = ch.fork(max_threads=1, max_memory_usage=8 << 30, max_block_size=8192,
                     log_comment=f"narrow:{target}:{table}")
    try:
        chunks = reader.stream(f"SELECT pre, toUInt8(if(path = '', 0, length(splitByChar('/', path)))) FROM {target}.parent_paths ORDER BY pre", "RowBinary")
        encoded = iter(preorder_ancestors(chunks, count, manifest["union_nodes"], root_depth=depth_of(manifest["prefix"])))
        first = next(encoded, None)
        # An empty parent tree (a leaf-only selected root) legitimately has no
        # payload. Otherwise prime before opening an idle HTTP INSERT.
        if first is not None:
            ch.insert(f"INSERT INTO {target}.{table} FORMAT RowBinary", chain((first,), encoded),
                      settings={"max_memory_usage": 8 << 30, "max_threads": 2, "max_insert_threads": 1})
        actual = int(ch.scalar(f"SELECT count() FROM {target}.{table}"))
        if actual != count:
            raise ValueError(f"hierarchy upload lost rows: {actual} != {count}; partial table retained")
        return {"directories": actual, "table": f"{target}.{table}", "seconds": round(time.monotonic() - start, 3),
                "engine": "stream", "incremental": False}
    finally:
        reader.close()


def merge_snapshots(target: str, count: int) -> str:
    """Distinct sorted snapshots can be unioned without a global hash aggregate."""
    identifier(target)
    if count < 1:
        raise ValueError("at least one snapshot is required")
    query = f"SELECT depth, path, parent FROM {target}.snapshot_0"
    for i in range(1, count):
        query = f"""SELECT assumeNotNull(coalesce(a.depth, b.depth)) AS depth,
            assumeNotNull(coalesce(a.path, b.path)) AS path, assumeNotNull(coalesce(a.parent, b.parent)) AS parent
            FROM ({query}) a FULL OUTER JOIN {target}.snapshot_{i} b ON a.depth = b.depth AND a.path = b.path"""
    return query


def tree_order_expr(path: str = "path") -> str:
    """A byte-string equivalent to lexicographic segment-array ordering.

    Encode a separator as NUL,NUL and a literal NUL as NUL,SOH. A
    separator then sorts before every real segment byte, including NUL,
    without conflating control bytes with structural path separators.
    """
    return f"replaceAll(replaceAll({path}, char(0), concat(char(0), char(1))), '/', concat(char(0), char(0)))"


def preorder_intervals(chunks: Iterable[bytes], count: int) -> Iterator[bytes]:
    """Stream `(id UInt32, relative_depth UInt8)` into `(id, pre, post)`.

    Input must be tree preorder, not plain path-string order. Only the ancestor
    stack and bounded transport buffers are retained, rather than N arrays.
    """
    record, encoded = struct.Struct("<IB"), struct.Struct("<III")
    pending, output = b"", bytearray()
    stack = []
    seen = 0
    for chunk in chunks:
        data = pending + chunk
        end = len(data) // record.size * record.size
        pending = data[end:]
        for node, depth in record.iter_unpack(data[:end]):
            if node >= count or depth < 1 or depth > len(stack) + 1 or seen >= count:
                raise ValueError(f"invalid preorder row: id={node}, depth={depth}, position={seen}")
            while len(stack) >= depth:
                old, pre = stack.pop()
                output.extend(encoded.pack(old, pre, seen - 1))
            stack.append((node, seen))
            seen += 1
            if len(output) >= 1 << 20:
                yield bytes(output)
                output.clear()
    if pending or seen != count:
        raise ValueError(f"incomplete preorder stream: {seen} != {count} rows, {len(pending)} trailing bytes")
    while stack:
        node, pre = stack.pop()
        output.extend(encoded.pack(node, pre, seen - 1))
    if output:
        yield bytes(output)


def stream_intervals(
    ch: Ch,
    target: str,
    prefix: str,
    *,
    table: str = "intervals_stream",
    order_engine: str = "escaped",
    checkpoint: bool = False,
) -> dict:
    """External tree sort + O(depth) interval construction; retain partial failures."""
    identifier(target)
    identifier(table)
    if order_engine not in ("segments", "escaped"):
        raise ValueError(f"unknown tree order engine: {order_engine}")
    if prefix.strip("/") != prefix:
        raise ValueError("a canonical subtree prefix is required (empty means global)")
    if checkpoint and table != "intervals":
        raise ValueError("only the build's intervals table can be checkpointed")
    if checkpoint:
        plan = json.loads(ch.scalar(f"SELECT doc FROM {target}.paths_manifest"))
        if plan["prefix"] != prefix:
            raise ValueError("interval prefix differs from the path checkpoint")
        if ch.scalar(f"EXISTS TABLE {target}.interval_manifest") == "1":
            raise ValueError("an interval checkpoint already exists")
    count = int(ch.scalar(f"SELECT count() FROM {target}.ids"))
    if not 0 < count < 2**32:
        raise ValueError("interval count must fit UInt32")
    settings = {"max_threads": 2, "max_memory_usage": 8 << 30, "max_bytes_before_external_sort": 256 << 20,
                "max_bytes_ratio_before_external_sort": 0,
                "max_block_size": 8192,
                "log_comment": f"narrow:{target}:intervals_stream"}
    ch.exec(f"CREATE TABLE {target}.{table} (id UInt32, pre UInt32, post UInt32) ENGINE = MergeTree ORDER BY id")
    reader = ch.fork(**settings)
    start = time.monotonic()
    try:
        # Plain strings put `a-b` between `a` and `a/child`, breaking subtree
        # contiguity. An escaped string avoids per-segment Array overhead.
        order = tree_order_expr() if order_engine == "escaped" else "splitByChar('/', path)"
        chunks = reader.stream(f"SELECT id, toUInt8(depth - {depth_of(prefix)} + 1) FROM {target}.ids ORDER BY {order}", "RowBinary")
        encoded = iter(preorder_intervals(chunks, count))
        # A large external sort may emit nothing for longer than the server's
        # HTTP receive timeout. Wait for one bounded payload BEFORE opening
        # the upload connection, rather than leaving an idle INSERT request.
        first = next(encoded)
        ch.insert(f"INSERT INTO {target}.{table} FORMAT RowBinary", chain((first,), encoded), settings=settings)
        actual = int(ch.scalar(f"SELECT count() FROM {target}.{table}"))
        if actual != count:
            raise ValueError(f"interval upload lost rows: {actual} != {count}")
        result = {"nodes": count, "table": f"{target}.{table}", "seconds": round(time.monotonic() - start, 3), "engine": "stream", "order_engine": order_engine}
        if checkpoint:
            interval_checkpoint(ch, target, "stream", result["seconds"], order_engine=order_engine)
        return result
    finally:
        reader.close()


def interval_checkpoint(
    ch: Ch,
    target: str,
    engine: str,
    seconds: float,
    *,
    order_engine: str,
) -> dict:
    """Publish only a completed hierarchy checkpoint; never overwrite one."""
    identifier(target)
    plan = json.loads(ch.scalar(f"SELECT doc FROM {target}.paths_manifest"))
    count = plan["union_nodes"]
    counts = {table: int(ch.scalar(f"SELECT count() FROM {target}.{table}")) for table in ("ids", "intervals", "parents")}
    if counts["ids"] != count or counts["intervals"] != count or counts["parents"] > count:
        raise ValueError(f"incomplete hierarchy cannot be checkpointed: {counts}")
    root = ch.json(f"""SELECT pre, post FROM {target}.intervals WHERE id IN (SELECT id FROM {target}.ids
        WHERE (depth, path) = ({depth_of(plan['prefix'])}, {lit(plan['prefix'])}))""")
    if root != [[0, count - 1]]:
        raise ValueError(f"incomplete hierarchy root span: {root}")
    result = {**plan, "parents": counts["parents"], "interval_engine": engine, "order_engine": order_engine, "seconds": seconds}
    ch.exec(f"CREATE TABLE {target}.interval_manifest (doc String) ENGINE = TinyLog")
    ch.exec(f"INSERT INTO {target}.interval_manifest VALUES ({lit(json.dumps(result))})")
    return result


def dictionary_checkpoint(ch: Ch, target: str) -> dict:
    """Explicitly checkpoint a completed dictionary, without overwriting data."""
    identifier(target)
    plan = json.loads(ch.scalar(f"SELECT doc FROM {target}.interval_manifest"))
    counts = {table: int(ch.scalar(f"SELECT count() FROM {target}.{table}"))
              for table in ("dictionary", "names", "names_sorted")}
    if counts["dictionary"] != plan["union_nodes"] or counts["names_sorted"] != plan["union_nodes"] or not 0 < counts["names"] <= plan["union_nodes"]:
        raise ValueError(f"incomplete dictionary cannot be checkpointed: {counts}")
    result = {**plan, "names": counts["names"]}
    ch.exec(f"CREATE TABLE {target}.dictionary_manifest (doc String) ENGINE = TinyLog")
    ch.exec(f"INSERT INTO {target}.dictionary_manifest VALUES ({lit(json.dumps(result))})")
    return result


def completed_snapshot(
    ch: Ch,
    target: str,
    index: int,
    create_sql: str,
    root_sql: str | None,
) -> bool:
    """Adopt only a completed matching legacy CTAS, never a partial table.

    Exact query text and source database bind the dates/prefix/epoch. Require
    successful query-log records (within seven days) and matching written-row
    counts, including the synthetic global-root INSERT when applicable.
    """
    identifier(target)
    if index < 0:
        raise ValueError("snapshot index must be nonnegative")
    table = f"{target}.snapshot_{index}"
    if ch.scalar(f"EXISTS TABLE {table}") != "1":
        return False
    written = 0
    for label, query in ((f"snapshot_{index}", create_sql), (f"snapshot_root_{index}", root_sql)):
        if query is None:
            continue
        count = ch.scalar("SELECT written_rows FROM system.query_log WHERE event_date >= today() - 7 "
                          f"AND type = 'QueryFinish' AND current_database = {lit(ch.db)} "
                          f"AND log_comment = {lit('narrow:' + target + ':' + label)} AND query = {lit(query.strip().rstrip(';'))} "
                          "ORDER BY event_time_microseconds DESC LIMIT 1")
        if count is None or count == "":
            raise ValueError(f"snapshot recovery needs a completed matching query: {label}")
        written += int(count)
    if int(ch.scalar(f"SELECT count() FROM {table}")) != written:
        raise ValueError(f"snapshot recovery row count differs from completed queries: snapshot_{index}")
    return True


def dictionary_join_settings() -> dict[str, object]:
    """Bound hash joins and keep numeric intervals on the final build side."""
    return {
        "max_threads": 2, "max_insert_threads": 1, "max_block_size": 8192,
        "join_algorithm": "grace_hash", "grace_hash_join_initial_buckets": 16,
        "max_bytes_before_external_join": 512 << 20, "max_bytes_ratio_before_external_join": 0,
        "max_bytes_in_join": 1 << 30, "join_overflow_mode": "throw",
        "query_plan_join_swap_table": "false",
    }


def name_statements(target: str) -> dict[str, str]:
    identifier(target)
    return {
        "names_sorted": f"CREATE TABLE {target}.names_sorted ENGINE = MergeTree ORDER BY l AS SELECT l FROM {target}.ids",
        "names": f"""CREATE TABLE {target}.names (nid UInt32, l String, INDEX tl l TYPE text(tokenizer = ngrams(3)))
            ENGINE = MergeTree ORDER BY l AS SELECT toUInt32(rowNumberInAllBlocks()) AS nid, l
            FROM (SELECT l FROM {target}.names_sorted GROUP BY l ORDER BY l)""",
    }


def completed_name_tables(
    ch: Ch,
    target: str,
    union_nodes: int,
) -> dict[str, int]:
    """Adopt completed vocabulary only with exact successful CTAS evidence."""
    counts = {}
    for label, statement in name_statements(target).items():
        table = f"{target}.{label}"
        if ch.scalar(f"EXISTS TABLE {table}") != "1":
            raise ValueError(f"name recovery needs an existing completed table: {label}")
        written = ch.scalar("SELECT written_rows FROM system.query_log WHERE event_date >= today() - 7 "
                            f"AND type = 'QueryFinish' AND current_database = {lit(ch.db)} "
                            f"AND log_comment = {lit('narrow:' + target + ':' + label)} AND query = {lit(statement)} "
                            "ORDER BY event_time_microseconds DESC LIMIT 1")
        if written is None or written == "":
            raise ValueError(f"name recovery needs a completed matching query: {label}")
        counts[label] = int(ch.scalar(f"SELECT count() FROM {table}"))
        if counts[label] != int(written):
            raise ValueError(f"name recovery row count differs from completed query: {label}")
    if counts["names_sorted"] != union_nodes or not 0 < counts["names"] <= union_nodes:
        raise ValueError(f"name recovery domain differs from completed intervals: {list(counts.values())} / {union_nodes}")
    return counts


def missing_parent_keys(ch: Ch, target: str) -> list:
    """Validate distinct parent requirements against the sorted path domain.

    Both inputs are already ordered by the join keys. Do not reconstruct a
    child-sized parent join or materialize a parent membership hash table.
    """
    identifier(target)
    missing = ch.json(f"""SELECT k.depth, k.parent FROM {target}.parent_keys k
        LEFT JOIN {target}.ids p ON k.depth = p.depth AND k.parent = p.path
        WHERE isNull(p.id) LIMIT 10""", settings={
            "join_algorithm": "full_sorting_merge", "join_use_nulls": 1,
            "max_threads": 2, "max_block_size": 8192, "max_memory_usage": 8 << 30,
            "max_bytes_before_external_sort": 256 << 20,
            "max_bytes_ratio_before_external_sort": 0,
            "log_comment": f"narrow:{target}:missing_parent_keys",
        })
    if not missing:
        required = int(ch.scalar(f"SELECT count() FROM {target}.parent_keys"))
        mapped = int(ch.scalar(f"SELECT count() FROM {target}.parents"))
        if mapped != required:
            raise ValueError(f"parent mapping row count differs from required keys: {mapped} != {required}")
    return missing


def build(
    store: Store,
    target: str,
    prefix: str,
    dates: tuple[str, ...],
    *,
    max_nodes: int = 50_000_000,
    interval_engine: str = "numpy",
    min_free_bytes: int = 0,
    union_engine: str = "group",
    resume_from: str | None = None,
    snapshot_ranges: int = 0,
    log: Callable[[str], None] = print,
) -> dict:
    """Create TARGET (dictionary/build data) and TARGET_0… (frozen scan views).

    SQL spills are bounded; only parent/depth/interval arrays cross into Python.
    Partial builds are kept for inspection and cannot be silently overwritten.
    """
    identifier(target)
    identifier(store.db)
    if prefix.strip("/") != prefix:
        raise ValueError("a canonical subtree prefix is required (empty means global)")
    if not dates or len(set(dates)) != len(dates):
        raise ValueError("supply distinct scan dates")
    if not 0 < max_nodes < 2**32:
        raise ValueError("max_nodes must fit UInt32")
    if interval_engine not in ("numpy", "stream"):
        raise ValueError(f"unknown interval engine: {interval_engine}")
    if union_engine not in ("group", "merge"):
        raise ValueError(f"unknown union engine: {union_engine}")
    if resume_from not in (None, "snapshots-partial", "snapshots", "paths", "intervals", "names", "dictionary"):
        raise ValueError(f"unknown resume stage: {resume_from}")
    if min_free_bytes < 0:
        raise ValueError("min_free_bytes cannot be negative")
    if snapshot_ranges < 0:
        raise ValueError("snapshot_ranges cannot be negative")
    scans = [store.scan(d) for d in dates]
    if any(s is None or s.version != 2 for s in scans):
        raise ValueError("all scans must exist and contain full v2 paths")
    dbs = [target, *[f"{target}_{i}" for i in range(len(dates))]]
    ch = store.session()
    for db in dbs:
        exists = ch.scalar(f"EXISTS DATABASE {db}") == "1"
        if exists and resume_from is None:
            raise ValueError(f"experimental database already exists: {db}")
        if not exists and resume_from is not None:
            raise ValueError(f"snapshot resume needs existing database: {db}")
    checkpoint = {"source_db": store.db, "prefix": prefix, "dates": list(dates)}
    if resume_from is not None:
        if resume_from != "snapshots-partial":
            actual = json.loads(ch.scalar(f"SELECT doc FROM {target}.snapshot_manifest"))
            if {k: actual[k] for k in checkpoint} != checkpoint:
                raise ValueError("snapshot checkpoint does not match the requested source, prefix and dates")
        later = {"snapshots-partial": ("snapshot_manifest", "paths", "ids", "manifest"),
                 "snapshots": ("paths", "ids", "manifest"), "paths": ("ids", "manifest"),
                 "intervals": ("names_sorted", "names", "dictionary", "manifest"),
                 "names": ("dictionary", "dictionary_manifest", "manifest"), "dictionary": ("manifest",)}
        for table in later[resume_from]:
            if ch.scalar(f"EXISTS TABLE {target}.{table}") == "1":
                raise ValueError(f"{resume_from} resume refuses later table: {target}.{table}")
        if resume_from in ("names", "dictionary"):
            for db in dbs[1:]:
                for table in ("metadata", "nodes", "nodes_by_name", "names"):
                    if ch.scalar(f"EXISTS TABLE {db}.{table}") == "1":
                        raise ValueError(f"{resume_from} resume refuses later table: {db}.{table}")
    settings = {
        "max_memory_usage": 8 << 30,
        "max_bytes_before_external_group_by": 1 << 30,
        "max_bytes_ratio_before_external_group_by": 0,
        "max_bytes_before_external_sort": 1 << 30,
        "max_bytes_ratio_before_external_sort": 0,
        "join_algorithm": "grace_hash",
        "grace_hash_join_initial_buckets": 16,
    }
    timings = {}

    def sql(label: str, statement: str, **extra: object) -> None:
        disk_reserve(ch, label, min_free_bytes)
        start = time.monotonic()
        ch.exec(statement, settings={**settings, **extra, "log_comment": f"narrow:{target}:{label}"})
        timings[label] = round(time.monotonic() - start, 3)
        log(f"{label}: {timings[label]}s")

    if resume_from is None:
        for db in dbs:
            sql(f"create_{db}", f"CREATE DATABASE {db}")
    dp = depth_of(prefix)
    bound = f"depth >= {dp} AND (path = {lit(prefix)} OR {under(prefix)})"
    snapshot_queries = []
    root_agg = AGG.replace("any(toString(kind)) AS kind", "'dir' AS kind").replace("max(n_children) AS nc", "toInt64(uniqExact(path)) AS nc")
    for i, scan in enumerate(scans):
        create_sql = f"""CREATE TABLE {target}.snapshot_{i} ENGINE = MergeTree ORDER BY (depth, path) AS
                SELECT depth, path, any(parent) AS parent, {AGG} FROM nodes
                WHERE {bound} AND {scan.live(bound)} GROUP BY depth, path"""
        root_sql = f"""INSERT INTO {target}.snapshot_{i}
                    SELECT toUInt8(0) AS depth, '' AS path, '' AS parent, {root_agg}
                    FROM nodes WHERE depth = 1 AND {scan.live('depth = 1')} HAVING count() > 0""" if prefix == "" else None
        snapshot_queries.append((create_sql, root_sql))
    # Validate every retained table before creating any missing one. Changing
    # requested dates/prefix/source must not silently mix snapshots.
    reused = [completed_snapshot(ch, target, i, *queries) for i, queries in enumerate(snapshot_queries)] if resume_from == "snapshots-partial" else [False] * len(scans)
    ranges = sample_bounds(ch, snapshot_ranges) if snapshot_ranges and resume_from in (None, "snapshots-partial") else ["1"]
    snapshot_counts = []
    for i, scan in enumerate(scans):
        if reused[i]:
            log(f"snapshot_{i}: reused completed matching queries")
        elif resume_from in (None, "snapshots-partial"):
            create_sql, root_sql = snapshot_queries[i]
            if snapshot_ranges:
                sql(f"snapshot_{i}_empty", f"""CREATE TABLE {target}.snapshot_{i} ENGINE = MergeTree ORDER BY (depth, path) AS
                    SELECT depth, path, any(parent) AS parent, {AGG} FROM nodes WHERE 0 GROUP BY depth, path""")
                for j, cond in enumerate(ranges):
                    where = f"{bound} AND ({cond})"
                    sql(f"snapshot_{i}_range_{j}", f"""INSERT INTO {target}.snapshot_{i}
                        SELECT depth, path, any(parent) AS parent, {AGG} FROM nodes
                        WHERE {where} AND {scan.live(where)} GROUP BY depth, path""", optimize_aggregation_in_order=1)
            else:
                sql(f"snapshot_{i}", create_sql, optimize_aggregation_in_order=1)
            if root_sql is not None:
                sql(f"snapshot_root_{i}", root_sql)
        if ch.scalar(f"SELECT count() FROM {target}.snapshot_{i} WHERE path = {lit(prefix)}") != "1":
            raise ValueError(f"prefix absent at {dates[i]}: {prefix}")
        snapshot_count = int(ch.scalar(f"SELECT count() FROM {target}.snapshot_{i}"))
        snapshot_counts.append(snapshot_count)
        if snapshot_count > max_nodes:
            raise ValueError(f"snapshot {dates[i]} has {snapshot_count} nodes, exceeds {max_nodes}; partial build retained")
    if resume_from is not None and resume_from != "snapshots-partial":
        if actual["snapshot_counts"] != snapshot_counts:
            raise ValueError("snapshot row counts differ from their completed checkpoint")
    else:
        sql("snapshot_manifest", f"CREATE TABLE {target}.snapshot_manifest (doc String) ENGINE = TinyLog")
        ch.exec(f"INSERT INTO {target}.snapshot_manifest VALUES ({lit(json.dumps({**checkpoint, 'snapshot_counts': snapshot_counts}))})")
    union = " UNION ALL ".join(f"SELECT depth, path, parent FROM {target}.snapshot_{i}" for i in range(len(dates)))
    path_query = merge_snapshots(target, len(dates)) if union_engine == "merge" else f"SELECT depth, path, any(parent) AS parent FROM ({union}) GROUP BY depth, path"
    completed_dictionary = resume_from == "dictionary"
    completed_hierarchy = resume_from in ("intervals", "names", "dictionary")
    if resume_from not in ("paths", "intervals", "names", "dictionary"):
        sql("paths", f"CREATE TABLE {target}.paths ENGINE = MergeTree ORDER BY (depth, path) AS {path_query}",
            max_threads=2, max_bytes_before_external_group_by=256 << 20, max_bytes_before_external_sort=256 << 20,
            **({"join_algorithm": "full_sorting_merge", "join_use_nulls": 1} if union_engine == "merge" else {}))
    count = int(ch.scalar(f"SELECT count() FROM {target}.paths"))
    if count > max_nodes:
        raise ValueError(f"union has {count} nodes, exceeds {max_nodes}; partial build retained")
    path_checkpoint = {**checkpoint, "snapshot_counts": snapshot_counts, "union_nodes": count, "union_engine": union_engine}
    if resume_from in ("paths", "intervals", "names", "dictionary"):
        if json.loads(ch.scalar(f"SELECT doc FROM {target}.paths_manifest")) != path_checkpoint:
            raise ValueError("path checkpoint does not match the requested snapshot union")
    else:
        sql("paths_manifest", f"CREATE TABLE {target}.paths_manifest (doc String) ENGINE = TinyLog")
        ch.exec(f"INSERT INTO {target}.paths_manifest VALUES ({lit(json.dumps(path_checkpoint))})")
    if not completed_hierarchy:
        sql("ids", f"""CREATE TABLE {target}.ids ENGINE = MergeTree ORDER BY (depth, path) AS
            SELECT toUInt32(rowNumberInAllBlocks()) AS id, depth, path, parent, {name_expr()} AS l
            FROM (SELECT depth, path, parent FROM {target}.paths ORDER BY depth, path)""",
            max_threads=1, max_bytes_before_external_sort=256 << 20)
    if int(ch.scalar(f"SELECT count() FROM {target}.ids")) != count:
        raise ValueError("path numbering lost rows")
    # Sorting first makes deduplication streamable. An external hash GROUP BY
    # can still retain an unbounded final merge at hundreds of millions of keys.
    if not completed_hierarchy:
        sql("parent_refs", f"""CREATE TABLE {target}.parent_refs ENGINE = MergeTree ORDER BY (depth, parent) AS
            SELECT toUInt8(depth - 1) AS depth, parent FROM {target}.ids WHERE path != {lit(prefix)}""",
            max_threads=2, max_bytes_before_external_sort=256 << 20)
        sql("parent_keys", f"""CREATE TABLE {target}.parent_keys ENGINE = MergeTree ORDER BY (depth, parent) AS
            SELECT depth, parent FROM {target}.parent_refs GROUP BY depth, parent""",
            max_threads=1, optimize_aggregation_in_order=1)
        sql("parents", f"""CREATE TABLE {target}.parents ENGINE = MergeTree ORDER BY path AS
            SELECT p.path AS path, p.id AS id FROM {target}.ids p INNER JOIN {target}.parent_keys k
            ON p.depth = k.depth AND p.path = k.parent""", max_threads=2, join_algorithm="full_sorting_merge",
            max_bytes_before_external_sort=256 << 20)
    start = time.monotonic()
    missing = missing_parent_keys(ch, target)
    timings["missing_parent_keys"] = round(time.monotonic() - start, 3)
    log(f"missing_parent_keys: {timings['missing_parent_keys']}s")
    if missing:
        raise ValueError(f"union lacks parents: {missing}")
    if completed_hierarchy:
        checkpointed = json.loads(ch.scalar(f"SELECT doc FROM {target}.interval_manifest"))
        expected = {**path_checkpoint, "parents": int(ch.scalar(f"SELECT count() FROM {target}.parents")), "interval_engine": interval_engine}
        if {k: checkpointed[k] for k in expected} != expected or int(ch.scalar(f"SELECT count() FROM {target}.intervals")) != count:
            raise ValueError("interval checkpoint differs from the requested completed hierarchy")
    elif interval_engine == "stream":
        disk_reserve(ch, "intervals_stream", min_free_bytes)
        result = stream_intervals(ch, target, prefix, table="intervals")
        timings["intervals_stream"] = result["seconds"]
        log(f"intervals_stream: {count} nodes, {result['seconds']}s")
    else:
        start = time.monotonic()
        raw = bytearray()
        for chunk in ch.stream(f"""SELECT if(c.path = {lit(prefix)}, toInt64(-1), toInt64(p.id)), toUInt8(c.depth - {dp} + 1)
            FROM {target}.ids c LEFT JOIN {target}.parents p ON c.parent = p.path ORDER BY c.id""", "RowBinary", settings=settings):
            raw.extend(chunk)
        rows = np.frombuffer(raw, dtype=[("parent", "<i8"), ("depth", "u1")])
        if len(rows) != count:
            raise ValueError(f"hierarchy row count {len(rows)} != {count}")
        pre, post = intervals(rows["parent"], rows["depth"])
        del rows, raw
        timings["intervals"] = round(time.monotonic() - start, 3)
        log(f"intervals: {count} nodes, {timings['intervals']}s")
        sql("interval_table", f"CREATE TABLE {target}.intervals (id UInt32, pre UInt32, post UInt32) ENGINE = MergeTree ORDER BY id")
        start = time.monotonic()
        ch.insert(f"INSERT INTO {target}.intervals FORMAT RowBinary", interval_rows(pre, post), settings=settings)
        timings["interval_upload"] = round(time.monotonic() - start, 3)
        del pre, post
    if not completed_hierarchy:
        interval_checkpoint(ch, target, interval_engine,
                            timings["intervals_stream"] if interval_engine == "stream" else timings["intervals"] + timings["interval_upload"],
                            order_engine="escaped" if interval_engine == "stream" else "numpy")
    if completed_dictionary:
        saved = json.loads(ch.scalar(f"SELECT doc FROM {target}.dictionary_manifest"))
        expected = {**checkpointed, "names": int(ch.scalar(f"SELECT count() FROM {target}.names"))}
        if saved != expected or int(ch.scalar(f"SELECT count() FROM {target}.dictionary")) != count:
            raise ValueError("dictionary checkpoint differs from the requested completed hierarchy")
    else:
        if resume_from == "names":
            completed_name_tables(ch, target, count)
            log("names: reused completed vocabulary")
        else:
            statements = name_statements(target)
            sql("names_sorted", statements["names_sorted"], max_threads=2, max_bytes_before_external_sort=256 << 20)
            sql("names", statements["names"], max_threads=1, optimize_aggregation_in_order=1,
                max_bytes_before_external_sort=256 << 20)
        sql("dictionary", f"""CREATE TABLE {target}.dictionary ENGINE = MergeTree ORDER BY (depth, path) AS
            SELECT p.id AS id, p.depth AS depth, p.path AS path, n.nid AS nid, i.pre AS pre, i.post AS post FROM {target}.ids p
            INNER JOIN {target}.names n ON p.l = n.l INNER JOIN {target}.intervals i ON p.id = i.id""", **dictionary_join_settings())
        dictionary_checkpoint(ch, target)
    merge_settings = dict(join_algorithm="full_sorting_merge", max_threads=2, max_block_size=8192,
                          max_bytes_before_external_sort=256 << 20)
    for i in range(len(dates)):
        db = dbs[i + 1]
        sql(f"metadata_{i}", f"""CREATE TABLE {db}.metadata ENGINE = MergeTree ORDER BY pre AS
            SELECT d.pre AS pre, s.* FROM {target}.snapshot_{i} s INNER JOIN {target}.dictionary d
            ON s.depth = d.depth AND s.path = d.path""", **merge_settings)
        sql(f"nodes_{i}", f"""CREATE TABLE {db}.nodes ENGINE = MergeTree ORDER BY pre AS
            SELECT d.pre AS pre, d.post AS post, d.depth AS depth, d.nid AS nid, s.b AS b, s.o AS o, d.path AS path FROM {target}.snapshot_{i} s
            INNER JOIN {target}.dictionary d ON s.depth = d.depth AND s.path = d.path""", **merge_settings)
        sql(f"by_name_{i}", f"CREATE TABLE {db}.nodes_by_name ENGINE = MergeTree ORDER BY (nid, pre) AS SELECT * FROM {db}.nodes",
            max_threads=2, max_block_size=8192, max_bytes_before_external_sort=256 << 20)
        sql(f"names_{i}", f"CREATE VIEW {db}.names AS SELECT * FROM {target}.names")
        if ch.scalar(f"SELECT count() FROM {db}.nodes") != ch.scalar(f"SELECT count() FROM {target}.snapshot_{i}"):
            raise ValueError(f"dictionary join lost or multiplied nodes at {dates[i]}")
    manifest = {"prefix": prefix, "dates": dates, "dbs": dbs[1:], "source_db": store.db, "union_nodes": count, "timings": timings,
                "incremental": False, "renders_tree": False, "interval_engine": interval_engine, "union_engine": union_engine,
                "snapshot_ranges": snapshot_ranges, "reused_snapshots": [i for i, reuse in enumerate(reused) if reuse]}
    sql("manifest", f"CREATE TABLE {target}.manifest (doc String) ENGINE = TinyLog")
    ch.exec(f"INSERT INTO {target}.manifest VALUES ({lit(json.dumps(manifest))})")
    ch.close()
    return manifest


def history_parent_source(
    target: str,
    db: str,
    prefix: str,
    tick: int,
    bound: str,
    numeric_parents: bool,
) -> str:
    """The rich coalescer's identical payload with either parent-resolution join."""
    identifier(target)
    identifier(db)
    source = f"SELECT * FROM {db}.metadata" + (f" WHERE {bound}" if bound else "")
    parents = f"SELECT pre, parent_pre FROM {target}.numeric_parents" if numeric_parents else f"SELECT parent, parent_pre FROM {target}.parent_ids"
    if bound and numeric_parents:
        parents += f" WHERE {bound}"
    elif bound:
        parents += f" WHERE parent IN (SELECT parent FROM ({source}))"
    join = "m.pre = p.pre" if numeric_parents else "m.parent = p.parent"
    return f"""SELECT toUInt16({tick}) AS tick, m.pre AS pre,
        if(m.path = {lit(prefix)}, toInt64(-1), toInt64(p.parent_pre)) AS parent_pre,
        m.depth AS depth, m.path AS path, m.parent AS parent, {', '.join('m.' + c + ' AS ' + c for c in AGG_COLS.split(', '))}
        FROM ({source}) m LEFT JOIN ({parents}) p ON {join}"""


def parent_benchmark(
    url: str,
    target: str,
    starts: tuple[int, ...],
    *,
    batch_rows: int = 1_000_000,
    numeric_join: str = "grace_hash",
) -> Iterator[dict]:
    """Paired warm rich-source reads, not a complete coalescer/response benchmark.

    Verify every ordered binary parent link, not a hash. At most two 12-MB
    payloads are collected per batch; all other fields share the same source.
    """
    identifier(target)
    if not 0 < batch_rows <= 1_000_000:
        raise ValueError("parent benchmark batch_rows must be positive" if batch_rows <= 0 else "parent benchmark batch_rows cannot exceed one million")
    if not starts or min(starts) < 0 or len(set(starts)) != len(starts):
        raise ValueError("parent benchmark needs nonnegative unique starts")
    if numeric_join not in ("grace_hash", "full_sorting_merge"):
        raise ValueError("parent benchmark numeric_join must be grace_hash or full_sorting_merge")
    ch = Ch(url, db=target, timeout=7200, max_threads=2, max_block_size=8192, max_memory_usage=8 << 30,
            max_bytes_before_external_sort=256 << 20, max_bytes_ratio_before_external_sort=0,
            max_bytes_before_external_group_by=256 << 20, max_bytes_ratio_before_external_group_by=0,
            join_algorithm="grace_hash", grace_hash_join_initial_buckets=16)
    try:
        manifest = json.loads(ch.scalar("SELECT doc FROM manifest"))
        checkpoint = json.loads(ch.scalar("SELECT doc FROM numeric_parent_manifest"))
        if checkpoint["target"] != target or checkpoint["nodes"] != manifest["union_nodes"]:
            raise ValueError("numeric parent checkpoint differs from the completed snapshot build")
        if max(starts) >= manifest["union_nodes"]:
            raise ValueError("parent benchmark start exceeds the frozen key domain")
        for tick, db in enumerate(manifest["dbs"]):
            for index, lo in enumerate(starts):
                hi = min(lo + batch_rows, manifest["union_nodes"])
                bound = f"pre >= {lo} AND pre < {hi}"
                sources = {numeric: history_parent_source(target, db, manifest["prefix"], tick, bound, numeric) for numeric in (False, True)}
                seconds = {}
                order = (False, True) if (tick + index) % 2 == 0 else (True, False)
                for numeric in order:
                    start = time.monotonic()
                    ch.exec(f"SELECT * FROM ({sources[numeric]}) FORMAT Null", fmt=None,
                            settings={"join_algorithm": numeric_join if numeric else "grace_hash",
                                      "log_comment": f"narrow:{target}:parent_bench:{tick}:{lo}:{int(numeric)}:{numeric_join}"})
                    seconds[numeric] = round(time.monotonic() - start, 6)
                links = [b"".join(ch.stream(f"SELECT pre, parent_pre FROM ({sources[numeric]}) ORDER BY pre", "RowBinary",
                                          settings={"join_algorithm": numeric_join if numeric else "grace_hash"})) for numeric in (False, True)]
                count = int(ch.scalar(f"SELECT count() FROM {db}.metadata WHERE {bound}"))
                if len(links[0]) != 12 * count or links[0] != links[1]:
                    raise ValueError(f"parent resolution mismatch: {db} / [{lo}, {hi})")
                previous = lo - 1
                for pre, parent in struct.iter_unpack("<Iq", links[0]):
                    if not previous < pre < hi or not -1 <= parent < pre:
                        raise ValueError(f"invalid parent mapping: {db} / {pre} / {parent}")
                    previous = pre
                yield {"target": target, "date": manifest["dates"][tick], "lo": lo, "hi": hi, "rows": count,
                       "threads": 2, "cold": False, "exact": True, "renders_tree": False,
                       "string_join": "grace_hash", "numeric_join": numeric_join,
                       "string_seconds": seconds[False], "numeric_seconds": seconds[True],
                       "speedup": seconds[False] / seconds[True] if seconds[True] else None,
                       "order": ["numeric" if numeric else "string" for numeric in order]}
    finally:
        ch.close()


class HistoryPublicationRecovery:
    """Verify the original build journal without replaying any data writes."""

    def __init__(self, ch: Ch, target: str) -> None:
        self.ch = ch
        self.target = target
        self.written: dict[str, int] = {}
        self.records: dict[str, list] = {}
        for comment, query, rows, milliseconds in ch.json(
            "SELECT log_comment, query, written_rows, query_duration_ms FROM system.query_log "
            "WHERE event_date >= today() - 7 AND type = 'QueryFinish' "
            f"AND current_database = {lit(ch.db)} AND startsWith(log_comment, {lit('narrow:' + target + ':')})",
        ):
            label = comment.removeprefix(f"narrow:{target}:")
            self.records.setdefault(label, []).append((query, rows, milliseconds))

    def verify(self, label: str, statement: str) -> float:
        records = self.records.get(label, [])
        query = statement.strip().rstrip(";")
        if len(records) != 1 or records[0][0] != query:
            raise ValueError(f"history publication recovery needs one completed matching query: {label}")
        _, rows, milliseconds = records[0]
        if label.startswith("history_nodes_"):
            table = "nodes_history"
        elif label.startswith("history_metadata_"):
            table = "metadata_history"
        else:
            table = {"history_by_name": "nodes_history_by_name", "history_by_parent": "metadata_history_by_parent"}.get(label, label)
        self.written[table] = self.written.get(table, 0) + int(rows)
        return round(milliseconds / 1000, 3)

    def check_counts(self) -> None:
        for table, rows in self.written.items():
            # The streamed hierarchy SELECT is a completion witness, not an INSERT.
            if table == "hierarchy_stream_read":
                continue
            actual = int(self.ch.scalar(f"SELECT count() FROM {self.target}.{table}"))
            if actual != rows:
                raise ValueError(f"history publication recovery row count differs: {table}: {actual} != {rows}")


def history(
    store: Store,
    target: str,
    *,
    batch_rows: int = 1_000_000,
    build_threads: int = 2,
    min_free_bytes: int = 0,
    numeric_parents: bool = False,
    numeric_ancestors: bool = False,
    coalescer: str = "window",
    resume_publication: bool = False,
    log: Callable[[str], None] = print,
) -> dict:
    """Coalesce frozen snapshots into scalar/rich SCD2 read models.

    The union hierarchy is still frozen. Lifetimes are valid at the selected
    scan dates only: omitted source scans are not reconstructed by this build.
    Snapshot databases remain untouched, so both layouts can be benchmarked.
    """
    identifier(target)
    if batch_rows <= 0:
        raise ValueError("history batch_rows must be positive")
    if build_threads <= 0:
        raise ValueError("history build_threads must be positive")
    if min_free_bytes < 0:
        raise ValueError("min_free_bytes cannot be negative")
    if coalescer not in ("window", "pair"):
        raise ValueError("history coalescer must be window or pair")
    ch = store.session()
    manifest = json.loads(ch.scalar(f"SELECT doc FROM {target}.manifest"))
    dates = manifest["dates"]
    instants = [scan_dt(d) for d in dates]
    if instants != sorted(instants):
        raise ValueError("history needs scan dates in increasing order")
    if coalescer == "pair" and (len(instants) != 2 or instants[0] >= instants[1]):
        raise ValueError("pair coalescing requires exactly two increasing snapshot instants")
    dbs = [f"{target}_h{i}" for i in range(len(dates))]
    for db in dbs:
        if ch.scalar(f"EXISTS DATABASE {db}") == "1":
            raise ValueError(f"experimental database already exists: {db}")
    if resume_publication and not numeric_ancestors:
        raise ValueError("publication recovery currently requires the streamed hierarchy build")
    if resume_publication and ch.scalar(f"EXISTS TABLE {target}.history_manifest") == "1":
        raise ValueError("history publication recovery refuses an existing history manifest")
    recovery = HistoryPublicationRecovery(ch, target) if resume_publication else None
    timings = {}
    if numeric_parents:
        if ch.scalar(f"EXISTS TABLE {target}.numeric_parent_manifest") == "1":
            parent_manifest = json.loads(ch.scalar(f"SELECT doc FROM {target}.numeric_parent_manifest"))
            if parent_manifest["target"] != target or parent_manifest["nodes"] != manifest["union_nodes"] or int(ch.scalar(f"SELECT count() FROM {target}.numeric_parents")) != manifest["union_nodes"]:
                raise ValueError("numeric parent checkpoint differs from the completed snapshot build")
            timings["numeric_parents"] = 0.0
        else:
            if recovery is not None:
                raise ValueError("history publication recovery needs the numeric parent checkpoint")
            parent_manifest = numeric_parent_index(ch.url, target, min_free_bytes=min_free_bytes)
            timings["numeric_parents"] = parent_manifest["seconds"]
        log(f"numeric_parents: {parent_manifest['nodes']} nodes, {parent_manifest['seconds']}s")
    settings = {"max_memory_usage": 8 << 30, "max_threads": build_threads, "max_block_size": 8192, "max_bytes_before_external_sort": 256 << 20,
                "max_bytes_ratio_before_external_sort": 0,
                "max_bytes_before_external_group_by": 256 << 20, "max_bytes_ratio_before_external_group_by": 0,
                "join_algorithm": "grace_hash", "grace_hash_join_initial_buckets": 16}
    def sql(label: str, statement: str) -> None:
        if recovery is not None:
            timings[label] = recovery.verify(label, statement)
            return
        disk_reserve(ch, label, min_free_bytes)
        start = time.monotonic()
        stage_settings = {**settings, "log_comment": f"narrow:{target}:{label}"}
        if (label == "parent_ids" or numeric_parents and label.startswith("history_metadata_")
                or coalescer == "pair" and label.startswith(("history_nodes_", "history_metadata_"))):
            stage_settings["join_algorithm"] = "full_sorting_merge"
        ch.exec(statement, settings=stage_settings)
        timings[label] = round(time.monotonic() - start, 3)
        log(f"{label}: {timings[label]}s")

    sql("parent_ids", f"""CREATE TABLE IF NOT EXISTS {target}.parent_ids ENGINE = MergeTree ORDER BY parent AS
        SELECT p.path AS parent, i.pre AS parent_pre FROM {target}.parents p INNER JOIN {target}.intervals i ON p.id = i.id""")
    dp = depth_of(manifest["prefix"])
    sql("parent_paths", f"""CREATE TABLE IF NOT EXISTS {target}.parent_paths ENGINE = MergeTree ORDER BY pre AS
        SELECT parent_pre AS pre, parent AS path FROM {target}.parent_ids""")
    if numeric_ancestors:
        if recovery is not None:
            recovery.records["hierarchy_stream_read"] = recovery.records.get("hierarchy", [])
            timings["hierarchy_stream_read"] = recovery.verify("hierarchy_stream_read", f"SELECT pre, toUInt8(if(path = '', 0, length(splitByChar('/', path)))) FROM {target}.parent_paths ORDER BY pre FORMAT RowBinary")
        else:
            result = stream_hierarchy(ch, target, table="hierarchy", min_free_bytes=min_free_bytes)
            timings["hierarchy_stream"] = result["seconds"]
            log(f"hierarchy_stream: {result['directories']} directories, {result['seconds']}s")
    else:
        sql("hierarchy_empty", f"CREATE TABLE {target}.hierarchy (pre UInt32, ancestors Array(UInt32)) ENGINE = MergeTree ORDER BY pre")
        # Keep both the aggregation domain and string-parent join build bounded.
        # A global GROUP BY over tens of millions of directory arrays can retain
        # an oversized final aggregate merge even when external spilling is on.
        for lo in range(0, manifest["union_nodes"], batch_rows):
            hi = min(lo + batch_rows, manifest["union_nodes"])
            source = f"""SELECT pre, arrayJoin(arrayMap(d -> arrayStringConcat(arraySlice(splitByChar('/', path), 1, d), '/'),
                range({dp}, if(path = '', 1, length(splitByChar('/', path)) + 1)))) AS ancestor
                FROM {target}.parent_paths WHERE pre >= {lo} AND pre < {hi}"""
            sql(f"hierarchy_{lo}", f"""INSERT INTO {target}.hierarchy
                SELECT pre, arraySort(groupArray(parent_pre)) AS ancestors FROM ({source}) h INNER JOIN (
                    SELECT parent, parent_pre FROM {target}.parent_ids WHERE parent IN (SELECT ancestor FROM ({source}))) a
                ON h.ancestor = a.parent GROUP BY pre""")
    for table in ("parent_ids", "hierarchy"):
        if ch.scalar(f"SELECT count() FROM {target}.{table}") != ch.scalar(f"SELECT count() FROM {target}.parents"):
            raise ValueError(f"incomplete intermediate table: {target}.{table}")
    for table, cols, values in (
        ("nodes", "pre, post, depth, nid, b, o, path", "b, o"),
        ("metadata", f"pre, parent_pre, depth, path, parent, {AGG_COLS}", AGG_COLS),
    ):
        def snapshot_parts(bound: str = "") -> list[str]:
            if table == "metadata":
                return [history_parent_source(target, db, manifest["prefix"], i, bound, numeric_parents)
                        for i, db in enumerate(manifest["dbs"])]
            return [f"SELECT toUInt16({i}) AS tick, {cols} FROM {db}.{table}" + (f" WHERE {bound}" if bound else "")
                    for i, db in enumerate(manifest["dbs"])]

        parts = snapshot_parts()
        # A missing path breaks an episode even if it reappears with identical
        # values. Compare tuples directly, rather than relying on hash equality.
        names = cols.split(", ")
        empty = [f"SELECT * FROM ({part}) WHERE 0" for part in parts]
        make_query = pair_query if coalescer == "pair" else window_query
        select = make_query(empty, names, values.split(", "), instants)
        sql(f"history_{table}_empty", f"CREATE TABLE {target}.{table}_history ENGINE = MergeTree ORDER BY (pre, vf) AS {select}")
        for lo in range(0, manifest["union_nodes"], batch_rows):
            hi = min(lo + batch_rows, manifest["union_nodes"])
            # Restrict each snapshot before either window. All observations of
            # each pre are in exactly one batch, including deletion/reappearance.
            bounded = snapshot_parts(f"pre >= {lo} AND pre < {hi}")
            select = make_query(bounded, names, values.split(", "), instants)
            sql(f"history_{table}_{lo}", f"INSERT INTO {target}.{table}_history {select}")
    sql("history_by_name", f"CREATE TABLE {target}.nodes_history_by_name ENGINE = MergeTree ORDER BY (nid, pre, vf) AS SELECT * FROM {target}.nodes_history")
    sql("history_by_parent", f"CREATE TABLE {target}.metadata_history_by_parent ENGINE = MergeTree ORDER BY (parent_pre, b, pre, vf) AS SELECT * FROM {target}.metadata_history")
    if recovery is not None:
        recovery.check_counts()
        log("publication recovery: all build statements and retained row counts verified; no data writes replayed")
        recovery = None
    for i, db in enumerate(dbs):
        sql(f"create_{db}", f"CREATE DATABASE {db}")
        at = dt_lit(instants[i])
        for table, source in (("nodes", "nodes_history"), ("nodes_by_name", "nodes_history_by_name"), ("metadata", "metadata_history"),
                              ("metadata_by_parent", "metadata_history_by_parent")):
            sql(f"{db}_{table}", f"CREATE VIEW {db}.{table} AS SELECT * EXCEPT (vf, vt) FROM {target}.{source} WHERE vf <= {at} AND vt > {at}")
        sql(f"{db}_names", f"CREATE VIEW {db}.names AS SELECT * FROM {target}.names")
        sql(f"{db}_hierarchy", f"CREATE VIEW {db}.hierarchy AS SELECT * FROM {target}.hierarchy")
        for table in ("nodes", "metadata"):
            if ch.scalar(f"SELECT count() FROM {db}.{table}") != ch.scalar(f"SELECT count() FROM {manifest['dbs'][i]}.{table}"):
                raise ValueError(f"history row count mismatch at {dates[i]} / {table}")
    counts = {t: int(ch.scalar(f"SELECT count() FROM {target}.{t}_history")) for t in ("nodes", "metadata")}
    result = {**manifest, "dbs": dbs, "snapshot_dbs": manifest["dbs"], "history": True, "history_timings": timings, "version_rows": counts,
              "history_threads": build_threads, "numeric_parents": numeric_parents,
              "hierarchy_engine": "stream" if numeric_ancestors else "string-join",
              "history_coalescer": coalescer,
              "history_metadata_join": "full_sorting_merge" if numeric_parents or coalescer == "pair" else "grace_hash",
              "asof_coverage": "selected scans only", "renders_tree": True}
    if resume_publication:
        result["publication_recovered"] = True
        result["history_timing_source"] = "query log (stream read excludes upload); publication wall clock"
    sql("history_manifest", f"CREATE TABLE {target}.history_manifest (doc String) ENGINE = TinyLog")
    ch.exec(f"INSERT INTO {target}.history_manifest VALUES ({lit(json.dumps(result))})")
    ch.close()
    return result


def rich_name_index(
    url: str,
    target: str,
    *,
    granularity: int = 8192,
    min_free_bytes: int = 0,
    variant: str | None = None,
) -> dict:
    """Experimental rich access path matching name-first discovery; no cutover.

    Copy rich versions ordered by (nid, pre, vf). Generic, multi-term and NOT
    predicates still require the normal preorder rich table.
    """
    identifier(target)
    suffix = f"_{identifier(variant)}" if variant is not None else ""
    table, view, marker = f"metadata_history_by_name{suffix}", f"metadata_by_name{suffix}", f"rich_name_manifest{suffix}"
    if granularity <= 0:
        raise ValueError("rich name index granularity must be positive")
    if min_free_bytes < 0:
        raise ValueError("min_free_bytes cannot be negative")
    ch = Ch(url, db=target, timeout=7200, max_threads=2, max_block_size=8192, max_memory_usage=8 << 30,
            max_bytes_before_external_sort=256 << 20, max_bytes_ratio_before_external_sort=0,
            join_algorithm="full_sorting_merge")
    try:
        manifest = json.loads(ch.scalar("SELECT doc FROM history_manifest"))
        if ch.scalar(f"EXISTS TABLE {table}") == "1":
            raise ValueError(f"experimental table already exists: {target}.{table}")
        if ch.scalar(f"EXISTS TABLE {marker}") == "1":
            raise ValueError(f"experimental checkpoint already exists: {target}.{marker}")
        for db in manifest["dbs"]:
            identifier(db)
            if ch.scalar(f"EXISTS TABLE {db}.{view}") == "1":
                raise ValueError(f"experimental view already exists: {db}.{view}")
        disk_reserve(ch, table, min_free_bytes)
        start = time.monotonic()
        ch.exec(f"""CREATE TABLE {table} ENGINE = MergeTree ORDER BY (nid, pre, vf)
            SETTINGS index_granularity = {granularity} AS
            SELECT d.nid AS nid, m.* FROM metadata_history m INNER JOIN (
                SELECT pre, nid FROM dictionary) d ON m.pre = d.pre""",
                settings={"log_comment": f"narrow:{target}:{table}"})
        count = int(ch.scalar(f"SELECT count() FROM {table}"))
        expected = int(ch.scalar("SELECT count() FROM metadata_history"))
        if count != expected:
            raise ValueError(f"rich name index lost versions: {count} != {expected}; partial table retained")
        for date, db in zip(manifest["dates"], manifest["dbs"], strict=True):
            at = dt_lit(scan_dt(date))
            ch.exec(f"CREATE VIEW {db}.{view} AS SELECT * EXCEPT (vf, vt) FROM {target}.{table} WHERE vf <= {at} AND vt > {at}")
        result = {"target": target, "versions": count, "granularity": granularity,
                  "seconds": round(time.monotonic() - start, 3), "production_cutover": False}
        if variant is not None:
            result.update(rich_name_variant_identity(target, variant))
        ch.exec(f"CREATE TABLE {marker} (doc String) ENGINE = TinyLog")
        ch.exec(f"INSERT INTO {marker} VALUES ({lit(json.dumps(result))})")
        return result
    finally:
        ch.close()


def directory_parent_index(
    url: str,
    target: str,
    *,
    min_free_bytes: int = 0,
) -> dict:
    """Compact immutable parent links, derived only from validated frozen DFS IDs.

    Non-root ordered arrays end with the direct parent followed by self. This
    derivation must not be used with opaque IDs sorted by numeric value.
    """
    identifier(target)
    if min_free_bytes < 0:
        raise ValueError("min_free_bytes cannot be negative")
    ch = Ch(url, db=target, timeout=7200, max_threads=2, max_memory_usage=8 << 30)
    try:
        manifest = json.loads(ch.scalar("SELECT doc FROM history_manifest"))
        if ch.scalar("EXISTS TABLE directory_parents") == "1":
            raise ValueError(f"experimental table already exists: {target}.directory_parents")
        for db in manifest["dbs"]:
            identifier(db)
            if ch.scalar(f"EXISTS TABLE {db}.directory_parents") == "1":
                raise ValueError(f"experimental view already exists: {db}.directory_parents")
        disk_reserve(ch, "directory_parents", min_free_bytes)
        start = time.monotonic()
        bad = int(ch.scalar("""SELECT countIf(empty(ancestors) OR ancestors[-1] != pre
            OR arraySort(ancestors) != ancestors OR length(arrayDistinct(ancestors)) != length(ancestors)) FROM hierarchy"""))
        if bad:
            raise ValueError(f"invalid frozen DFS directory ancestry: {bad} rows")
        ch.exec("""CREATE TABLE directory_parents ENGINE = MergeTree ORDER BY pre AS
            SELECT pre, if(length(ancestors) = 1, toInt64(-1), toInt64(ancestors[-2])) AS parent_pre FROM hierarchy""",
                settings={"log_comment": f"narrow:{target}:directory_parents"})
        rows = int(ch.scalar("SELECT count() FROM directory_parents"))
        if rows != int(ch.scalar("SELECT count() FROM hierarchy")):
            raise ValueError("directory parent index lost rows; partial table retained")
        for db in manifest["dbs"]:
            ch.exec(f"CREATE VIEW {db}.directory_parents AS SELECT * FROM {target}.directory_parents")
        result = {"target": target, "directories": rows, "seconds": round(time.monotonic() - start, 3), "production_cutover": False}
        ch.exec("CREATE TABLE parent_index_manifest (doc String) ENGINE = TinyLog")
        ch.exec(f"INSERT INTO parent_index_manifest VALUES ({lit(json.dumps(result))})")
        return result
    finally:
        ch.close()


def root_metadata_source(
    db: str,
    ast: Ast,
    *,
    name_index: bool,
    hit: bool,
    name_index_variant: str | None = None,
) -> str:
    """Use name ranges only when every discovered root passed the last-name test."""
    identifier(db)
    suffix = f"_{identifier(name_index_variant)}" if name_index_variant is not None else ""
    if name_index_variant is not None and not name_index:
        raise ValueError("a rich name-index variant requires name_index")
    if name_index and not hit and literal_name_only(ast):
        return f"SELECT * FROM {db}.metadata_by_name{suffix} WHERE nid IN (SELECT nid FROM cn) AND pre IN (SELECT pre FROM roots)"
    return f"SELECT * FROM {db}.metadata WHERE pre IN (SELECT pre FROM roots)"


def evaluate(
    url: str,
    db: str,
    prefix: str,
    query: str,
    syntax: str = "simple",
    threads: int = 8,
    *,
    path_free: bool = True,
    name_index: bool = False,
    metadata_paths: bool = True,
) -> dict:
    """Discovery, complete root identity and late metadata timings, NOT a tree response."""
    if prefix.strip("/") != prefix:
        raise ValueError("a canonical subtree prefix is required (empty means global)")
    ast = parse(query, syntax)
    if ast is None:
        raise ValueError("a nonempty filter is required")
    ix = ChIndex(url, db=identifier(db), threads=threads, bounded_view=prefix, path_free=path_free, trigram_names=True)
    ix.settings.update(max_memory_usage=str(8 << 30), max_bytes_before_external_sort=str(256 << 20),
                       max_bytes_ratio_before_external_sort="0", max_bytes_before_external_group_by=str(256 << 20),
                       max_bytes_ratio_before_external_group_by="0", max_block_size="8192")
    ch = Ch(url, db=db, session=False, **{k: v for k, v in ix.settings.items() if k != "database"})
    try:
        result = ix.evaluate(ast, prefix)
        start = time.monotonic()
        if result.roots > 50_000:
            roots = None
            n, fingerprint = path_fingerprint(ch, ix.root_paths_sql())
            if n != result.roots:
                raise ValueError(f"root fingerprint lost rows: {n} != {result.roots}")
        else:
            roots, n, fingerprint = ix.roots_summary(50_000)
        materialize_s = time.monotonic() - start
        start = time.monotonic()
        # Read full root metadata only AFTER discovery. Exclusions are read
        # separately here; net rich aggregates and the level walk are not implemented.
        source = root_metadata_source(db, ast, name_index=name_index, hit=result.hit)
        ch.exec(source if metadata_paths else f"SELECT * EXCEPT (path) FROM ({source})", fmt="Null")
        if ast.neg:
            columns = "*" if metadata_paths else "* EXCEPT (path)"
            ch.exec(f"SELECT {columns} FROM metadata WHERE pre IN (SELECT pre FROM ex)", fmt="Null")
        late_s = time.monotonic() - start
        return {"roots": roots, "n": n, "md5": fingerprint, "b": result.b, "o": result.o, "excluded": result.excluded,
                "discovery_s": result.stats["s"], "materialize_s": round(materialize_s, 4), "late_metadata_s": round(late_s, 4),
                "stages": result.stats, "renders_tree": False}
    finally:
        for name in ("roots", "ex", "cr", "cn"):
            ch.exec(f"DROP TEMPORARY TABLE IF EXISTS {name}")


def check_preorder(chunks: Iterable[bytes], count: int) -> None:
    """Validate every subtree endpoint from ordered (pre, post, depth) records.

    A node ends just before the next row at its depth or above. This checks
    all endpoints, not only their bounds, using an O(depth) stack.
    """
    record = struct.Struct("<IIB")
    pending, stack, seen = b"", [], 0
    for chunk in chunks:
        data = pending + chunk
        end = len(data) // record.size * record.size
        pending = data[end:]
        for pre, post, depth in record.iter_unpack(data[:end]):
            if pre != seen or seen >= count or depth < 1 or depth > len(stack) + 1 or not pre <= post < count:
                raise ValueError(f"invalid preorder interval: pre={pre}, post={post}, depth={depth}, position={seen}")
            if seen == 0 and post != count - 1:
                raise ValueError(f"invalid root endpoint: {post} != {count - 1}")
            while len(stack) >= depth:
                actual = stack.pop()
                if actual != seen - 1:
                    raise ValueError(f"invalid subtree endpoint: {actual} != {seen - 1}")
            stack.append(post)
            seen += 1
    if pending or seen != count:
        raise ValueError(f"incomplete interval audit: {seen} != {count} rows, {len(pending)} trailing bytes")
    for actual in stack:
        if actual != count - 1:
            raise ValueError(f"invalid final subtree endpoint: {actual} != {count - 1}")


def progress(url: str, target: str) -> dict:
    """Small read-only construction diagnostics, safe beside a running stage."""
    identifier(target)
    ch = Ch(url, session=False, timeout=30)
    try:
        if ch.scalar(f"EXISTS DATABASE {target}") != "1":
            raise ValueError(f"experimental database does not exist: {target}")
        active = ch.json(f"""SELECT Settings['log_comment'], query_id, round(elapsed, 1), read_rows, written_rows, read_bytes,
            memory_usage, peak_memory_usage, peak_threads_usage, ProfileEvents['UserTimeMicroseconds']
            FROM system.processes WHERE (position(query, {lit(target)}) > 0 OR current_database = {lit(target)}
                OR startsWith(current_database, {lit(target + '_')})) AND position(query, 'system.processes') = 0
            ORDER BY elapsed DESC""")
        tables = ch.json(f"""SELECT database, table, sum(rows), sum(bytes_on_disk) FROM system.parts
            WHERE active AND (database = {lit(target)} OR startsWith(database, {lit(target + '_')}))
            GROUP BY database, table ORDER BY database, table""")
        disks = ch.json("SELECT name, free_space, total_space FROM system.disks ORDER BY name")
        stages = ch.json(f"""SELECT log_comment, toString(event_time), toString(type), round(query_duration_ms / 1000, 3), memory_usage
            FROM system.query_log WHERE event_date >= today() - 1 AND startsWith(log_comment, {lit('narrow:' + target + ':')})
            AND type != 'QueryStart' ORDER BY event_time_microseconds DESC LIMIT 12""")
        return {
            "target": target,
            "active": [dict(zip(("label", "query_id", "elapsed_s", "read_rows", "written_rows", "read_bytes", "memory_bytes",
                                 "peak_memory_bytes", "peak_threads", "user_cpu_us"), row, strict=True)) for row in active],
            "tables": [dict(zip(("database", "table", "rows", "bytes"), row, strict=True)) for row in tables],
            "disks": [dict(zip(("name", "free_bytes", "total_bytes"), row, strict=True)) for row in disks],
            "recent_stages": [dict(zip(("label", "at", "status", "seconds", "memory_bytes"), row, strict=True)) for row in stages],
        }
    finally:
        ch.close()


def query_profile(
    url: str,
    target: str,
    *,
    seconds: int = 900,
    limit: int = 50,
    min_ms: int = 100,
) -> dict:
    """Recent completed-query costs, including both historical-view sessions.

    This reads query-log metadata only; it does not flush logs, scan the path
    tables, reset caches, or replay queries. SQL text may contain private paths.
    Logical read_bytes differs from file reads and OS block-device reads; a
    missing OSReadBytes event stays unknown, not a claimed zero-disk read.
    """
    identifier(target)
    if seconds <= 0 or limit <= 0 or min_ms < 0:
        raise ValueError("profile seconds/limit must be positive and min_ms nonnegative")
    ch = Ch(url, session=False, timeout=30)
    try:
        rows = ch.json(f"""SELECT current_database, toString(event_time), toString(type), query_duration_ms,
            read_rows, read_bytes, memory_usage, ProfileEvents['UserTimeMicroseconds'],
            ProfileEvents['ExternalAggregationWritePart'], ProfileEvents['ExternalSortWritePart'],
            ProfileEvents['ReadBufferFromFileDescriptorReadBytes'],
            if(mapContains(ProfileEvents, 'OSReadBytes'), ProfileEvents['OSReadBytes'], NULL),
            ProfileEvents['DiskReadElapsedMicroseconds'], query
            FROM system.query_log
            WHERE event_date >= today() - toUInt32(ceil({seconds} / 86400))
                AND event_time >= now() - INTERVAL {seconds} SECOND
                AND (current_database = {lit(target)} OR startsWith(current_database, {lit(target + '_')}))
                AND type != 'QueryStart' AND query_duration_ms >= {min_ms}
            ORDER BY event_time_microseconds DESC LIMIT {limit}""")
        fields = ("database", "at", "status", "duration_ms", "read_rows", "read_bytes", "peak_memory_bytes",
                  "user_cpu_us", "aggregation_spills", "sort_spills", "file_read_bytes", "os_read_bytes", "read_wait_us", "sql")
        return {"target": target, "lookback_s": seconds, "limit": limit, "min_ms": min_ms,
                "queries": [dict(zip(fields, row, strict=True)) for row in rows]}
    finally:
        ch.close()


def audit(url: str, target: str) -> dict:
    """Exact full-domain numbering/count checks without N-sized client arrays.

    Read-only but heavy: run after construction, not concurrently with a bench.
    Preorder uniqueness needs an external numeric sort; ID checks read in order.
    This does not replace query/root truth or rich-value response comparisons.
    """
    identifier(target)
    ch = Ch(url, db=target, timeout=7200, max_threads=1, max_memory_usage=8 << 30,
            max_bytes_before_external_sort=256 << 20, max_bytes_ratio_before_external_sort=0)
    try:
        manifest = json.loads(ch.scalar("SELECT doc FROM manifest"))
        count = manifest["union_nodes"]
        checks = {}

        def ranks(
            label: str,
            table: str,
            key: str,
            order: str,
            expected: int | None = None,
        ) -> int:
            n, bad, lo, hi = ch.json(f"""SELECT count(), sum({key} != rowNumberInAllBlocks()), min({key}), max({key})
                FROM (SELECT {key} FROM {table} ORDER BY {order})""")[0]
            want = n if expected is None else expected
            if [n, bad, lo, hi] != [want, 0, 0, want - 1]:
                raise ValueError(f"{label}: numbering audit failed: {[n, bad, lo, hi]} != {[want, 0, 0, want - 1]}")
            checks[label] = True
            return n

        ranks("path_ids", "ids", "id", "depth, path", count)
        ranks("interval_ids", "intervals", "id", "id", count)
        ranks("preorder_ids", "intervals", "pre", "pre", count)
        names = ranks("name_ids", "names", "nid", "l")
        bad = int(ch.scalar(f"SELECT countIf(pre > post OR post >= {count} OR id >= {count}) FROM intervals"))
        if bad:
            raise ValueError(f"interval bounds audit failed: {bad} invalid rows")
        checks["interval_bounds"] = True
        root = ch.json(f"SELECT pre, post FROM dictionary WHERE (depth, path) = ({depth_of(manifest['prefix'])}, {lit(manifest['prefix'])})")
        if root != [[0, count - 1]]:
            raise ValueError(f"root span audit failed: {root} != {[[0, count - 1]]}")
        checks["root_span"] = True
        dp = depth_of(manifest["prefix"])
        check_preorder(ch.stream(f"SELECT pre, post, toUInt8(depth - {dp} + 1) FROM dictionary ORDER BY pre", "RowBinary"), count)
        checks["preorder_tree"] = True
        if int(ch.scalar("SELECT count() FROM dictionary")) != count:
            raise ValueError("dictionary row-count audit failed")
        checks["dictionary_rows"] = True
        snapshots = {}
        for i, (day, db) in enumerate(zip(manifest["dates"], manifest["dbs"], strict=True)):
            expected = int(ch.scalar(f"SELECT count() FROM snapshot_{i}"))
            actual = {table: int(ch.scalar(f"SELECT count() FROM {db}.{table}")) for table in ("nodes", "nodes_by_name", "metadata")}
            if actual != {table: expected for table in actual}:
                raise ValueError(f"snapshot audit failed at {day}: {actual} != {expected} each")
            snapshots[day] = expected
        checks["snapshot_rows"] = True
        return {"union_nodes": count, "names": names, "snapshots": snapshots, "checks": checks}
    finally:
        ch.close()


def compare(
    store: Store,
    date: str,
    prefix: str,
    query: str,
    syntax: str = "simple",
    *,
    timings: dict | None = None,
) -> dict:
    """Uncapped root identities and totals from the existing historical implementation."""
    ch = store.session()
    try:
        scan, ast = store.scan(date), parse(query, syntax)
        if scan is None or ast is None:
            raise ValueError("an existing scan and nonempty filter are required")
        start = time.monotonic()
        pr = filter_prepare(ch, scan, prefix, ast)
        if timings is not None:
            timings["discovery_s"] = round(time.monotonic() - start, 4)
        start = time.monotonic()
        if pr is None:
            return {"roots": [], "n": 0, "md5": None, "b": 0, "o": 0}
        if pr.n_roots <= 50_000:
            roots = [r[0] for r in ch.json(f"SELECT path FROM rn_{pr.sfx} ORDER BY path")]
            md5 = None
        else:
            roots = None
            n, md5 = path_fingerprint(ch, f"SELECT path FROM rn_{pr.sfx}")
            if n != pr.n_roots:
                raise ValueError(f"canonical root fingerprint lost rows: {n} != {pr.n_roots}")
        if timings is not None:
            timings["materialize_s"] = round(time.monotonic() - start, 4)
        return {"roots": roots, "n": pr.n_roots, "md5": md5, "b": pr.total.b, "o": pr.total.o}
    finally:
        ch.close()


def signature(row: dict) -> dict:
    """Complete root identity in the existing benchmark truth's fingerprint format."""
    from ..bench.truth import md5_paths

    return {"n": row["n"], "md5": row["md5"] if row["roots"] is None else md5_paths(row["roots"]), "b": row["b"], "o": row["o"]}


def compare_discovery_runs(
    before: list[dict],
    after: list[dict],
) -> list[dict]:
    """Pair uncapped discovery/component records, never complete-body timings.

    Use saved JSONL files, not stdout records that omit small root lists.
    Fingerprints retain the benchmark's existing newline-delimited MD5 format.
    """
    fields = ("prefix", "date", "query", "trial", "cold")

    def index(rows: list[dict]) -> dict[tuple, dict]:
        if not rows:
            raise ValueError("an empty discovery run cannot be compared")
        result = {}
        for row in rows:
            key = tuple(row[field] for field in fields)
            if key in result:
                raise ValueError(f"duplicate discovery case: {key}")
            result[key] = row
        return result

    a, b = index(before), index(after)
    if a.keys() != b.keys():
        raise ValueError("discovery workloads differ; dates, queries, trials and cache modes must match")
    result = []
    for key, old in a.items():
        new = b[key]
        for field in ("threads", "query_text", "syntax"):
            if old.get(field) is not None and new.get(field) is not None and old[field] != new[field]:
                raise ValueError(f"discovery {field} differs: {key}")
        for row in (old, new):
            if row.get("exact") is not True or row.get("renders_tree") is not False:
                raise ValueError(f"both discovery runs must pass canonical identity comparison: {key}")
            roots = row["roots"]
            if roots is None:
                if row["n"] <= 0 or not isinstance(row.get("md5"), str) or not re.fullmatch(r"[0-9a-f]{32}", row["md5"]):
                    raise ValueError(f"uncapped root fingerprint is required: {key}")
            elif not isinstance(roots, list) or len(roots) != row["n"] or roots != sorted(set(roots)):
                raise ValueError(f"root list must be sorted, unique and complete: {key}")
        if signature(old) != signature(new):
            raise ValueError(f"discovery root identities or totals differ: {key}")
        timings = {}
        for field in ("discovery_s", "materialize_s", "late_metadata_s"):
            if field not in old or field not in new:
                continue
            if not isfinite(old[field]) or not isfinite(new[field]) or old[field] < 0 or new[field] < 0:
                raise ValueError(f"discovery durations must be finite and nonnegative: {key}")
            timings[field] = {"before": old[field], "after": new[field],
                              "speedup": round(old[field] / new[field], 4) if new[field] else None}
        result.append({
            **dict(zip(fields, key, strict=True)),
            "same_roots_and_totals": True,
            "threads_verified": old.get("threads") is not None and new.get("threads") is not None,
            "before_mode": {field: old.get(field) for field in ("path_free", "name_index", "metadata_paths", "threads")},
            "after_mode": {field: new.get(field) for field in ("path_free", "name_index", "metadata_paths", "threads")},
            "timings": timings,
        })
    return result


def summarize(records: Iterable[dict]) -> dict:
    from ..bench.local import pct

    groups = {}
    for row in records:
        key = row["date"], row["trial"]
        group = groups.setdefault(key, {"exact": 0, "truth_exact": 0, "truth_checked": 0, "timings": {}})
        group["exact"] += row.get("exact") is True
        group["truth_exact"] += row.get("truth_exact") is True
        group["truth_checked"] += isinstance(row.get("truth_exact"), bool)
        times = {k: row[k] for k in ("discovery_s", "materialize_s", "late_metadata_s")}
        times["combined_s"] = sum(times.values())
        if "baseline" in row:
            times["baseline_discovery_s"] = row["baseline"]["discovery_s"]
        for k, value in times.items():
            group["timings"].setdefault(k, []).append(value)
    return {f"{date}/t{trial}": {"n": len(g["timings"]["discovery_s"]), "exact": g["exact"], "truth_checked": g["truth_checked"], "truth_exact": g["truth_exact"],
                "timings": {k: {"p50": pct(v, .5), "p90": pct(v, .9), "max": max(v)} for k, v in g["timings"].items()}}
            for (date, trial), g in sorted(groups.items())}
