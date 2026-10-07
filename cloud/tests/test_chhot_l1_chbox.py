"""The opt-in scan-free lane never waits for or falls back to ClickHouse."""

from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from json import loads
from pathlib import Path
from threading import BoundedSemaphore, Thread
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from dt_cloud.box import server as bs
from dt_cloud.chstore.hot_l1_batch_catalog import HotL1BatchCatalog
from test_chhot_l1_batch_catalog import artifact, expected_view, stream_artifact, write


class Store:
    url, db, root_label = "http://unused.invalid", "source", "fixture"

    def session(self):
        raise AssertionError("hot request called ClickHouse")

    def scans(self, *, refresh: bool) -> dict:
        assert refresh is True
        return {"scan": SimpleNamespace(id="2026-10-05", version="v1")}


@pytest.fixture
def box(tmp_path: Path) -> bs.ChBox:
    catalog = HotL1BatchCatalog.load([write(tmp_path / "before.json", artifact("2026-10-04")), write(tmp_path / "artifact.json", stream_artifact())])
    result = bs.ChBox(Store(), hot_l1_catalog=catalog, gate=BoundedSemaphore(1))
    assert result.gate.acquire(blocking=False) is True
    return result


@pytest.fixture
def server(box: bs.ChBox):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), bs.make_handler(box, "s3cret"))
    httpd.daemon_threads = True
    thread = Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        thread.join(timeout=2)
        httpd.server_close()


def request(server: ThreadingHTTPServer, query: str, *, token: str | None = "s3cret") -> tuple[int, dict, object]:
    connection = HTTPConnection(*server.server_address, timeout=2)
    try:
        connection.request("GET", "/api/hot-l1?" + query, headers={"Authorization": f"Bearer {token}"} if token else {})
        response = connection.getresponse()
        data = response.read()
        return response.status, dict(response.getheaders()), loads(data) if response.getheader("content-type") == "application/json" else data.decode()
    finally:
        connection.close()


def test_hot_body_succeeds_while_rich_gate_is_occupied(server: ThreadingHTTPServer, box: bs.ChBox) -> None:
    status, headers, body = request(server, "date=2026-10-05&name=.JSON")
    assert (status, body) == (200, expected_view(stream_artifact()))
    assert headers["cache-control"] == "private, no-store"
    assert box.gate.acquire(blocking=False) is False
    assert box.hot_l1_gate.acquire(blocking=False) is True
    box.hot_l1_gate.release()


def test_excess_hot_lane_refuses_without_waiting_or_fallback(server: ThreadingHTTPServer, box: bs.ChBox) -> None:
    assert box.hot_l1_gate.acquire(blocking=False) is True
    assert box.hot_l1_gate.acquire(blocking=False) is True
    try:
        status, headers, body = request(server, "date=2026-10-05&name=.json")
        assert (status, headers["retry-after"], body) == (503, "1", {"error": "hot L1 serving slots busy; retry shortly"})
    finally:
        box.hot_l1_gate.release()
        box.hot_l1_gate.release()


def test_hot_diff_is_complete_while_rich_gate_is_occupied(server: ThreadingHTTPServer, box: bs.ChBox) -> None:
    status, _, body = request(server, "date=2026-10-05&name=.json&from=2026-10-04")
    assert (status, body) == (200, box.hot_l1_catalog.diff("2026-10-04", "2026-10-05", ".json"))
    assert body["delta"] == {"b": -6, "o": 0}


@pytest.mark.parametrize("query,message", [
    ("date=2026-10-05&name=.json&name=.npy", "duplicate query parameter"),
    ("date=2026-10-05&name=.json&depth=2", "unknown query parameter"),
    ("date=2026-10-05", "date and name are required and must be nonempty"),
    ("date=2026-10-03&name=.json", "hot L1 batch pattern/date is not registered; no scan fallback"),
    ("date=2026-10-05&name=.json&path=a", "hot L1 batch catalog serves the global root only"),
])
def test_integrated_route_uses_standalone_strict_contract(server: ThreadingHTTPServer, query: str, message: str) -> None:
    status, _, body = request(server, query)
    assert (status, body) == (400, {"error": message})


def test_same_box_auth_and_absent_catalog_are_explicit(server: ThreadingHTTPServer, box: bs.ChBox) -> None:
    status, _, body = request(server, "date=2026-10-05&name=.json", token=None)
    assert (status, body) == (401, "unauthorized")
    box.hot_l1_catalog = None
    status, _, body = request(server, "date=2026-10-05&name=.json")
    assert (status, body) == (501, {"error": "hot L1 catalog is not selected; no scan fallback"})


@pytest.mark.parametrize("failure", [False, True])
def test_hot_slot_release_includes_error_and_disconnected_response(box: bs.ChBox, failure: bool) -> None:
    box.hot_l1_gate = BoundedSemaphore(1)
    handler = object.__new__(bs.make_handler(box, None))
    handler.path = "/api/hot-l1?date=2026-10-05&name=" + (".json" if failure else "cold")
    calls = []

    def send(status: int, body: str, t0: float, *args: object, **kwargs: object) -> None:
        calls.append((status, loads(body)))
        if failure:
            assert box.hot_l1_gate.acquire(blocking=False) is False
            raise BrokenPipeError("disconnected")

    handler._send = send
    if failure:
        with pytest.raises(BrokenPipeError) as caught:
            handler.do_GET()
        assert str(caught.value) == "disconnected"
        assert calls == [(200, expected_view(stream_artifact()))]
    else:
        handler.do_GET()
        assert calls == [(400, {"error": "hot L1 batch pattern/date is not registered; no scan fallback"})]
    assert box.hot_l1_gate.acquire(blocking=False) is True
    box.hot_l1_gate.release()


def test_health_only_adds_metadata_when_catalog_is_selected(box: bs.ChBox) -> None:
    metadata = box.hot_l1_catalog.metadata()
    base = {"state": "ready", "engine": "ch", "scans": [{"date": "2026-10-05", "version": "v1"}]}
    assert box.health() == {**base, "hot_l1": metadata}
    box.hot_l1_catalog = None
    assert box.health() == base


def test_startup_pins_once_and_invalid_generation_fails_before_bind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud.chstore import hot_l1_publish as publisher

    root = tmp_path / "published"
    manifest = publisher.publish((write(tmp_path / "artifact.json", stream_artifact()),), root)
    calls, real_load = [], publisher.load

    def load(path: Path) -> HotL1BatchCatalog:
        calls.append(("load", path))
        return real_load(path)

    monkeypatch.setattr(publisher, "load", load)
    box = bs.ChBox(Store(), hot_l1_generation=root)
    box.start()
    box.start()
    assert calls == [("load", root)]
    assert box.hot_l1_catalog.view("2026-10-05", ".json") == expected_view(stream_artifact())
    (root / manifest["artifacts"][0]["file"]).write_bytes(b"corrupt\n")
    monkeypatch.setattr(bs, "ThreadingHTTPServer", lambda *args: calls.append(("bind",)))
    with pytest.raises(ValueError) as caught:
        bs.serve(bs.ChBox(Store(), hot_l1_generation=root), bind="127.0.0.1", port=8080, token="s3cret")
    assert str(caught.value) == "published artifact length/SHA256 disagrees with its pinned manifest"
    assert calls == [("load", root), ("load", root)]


def test_selected_numeric_target_mismatch_refuses_before_ch_or_bind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud.chstore.hot_l1_publish import publish

    root = tmp_path / "published"
    publish((write(tmp_path / "artifact.json", stream_artifact()),), root)
    binds = []
    monkeypatch.setattr(bs, "ThreadingHTTPServer", lambda *args: binds.append(args))
    box = bs.ChBox(Store(), narrow_target="other_fleet", hot_l1_generation=root)
    with pytest.raises(ValueError) as caught:
        bs.serve(box, bind="127.0.0.1", port=8080, token="s3cret")
    assert str(caught.value) == "published hot L1 catalog target differs from the selected numeric target"
    assert box.hot_l1_catalog.target == "fleet"
    assert binds == []


def test_cli_hot_generation_requires_ch_and_forwards_selected_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud.cli import main

    result = CliRunner().invoke(main, ["serve-query", "-A", "-g", "published", "unused"])
    assert (result.exit_code, result.output.splitlines()) == (2, [
        "Usage: main serve-query [OPTIONS] ROOT", "Try 'main serve-query --help' for help.", "",
        "Error: --hot-l1-generation requires --engine ch",
    ])
    calls = []
    monkeypatch.setattr(bs, "serve", lambda box, **kwargs: calls.append((box.hot_l1_generation, box.hot_l1_catalog, kwargs)))
    result = CliRunner().invoke(main, ["serve-query", "-A", "-e", "ch", "-g", "published", "-p", "8087", "unused"])
    assert (result.exit_code, result.stdout, result.stderr) == (0, "", "")
    assert calls == [(Path("published"), None, {"bind": "0.0.0.0", "port": 8087, "token": None})]
