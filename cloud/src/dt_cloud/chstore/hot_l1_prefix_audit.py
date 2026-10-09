"""Per-date prefix closure over frozen audited DFS geometry, not an L1 rebuild.

The source must be the immutable snapshot used for the accepted artifact.
This proof validates immediate-parent presence, not the union's path geometry
or scalar contents. The original frozen-dictionary audit remains required.
"""

from hashlib import sha256
from json import dumps, loads
from os import X_OK, access
from pathlib import Path
from struct import pack
from subprocess import PIPE, Popen, TimeoutExpired
from time import monotonic
from uuid import uuid4

from .client import Ch, lit
from .coarse import CoarseRequest
from .hot_l1 import _buckets
from .hot_l1_batch_catalog import HotL1BatchCatalog
from .hot_l1_batch_sql import _number
from .hot_l1_catalog import _unique_object
from .narrow import identifier

SOURCE_CONTRACT = "immutable accepted snapshot over separately audited frozen union geometry; immediate-parent presence only, not scalar/path-geometry validation"


def audit(
    ch: Ch,
    target: str,
    date: str,
    accepted: Path,
    *,
    binary: Path,
) -> dict:
    identifier(target)
    if not binary.is_file() or not access(binary, X_OK):
        raise CoarseRequest("prefix audit requires an explicit existing native executable")
    start = monotonic()
    data = accepted.read_bytes()
    catalog = HotL1BatchCatalog.from_bytes([data])
    body = loads(data, object_pairs_hook=_unique_object)
    if catalog.target != target or body["date"] != date:
        raise CoarseRequest("prefix audit accepted artifact target/date differs from request")
    expected = body["source_validation"]["rows"]
    manifest = loads(ch.scalar(f"SELECT doc FROM {target}.history_manifest"))
    if manifest["prefix"] != "" or date not in manifest["dates"]:
        raise CoarseRequest("prefix audit requires a global frozen target containing the requested scan")
    db = identifier(manifest["dbs"][manifest["dates"].index(date)])
    if db != body["snapshot_db"]:
        raise CoarseRequest("prefix audit snapshot differs from accepted artifact")
    buckets = _buckets(ch, target)
    accepted_bounds = sorted([(b["pre"], b["post"], b["path"]) for b in body["results"][0]["buckets"]])
    if [tuple(row) for row in buckets] != accepted_bounds:
        raise CoarseRequest("prefix audit frozen bounds differ from accepted artifact")
    rows = ch.json(f"""SELECT count(),countIf(isNull(pre) OR isNull(post) OR isNull(depth) OR pre < 0 OR post < pre OR depth < 0 OR depth > 255),
        countIf(pre=0),maxIf(post,pre=0),maxIf(depth,pre=0) FROM {db}.nodes""")
    if len(rows) != 1 or len(rows[0]) != 5:
        raise RuntimeError("prefix audit source preflight returned an invalid shape")
    count, invalid, roots, root_post, root_depth = map(_number, rows[0])
    if count != expected or invalid or (roots, root_post, root_depth) != (1, buckets[-1][1], 0):
        raise CoarseRequest("prefix audit source count/scalars/root disagree with accepted snapshot")
    control = b"HL1PRE01" + pack("<QB", count, len(buckets)) + b"".join(pack("<QQ", lo, hi) for lo, hi, _ in buckets)
    query_id = "hot_l1_prefix_" + uuid4().hex
    reader = ch.fork(query_id=query_id)
    child, source = None, None
    try:
        child = Popen([str(binary.resolve()), "--prefix-audit"], stdin=PIPE, stdout=PIPE, stderr=PIPE)
        child.stdin.write(control)
        source = reader.stream(f"""SELECT assumeNotNull(toUInt64(pre)),assumeNotNull(toUInt64(post)),assumeNotNull(toUInt8(depth))
            FROM {db}.nodes ORDER BY pre""", fmt="RowBinary")
        for chunk in source:
            child.stdin.write(chunk)
        child.stdin.close()
        child.stdin = None
        output, error = child.communicate(timeout=ch.timeout)
        if child.returncode != 0:
            raise RuntimeError(f"native prefix audit failed: {error.decode('utf-8', errors='replace').strip()[:500]}")
        native = loads(output, object_pairs_hook=_unique_object)
        if (not isinstance(native, dict) or set(native) != {"schema", "complete", "prefix_closed", "nodes_read", "peak_stack"} or
                native["schema"] != "hot-l1-native-prefix-v1" or native["complete"] is not True or native["prefix_closed"] is not True or
                type(native["nodes_read"]) is not int or native["nodes_read"] != count or
                type(native["peak_stack"]) is not int or not 1 <= native["peak_stack"] <= count):
            raise RuntimeError("native prefix audit output disagrees with the complete source")
    except BaseException as error:
        try:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        finally:
            ch.exec(f"KILL QUERY WHERE query_id={lit(query_id)} SYNC", fmt=None)
        if isinstance(error, BrokenPipeError) and child is not None:
            reason = child.stderr.read(4096).decode("utf-8", errors="replace").strip()[:500]
            raise RuntimeError(f"native prefix audit refused its input: {reason}") from error
        raise
    finally:
        try:
            if source is not None:
                source.close()
            if child is not None:
                for pipe in (child.stdin, child.stdout, child.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except BrokenPipeError:
                            # An early refusing auditor no longer consumes stdin.
                            pass
        finally:
            reader.close()
    return {"schema": "hot-l1-prefix-proof-v1", "complete": True, "prefix_closed": True, "target": target, "date": date,
            "snapshot_db": db, "nodes_read": count, "buckets": [{"pre": lo, "post": hi, "path": path} for lo, hi, path in buckets],
            "accepted_artifact": {"path": str(accepted), "sha256": sha256(data).hexdigest(), "bytes": len(data)},
            "source_contract": SOURCE_CONTRACT, "source_query_id": query_id, "native": native, "audit_s": round(monotonic() - start, 6)}


def bench(
    url: str,
    target: str,
    date: str,
    accepted: Path,
    out: Path,
    *,
    binary: Path,
    seconds: int = 1800,
) -> dict:
    if out.exists():
        raise ValueError("prefix proof output must be new")
    if type(seconds) is not int or not 1 <= seconds <= 3600:
        raise ValueError("prefix audit deadline must be in 1..3600 seconds")
    ch = Ch(url, db=target, timeout=seconds + 60, max_threads=4, max_memory_usage=2 << 30,
            max_execution_time=seconds, timeout_before_checking_execution_speed=0, timeout_overflow_mode="throw",
            max_bytes_before_external_sort=256 << 20, max_bytes_ratio_before_external_sort=0,
            max_temporary_data_on_disk_size_for_query=8 << 30)
    try:
        result = audit(ch, target, date, accepted, binary=binary)
        output = out.open("x")
        try:
            with output:
                output.write(dumps(result) + "\n")
        except BaseException:
            out.unlink()  # Only the new file exclusively created above is owned.
            raise
        return result
    finally:
        ch.close()
