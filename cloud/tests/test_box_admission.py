"""Coarse requests fail fast when busy and own their slot through cleanup."""

from io import BytesIO
from http.server import BaseHTTPRequestHandler
from threading import BoundedSemaphore
from types import SimpleNamespace
from typing import Callable, Generator, NoReturn

import pytest

from dt_cloud.box import server as bs


class AdmissionGate(BoundedSemaphore):
    def __init__(self, slots: int, events: list) -> None:
        super().__init__(slots)
        self.events = events

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        assert blocking is False
        assert timeout is None
        acquired = super().acquire(blocking=False)
        self.events.append(("acquire", acquired))
        return acquired

    def release(self, n: int = 1) -> None:
        super().release(n)
        self.events.append(("release", n))


def handler_for(
    monkeypatch: pytest.MonkeyPatch,
    gate: AdmissionGate,
    route: Callable[[object, dict], Generator[str, None, None]],
    *,
    failure: str | None = None,
) -> tuple[BaseHTTPRequestHandler, list[tuple]]:
    replies, events = [], gate.events

    class Output(BytesIO):
        def write(self, data: bytes) -> int:
            if failure == "write":
                raise BrokenPipeError("disconnected")
            return super().write(data)

    def send_response(status: int) -> None:
        if failure == "headers":
            raise OSError("headers failed")
        events.append(("status", status))

    monkeypatch.setitem(bs.ROUTES, "/api/coarse", route)
    box = SimpleNamespace(gate=gate, start=lambda: None)
    handler = object.__new__(bs.make_handler(box, None))
    handler.path = "/api/coarse?date=2026-10-05&name=zarr.json"
    handler.wfile = Output()
    handler.send_response = send_response
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    handler._engine = lambda *args: None
    handler._done = lambda *args: events.append(("done",))
    handler._send = lambda status, body, t0, ctype, headers=None, state=None: replies.append((status, body, ctype, headers, state))
    return handler, replies


def test_busy_coarse_request_never_starts_backend_or_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    events = []
    gate = AdmissionGate(0, events)

    def route(box: object, qs: dict) -> Generator[str, None, None]:
        events.append(("backend",))
        yield "unreachable"

    handler, replies = handler_for(monkeypatch, gate, route)
    handler.do_GET()
    assert events == [("acquire", False)]
    assert replies == [(503, "coarse serving slots busy; retry shortly", "text/plain", {"retry-after": "1"}, "loading")]
    assert handler.wfile.getvalue() == b""


def test_active_coarse_stream_refuses_extra_reads_until_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    events = []
    gate = AdmissionGate(1, events)

    def active() -> Generator[str, None, None]:
        try:
            yield "first"
            yield "tail"
        finally:
            events.append(("active-close",))

    def refused(box: object, qs: dict) -> Generator[str, None, None]:
        events.append(("unexpected-backend",))
        yield "unreachable"

    stream = bs._gated_stream(bs._coarse_admission(gate), active)
    assert next(stream) == "first"
    handler, replies = handler_for(monkeypatch, gate, refused)
    handler.do_GET()
    stream.close()
    assert events == [("acquire", True), ("acquire", False), ("active-close",), ("release", 1)]
    assert replies == [(503, "coarse serving slots busy; retry shortly", "text/plain", {"retry-after": "1"}, "loading")]
    assert gate.acquire(blocking=False) is True
    gate.release()


@pytest.mark.parametrize("failure", [None, "headers", "write", "backend", "tail"])
def test_admitted_coarse_releases_slot_after_cleanup(monkeypatch: pytest.MonkeyPatch, failure: str | None) -> None:
    events = []
    gate = AdmissionGate(1, events)

    def route(box: object, qs: dict) -> Generator[str, None, None]:
        try:
            events.append(("first",))
            if failure == "backend":
                raise RuntimeError("fixture backend failed")
            yield '{"ok":'
            events.append(("tail",))
            if failure == "tail":
                raise RuntimeError("fixture tail failed")
            yield "true}"
        finally:
            events.append(("close",))

    handler, replies = handler_for(monkeypatch, gate, route, failure=failure)
    if failure in ("headers", "write", "tail"):
        expected = {"headers": "headers failed", "write": "disconnected", "tail": "fixture tail failed"}[failure]
        with pytest.raises(RuntimeError if failure == "tail" else OSError, match=f"^{expected}$"):
            handler.do_GET()
    else:
        handler.do_GET()
    if failure == "backend":
        assert events == [("acquire", True), ("first",), ("close",), ("release", 1)]
        assert replies == [(503, "backend error: RuntimeError", "text/plain", {"retry-after": "10"}, None)]
        assert handler.wfile.getvalue() == b""
    elif failure == "headers":
        assert events == [("acquire", True), ("first",), ("close",), ("release", 1)]
        assert replies == []
        assert handler.wfile.getvalue() == b""
    elif failure == "write":
        assert events == [("acquire", True), ("first",), ("status", 200), ("close",), ("release", 1)]
        assert replies == []
        assert handler.wfile.getvalue() == b""
    elif failure == "tail":
        assert events == [("acquire", True), ("first",), ("status", 200), ("tail",), ("close",), ("release", 1)]
        assert replies == []
        assert handler.wfile.getvalue() == b'6\r\n{"ok":\r\n'
    else:
        assert events == [("acquire", True), ("first",), ("status", 200), ("tail",), ("close",), ("release", 1), ("done",)]
        assert replies == []
        assert handler.wfile.getvalue() == b'6\r\n{"ok":\r\n5\r\ntrue}\r\n0\r\n\r\n'
    assert gate.acquire(blocking=False) is True
    gate.release()


def test_producer_creation_failure_releases_admitted_slot() -> None:
    events = []
    gate = AdmissionGate(1, events)

    def fail_to_create() -> NoReturn:
        raise RuntimeError("no iterator")

    stream = bs._gated_stream(bs._coarse_admission(gate), fail_to_create)
    with pytest.raises(RuntimeError, match="^no iterator$"):
        next(stream)
    assert events == [("acquire", True), ("release", 1)]
    assert gate.acquire(blocking=False) is True
    gate.release()
