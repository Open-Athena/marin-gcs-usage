"""Data extracts for the web viz (``gcs-usage webdata``).

Everything here is laptop-scale DuckDB over the deduped listing parquet:
a nested prefix tree for the treemap, created-month age strata, and a
small meta blob. Output is plain JSON consumed by ``site/``.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

import duckdb

FOLD_MIN_BYTES = 20e9  # children below this fold into "(other ×N)"
TOP_USERS_PER_NODE = 5


def _attr_of(g: list[dict]) -> dict:
    """Attribution summary of a row group: team-bytes map + top user-bytes."""
    tm: dict[str, int] = defaultdict(int)
    ub: dict[str, int] = defaultdict(int)
    for r in g:
        tm[r["team"]] += r["bytes"]
        if r["user"]:
            ub[r["user"]] += r["bytes"]
    top = sorted(ub.items(), key=lambda kv: -kv[1])[:TOP_USERS_PER_NODE]
    out = {"tm": dict(sorted(tm.items(), key=lambda kv: -kv[1]))}
    if top:
        out["us"] = [[u, b] for u, b in top]
    return out


def _build(rows: list[dict], levels: list[str], attr: bool = False) -> list[dict]:
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
        if attr:
            node.update(_attr_of(g))
        if levels[1:] and name != "":
            kids = _build(g, levels[1:], attr)
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
            folded: dict = {
                "n": f"(other ×{len(small)})",
                "b": sum(n["b"] for n in small),
                "o": sum(n["o"] for n in small),
            }
            if attr:
                tm: dict[str, int] = defaultdict(int)
                ub: dict[str, int] = defaultdict(int)
                for n in small:
                    for t, tb in n.get("tm", {}).items():
                        tm[t] += tb
                    for u, b_ in n.get("us", []):
                        ub[u] += b_
                folded["tm"] = dict(sorted(tm.items(), key=lambda kv: -kv[1]))
                top = sorted(ub.items(), key=lambda kv: -kv[1])[:TOP_USERS_PER_NODE]
                if top:
                    folded["us"] = [[u, b_] for u, b_ in top]
            big.append(folded)
    return big


def write_webdata(
    listing: str,
    out_dir: Path,
    asof: str,
    attributions: tuple[str, ...] = (),
    identities_path: Path | None = None,
) -> dict:
    """Write tree.json / age.json / meta.json under ``out_dir``; returns meta.

    With ``attributions``, every dir is attributed (deepest-prefix-wins, same
    join as ``report``) and each tree node carries ``tm`` (team-bytes map) and
    ``us`` (top user-bytes) for ownership overlays.
    """
    attr = bool(attributions)
    con = duckdb.connect()
    # attribution mode is node-scale (34M-dir python-side walk); plain mode
    # stays laptop-safe
    con.execute(f"SET memory_limit='{'24GB' if attr else '6GB'}'")
    if attr:
        from .identity import DEFAULT_IDENTITIES, load_identities
        from .prefixes import deepest_lookup, load_prefix_map

        identities = load_identities(identities_path or DEFAULT_IDENTITIES)
        deepest = deepest_lookup(load_prefix_map(con, attributions, identities, listing))
        dir_rows = con.execute(
            f"""
            WITH d AS (
              SELECT bucket,
                CASE WHEN name LIKE '%/%' THEN regexp_replace(name, '/[^/]*$', '') ELSE '' END AS dir,
                size_bytes
              FROM read_parquet('{listing}')
            )
            SELECT bucket, dir, sum(size_bytes)::BIGINT AS bytes, count(*)::BIGINT AS objects
            FROM d GROUP BY ALL
            """
        ).fetchall()
        agg: dict[tuple, list] = defaultdict(lambda: [0, 0])
        for bucket, dir_, nbytes, objects in dir_rows:
            row = deepest(f"{bucket}/{dir_}" if dir_ else bucket)
            user, team = (row[0], row[1]) if row else (None, "unattributed")
            parts = dir_.split("/") if dir_ else []
            key = (bucket, *((parts + ["", "", ""])[:3]), user, team)
            a = agg[key]
            a[0] += nbytes
            a[1] += objects
        cols = ["bucket", "d1", "d2", "d3", "user", "team", "bytes", "objects"]
        rows = [dict(zip(cols, (*k, b, o))) for k, (b, o) in agg.items()]
    else:
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
            **(_attr_of(g) if attr else {}),
            "c": _build(g, ["d1", "d2", "d3"], attr),
        }
        for bucket, g in buckets.items()
    ]
    roots.sort(key=lambda n: -n["b"])
    total_b = sum(n["b"] for n in roots)
    total_o = sum(n["o"] for n in roots)
    tree = {"n": "marin GCS", "b": total_b, "o": total_o, "c": roots}
    if attr:
        tree.update(_attr_of([r for g in buckets.values() for r in g]))

    if attr:
        # (month, d1, team) strata: attribute at full-dir granularity (same
        # cached deepest-prefix walk as the tree), streamed in record batches.
        reader = con.execute(
            f"""
            SELECT strftime(created, '%Y-%m') AS m, bucket,
              CASE WHEN name LIKE '%/%' THEN regexp_replace(name, '/[^/]*$', '') ELSE '' END AS dir,
              sum(size_bytes)::BIGINT AS bytes, count(*)::BIGINT AS objects
            FROM read_parquet('{listing}')
            WHERE created IS NOT NULL
            GROUP BY ALL
            """
        ).fetch_record_batch(1_000_000)
        age_agg: dict[tuple, list] = defaultdict(lambda: [0, 0])
        for batch in reader:
            d = batch.to_pydict()
            for m, bucket, dir_, nbytes, objects in zip(
                d["m"], d["bucket"], d["dir"], d["bytes"], d["objects"]
            ):
                row = deepest(f"{bucket}/{dir_}" if dir_ else bucket)
                team = row[1] if row else "unattributed"
                d1 = dir_.split("/", 1)[0] if dir_ else "(files)"
                a = age_agg[(m, d1, team)]
                a[0] += nbytes
                a[1] += objects
        age_rows = [
            {"m": m, "d1": d1, "t": t, "b": b, "o": o}
            for (m, d1, t), (b, o) in sorted(age_agg.items())
        ]
    else:
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
