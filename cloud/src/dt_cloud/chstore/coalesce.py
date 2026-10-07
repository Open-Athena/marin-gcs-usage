"""Frozen-history SQL: general episodes and an isolated two-snapshot alternative."""

import json
from pathlib import Path
from tempfile import TemporaryFile
from time import monotonic
from typing import Callable

from .client import Ch
from .schema import OPEN, dt_lit, scan_dt
from .serve import AGG_COLS


def window_query(
    parts: list[str],
    columns: list[str],
    values: list[str],
    instants: list[str],
) -> str:
    """The existing episode/window coalescer, shared with read-only experiments."""
    bounds = "[" + ", ".join(dt_lit(d) for d in [*instants, OPEN]) + "]"
    union, value_sql = " UNION ALL ".join(parts), ", ".join(values)
    source = f"""SELECT *, sum(fresh) OVER (PARTITION BY pre ORDER BY tick) AS episode FROM (
            SELECT *, tick = 0 OR previous.1 + 1 != tick OR previous.2 != tuple({value_sql}) AS fresh FROM (
                SELECT *, lagInFrame(tuple(tick, tuple({value_sql}))) OVER (
                    PARTITION BY pre ORDER BY tick ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS previous
                FROM ({union})))"""
    agg = ", ".join(f"any({c}) AS {c}" for c in columns if c != "pre")
    return f"""SELECT pre, {agg}, {bounds}[min(tick) + 1] AS vf, {bounds}[max(tick) + 2] AS vt
            FROM ({source}) GROUP BY pre, episode"""


def pair_query(
    parts: list[str],
    columns: list[str],
    values: list[str],
    instants: list[str],
) -> str:
    """One full join emits unchanged, changed, born and vanished versions.

    Exactly two selected snapshots, unique pre per input, immutable identity
    columns outside `values`, and join_use_nulls=0 are required. Intended for
    full_sorting_merge on preorder inputs. No data or publication is modified.
    """
    if len(parts) != 2 or len(instants) != 2 or instants[0] >= instants[1]:
        raise ValueError("pair coalescing requires exactly two increasing snapshot instants")
    if not columns or columns[0] != "pre" or not values:
        raise ValueError("pair coalescing requires pre-led columns and comparison values")
    changed = f"tuple({', '.join('o.' + c for c in values)}) != tuple({', '.join('n.' + c for c in values)})"
    old = ", ".join("o." + c for c in columns)
    new = ", ".join("n." + c for c in columns)
    first, second, opened = (dt_lit(d) for d in (*instants, OPEN))
    projections = ", ".join(f"v.{i + 2} AS {c}" for i, c in enumerate([*columns, "vf", "vt"]))
    return f"""SELECT {projections} FROM (
        SELECT arrayJoin(arrayFilter(v -> v.1, [
            tuple(o.present, {old}, {first}, if(n.present AND NOT ({changed}), {opened}, {second})),
            tuple(n.present AND (NOT o.present OR {changed}), {new}, {second}, {opened})
        ])) AS v
        FROM (SELECT *, toUInt8(1) AS present FROM ({parts[0]})) o
        FULL OUTER JOIN (SELECT *, toUInt8(1) AS present FROM ({parts[1]})) n ON o.pre = n.pre
    )"""


def benchmark(
    ch: Ch,
    target: str,
    starts: tuple[int, ...],
    *,
    table: str = "metadata",
    batch_rows: int = 1_000_000,
    threads: int = 2,
    trials: int = 2,
    temp_dir: Path,
    emit: Callable[[dict], None] = print,
) -> None:
    """Bounded read-only plan A/B; exact sorted bytes, not a hash or publication.

    Verification streams the old plan to a temporary dev-node file and compares
    the new plan chunk by chunk, keeping RAM bounded. Timed FORMAT Null queries
    exclude this verification. The temporary file is removed on exit.
    """
    from .narrow import disk_reserve, history_parent_source, identifier

    identifier(target)
    if table not in ("nodes", "metadata"):
        raise ValueError("coalescing benchmark table must be nodes or metadata")
    if not 0 < batch_rows <= 1_000_000 or threads <= 0 or trials <= 0:
        raise ValueError("provide positive threads/trials and batch_rows in 1..1000000")
    if not starts or len(set(starts)) != len(starts) or min(starts) < 0:
        raise ValueError("provide distinct nonnegative start IDs")
    manifest = json.loads(ch.scalar(f"SELECT doc FROM {target}.manifest"))
    dates = [scan_dt(d) for d in manifest["dates"]]
    if len(dates) != 2 or dates[0] >= dates[1]:
        raise ValueError("pair coalescing requires exactly two increasing snapshot instants")
    count = manifest["union_nodes"]
    if max(starts) >= count:
        raise ValueError("benchmark start exceeds the frozen key domain")
    dbs = [identifier(db) for db in manifest["dbs"]]
    if len(dbs) != 2:
        raise ValueError("pair coalescing requires exactly two snapshot databases")
    if table == "metadata":
        checkpoint = json.loads(ch.scalar(f"SELECT doc FROM {target}.numeric_parent_manifest"))
        if (checkpoint["target"], checkpoint["nodes"]) != (target, count):
            raise ValueError("numeric parent checkpoint differs from the frozen domain")
    disk_reserve(ch, "coalesce_bench", 64 << 30)
    temp_dir.mkdir(parents=True, exist_ok=True)
    columns = (f"pre, parent_pre, depth, path, parent, {AGG_COLS}" if table == "metadata" else "pre, post, depth, nid, b, o, path").split(", ")
    values = (AGG_COLS if table == "metadata" else "b, o").split(", ")
    settings = {"max_threads": threads, "max_block_size": 8192, "max_memory_usage": 8 << 30,
                "max_bytes_before_external_sort": 256 << 20, "max_bytes_ratio_before_external_sort": 0,
                "max_bytes_before_external_group_by": 256 << 20, "max_bytes_ratio_before_external_group_by": 0,
                "join_algorithm": "full_sorting_merge", "join_use_nulls": 0}
    for trial in range(trials):
        for index, lo in enumerate(starts):
            hi = min(lo + batch_rows, count)
            bound = f"pre >= {lo} AND pre < {hi}"
            parts = [history_parent_source(target, db, manifest["prefix"], i, bound, True) if table == "metadata"
                     else f"SELECT toUInt16({i}) AS tick, {', '.join(columns)} FROM {db}.nodes WHERE {bound}"
                     for i, db in enumerate(dbs)]
            queries = {"window": window_query(parts, columns, values, dates), "pair": pair_query(parts, columns, values, dates)}
            label = f"coalesce:{target}:{table}:{lo}:{trial}"
            order = ["window", "pair"] if (trial + index) % 2 == 0 else ["pair", "window"]
            timings = {}
            for plan in order:
                start = monotonic()
                ch.exec(queries[plan], fmt="Null", settings={**settings, "log_comment": f"{label}:{plan}"})
                timings[plan] = round(monotonic() - start, 6)
            start, size, exact = monotonic(), 0, True
            with TemporaryFile(dir=temp_dir) as baseline:
                for chunk in ch.stream(f"SELECT * FROM ({queries['window']}) ORDER BY pre, vf", "RowBinary",
                                       settings={**settings, "log_comment": f"{label}:window:verify"}):
                    baseline.write(chunk)
                    size += len(chunk)
                baseline.seek(0)
                for chunk in ch.stream(f"SELECT * FROM ({queries['pair']}) ORDER BY pre, vf", "RowBinary",
                                       settings={**settings, "log_comment": f"{label}:pair:verify"}):
                    if baseline.read(len(chunk)) != chunk:
                        exact = False
                if baseline.read(1):
                    exact = False
            emit({"target": target, "table": table, "lo": lo, "hi": hi, "trial": trial, "threads": threads,
                  "order": order, "seconds": timings, "verification_s": round(monotonic() - start, 6),
                  "exact": exact, "result_bytes": size, "publication": False,
                  "cache": "no-reset-alternating-order", "log_comment": label})
            if not exact:
                raise ValueError(f"coalesced rows differ at {table} range [{lo}, {hi})")
