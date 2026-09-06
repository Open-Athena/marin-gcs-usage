"""`_d1_query` transient-error retry (the 2026-09-01 [team]-variant 401)."""

from __future__ import annotations

import io
import urllib.error
import urllib.request

import pytest

from gcs_usage import index_footer
from gcs_usage.index_footer import D1_RETRIES, _d1_query


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.cloudflare.com/...",
        code=code,
        msg="err",
        hdrs=None,
        fp=io.BytesIO(b'{"success":false,"errors":[{"code":10000,"message":"Authentication error"}]}'),
    )


class _Resp:
    def read(self) -> bytes:
        return b'{"success": true, "result": []}'


def test_d1_query_retries_transient_401(monkeypatch):
    """Two spurious 401s then success: exactly 3 attempts, no exception."""
    monkeypatch.setattr(index_footer, "D1_RETRY_SLEEP", 0.0)
    calls: list[str] = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        if len(calls) <= 2:
            raise _http_error(401)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    _d1_query("SELECT 1", acct="acct", tok="tok")
    assert calls == [
        "https://api.cloudflare.com/client/v4/accounts/acct/d1/database/" + index_footer.D1_DB_ID + "/query",
    ] * 3


def test_d1_query_persistent_401_raises_after_all_retries(monkeypatch):
    monkeypatch.setattr(index_footer, "D1_RETRY_SLEEP", 0.0)
    calls: list[int] = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise _http_error(401)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as ei:
        _d1_query("SELECT 1", acct="acct", tok="tok")
    assert str(ei.value) == (
        'D1 query failed (401): {"success":false,"errors":'
        '[{"code":10000,"message":"Authentication error"}]}'
    )
    assert calls == [1] * (D1_RETRIES + 1)


def test_d1_query_non_retryable_status_fails_fast(monkeypatch):
    """A 400 (bad SQL / bad scope shape) is not transient — one attempt only."""
    monkeypatch.setattr(index_footer, "D1_RETRY_SLEEP", 0.0)
    calls: list[int] = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise _http_error(400)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError):
        _d1_query("SELECT 1", acct="acct", tok="tok")
    assert calls == [1]


# --- compact `rg_json` (2026-09-06) ------------------------------------------

import json
import sqlite3

import pyarrow as pa
import pyarrow.parquet as pq

from gcs_usage.index_footer import COMPACT_SQL, _group_rows, _schema_json


def _write_index(path) -> "pq.FileMetaData":
    """A two-group path-index-shaped parquet (dictionary + plain columns)."""
    t = pa.table({
        "path": pa.array([f"marin-b/d{i}" for i in range(6)]),
        "depth": pa.array([2] * 6, pa.int64()),
        "usr": pa.array([None, "u1", "u1", None, "u2", "u2"]),
        "b": pa.array([10, 20, 30, 40, 50, 60], pa.int64()),
    })
    pq.write_table(t, path, row_group_size=4, compression="snappy", use_dictionary=["depth", "usr"])
    return pq.ParquetFile(path).metadata


def test_group_rows_compact_form(tmp_path):
    md = _write_index(tmp_path / "i.parquet")
    rows = _group_rows(md)
    assert [(r["rg"], r["row_start"], r["row_end"], r["d_min"], r["d_max"], r["p_min"], r["p_max"], r["b_max"], r["u_min"], r["u_max"]) for r in rows] == [
        (0, 0, 4, 2, 2, "marin-b/d0", "marin-b/d3", 40, "u1", "u1"),
        (1, 4, 6, 2, 2, "marin-b/d4", "marin-b/d5", 60, "u2", "u2"),
    ]
    assert set(rows[0]) == {"rg", "d_min", "d_max", "p_min", "p_max", "b_max", "u_min", "u_max", "row_start", "row_end", "rg_json"}
    for r in rows:
        rg = md.row_group(r["rg"])
        expected = [
            rg.num_rows,
            "SNAPPY",
            [[c.data_page_offset, c.total_compressed_size, c.dictionary_page_offset or 0] for c in (rg.column(i) for i in range(rg.num_columns))],
        ]
        assert json.loads(r["rg_json"]) == expected
        assert r["rg_json"] == json.dumps(expected, separators=(",", ":"))
    # Dictionary columns carry a dictionary page offset; plain ones store 0.
    cols = json.loads(rows[0]["rg_json"])[2]
    assert [bool(c[2]) for c in cols] == [False, True, True, False]
    assert [el["name"] for el in _schema_json(md)["schema"][1:]] == ["path", "depth", "usr", "b"]


def _verbose_rg_json(md: "pq.FileMetaData", g: int) -> str:
    """The pre-2026-09-06 thrift-shaped row (what older D1 rows hold)."""
    rg = md.row_group(g)
    cols = []
    for c in range(rg.num_columns):
        cc = rg.column(c)
        m = {
            "type": cc.physical_type, "encodings": list(cc.encodings), "path_in_schema": cc.path_in_schema.split("."),
            "codec": cc.compression, "num_values": str(cc.num_values),
            "total_uncompressed_size": str(cc.total_uncompressed_size), "total_compressed_size": str(cc.total_compressed_size),
            "data_page_offset": str(cc.data_page_offset),
        }
        if cc.dictionary_page_offset is not None:
            m["dictionary_page_offset"] = str(cc.dictionary_page_offset)
        cols.append({"file_offset": str(cc.file_offset), "meta_data": m})
    return json.dumps({"columns": cols, "total_byte_size": str(rg.total_byte_size), "num_rows": str(rg.num_rows)}, separators=(",", ":"))


def test_compact_sql_rewrites_verbose_rows_to_the_synced_form(tmp_path):
    md = _write_index(tmp_path / "i.parquet")
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE index_groups (date TEXT, variant TEXT, rg INTEGER, rg_json TEXT)")
    for g in range(md.num_row_groups):
        con.execute("INSERT INTO index_groups VALUES ('2026-09-01', 'path', ?, ?)", (g, _verbose_rg_json(md, g)))
    con.execute("INSERT INTO index_groups VALUES ('2026-09-02', 'path', 0, ?)", (_verbose_rg_json(md, 0),))
    con.execute(COMPACT_SQL.format(date="2026-09-01", variant="path"))
    got = con.execute("SELECT date, rg, rg_json FROM index_groups ORDER BY date, rg").fetchall()
    fresh = {r["rg"]: r["rg_json"] for r in _group_rows(md)}
    assert got == [
        ("2026-09-01", 0, fresh[0]),
        ("2026-09-01", 1, fresh[1]),
        ("2026-09-02", 0, _verbose_rg_json(md, 0)),  # other scans untouched
    ]
    # Idempotent: compact rows don't match the verbose-form predicate.
    con.execute(COMPACT_SQL.format(date="2026-09-01", variant="path"))
    assert con.execute("SELECT rg_json FROM index_groups WHERE date='2026-09-01' ORDER BY rg").fetchall() == [(fresh[0],), (fresh[1],)]


def test_sync_d1_packs_inserts_greedily_under_the_byte_limit(tmp_path, monkeypatch):
    from gcs_usage.index_footer import sync_d1

    md = _write_index(tmp_path / "i.parquet")
    rows = [dict(r) for r in _group_rows(md) * 6]  # 12 group rows (copies; packing only cares about size)
    for i, r in enumerate(rows):
        r["rg"] = i
    monkeypatch.setattr(index_footer, "extract", lambda _p: ({"version": 1, "schema": []}, rows))
    monkeypatch.setattr(index_footer, "_creds", lambda: ("tok", "acct"))
    sent: list[str] = []
    monkeypatch.setattr(index_footer, "_d1_query", lambda sql, acct, tok, db_id: sent.append(sql) or [])
    limit = 900
    n = sync_d1("2026-09-01", "x.parquet", variant="path", insert_bytes=limit)
    assert n == 12
    assert sent[:2] == [
        "DELETE FROM index_schema WHERE date='2026-09-01' AND variant='path';",
        "DELETE FROM index_groups WHERE date='2026-09-01' AND variant='path';",
    ]
    assert sent[-1] == "INSERT INTO index_schema (date, variant, version, schema_json, floor_bytes) VALUES ('2026-09-01', 'path', 1, '[]', NULL);"
    inserts = sent[2:-1]
    head = "INSERT OR REPLACE INTO index_groups (date, variant, rg, d_min, d_max, p_min, p_max, b_max, u_min, u_max, row_start, row_end, rg_json) VALUES "
    tuples = [stmt[len(head):-1].split("),(") for stmt in inserts]
    assert all(stmt.startswith(head) and stmt.endswith(";") and len(stmt) <= limit for stmt in inserts)
    assert [len(t) for t in tuples] == [5, 5, 2]  # 12 rows, five ~150-byte tuples per 900-byte statement
    # Greedy: no statement could have taken the next one's first tuple.
    for stmt, nxt in zip(inserts, inserts[1:]):
        first = nxt[len(head):].split("),(")[0] + ")"
        assert len(stmt) + 1 + len(first) > limit
    assert [t.split(", ")[2] for stmt in tuples for t in stmt] == [str(i) for i in range(12)]
