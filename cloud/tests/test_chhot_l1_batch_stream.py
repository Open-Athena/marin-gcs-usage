from io import BytesIO, StringIO
from json import dumps
from os import environ
from pathlib import Path
from struct import pack
from subprocess import TimeoutExpired
from types import SimpleNamespace

import pytest

from dt_cloud.chstore import hot_l1_batch_stream as module
from dt_cloud.chstore.client import Ch

from chserver import ch_db, ch_url  # noqa: F401
from test_chhot_l1 import fleet  # noqa: F401
from test_chhot_l1_batch_sql import literals  # noqa: F401

QID = "hot_l1_stream_" + "b" * 32
BUCKETS = [[1, 2, "a"]]
STREAM_SQL = ("SELECT assumeNotNull(toUInt64(pre)),assumeNotNull(toUInt64(post)), "
              "assumeNotNull(toUInt64(b)),assumeNotNull(toUInt64(o)),"
              f"assumeNotNull(lowerUTF8({module.NAME})) FROM snap.nodes ORDER BY pre")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    binary = tmp_path / "native"
    binary.write_text("private precompiled executable placeholder\n")
    native = {"schema": "hot-l1-native-stream-v1", "exact": True, "incremental": False, "levels": 1,
              "nodes_read": 3, "registered_predicates": 2, "peak_stack": 3, "peak_active": 2,
              "native_peak_rss_bytes": 1234, "matrix": [{"predicate_id": 1, "buckets": [["8", "5"]]},
                                                       {"predicate_id": 2, "buckets": [["8", "5"]]}]}
    raw = (pack("<QQQQ", 0, 2, 8, 5) + b"\0" + pack("<QQQQ", 1, 2, 8, 5) + b"\x01a" +
           pack("<QQQQ", 2, 2, 8, 5) + module._string("åro"))
    state = SimpleNamespace(binary=binary, native=native, raw=raw, fail=None, calls=[], audit=[[3, 0, 9, 0, 1, 1, 2]],
                            stderr=StringIO(), input=None, child=None, output=None, exit_code=0)

    class Reader:
        def stream(self, sql: str, *, fmt: str):
            state.calls.append(("stream", " ".join(sql.split()), fmt))
            try:
                yield raw[:19]
                if state.fail == "source":
                    raise RuntimeError("source failed")
                yield raw[19:]
            finally:
                state.calls.append(("source-close",))

        def close(self) -> None:
            state.calls.append(("reader-close",))

    class Ch:
        timeout = 660

        def scalar(self, sql: str) -> str:
            state.calls.append(("manifest", sql))
            return dumps({"prefix": "", "dates": ["2026-10-05"], "dbs": ["snap"]})

        def json(self, sql: str) -> list:
            state.calls.append(("audit", " ".join(sql.split())))
            return state.audit

        def fork(self, **settings: object) -> Reader:
            state.calls.append(("fork", settings))
            return Reader()

        def exec(self, sql: str, *, fmt: object) -> None:
            state.calls.append(("cancel", sql, fmt))

    class Input(BytesIO):
        def write(self, data: bytes) -> int:
            if state.fail == "pipe" and data == raw[:19]:
                raise BrokenPipeError("child stopped reading")
            return super().write(data)

        def close(self) -> None:
            if not self.closed:
                state.input = self.getvalue()
            super().close()

    class Child:
        def __init__(self, args: list, **kwargs: object) -> None:
            state.calls.append(("spawn", args, kwargs))
            self.stdin, self.stdout, self.stderr = Input(), BytesIO(), BytesIO(b"native progress\n")
            self.returncode = None
            state.child = self

        def communicate(self, *, timeout: float) -> tuple[bytes, None]:
            assert self.stdin is None
            assert self.stderr is None
            state.calls.append(("communicate", timeout))
            if state.fail == "timeout":
                raise TimeoutExpired("private-native", timeout)
            self.returncode = state.exit_code
            return state.output if state.output is not None else dumps(state.native).encode(), None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            state.calls.append(("terminate",))

        def kill(self) -> None:
            state.calls.append(("kill",))

        def wait(self, *, timeout: float) -> int:
            state.calls.append(("wait", timeout))
            if state.fail == "timeout" and self.returncode is None:
                self.returncode = -9
                raise TimeoutExpired("private-native", timeout)
            self.returncode = -15
            return self.returncode

    monkeypatch.setattr(module, "access", lambda path, mode: True)
    monkeypatch.setattr(module, "Popen", Child)
    monkeypatch.setattr(module, "_buckets", lambda ch, target: BUCKETS)
    monkeypatch.setattr(module, "monotonic", lambda: 1.0)
    monkeypatch.setattr(module, "uuid4", lambda: SimpleNamespace(hex="b" * 32))
    monkeypatch.setattr(module, "stderr", state.stderr)
    state.ch = Ch()
    return state


def build(fake: SimpleNamespace) -> dict:
    return module.build(fake.ch, "fleet", "2026-10-05", ("a", "åro"), binary=fake.binary)


def test_exact_binary_protocol_chunks_complete_matrix_and_stats(fake: SimpleNamespace) -> None:
    result = build(fake)
    assert fake.input == b"HL1DFS01" + pack("<QIB", 3, 2, 1) + pack("<QQ", 1, 2) + b"\x01a\x04\xc3\xa5ro" + fake.raw
    completed = [{"pre": 1, "post": 2, "path": "a", "b": 8, "o": 5}]
    assert result == {"schema": "hot-l1-batch-stream-v1", "target": "fleet", "snapshot_db": "snap", "date": "2026-10-05",
                      "exact": True, "incremental": False, "levels": 1, "scope": module.SCOPE,
                      "results": [{"predicate_id": 1, "pattern": "a", "root": {"b": 8, "o": 5}, "buckets": completed},
                                  {"predicate_id": 2, "pattern": "åro", "root": {"b": 8, "o": 5}, "buckets": completed}],
                      "compiled_patterns": 2, "source_query_id": QID,
                      "native": {key: value for key, value in fake.native.items() if key != "matrix"},
                      "source_validation": {"rows": 3, "invalid_utf8_paths": 0, "path_bytes": 9, "invalid_scalar_rows": 0},
                      "stages": {"source_validation_s": 0.0, "aggregate_s": 0.0}, "build_s": 0.0}
    assert [call for call in fake.calls if call[0] in {"fork", "stream", "communicate", "source-close", "reader-close"}] == [
        ("fork", {"query_id": QID}), ("stream", " ".join(STREAM_SQL.split()), "RowBinary"),
        ("source-close",), ("communicate", 660), ("reader-close",),
    ]
    assert fake.stderr.getvalue() == "native progress\n"
    assert fake.child.stdout.closed is True


def test_rowbinary_string_varuint_and_unicode() -> None:
    assert module._string("å" * 100) == b"\xc8\x01" + "å".encode() * 100
    assert module._string("") == b"\0"


def test_explicit_source_component_does_not_invent_frozen_history(fake: SimpleNamespace) -> None:
    result = module.aggregate_source(fake.ch, "snap", ("A", "åro"), BUCKETS, 3, binary=fake.binary)
    assert result == {
        "results": [
            {"predicate_id": 1, "pattern": "a", "root": {"b": 8, "o": 5},
             "buckets": [{"pre": 1, "post": 2, "path": "a", "b": 8, "o": 5}]},
            {"predicate_id": 2, "pattern": "åro", "root": {"b": 8, "o": 5},
             "buckets": [{"pre": 1, "post": 2, "path": "a", "b": 8, "o": 5}]},
        ],
        "source_query_id": QID,
        "native": {"schema": "hot-l1-native-stream-v1", "exact": True, "incremental": False,
                   "levels": 1, "nodes_read": 3, "registered_predicates": 2, "peak_stack": 3,
                   "peak_active": 2, "native_peak_rss_bytes": 1234},
        "aggregate_s": 0.0,
    }
    assert [call for call in fake.calls if call[0] in {"manifest", "audit"}] == []
    assert fake.input == b"HL1DFS01" + pack("<QIB", 3, 2, 1) + pack("<QQ", 1, 2) + b"\x01a\x04\xc3\xa5ro" + fake.raw


@pytest.mark.parametrize("buckets", [
    None, [], [[True, 2, "a"]], [[2, 2, "a"]], [[1, 1, "a"]], [[1, 1 << 64, "a"]],
    [[1, 0, "a"]], [[1, 2, ""]], [[1, 2, "a\0"]], [[1, 1, "a"], [2, 2, "a"]],
])
def test_explicit_source_bad_bounds_refuse_before_child(fake: SimpleNamespace, buckets) -> None:
    with pytest.raises(ValueError) as caught:
        module.aggregate_source(fake.ch, "snap", ("a",), buckets, 3, binary=fake.binary)
    assert str(caught.value) == "native source requires one to six complete ordered bucket bounds"
    assert fake.calls == []


def test_explicit_source_allows_absent_nodes_in_frozen_union_geometry(fake: SimpleNamespace) -> None:
    buckets = [[1, 3, "a"]]
    result = module.aggregate_source(fake.ch, "snap", ("a", "åro"), buckets, 3, binary=fake.binary)
    assert result["results"] == [
        {"predicate_id": 1, "pattern": "a", "root": {"b": 8, "o": 5},
         "buckets": [{"pre": 1, "post": 3, "path": "a", "b": 8, "o": 5}]},
        {"predicate_id": 2, "pattern": "åro", "root": {"b": 8, "o": 5},
         "buckets": [{"pre": 1, "post": 3, "path": "a", "b": 8, "o": 5}]},
    ]
    assert fake.input == b"HL1DFS01" + pack("<QIB", 3, 2, 1) + pack("<QQ", 1, 3) + b"\x01a\x04\xc3\xa5ro" + fake.raw


@pytest.mark.parametrize("rows,patterns", [(True, ("a",)), (0, ("a",)), (3, ()), (3, ("a/b",))])
def test_explicit_source_bad_count_or_patterns_refuse_before_child(fake: SimpleNamespace, rows, patterns) -> None:
    with pytest.raises(ValueError) as caught:
        module.aggregate_source(fake.ch, "snap", patterns, BUCKETS, rows, binary=fake.binary)
    assert str(caught.value) == "native source requires positive rows and nonempty NUL/slash-free literals of at most 512 characters"
    assert fake.calls == []


@pytest.mark.parametrize("patterns", [(), ("a", "A"), ("a/b",), ("a\0",), ("",)])
def test_invalid_catalog_refuses_before_source_or_child(fake: SimpleNamespace, patterns: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        module.build(fake.ch, "fleet", "2026-10-05", patterns, binary=fake.binary)
    assert fake.calls == []


def test_missing_binary_refuses_before_source(fake: SimpleNamespace) -> None:
    with pytest.raises(ValueError) as caught:
        module.build(fake.ch, "fleet", "2026-10-05", ("a",), binary=fake.binary.with_name("absent"))
    assert str(caught.value) == "native stream requires an explicit existing executable"
    assert fake.calls == []


@pytest.mark.parametrize("audit", [[[3, 1, 9, 0, 1, 1, 2]], [[3, 0, 9, 1, 1, 1, 2]],
                                   [[3, 0, 9, 0, 2, 1, 2]], [[3, 0, 9, 0, 1, 1, 99]]])
def test_failed_source_audit_never_spawns_or_streams(fake: SimpleNamespace, audit: list) -> None:
    fake.audit = audit
    with pytest.raises(ValueError) as caught:
        build(fake)
    assert str(caught.value) == "native stream source failed UTF-8/scalar/global-root audit"
    assert [call[0] for call in fake.calls] == ["manifest", "audit"]


@pytest.mark.parametrize("failure", ["source", "pipe"])
def test_source_and_pipe_errors_reap_owned_child_and_cancel_exact_query(fake: SimpleNamespace, failure: str) -> None:
    fake.fail = failure
    with pytest.raises(RuntimeError if failure == "source" else BrokenPipeError) as caught:
        build(fake)
    assert str(caught.value) == ("source failed" if failure == "source" else "child stopped reading")
    assert [call for call in fake.calls if call[0] in {"terminate", "wait", "cancel", "source-close", "reader-close"}] == (
        [("source-close",)] if failure == "source" else []
    ) + [("terminate",), ("wait", 5), ("cancel", f"KILL QUERY WHERE query_id='{QID}' SYNC", None)] + (
        [("source-close",)] if failure == "pipe" else []
    ) + [("reader-close",)]


def test_timeout_terminate_escalates_only_owned_child_to_kill(fake: SimpleNamespace) -> None:
    fake.fail = "timeout"
    with pytest.raises(TimeoutExpired):
        build(fake)
    assert [call for call in fake.calls if call[0] in {"terminate", "wait", "kill", "cancel", "reader-close"}] == [
        ("terminate",), ("wait", 5), ("kill",), ("wait", 5),
        ("cancel", f"KILL QUERY WHERE query_id='{QID}' SYNC", None), ("reader-close",),
    ]


def test_failed_child_status_never_accepts_success_shaped_json(fake: SimpleNamespace) -> None:
    fake.exit_code = 1
    with pytest.raises(RuntimeError) as caught:
        build(fake)
    assert str(caught.value) == "native stream engine exited with status 1"
    assert fake.calls[-2:] == [("cancel", f"KILL QUERY WHERE query_id='{QID}' SYNC", None), ("reader-close",)]


@pytest.mark.parametrize("change", [lambda body: body.update(nodes_read=2), lambda body: body["matrix"].pop(),
                                     lambda body: body["matrix"][0].update(predicate_id=2),
                                     lambda body: body["matrix"][0].update(buckets=[]),
                                     lambda body: body["matrix"][0].update(buckets=[["8", "5", "0"]]),
                                     lambda body: body.update(native_peak_rss_bytes=-1)])
def test_incomplete_or_malformed_native_matrix_refused(fake: SimpleNamespace, change: object) -> None:
    change(fake.native)
    with pytest.raises(RuntimeError):
        build(fake)
    assert fake.calls[-2:] == [("cancel", f"KILL QUERY WHERE query_id='{QID}' SYNC", None), ("reader-close",)]


def test_malformed_native_json_refused_and_reader_closed(fake: SimpleNamespace) -> None:
    fake.output = b'{"exact": true\n'
    with pytest.raises(ValueError):
        build(fake)
    assert fake.calls[-2:] == [("cancel", f"KILL QUERY WHERE query_id='{QID}' SYNC", None), ("reader-close",)]


@pytest.fixture
def native_binary() -> Path:
    value = environ.get("HL1_NATIVE_BINARY")
    if not value:
        pytest.skip("native adapter requires explicit HL1_NATIVE_BINARY; never compiles locally")
    return Path(value)


def own_oracle(own: dict, patterns: tuple[str, ...], bounds: list[list]) -> list[dict]:
    results = []
    for q, pattern in enumerate(patterns, 1):
        rows = []
        for pre, post, bucket in bounds:
            values = [value for path, value in own.items()
                      if path.split("/")[0] == bucket and pattern.lower() in path.lower()]
            rows.append({"pre": pre, "post": post, "path": bucket,
                         "b": sum(value[0] for value in values), "o": sum(value[1] for value in values)})
        results.append({"predicate_id": q, "pattern": pattern.lower(),
                        "root": {"b": sum(row["b"] for row in rows), "o": sum(row["o"] for row in rows)}, "buckets": rows})
    return results


@pytest.mark.skipif(not environ.get("HL1_NATIVE_BINARY"), reason="native adapter acceptance requires explicit HL1_NATIVE_BINARY")
@pytest.mark.parametrize("date", ["2026-10-04", "2026-10-05"])
def test_real_adapter_all_queries_and_buckets_match_own_oracle_on_two_dates(
    native_binary: Path,
    fleet: dict,
    ch_db: str,
    ch_url: str,
    date: str,
) -> None:
    patterns = (".JSON", "json", "hit.json", "%", "_", "bucket", "bucket-a", "absent", "Å", "åRo")
    ch = Ch(ch_url, db=ch_db)
    try:
        bounds = ch.json("SELECT pre,post,path FROM dictionary WHERE depth=1 ORDER BY path")
        body = module.build(ch, ch_db, date, patterns, binary=native_binary)
        own = fleet["own"] if date == "2026-10-04" else fleet["after"]
        assert body["results"] == own_oracle(own, patterns, bounds)
        assert (body["schema"], body["target"], body["date"], body["exact"], body["incremental"], body["levels"]) == (
            "hot-l1-batch-stream-v1", ch_db, date, True, False, 1,
        )
        assert body["source_validation"]["rows"] == int(ch.scalar(f"SELECT count() FROM {body['snapshot_db']}.nodes"))
        assert (body["native"]["nodes_read"], body["native"]["registered_predicates"], body["compiled_patterns"]) == (
            body["source_validation"]["rows"], len(patterns), len(patterns),
        )
        assert type(body["native"]["native_peak_rss_bytes"]) is int
        assert body["native"]["native_peak_rss_bytes"] > 0
    finally:
        ch.close()


@pytest.mark.skipif(not environ.get("HL1_NATIVE_BINARY"), reason="native adapter acceptance requires explicit HL1_NATIVE_BINARY")
@pytest.mark.parametrize("large_catalog", [False, True])
def test_real_adapter_unicode_punctuation_or_above_256_ids_match_own_oracle(
    native_binary: Path,
    literals: dict,
    ch_url: str,
    large_catalog: bool,
) -> None:
    patterns = (tuple(f"never-{i:03d}" for i in range(299)) + ("last-299",) if large_catalog else
                ("q.q", "🙂", "Å", "åRo", "line\nbreak", "[+]", "\\", "repeat", "%_", "a", "absent"))
    ch = Ch(ch_url)
    try:
        body = module.build(ch, literals["db"], "2026-10-05", patterns, binary=native_binary)
        assert body["results"] == own_oracle(literals["own"], patterns, [[1, literals["post"], "a"]])
        assert body["compiled_patterns"] == len(patterns)
    finally:
        ch.close()
