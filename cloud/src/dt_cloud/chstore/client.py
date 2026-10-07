"""A small ClickHouse HTTP client (stdlib only): statements, TSV / JSON rows,
streamed results and streamed inserts, one session per `Ch` (so temporary
tables live across one request's statements).

Credentials come from `$CLICKHOUSE_USER` / `$CLICKHOUSE_PASSWORD` (sent as
headers, never logged); none = the server's `default` user."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Iterable, Iterator

DEFAULT_URL = "http://localhost:8123"


def rowbinary_strings(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Decode a single RowBinary String column without collecting the result.

    Keep only an incomplete record between chunks. Reject malformed UInt64
    length prefixes and oversized records rather than buffering indefinitely.
    """
    pending = b""
    for chunk in chunks:
        data, pos = pending + chunk, 0
        while pos < len(data):
            start, length, shift = pos, 0, 0
            while pos < len(data):
                byte = data[pos]
                pos += 1
                if shift > 63 or (shift == 63 and byte > 1):
                    raise ValueError("invalid RowBinary string length")
                length |= (byte & 127) << shift
                if not byte & 128:
                    break
                shift += 7
            else:
                pos = start
                break
            if length > 64 << 20:
                raise ValueError("RowBinary string exceeds 64 MiB")
            end = pos + length
            if end > len(data):
                pos = start
                break
            yield data[pos:end]
            pos = end
        pending = data[pos:]
    if pending:
        raise ValueError("truncated RowBinary string stream")


class ChError(RuntimeError):
    def __init__(self, status: int, text: str, sql: str):
        super().__init__(f"ClickHouse {status}: {text[:2000]}\n--- {sql[:1500]}")
        self.status, self.text = status, text


def lit(s: str) -> str:
    """A ClickHouse string literal (backslash escapes)."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def statement_timeout_settings(seconds: int) -> dict[str, str]:
    """A cooperative wall-clock query limit that throws, never returns partials.

    ClickHouse cannot interrupt every aggregation/analysis phase immediately;
    callers must still inspect server activity after a transport timeout.
    """
    if seconds <= 0:
        raise ValueError("statement timeout must be positive")
    return {"max_execution_time": str(seconds), "timeout_before_checking_execution_speed": "0", "timeout_overflow_mode": "throw"}


class Ch:
    """One ClickHouse session over HTTP. `settings` ride on every statement."""

    def __init__(self, url: str = DEFAULT_URL, *, db: str = "default", session: bool = True, timeout: float = 900, **settings: object):
        self.url = url.rstrip("/")
        self.db = db
        self.timeout = timeout
        self.settings: dict[str, str] = {"database": db, **{k: str(v) for k, v in settings.items()}}
        if session:
            self.settings.update(session_id=uuid.uuid4().hex, session_timeout="600")
        self._tmp: list[str] = []
        self.headers: dict[str, str] = {}
        user, pw = os.environ.get("CLICKHOUSE_USER"), os.environ.get("CLICKHOUSE_PASSWORD")
        if user:
            self.headers["X-ClickHouse-User"] = user
        if pw:
            self.headers["X-ClickHouse-Key"] = pw

    def fork(self, **settings: object) -> "Ch":
        """A new session on the same server and database."""
        base = {k: v for k, v in self.settings.items() if k not in ("database", "session_id", "session_timeout")}
        return Ch(self.url, db=self.db, timeout=self.timeout, **{**base, **settings})

    def _req(self, sql: str | None, data: bytes | Iterable[bytes] | None, settings: dict | None) -> urllib.request.Request:
        params = {**self.settings, **{k: str(v) for k, v in (settings or {}).items()}}
        if sql is not None and data is not None:
            params["query"] = sql
        body = data if data is not None else (sql or "").encode()
        return urllib.request.Request(f"{self.url}/?{urllib.parse.urlencode(params)}", data=body, method="POST", headers=self.headers)

    def _open(self, sql: str, data=None, settings: dict | None = None):
        try:
            return urllib.request.urlopen(self._req(sql, data, settings), timeout=self.timeout)
        except urllib.error.HTTPError as e:
            raise ChError(e.code, e.read().decode(errors="replace"), sql) from None

    def exec(self, sql: str, *, fmt: str | None = "TSV", settings: dict | None = None) -> str:
        """Run a statement; a SELECT gets `FORMAT fmt` appended."""
        q = sql.strip().rstrip(";")
        if fmt and q.split(None, 1)[0].upper() in ("SELECT", "WITH"):
            q += f" FORMAT {fmt}"
        with self._open(q, settings=settings) as r:
            return r.read().decode()

    def rows(self, sql: str, settings: dict | None = None) -> list[list[str]]:
        return [line.split("\t") for line in self.exec(sql, settings=settings).splitlines()]

    def one(self, sql: str, settings: dict | None = None) -> list[str]:
        r = self.rows(sql, settings)
        return r[0] if r else []

    def scalar(self, sql: str, settings: dict | None = None) -> str | None:
        r = self.one(sql, settings)
        return r[0] if r else None

    def json(self, sql: str, settings: dict | None = None) -> list[list]:
        """Rows as JSON arrays (`JSONCompactEachRow`): typed values, nested arrays and maps."""
        out = self.exec(sql, fmt="JSONCompactEachRow", settings={"output_format_json_quote_64bit_integers": 0, **(settings or {})})
        return [json.loads(line) for line in out.splitlines()]

    def stream(self, sql: str, fmt: str = "TSVRaw", settings: dict | None = None, chunk: int = 1 << 20) -> Iterator[bytes]:
        """A SELECT's output as raw byte chunks."""
        q = sql.strip().rstrip(";") + f" FORMAT {fmt}"
        with self._open(q, settings=settings) as r:
            while True:
                b = r.read(chunk)
                if not b:
                    return
                yield b

    def insert(self, sql: str, data: Iterable[bytes], settings: dict | None = None) -> str:
        """`INSERT … FORMAT …` with the data streamed as the request body (chunked)."""
        try:
            with urllib.request.urlopen(self._req(sql, data, settings), timeout=self.timeout) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            raise ChError(e.code, e.read().decode(errors="replace"), sql) from None

    def tmp(
        self,
        name: str,
        sql: str,
        settings: dict | None = None,
        *,
        disk: bool = False,
        ordered: bool = True,
        order_by: str | tuple[str, ...] | None = None,
        set_index: bool = False,
    ) -> None:
        """A session temporary table from a SELECT. `disk` uses a MergeTree for results that can
        hold millions of rows. Keep path ordering for bounded alphabetical reads, but candidates
        consumed in full can avoid sorting with `ordered=False`. The default is Memory."""
        if order_by is not None:
            columns = (order_by,) if isinstance(order_by, str) else order_by
            if not disk or not isinstance(columns, tuple) or not columns or any(
                not isinstance(column, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", column) for column in columns
            ):
                raise ValueError("temporary order key requires disk and SQL identifiers")
        if set_index and (disk or order_by is not None):
            raise ValueError("Set temporary table cannot also use a disk order")
        self.exec(f"DROP TEMPORARY TABLE IF EXISTS {name}", settings=settings)
        key = f"({','.join(order_by)})" if isinstance(order_by, tuple) else order_by or ("path" if ordered else "tuple()")
        engine = "Set SETTINGS persistent = 0" if set_index else f"MergeTree ORDER BY {key}" if disk else "Memory"
        self._tmp.append(name)
        self.exec(f"CREATE TEMPORARY TABLE {name} ENGINE = {engine} AS {sql}", settings=settings)

    def close(self) -> None:
        """Drop this session's temporary tables now instead of retaining their RAM/disk until its timeout."""
        for name in reversed(self._tmp):
            try:
                self.exec(f"DROP TEMPORARY TABLE IF EXISTS {name}")
            except (OSError, RuntimeError):
                # The session timeout remains the cleanup fallback when the
                # client disconnected or ClickHouse itself became unavailable.
                pass
        self._tmp.clear()
