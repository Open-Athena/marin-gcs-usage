"""Stream frozen scalar nodes directly into an explicitly supplied native engine.

The source must remain immutable between audit and stream. No node records are
decoded or collected in Python. Only the registered-query/bucket matrix returns.
"""

from json import loads
from os import X_OK, access
from pathlib import Path
from struct import pack
from subprocess import PIPE, Popen, TimeoutExpired
from sys import stderr
from threading import Thread
from time import monotonic
from uuid import uuid4

from .client import Ch, lit
from .coarse import CoarseRequest
from .hot_l1 import _buckets, _complete
from .hot_l1_batch_sql import NAME, SCOPE, _number
from .hot_l1_catalog import _unique_object
from .narrow import identifier


def _string(value: str) -> bytes:
    data, prefix = value.encode("utf-8"), bytearray()
    length = len(data)
    while length >= 128:
        prefix.append((length & 127) | 128)
        length >>= 7
    return bytes(prefix) + bytes([length]) + data


def build(
    ch: Ch,
    target: str,
    date: str,
    patterns: tuple[str, ...],
    *,
    binary: Path,
) -> dict:
    identifier(target)
    if not binary.is_file() or not access(binary, X_OK):
        raise CoarseRequest("native stream requires an explicit existing executable")
    if not patterns or len(patterns) >= 1 << 32 or any(not isinstance(p, str) or not p or "/" in p or "\0" in p or len(p) > 512 for p in patterns):
        raise CoarseRequest("native stream requires nonempty slash-free, NUL-free literals of at most 512 characters")
    patterns = tuple(p.lower() for p in patterns)
    if len(set(patterns)) != len(patterns):
        raise CoarseRequest("native stream normalized literals must be unique")
    start = monotonic()
    manifest = loads(ch.scalar(f"SELECT doc FROM {target}.history_manifest"))
    if manifest["prefix"] != "" or date not in manifest["dates"]:
        raise CoarseRequest("native stream requires a global frozen target containing the requested scan")
    db = identifier(manifest["dbs"][manifest["dates"].index(date)])
    buckets = _buckets(ch, target)
    stage = monotonic()
    audit = ch.json(f"""SELECT count(),countIf(isNull(path) OR NOT isValidUTF8(path)),sum(length(path)),
        countIf(isNull(pre) OR isNull(post) OR isNull(b) OR isNull(o) OR pre < 0 OR post < pre OR b < 0 OR o < 0),
        countIf(pre=0),countIf(pre=0 AND path=''),maxIf(post,pre=0) FROM {db}.nodes""")
    if len(audit) != 1 or len(audit[0]) != 7:
        raise RuntimeError("native stream source audit returned an invalid shape")
    rows, invalid_utf8, path_bytes, invalid_scalars, roots, named_roots, root_post = map(_number, audit[0])
    if invalid_utf8 or invalid_scalars or not rows or (roots, named_roots, root_post) != (1, 1, buckets[-1][1]):
        raise CoarseRequest("native stream source failed UTF-8/scalar/global-root audit")
    audit_s = monotonic() - stage
    result = aggregate_source(ch, db, patterns, buckets, rows, binary=binary)
    return {"schema": "hot-l1-batch-stream-v1", "target": target, "snapshot_db": db, "date": date,
            "exact": True, "incremental": False, "levels": 1, "scope": SCOPE, "results": result["results"],
            "compiled_patterns": len(patterns), "source_query_id": result["source_query_id"], "native": result["native"],
            "source_validation": {"rows": rows, "invalid_utf8_paths": invalid_utf8, "path_bytes": path_bytes, "invalid_scalar_rows": invalid_scalars},
            "stages": {"source_validation_s": round(audit_s, 6), "aggregate_s": result["aggregate_s"]}, "build_s": round(monotonic() - start, 6)}


def aggregate_source(
    ch: Ch,
    db: str,
    patterns: tuple[str, ...],
    buckets: list[list],
    rows: int,
    *,
    binary: Path,
) -> dict:
    """Native aggregation component over an explicitly audited immutable source.

    The caller must separately validate source scalar contents, parent closure,
    geometry and identity. This helper neither claims publication acceptance nor
    looks up a frozen history manifest; daily sources need not invent one.
    """
    identifier(db)
    if not binary.is_file() or not access(binary, X_OK):
        raise CoarseRequest("native stream requires an explicit existing executable")
    if (type(rows) is not int or not 1 <= rows < 1 << 64 or not patterns or len(patterns) >= 1 << 32 or
            any(not isinstance(p, str) or not p or "/" in p or "\0" in p or len(p) > 512 for p in patterns)):
        raise CoarseRequest("native source requires positive rows and nonempty NUL/slash-free literals of at most 512 characters")
    patterns = tuple(p.lower() for p in patterns)
    if len(set(patterns)) != len(patterns):
        raise CoarseRequest("native stream normalized literals must be unique")
    if not isinstance(buckets, list) or not 1 <= len(buckets) <= 6:
        raise CoarseRequest("native source requires one to six complete ordered bucket bounds")
    last = 0
    for row in buckets:
        if (not isinstance(row, (list, tuple)) or len(row) != 3 or
                any(type(n) is not int for n in row[:2]) or row[0] != last + 1 or row[1] < row[0] or
                row[1] >= 1 << 64 or not isinstance(row[2], str) or not row[2] or "\0" in row[2]):
            raise CoarseRequest("native source requires one to six complete ordered bucket bounds")
        row[2].encode("utf-8")
        last = row[1]
    if rows > last + 1 or len({row[2] for row in buckets}) != len(buckets):
        raise CoarseRequest("native source requires one to six complete ordered bucket bounds")
    encoded = [_string(p) for p in patterns]
    query_id = "hot_l1_stream_" + uuid4().hex
    reader = ch.fork(query_id=query_id)
    child, source, logger, error_pipe = None, None, None, None
    log_errors = []
    stage = monotonic()

    def forward_stderr() -> None:
        try:
            while chunk := error_pipe.read(4096):
                stderr.write(chunk.decode("utf-8", errors="replace"))
                stderr.flush()
        except BaseException as error:
            log_errors.append(error)

    try:
        child = Popen([str(binary.resolve())], stdin=PIPE, stdout=PIPE, stderr=PIPE)
        error_pipe = child.stderr
        logger = Thread(target=forward_stderr, name="hot-l1-native-stderr", daemon=True)
        logger.start()
        child.stdin.write(b"HL1DFS01" + pack("<QIB", rows, len(patterns), len(buckets)))
        for pre, post, _ in buckets:
            child.stdin.write(pack("<QQ", pre, post))
        for literal in encoded:
            child.stdin.write(literal)
        source = reader.stream(f"""SELECT assumeNotNull(toUInt64(pre)),assumeNotNull(toUInt64(post)),
            assumeNotNull(toUInt64(b)),assumeNotNull(toUInt64(o)),assumeNotNull(lowerUTF8({NAME})) FROM {db}.nodes ORDER BY pre""", fmt="RowBinary")
        for chunk in source:
            child.stdin.write(chunk)
        child.stdin.close()
        child.stdin = None
        child.stderr = None  # The logger is the sole reader of this pipe.
        output, _ = child.communicate(timeout=ch.timeout)
        logger.join()
        if log_errors:
            raise RuntimeError("native stream stderr forwarding failed") from log_errors[0]
        if child.returncode != 0:
            raise RuntimeError(f"native stream engine exited with status {child.returncode}")
        native = loads(output, object_pairs_hook=_unique_object)
        if (not isinstance(native, dict) or native.get("schema") != "hot-l1-native-stream-v1" or native.get("exact") is not True or
                native.get("incremental") is not False or type(native.get("levels")) is not int or native["levels"] != 1 or native.get("nodes_read") != rows or
                native.get("registered_predicates") != len(patterns)):
            raise RuntimeError("native stream output disagrees with the complete source/catalog")
        for field in ("nodes_read", "registered_predicates", "peak_stack", "peak_active", "native_peak_rss_bytes"):
            if type(native.get(field)) is not int or native[field] < 0:
                raise RuntimeError("native stream returned invalid process statistics")
        matrix = native.get("matrix")
        if not isinstance(matrix, list) or len(matrix) != len(patterns):
            raise RuntimeError("native stream returned an incomplete query matrix")
        results = []
        for q, (pattern, row) in enumerate(zip(patterns, matrix, strict=True), 1):
            if not isinstance(row, dict) or type(row.get("predicate_id")) is not int or row["predicate_id"] != q or not isinstance(row.get("buckets"), list) or len(row["buckets"]) != len(buckets):
                raise RuntimeError("native stream returned invalid query IDs/bucket counts")
            totals = []
            for (pre, _, _), pair in zip(buckets, row["buckets"], strict=True):
                if not isinstance(pair, list) or len(pair) != 2:
                    raise RuntimeError("native stream returned an invalid bucket pair")
                totals.append([pre, *map(_number, pair)])
            completed = _complete(buckets, totals)
            results.append({"predicate_id": q, "pattern": pattern, "root": {"b": sum(r["b"] for r in completed), "o": sum(r["o"] for r in completed)}, "buckets": completed})
    except BaseException:
        try:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        finally:
            try:
                ch.exec(f"KILL QUERY WHERE query_id={lit(query_id)} SYNC", fmt=None)
            finally:
                if source is not None:
                    source.close()
        raise
    finally:
        try:
            if child is not None:
                for pipe in (child.stdin, child.stdout, error_pipe):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except BrokenPipeError:
                            # The terminated consumer cannot accept buffered input.
                            pass
            if logger is not None:
                logger.join(timeout=5)
        finally:
            reader.close()
    return {"results": results, "source_query_id": query_id, "native": {k: v for k, v in native.items() if k != "matrix"},
            "aggregate_s": round(monotonic() - stage, 6)}
