"""A ClickHouse server for the store's tests: `$CLICKHOUSE_URL` if set (a
running server), else one started from `$CLICKHOUSE_BIN` / `clickhouse` on
PATH in a temp dir; neither = the tests skip. Each module gets its own
database."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

import pytest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ping(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/ping", timeout=1) as r:
            return r.read().strip() == b"Ok."
    except OSError:
        return False


@pytest.fixture(scope="session")
def ch_url(tmp_path_factory):
    url = os.environ.get("CLICKHOUSE_URL")
    if url:
        if not _ping(url):
            pytest.skip(f"$CLICKHOUSE_URL {url} doesn't answer")
        yield url
        return
    binary = os.environ.get("CLICKHOUSE_BIN") or shutil.which("clickhouse")
    if not binary:
        pytest.skip("no ClickHouse ($CLICKHOUSE_URL, $CLICKHOUSE_BIN or `clickhouse` on PATH)")
    d: Path = tmp_path_factory.mktemp("clickhouse")
    http, tcp = _free_port(), _free_port()
    args = [binary, "server", "--", f"--path={d}/data/", f"--http_port={http}", f"--tcp_port={tcp}", "--mysql_port=0", "--postgresql_port=0",
            "--logger.console=0", f"--logger.log={d}/log.txt", f"--logger.errorlog={d}/err.txt", f"--user_files_path={d}/files/"]
    p = subprocess.Popen(args, cwd=d, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{http}"
    t0 = time.monotonic()
    while not _ping(url):
        if p.poll() is not None or time.monotonic() - t0 > 60:
            p.kill()
            pytest.skip(f"ClickHouse didn't start (see {d}/err.txt)")
        time.sleep(0.2)
    yield url
    p.terminate()
    p.wait(30)


@pytest.fixture(scope="module")
def ch_db(ch_url):
    from dt_cloud.chstore.client import Ch

    db = f"t_{uuid.uuid4().hex[:10]}"
    Ch(ch_url, db="default", session=False).exec(f"CREATE DATABASE {db}")
    yield db
    Ch(ch_url, db="default", session=False).exec(f"DROP DATABASE IF EXISTS {db} SYNC")
