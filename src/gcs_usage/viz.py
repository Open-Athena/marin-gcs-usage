"""Data extracts for the web viz (``gcs-usage webdata``).

Everything here is laptop-scale DuckDB over the deduped listing parquet:
a nested prefix tree for the treemap, created-month age strata, and a
small meta blob. Output is plain JSON consumed by ``site/``.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import duckdb

FOLD_MIN_BYTES = 20e9  # children below this fold into "(other ×N)"


def _build(rows: list[dict], levels: list[str]) -> list[dict]:
    """Nested {n,b,o,c} tree from flat dir-component rows; small children folded."""
    if not levels:
        return []
    key = levels[0]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r[key] or "", []).append(r)
    out = []
    for name, g in groups.items():
        b = sum(r["bytes"] for r in g)
        o = sum(r["objects"] for r in g)
        node: dict = {"n": name if name else "(files)", "b": b, "o": o}
        if levels[1:] and name != "":
            kids = _build(g, levels[1:])
            if len(kids) > 1 or (kids and kids[0]["n"] != "(files)"):
                node["c"] = kids
        out.append(node)
    out.sort(key=lambda n: -n["b"])
    big = [n for n in out if n["b"] >= FOLD_MIN_BYTES]
    small = [n for n in out if n["b"] < FOLD_MIN_BYTES]
    if small:
        if len(small) == 1:
            big.append(small[0])
        else:
            big.append(
                {
                    "n": f"(other ×{len(small)})",
                    "b": sum(n["b"] for n in small),
                    "o": sum(n["o"] for n in small),
                }
            )
    return big


def write_webdata(listing: str, out_dir: Path, asof: str) -> dict:
    """Write tree.json / age.json / meta.json under ``out_dir``; returns meta."""
    con = duckdb.connect()
    con.execute("SET memory_limit='6GB'")

    dir_rows = con.execute(
        f"""
        WITH d AS (
          SELECT bucket,
            CASE WHEN name LIKE '%/%' THEN regexp_replace(name, '/[^/]*$', '') ELSE '' END AS dir,
            size_bytes
          FROM read_parquet('{listing}')
        )
        SELECT bucket,
          coalesce(regexp_extract(dir, '^([^/]+)', 1), '') AS d1,
          coalesce(regexp_extract(dir, '^[^/]+/([^/]+)', 1), '') AS d2,
          coalesce(regexp_extract(dir, '^[^/]+/[^/]+/([^/]+)', 1), '') AS d3,
          sum(size_bytes)::BIGINT AS bytes, count(*)::BIGINT AS objects
        FROM d GROUP BY ALL
        """
    ).fetchall()
    cols = ["bucket", "d1", "d2", "d3", "bytes", "objects"]
    rows = [dict(zip(cols, r)) for r in dir_rows]

    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault(r["bucket"], []).append(r)
    roots = [
        {
            "n": bucket,
            "b": sum(r["bytes"] for r in g),
            "o": sum(r["objects"] for r in g),
            "c": _build(g, ["d1", "d2", "d3"]),
        }
        for bucket, g in buckets.items()
    ]
    roots.sort(key=lambda n: -n["b"])
    total_b = sum(n["b"] for n in roots)
    total_o = sum(n["o"] for n in roots)
    tree = {"n": "marin GCS", "b": total_b, "o": total_o, "c": roots}

    age = con.execute(
        f"""
        SELECT strftime(created, '%Y-%m') AS created_month,
          regexp_extract(name, '^([^/]+)/', 1) AS d1,
          sum(size_bytes)::BIGINT AS bytes, count(*)::BIGINT AS objects
        FROM read_parquet('{listing}')
        WHERE created IS NOT NULL
        GROUP BY ALL ORDER BY created_month
        """
    ).fetchall()
    age_rows = [
        {"m": m, "d1": d1 or "(files)", "b": b, "o": o} for m, d1, b, o in age
    ]

    classes = con.execute(
        f"""
        SELECT storage_class_id, sum(size_bytes)::BIGINT AS bytes
        FROM read_parquet('{listing}') GROUP BY ALL ORDER BY storage_class_id
        """
    ).fetchall()

    meta = {
        "asof": asof,
        "generated": dt.date.today().isoformat(),
        "total_bytes": total_b,
        "total_objects": total_o,
        "class_bytes": {int(c): int(b) for c, b in classes},
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "tree.json").write_text(json.dumps(tree, separators=(",", ":")) + "\n")
    (out_dir / "age.json").write_text(json.dumps(age_rows, separators=(",", ":")) + "\n")
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return meta
