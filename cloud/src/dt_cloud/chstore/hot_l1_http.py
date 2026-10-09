"""Isolated dev HTTP reads from loaded batch artifacts, never a source scan.

One synchronous bounded-memory handler serves tiny root bodies; no per-request
waiting thread pool. This is not a replacement for canonical TM serving or a
production deployment configuration. Health exposes registry metadata only.
"""

from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, HTTPServer
from ipaddress import ip_address
from json import dumps
from os import environ
from pathlib import Path
from re import search
from socket import AF_INET6
from sys import stderr
from typing import Iterable
from urllib.parse import parse_qsl, urlsplit

from .hot_l1_batch_catalog import HotL1BatchCatalog
from .hot_l1_catalog import CatalogRequest


def token_from_env(var: str) -> str | None:
    """Same token environment semantics as the existing query box."""
    return environ.get(var) or None


def _loopback(bind: str) -> bool:
    try:
        return ip_address(bind).is_loopback
    except ValueError:
        return False


def query_catalog(catalog: HotL1BatchCatalog, raw_query: str) -> dict:
    """The shared strict root-only request contract; no scan fallback."""
    try:
        if search(r"%(?![0-9a-fA-F]{2})", raw_query):
            raise ValueError("invalid percent encoding")
        pairs = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=4)
    except ValueError as error:
        raise CatalogRequest("invalid query parameters") from error
    query = {}
    for name, value in pairs:
        if name not in ("date", "name", "from", "path"):
            raise CatalogRequest("unknown query parameter")
        if name in query:
            raise CatalogRequest("duplicate query parameter")
        query[name] = value
    if not query.get("date") or not query.get("name"):
        raise CatalogRequest("date and name are required and must be nonempty")
    if "from" in query and not query["from"]:
        raise CatalogRequest("from must be nonempty when provided")
    return (catalog.diff(query["from"], query["date"], query["name"], path=query.get("path", "")) if "from" in query else
            catalog.view(query["date"], query["name"], path=query.get("path", "")))


def make_handler(catalog: HotL1BatchCatalog, token: str | None) -> type[BaseHTTPRequestHandler]:
    if token == "":
        raise ValueError("hot L1 bearer token must not be empty")

    class Handler(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, format: str, *args: object) -> None:
            # Never log bearer credentials or private query strings/totals.
            pass

        def _authed(self) -> bool:
            if token is None:
                return _loopback(self.client_address[0])
            headers = self.headers.get_all("Authorization", [])
            return len(headers) == 1 and headers[0].startswith("Bearer ") and compare_digest(headers[0][7:].encode(), token.encode())

        def _send(self, status: int, body: dict) -> None:
            data = (dumps(body) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Query-Engine", "hot-l1-catalog")
            if status == 401:
                self.send_header("WWW-Authenticate", 'Bearer realm="hot-l1"')
            if status == 405:
                self.send_header("Allow", "GET")
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                # The disconnected reader cannot receive its completed body.
                pass

        def do_GET(self) -> None:  # noqa: N802
            try:
                url = urlsplit(self.path)
            except ValueError:
                return self._send(400, {"error": "invalid request target"})
            if url.path == "/healthz":
                if url.query:
                    return self._send(400, {"error": "healthz accepts no query parameters"})
                return self._send(200, {"state": "ready", "catalog": catalog.metadata()})
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            if url.path != "/api/hot-l1":
                return self._send(404, {"error": "not found"})
            try:
                body = query_catalog(catalog, url.query)
            except CatalogRequest as error:
                return self._send(400, {"error": str(error)})
            self._send(200, body)

        def _unsupported(self) -> None:
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            self._send(405, {"error": "only GET is supported"})

        do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_TRACE = do_CONNECT = _unsupported

    return Handler


def serve(
    artifacts: Iterable[Path] = (),
    *,
    generation_root: Path | None = None,
    bind: str = "127.0.0.1",
    port: int = 8082,
    token: str | None,
) -> None:
    if token is None and not _loopback(bind):
        raise ValueError("hot L1 no-auth serving requires a numeric loopback bind")
    if token == "":
        raise ValueError("hot L1 bearer token must not be empty")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("hot L1 port must be in 1..65535")
    artifacts = tuple(artifacts)
    if bool(artifacts) == (generation_root is not None):
        raise ValueError("hot L1 serving requires either explicit artifacts or generation_root, not both")
    if generation_root is None:
        catalog = HotL1BatchCatalog.load(artifacts)
    else:
        from .hot_l1_publish import load

        catalog = load(generation_root)
    server_class = HTTPServer
    try:
        if ip_address(bind).version == 6:
            class IPv6Server(HTTPServer):
                address_family = AF_INET6
            server_class = IPv6Server
    except ValueError:
        # An authenticated explicit hostname bind is a valid socket input.
        pass
    with server_class((bind, port), make_handler(catalog, token)) as server:
        print(f"serve-hot-l1: listening on {bind}:{port} ({'bearer auth' if token else 'loopback NO auth'})", file=stderr, flush=True)
        server.serve_forever()
