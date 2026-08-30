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


def sync_d1(
    date: str,
    parquet_path: str,
    *,
    db: str = "oa-gcs-usage-auth",
    remote: bool = True,
    chunk: int = 400,
) -> int:
    """Extract footer for ``date`` and upsert into D1 (index_schema/index_groups).
    Runs `wrangler d1 execute` in chunked SQL files. Returns #groups written."""
    schema, rows = extract(parquet_path)
    stmts = [
        f"DELETE FROM index_schema WHERE date='{date}';",
        f"DELETE FROM index_groups WHERE date='{date}';",
        "INSERT INTO index_schema (date, version, schema_json) VALUES "
        f"('{date}', {schema['version']}, '{_sql_escape(json.dumps(schema['schema'], separators=(',', ':')))}');",
    ]
    for r in rows:
        stmts.append(
            "INSERT INTO index_groups (date, rg, d_min, d_max, p_min, p_max, b_max, row_start, row_end, rg_json) VALUES "
            f"('{date}', {r['rg']}, {r['d_min']}, {r['d_max']}, "
            f"'{_sql_escape(r['p_min'])}', '{_sql_escape(r['p_max'])}', {r['b_max']}, "
            f"{r['row_start']}, {r['row_end']}, '{_sql_escape(r['rg_json'])}');"
        )
    site = _site_dir()
    mode = "--remote" if remote else "--local"
    for i in range(0, len(stmts), chunk):
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
            tf.write("\n".join(stmts[i : i + chunk]))
            sqlpath = tf.name
        cmd = ["npx", "wrangler", "d1", "execute", db, mode, "--file", sqlpath]
        if remote:
            cmd.append("--yes")  # skip the remote-write confirmation prompt
        subprocess.run(cmd, check=True, cwd=str(site))
    return len(rows)
