"""Extract a parquet's footer as hyparquet-compatible JSON (schema + per-row-group
column metadata) and sync it to the site's D1, so the Cloudflare reader can build
a subset ``FileMetaData`` for a prefix query without parsing the whole footer on a
cold isolate (specs/path-agnostic-serving.md §2.1 — the footer-in-D1 seam).

Only the fields a *read* needs are kept (offsets/sizes/codec/encodings/type), plus
per-group (depth, path, bytes) min/max for row-group pruning. BigInts serialize as
strings; the reader revives them.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import pyarrow.parquet as pq

# Physical/logical strings pyarrow emits already match parquet-thrift (and thus
# hyparquet) — no remapping table needed; we just restructure.


def _site_dir() -> Path:
    """Nearest ancestor ``site/`` holding wrangler.toml (repo layout)."""
    for base in [Path.cwd(), *Path.cwd().parents, Path(__file__).resolve().parents[3]]:
        cand = base / "site"
        if (cand / "wrangler.toml").exists():
            return cand
    raise FileNotFoundError("site/wrangler.toml not found (run from the repo)")


def _schema_json(md: "pq.FileMetaData") -> dict:
    """hyparquet `FileMetaData.schema` (root element + one leaf per column)."""
    sch = md.schema
    root = {"repetition_type": "REQUIRED", "name": sch.to_arrow_schema().pandas_metadata and "schema" or "schema", "num_children": md.num_columns}
    # The Arrow name of the root isn't load-bearing (reads key off leaf
    # path_in_schema); use a stable placeholder.
    root["name"] = "schema"
    leaves = []
    for i in range(md.num_columns):
        col = sch.column(i)
        el: dict = {"type": col.physical_type, "repetition_type": "OPTIONAL" if col.max_definition_level else "REQUIRED", "name": col.name}
        ct = col.converted_type
        if ct and ct != "NONE":
            el["converted_type"] = ct
        leaves.append(el)
    return {"version": 1, "schema": [root, *leaves]}


def _group_rows(md: "pq.FileMetaData") -> list[dict]:
    """One row per row group: pruning stats + the stripped RowGroup JSON."""
    rows = []
    row_start = 0
    di = md.schema.names.index("depth")
    pi = md.schema.names.index("path")
    bi = md.schema.names.index("b")
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        n = rg.num_rows
        cols = []
        for c in range(rg.num_columns):
            cc = rg.column(c)
            m = {
                "type": cc.physical_type,
                "encodings": list(cc.encodings),
                "path_in_schema": cc.path_in_schema.split("."),
                "codec": cc.compression,
                "num_values": str(cc.num_values),
                "total_uncompressed_size": str(cc.total_uncompressed_size),
                "total_compressed_size": str(cc.total_compressed_size),
                "data_page_offset": str(cc.data_page_offset),
            }
            if cc.dictionary_page_offset is not None:
                m["dictionary_page_offset"] = str(cc.dictionary_page_offset)
            cols.append({"file_offset": str(cc.file_offset), "meta_data": m})
        rg_json = {"columns": cols, "total_byte_size": str(rg.total_byte_size), "num_rows": str(n)}
        ds, ps, bs = rg.column(di).statistics, rg.column(pi).statistics, rg.column(bi).statistics
        rows.append({
            "rg": g,
            "d_min": int(ds.min), "d_max": int(ds.max),
            "p_min": ps.min, "p_max": ps.max,
            "b_max": int(bs.max),
            "row_start": row_start, "row_end": row_start + n,
            "rg_json": json.dumps(rg_json, separators=(",", ":")),
        })
        row_start += n
    return rows


def extract(parquet_path: str) -> tuple[dict, list[dict]]:
    """Return (schema_meta, group_rows) from a local or fsspec-readable parquet."""
    import gcsfs

    opener = gcsfs.GCSFileSystem().open if parquet_path.startswith(("gs://", "oa-")) else open
    with opener(parquet_path, "rb") as f:
        md = pq.ParquetFile(f).metadata
    return _schema_json(md), _group_rows(md)


def _sql_escape(s: str) -> str:
    return s.replace("'", "''")


# The D1 database `/query` runs one SQL string; we send multi-row INSERTs.
D1_DB_ID = "e52398b7-5538-4bc4-83db-3355a1b5ef9a"  # oa-gcs-usage-auth (site/wrangler.toml)


def _creds() -> tuple[str, str]:
    """(api_token, account_id) from the env, falling back to the repo .envrc —
    so it works in the Batch job (env) and from a laptop (direnv/.envrc)."""
    tok = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID") or os.environ.get("OA_CF_ACCT", "")
    if not (tok and acct):
        try:
            envrc = _site_dir().parent / ".envrc"
            for line in envrc.read_text().splitlines():
                m = re.match(r"^\s*export\s+(CLOUDFLARE_API_TOKEN|OA_CF_ACCT)=[\"']?([^\"'\s]+)", line)
                if m:
                    if m.group(1) == "CLOUDFLARE_API_TOKEN" and not tok:
                        tok = m.group(2)
                    if m.group(1) == "OA_CF_ACCT" and not acct:
                        acct = m.group(2)
        except FileNotFoundError:
            pass
    if not (tok and acct):
        raise RuntimeError("need CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID (or OA_CF_ACCT)")
    return tok, acct


def _d1_query(sql: str, acct: str, tok: str, db_id: str = D1_DB_ID) -> None:
    """Run one SQL string against D1 over the HTTP API (no Node/wrangler)."""
    import urllib.error
    import urllib.request

    url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/d1/database/{db_id}/query"
    req = urllib.request.Request(
        url,
        data=json.dumps({"sql": sql}).encode(),
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        # Surface D1's error body (scope/SQL) without echoing the token.
        raise RuntimeError(f"D1 query failed ({e.code}): {e.read().decode()[:300]}") from None
    if not resp.get("success"):
        raise RuntimeError(f"D1 query error: {resp.get('errors')}")


def sync_d1(
    date: str,
    parquet_path: str,
    *,
    db_id: str = D1_DB_ID,
    remote: bool = True,
    rows_per_insert: int = 8,
) -> int:
    """Extract the footer for ``date`` and upsert it into D1
    (index_schema/index_groups) over the Cloudflare **HTTP API** — pure Python,
    so it runs in the Node-less Batch image. Returns #row groups written.
    ``remote=False`` uses the local wrangler D1 (dev only, via `d1 execute`)."""
    schema, rows = extract(parquet_path)
    schema_sql = (
        f"DELETE FROM index_schema WHERE date='{date}';"
        f"DELETE FROM index_groups WHERE date='{date}';"
        "INSERT INTO index_schema (date, version, schema_json) VALUES "
        f"('{date}', {schema['version']}, '{_sql_escape(json.dumps(schema['schema'], separators=(',', ':')))}');"
    )

    def group_values(r: dict) -> str:
        return (
            f"('{date}', {r['rg']}, {r['d_min']}, {r['d_max']}, "
            f"'{_sql_escape(r['p_min'])}', '{_sql_escape(r['p_max'])}', {r['b_max']}, "
            f"{r['row_start']}, {r['row_end']}, '{_sql_escape(r['rg_json'])}')"
        )

    cols = "(date, rg, d_min, d_max, p_min, p_max, b_max, row_start, row_end, rg_json)"
    if not remote:  # dev: local wrangler D1
        stmts = [schema_sql] + [f"INSERT INTO index_groups {cols} VALUES {group_values(r)};" for r in rows]
        site = _site_dir()
        for i in range(0, len(stmts), 300):
            with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
                tf.write("\n".join(stmts[i : i + 300]))
                sqlpath = tf.name
            subprocess.run(["npx", "wrangler", "d1", "execute", "oa-gcs-usage-auth", "--local", "--file", sqlpath], check=True, cwd=str(site))
        return len(rows)

    tok, acct = _creds()
    _d1_query(schema_sql, acct, tok, db_id)
    for i in range(0, len(rows), rows_per_insert):
        chunk = rows[i : i + rows_per_insert]
        sql = f"INSERT INTO index_groups {cols} VALUES " + ",".join(group_values(r) for r in chunk) + ";"
        _d1_query(sql, acct, tok, db_id)
    return len(rows)
