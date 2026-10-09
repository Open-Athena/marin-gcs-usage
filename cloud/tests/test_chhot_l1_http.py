"""Exact HTTP bodies, authentication and explicit no-fallback refusals."""

from http.client import HTTPConnection
from http.server import HTTPServer
from io import StringIO
from json import loads
from pathlib import Path
from threading import Thread

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import hot_l1_http as module
from dt_cloud.chstore.hot_l1_batch_catalog import HotL1BatchCatalog
from test_chhot_l1_batch_catalog import artifact, expected_view, stream_artifact, write


@pytest.fixture
def catalog(tmp_path: Path) -> HotL1BatchCatalog:
    return HotL1BatchCatalog.load([write(tmp_path / "before.json", artifact("2026-10-04")), write(tmp_path / "after.json", stream_artifact())])


@pytest.fixture
def server(catalog: HotL1BatchCatalog):
    httpd = HTTPServer(("127.0.0.1", 0), module.make_handler(catalog, "s3cret"))
    thread = Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        thread.join(timeout=2)
        httpd.server_close()


def request(
    server: HTTPServer,
    path: str,
    *,
    token: str | None = "s3cret",
    method: str = "GET",
) -> tuple[int, dict, dict]:
    connection = HTTPConnection(*server.server_address, timeout=2)
    try:
        connection.request(method, path, headers={"Authorization": f"Bearer {token}"} if token is not None else {})
        response = connection.getresponse()
        data = response.read()
        return response.status, dict(response.getheaders()), loads(data) if data else {}
    finally:
        connection.close()


def test_authenticated_root_and_diff_exact_http_bodies(server: HTTPServer, catalog: HotL1BatchCatalog) -> None:
    status, headers, body = request(server, "/api/hot-l1?date=2026-10-05&name=.JSON&path=")
    assert (status, body) == (200, expected_view(stream_artifact()))
    assert {key: headers[key] for key in ("Content-Type", "Cache-Control", "X-Query-Engine")} == {
        "Content-Type": "application/json; charset=utf-8", "Cache-Control": "private, no-store", "X-Query-Engine": "hot-l1-catalog",
    }
    status, _, body = request(server, "/api/hot-l1?date=2026-10-05&name=.json&from=2026-10-04")
    assert (status, body) == (200, catalog.diff("2026-10-04", "2026-10-05", ".json"))


def test_health_is_public_metadata_only(server: HTTPServer, catalog: HotL1BatchCatalog) -> None:
    status, _, body = request(server, "/healthz", token=None)
    assert (status, body) == (200, {"state": "ready", "catalog": catalog.metadata()})
    status, _, body = request(server, "/healthz?name=.json", token=None)
    assert (status, body) == (400, {"error": "healthz accepts no query parameters"})


@pytest.mark.parametrize("token", [None, "wrong", "s3cret "])
def test_api_requires_exact_bearer_auth(server: HTTPServer, token: str | None) -> None:
    status, headers, body = request(server, "/api/hot-l1?date=2026-10-05&name=.json", token=token)
    assert (status, body, headers["WWW-Authenticate"]) == (401, {"error": "unauthorized"}, 'Bearer realm="hot-l1"')


def test_duplicate_auth_headers_are_refused(server: HTTPServer) -> None:
    connection = HTTPConnection(*server.server_address, timeout=2)
    try:
        connection.putrequest("GET", "/api/hot-l1?date=2026-10-05&name=.json")
        connection.putheader("Authorization", "Bearer s3cret")
        connection.putheader("Authorization", "Bearer s3cret")
        connection.endheaders()
        response = connection.getresponse()
        assert (response.status, loads(response.read())) == (401, {"error": "unauthorized"})
    finally:
        connection.close()


@pytest.mark.parametrize("query,message", [
    ("date=2026-10-05&name=.json&name=.npy", "duplicate query parameter"),
    ("date=2026-10-05&name=.json&depth=2", "unknown query parameter"),
    ("date=2026-10-05", "date and name are required and must be nonempty"),
    ("date=2026-10-05&name=", "date and name are required and must be nonempty"),
    ("date=2026-10-05&name=.json&from=", "from must be nonempty when provided"),
    ("date=2026-10-05&name=.json&path=a", "hot L1 batch catalog serves the global root only"),
    ("date=2026-10-03&name=.json", "hot L1 batch pattern/date is not registered; no scan fallback"),
    ("date=2026-10-05&name=cold", "hot L1 batch pattern/date is not registered; no scan fallback"),
    ("date=2026-10-05&name=.json&from=2026-10-05", "hot L1 batch comparison requires before date to precede after date"),
    ("date=2026-10-05&name=%FF", "invalid query parameters"),
    ("date=2026-10-05&name=%XY", "invalid query parameters"),
    ("date=2026-10-05&name", "invalid query parameters"),
])
def test_invalid_or_unregistered_requests_never_fallback(server: HTTPServer, query: str, message: str) -> None:
    status, _, body = request(server, "/api/hot-l1?" + query)
    assert (status, body) == (400, {"error": message})


def test_unknown_route_and_write_method_refuse(server: HTTPServer) -> None:
    status, _, body = request(server, "/api/subtree?date=2026-10-05&name=.json")
    assert (status, body) == (404, {"error": "not found"})
    status, headers, body = request(server, "/api/hot-l1", method="POST")
    assert (status, body, headers["Allow"]) == (405, {"error": "only GET is supported"}, "GET")
    status, headers, body = request(server, "/api/hot-l1", method="HEAD")
    assert (status, body, headers["Allow"]) == (405, {}, "GET")


@pytest.mark.parametrize("address,authorized", [("127.0.0.1", True), ("::1", True), ("192.0.2.1", False)])
def test_no_auth_handler_still_refuses_nonloopback_clients(catalog: HotL1BatchCatalog, address: str, authorized: bool) -> None:
    handler = object.__new__(module.make_handler(catalog, None))
    handler.client_address = address, 1234
    assert handler._authed() is authorized


def test_handler_queries_do_not_reopen_artifacts(server: HTTPServer, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("HTTP request attempted artifact access")

    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    status, _, body = request(server, "/api/hot-l1?date=2026-10-05&name=.json")
    assert (status, body) == (200, expected_view(stream_artifact()))


@pytest.mark.parametrize("bind", ["0.0.0.0", "192.0.2.1", "localhost", "::"])
def test_no_auth_nonloopback_bind_refuses_before_loading(bind: str) -> None:
    with pytest.raises(ValueError) as caught:
        module.serve([Path("missing.json")], bind=bind, token=None)
    assert str(caught.value) == "hot L1 no-auth serving requires a numeric loopback bind"


def test_serve_loads_once_and_defaults_to_distinct_dev_port(catalog: HotL1BatchCatalog, monkeypatch: pytest.MonkeyPatch) -> None:
    calls, artifacts = [], (Path("first.json"), Path("second.json"))

    class Server:
        def __init__(self, address: tuple[str, int], handler: object) -> None:
            calls.append(("server", address))

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            calls.append(("close",))

        def serve_forever(self) -> None:
            calls.append(("serve",))

    def load(paths: tuple[Path, ...]) -> HotL1BatchCatalog:
        calls.append(("load", paths))
        return catalog

    monkeypatch.setattr(module.HotL1BatchCatalog, "load", load)
    monkeypatch.setattr(module, "HTTPServer", Server)
    stderr = StringIO()
    monkeypatch.setattr(module, "stderr", stderr)
    module.serve(artifacts, token=None)
    assert calls == [("load", artifacts), ("server", ("127.0.0.1", 8082)), ("serve",), ("close",)]
    assert stderr.getvalue() == "serve-hot-l1: listening on 127.0.0.1:8082 (loopback NO auth)\n"


@pytest.mark.parametrize("no_auth", [False, True])
def test_cli_forwards_explicit_artifacts_and_auth_without_starting_server(monkeypatch: pytest.MonkeyPatch, no_auth: bool) -> None:
    from dt_cloud.cli import main

    calls = []
    monkeypatch.setenv("TEST_HOT_TOKEN", "private-secret")
    monkeypatch.setattr(module, "serve", lambda *args, **kwargs: calls.append((args, kwargs)))
    args = ["serve-hot-l1", "-b", "127.0.0.1", "-p", "8092", "-T", "TEST_HOT_TOKEN", "first.json", "second.json"]
    if no_auth:
        args += ["-A"]
    result = CliRunner().invoke(main, args)
    assert (result.exit_code, result.stdout, result.stderr) == (0, "", "")
    assert calls == [(((Path("first.json"), Path("second.json")),), {"generation_root": None, "bind": "127.0.0.1", "port": 8092, "token": None if no_auth else "private-secret"})]


def test_cli_auth_refusals_are_precise(monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud.cli import main

    monkeypatch.delenv("QUERY_BOX_TOKEN", raising=False)
    result = CliRunner().invoke(main, ["serve-hot-l1", "first.json"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.splitlines() == ["Usage: main serve-hot-l1 [OPTIONS] [ARTIFACTS]...", "Try 'main serve-hot-l1 --help' for help.", "",
                                         "Error: $QUERY_BOX_TOKEN is unset (or pass -A for loopback-only no-auth serving)"]
    result = CliRunner().invoke(main, ["serve-hot-l1", "-A", "-b", "0.0.0.0", "first.json"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.splitlines() == ["Usage: main serve-hot-l1 [OPTIONS] [ARTIFACTS]...", "Try 'main serve-hot-l1 --help' for help.", "",
                                         "Error: hot L1 no-auth serving requires a numeric loopback bind"]


def test_cli_generation_root_and_mutually_exclusive_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud.cli import main

    calls = []
    monkeypatch.setenv("QUERY_BOX_TOKEN", "test-secret")
    monkeypatch.setattr(module, "serve", lambda *args, **kwargs: calls.append((args, kwargs)))
    result = CliRunner().invoke(main, ["serve-hot-l1", "-g", "published"])
    assert (result.exit_code, result.stdout, result.stderr) == (0, "", "")
    assert calls == [(((),), {"generation_root": Path("published"), "bind": "127.0.0.1", "port": 8082, "token": "test-secret"})]
    for args in ([], ["-g", "published", "first.json"]):
        result = CliRunner().invoke(main, ["serve-hot-l1", *args])
        assert (result.exit_code, result.stdout, result.stderr.splitlines()) == (2, "", [
            "Usage: main serve-hot-l1 [OPTIONS] [ARTIFACTS]...", "Try 'main serve-hot-l1 --help' for help.", "",
            "Error: hot L1 serving requires either explicit artifacts or generation_root, not both",
        ])
    assert len(calls) == 1


@pytest.mark.parametrize("both", [False, True])
def test_http_source_selection_refuses_before_bind(monkeypatch: pytest.MonkeyPatch, both: bool) -> None:
    calls = []
    monkeypatch.setattr(module, "HTTPServer", lambda *args: calls.append(args))
    with pytest.raises(ValueError) as caught:
        module.serve((Path("artifact.json"),) if both else (), generation_root=Path("root") if both else None, token=None)
    assert str(caught.value) == "hot L1 serving requires either explicit artifacts or generation_root, not both"
    assert calls == []


@pytest.mark.parametrize("corrupt", [False, True])
def test_missing_or_corrupt_generation_refuses_before_socket_bind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corrupt: bool) -> None:
    from dt_cloud.chstore.hot_l1_publish import publish

    root, calls = tmp_path / "published", []
    if corrupt:
        manifest = publish((write(tmp_path / "artifact.json", stream_artifact()),), root)
        (root / manifest["artifacts"][0]["file"]).write_bytes(b"corrupt\n")
    monkeypatch.setattr(module, "HTTPServer", lambda *args: calls.append(args))
    with pytest.raises(ValueError if corrupt else FileNotFoundError) as caught:
        module.serve(generation_root=root, token=None)
    if corrupt:
        assert str(caught.value) == "published artifact length/SHA256 disagrees with its pinned manifest"
    else:
        assert caught.value.filename == str(root / "current.json")
    assert calls == []


def test_published_startup_pins_once_and_queries_ignore_new_current_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from copy import deepcopy
    from email.message import Message
    from dt_cloud.chstore import hot_l1_publish as publisher

    root, calls, original = tmp_path / "published", [], stream_artifact()
    publisher.publish((write(tmp_path / "old.json", original),), root)
    changed = deepcopy(original)
    changed["results"][0]["root"]["b"] = changed["results"][0]["buckets"][0]["b"] = 999
    new_path = write(tmp_path / "new.json", changed)
    real_load = publisher.load

    def load(path: Path) -> HotL1BatchCatalog:
        calls.append(("load", path))
        return real_load(path)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("request reread published files")

    class Server:
        def __init__(self, address: tuple[str, int], handler: object) -> None:
            calls.append(("bind", address))
            self.handler = handler

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            calls.append(("close",))

        def serve_forever(self) -> None:
            publisher.publish((new_path,), root)
            monkeypatch.setattr(Path, "read_bytes", forbidden)
            monkeypatch.setattr(Path, "read_text", forbidden)
            request = object.__new__(self.handler)
            request.path = "/api/hot-l1?date=2026-10-05&name=.json"
            request.client_address = "127.0.0.1", 1234
            request.headers = Message()
            request.headers["Authorization"] = "Bearer test-secret"
            request._send = lambda status, body: calls.append(("response", status, body))
            request.do_GET()

    monkeypatch.setattr(publisher, "load", load)
    monkeypatch.setattr(module, "HTTPServer", Server)
    monkeypatch.setattr(module, "stderr", StringIO())
    module.serve(generation_root=root, token="test-secret")
    assert calls == [("load", root), ("bind", ("127.0.0.1", 8082)), ("response", 200, expected_view(original)), ("close",)]
