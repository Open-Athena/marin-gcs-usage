"""The concurrency slot owns backend work throughout a streamed response."""

from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace

import pytest

from dt_cloud.box import server as bs


@pytest.mark.parametrize("failure", [None, "headers", "write"])
def test_stream_holds_gate_until_generator_cleanup(monkeypatch, failure):
    active, events = [False], []

    @contextmanager
    def gate():
        active[0] = True
        events.append(("acquire", True))
        try:
            yield
        finally:
            active[0] = False
            events.append(("release", False))

    def route(box, qs):
        try:
            events.append(("first", active[0]))
            yield '{"ok":'
            events.append(("tail", active[0]))
            yield "true}"
        finally:
            events.append(("close", active[0]))

    class Output(BytesIO):
        def write(self, data: bytes) -> int:
            if failure == "write":
                raise BrokenPipeError("disconnected")
            return super().write(data)

    def send_response(status: int) -> None:
        if failure == "headers":
            raise OSError("headers failed")
        assert status == 200

    monkeypatch.setitem(bs.ROUTES, "/api/subtree", route)
    box = SimpleNamespace(gate=gate(), start=lambda: None)
    handler = object.__new__(bs.make_handler(box, None))
    handler.path = "/api/subtree"
    handler.wfile = Output()
    handler.send_response = send_response
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    handler._engine = lambda *args: None
    handler._done = lambda *args: events.append(("done", active[0]))

    if failure:
        with pytest.raises(OSError) as caught:
            handler.do_GET()
        assert str(caught.value) == ("headers failed" if failure == "headers" else "disconnected")
        assert events == [("acquire", True), ("first", True), ("close", True), ("release", False)]
        assert handler.wfile.getvalue() == b""
    else:
        handler.do_GET()
        assert events == [
            ("acquire", True), ("first", True), ("tail", True),
            ("close", True), ("release", False), ("done", False),
        ]
        assert handler.wfile.getvalue() == b'6\r\n{"ok":\r\n5\r\ntrue}\r\n0\r\n\r\n'
