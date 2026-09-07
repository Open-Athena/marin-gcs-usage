"""The A.3 gate: DT's `import -e duckdb --label usr` layer-2 (or its `dirs`
tier) a2a against mgu's floor-free path index, one bucket at a time.

Both are one row per `(path, usr)` with descendant-inclusive sums; DT's
paths are relative to the bucket (`''` = root) where mgu's carry it
(`<bucket>[/…]`, the bucket itself at depth 1). Compared columns:

  mgu   b     o        c2 c3 c4                       wts / wb
  DT    size  n_files  sum_storage_class_id_{2,3,4}   mtime_mean

`mtime_mean` is compared to `wts / wb` within two seconds: DT weights the
exact epoch, mgu weights `epoch(created)::BIGINT` per object (rounded), so
a subtree of same-fraction timestamps drifts by up to a second. A class
pivot DT's file lacks (the bucket has no bytes in that class, or no `-p`)
is compared against 0, and said so; mgu leaves an empty pivot NULL. DT's
root is `.`; mgu's is the bucket. `o` also reports Σ(mgu − DT): GCS folder
placeholders (zero-byte objects named `…/`) are objects to mgu and dir
markers to DT, so the sum should equal their count.
"""
from __future__ import annotations

import duckdb

CLASS_COLS = {"c2": "sum_storage_class_id_2", "c3": "sum_storage_class_id_3", "c4": "sum_storage_class_id_4"}


def compare(bucket: str, index_path: str, dt_path: str, top: int = 10) -> dict:
    con = duckdb.connect()
    dt_cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{dt_path}') LIMIT 0").fetchall()}
    if not {"path", "usr", "size", "n_files"} <= dt_cols:
        raise ValueError(f"{dt_path}: need path, usr, size, n_files (has {sorted(dt_cols)})")
    classes = {m: d for m, d in CLASS_COLS.items() if d in dt_cols}
    has_mtime = "mtime_mean" in dt_cols
    b_esc = bucket.replace("'", "''")
    con.execute(
        f"""
        CREATE TEMP TABLE m AS
        SELECT CASE WHEN path = '{b_esc}' THEN '' ELSE substr(path, {len(bucket) + 2}) END AS path,
               usr, b, o, wts, wb, c2, c3, c4
        FROM read_parquet('{index_path}')
        WHERE path = '{b_esc}' OR path LIKE '{b_esc}/%'
        """
    )
    con.execute(f"CREATE TEMP TABLE d AS SELECT * REPLACE (CASE WHEN path = '.' THEN '' ELSE path END AS path) FROM read_parquet('{dt_path}')")
    for m, dc in CLASS_COLS.items():
        if dc not in dt_cols:
            con.execute(f"ALTER TABLE d ADD COLUMN {dc} BIGINT DEFAULT 0")
    con.execute(
        """
        CREATE TEMP TABLE j AS
        SELECT COALESCE(m.path, d.path) AS path, COALESCE(m.usr, d.usr) AS usr,
               m.path IS NOT NULL AS in_m, d.path IS NOT NULL AS in_d, m.*, d.* EXCLUDE (path, usr)
        FROM m FULL OUTER JOIN d ON m.path = d.path AND m.usr IS NOT DISTINCT FROM d.usr
        """
    )
    n = lambda sql: con.execute(sql).fetchone()[0]
    rows = lambda sql: [dict(zip([c[0] for c in con.description], r)) for r in con.execute(sql).fetchall()]
    report: dict = {
        "bucket": bucket,
        "rows": {"mgu": n("SELECT count(*) FROM m"), "dt": n("SELECT count(*) FROM d"), "both": n("SELECT count(*) FROM j WHERE in_m AND in_d")},
        "root": {
            "mgu": rows("SELECT usr, b, o FROM m WHERE path = '' ORDER BY usr NULLS FIRST"),
            "dt": rows("SELECT usr, size AS b, n_files AS o FROM d WHERE path = '' ORDER BY usr NULLS FIRST"),
        },
        "only": {
            "mgu": {"n": n("SELECT count(*) FROM j WHERE NOT in_d"), "examples": rows(f"SELECT path, usr, b, o FROM j WHERE NOT in_d ORDER BY b DESC LIMIT {top}")},
            "dt": {"n": n("SELECT count(*) FROM j WHERE NOT in_m"), "examples": rows(f"SELECT path, usr, size AS b, n_files AS o FROM j WHERE NOT in_m ORDER BY size DESC LIMIT {top}")},
        },
        "mismatch": {},
        "against_zero": [m for m, dc in CLASS_COLS.items() if dc not in dt_cols],
        "skipped": [] if has_mtime else ["mtime"],
    }
    checks = {"b": ("b", "size"), "o": ("o", "n_files"), **{m: (f"COALESCE({m}, 0)", f"COALESCE({d}, 0)") for m, d in CLASS_COLS.items()}}
    for name, (mc, dc) in checks.items():
        where = f"in_m AND in_d AND {mc} IS DISTINCT FROM {dc}"
        report["mismatch"][name] = {
            "n": n(f"SELECT count(*) FROM j WHERE {where}"),
            "examples": rows(f"SELECT path, usr, {mc} AS mgu, {dc} AS dt FROM j WHERE {where} ORDER BY abs({mc} - {dc}) DESC LIMIT {top}"),
        }
    report["mismatch"]["o"]["delta_sum"] = n("SELECT COALESCE(sum(o - n_files), 0) FROM j WHERE in_m AND in_d")
    if has_mtime:
        where = "in_m AND in_d AND (wb > 0 OR mtime_mean IS NOT NULL) AND abs(COALESCE(wts / NULLIF(wb, 0), 0) - COALESCE(mtime_mean, 0)) > 2"
        report["mismatch"]["mtime"] = {
            "n": n(f"SELECT count(*) FROM j WHERE {where}"),
            "examples": rows(f"SELECT path, usr, wts / NULLIF(wb, 0) AS mgu, mtime_mean AS dt FROM j WHERE {where} ORDER BY abs(COALESCE(wts / NULLIF(wb, 0), 0) - COALESCE(mtime_mean, 0)) DESC LIMIT {top}"),
        }
    report["ok"] = report["only"]["mgu"]["n"] == 0 and report["only"]["dt"]["n"] == 0 and all(v["n"] == 0 for v in report["mismatch"].values())
    return report


def render(r: dict) -> str:
    out = [f"{r['bucket']}: rows mgu={r['rows']['mgu']:,} dt={r['rows']['dt']:,} both={r['rows']['both']:,}"]
    for side in ("mgu", "dt"):
        out.append(f"  root {side}: " + "; ".join(f"{x['usr'] or '∅'} b={x['b']:,} o={x['o']:,}" for x in r["root"][side]))
    for side in ("mgu", "dt"):
        o = r["only"][side]
        out.append(f"  only {side}: {o['n']:,}" + "".join(f"\n    {e['path'] or '(root)'} [{e['usr'] or '∅'}] b={e['b']:,} o={e['o']:,}" for e in o["examples"]))
    for col, m in r["mismatch"].items():
        out.append(f"  {col} mismatches: {m['n']:,}" + (f" (Σ mgu−dt = {m['delta_sum']:,})" if "delta_sum" in m else "") + "".join(f"\n    {e['path'] or '(root)'} [{e['usr'] or '∅'}] mgu={e['mgu']} dt={e['dt']}" for e in m["examples"]))
    if r["against_zero"]:
        out.append(f"  class pivots absent from the DT file, compared against 0: {', '.join(r['against_zero'])}")
    if r["skipped"]:
        out.append(f"  skipped (not in DT file): {', '.join(r['skipped'])}")
    out.append("  OK: exact" if r["ok"] else "  DIFFERENT")
    return "\n".join(out)
