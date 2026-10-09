"""Closure proof binding, transport refusals and native missing-parent fixtures."""

from hashlib import sha256
from io import BytesIO
from json import dumps, loads
from pathlib import Path
from os import environ
from re import fullmatch
from struct import pack
from subprocess import run
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import hot_l1_prefix_audit as module
from dt_cloud.chstore.hot_l1_batch import Bucket, Node, Predicate, build
from dt_cloud.chstore.client import Ch, lit
from test_chhot_l1_batch_catalog import stream_artifact, write
from test_chhot_l1_native_stream import binary  # noqa: F401
from chserver import ch_db, ch_url  # noqa: F401
from test_chhot_l1 import fleet  # noqa: F401


TAG = "hot_l1_prefix_" + "a" * 32
NATIVE = {"schema": "hot-l1-native-prefix-v1", "complete": True, "prefix_closed": True, "nodes_read": 9, "peak_stack": 3}


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    events, artifact = [], stream_artifact()
    accepted = write(tmp_path / "accepted.json", artifact)
    executable = tmp_path / "native"
    executable.touch()
    executable.chmod(0o700)
    state = SimpleNamespace(events=events, artifact=artifact, accepted=accepted, binary=executable, nodes=9, native=dict(NATIVE), error=None, rc=0, killed=False, early=False)

    class Client:
        timeout = 10

        def scalar(self, sql: str) -> str:
            events.append(("manifest", sql))
            return dumps({"prefix": "", "dates": ["2026-10-05"], "dbs": ["snapshot_20261005"]})

        def json(self, sql: str) -> list:
            events.append(("json", sql))
            if len([e for e in events if e[0] == "json"]) == 1:
                return [[1, 4, "a"], [5, 8, "b"]]
            if len([e for e in events if e[0] == "json"]) == 2:
                return [[0, 8]]
            return [[state.nodes, 0, 1, 8, 0]]

        def fork(self, **kwargs: object):
            events.append(("fork", kwargs))
            return self

        def stream(self, sql: str, fmt: str):
            events.append(("stream", sql, fmt))
            if state.error:
                raise state.error
            yield b"nodechunk"

        def exec(self, sql: str, **kwargs: object) -> None:
            events.append(("cancel", sql, kwargs))
            state.killed = True

        def close(self) -> None:
            events.append(("close",))

    class Input(BytesIO):
        def write(self, value: bytes) -> int:
            if value == b"nodechunk" and state.early:
                raise BrokenPipeError("early refusal")
            return super().write(value)

        def close(self) -> None:
            state.control = self.getvalue()
            super().close()

    class Child:
        def __init__(self, args: list[str], **kwargs: object) -> None:
            events.append(("spawn", args))
            self.stdin, self.stdout, self.stderr = Input(), BytesIO(), BytesIO(b"hot-l1-native: prefix node lacks its present immediate parent\n")
            self.returncode = None

        def communicate(self, **kwargs: object):
            assert self.stdin is None
            events.append(("communicate", kwargs))
            self.returncode = state.rc
            return dumps(state.native).encode(), b"hot-l1-native: fixture refusal\n" if state.rc else b""

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            events.append(("terminate",))

        def wait(self, **kwargs: object) -> None:
            self.returncode = -15
            events.append(("wait", kwargs))

    state.client = Client()
    monkeypatch.setattr(module, "Popen", Child)
    monkeypatch.setattr(module, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    monkeypatch.setattr(module, "monotonic", lambda: 100.0)
    return state


def test_adapter_binds_accepted_hash_counts_bounds_and_complete_native_proof(fake: SimpleNamespace) -> None:
    result = module.audit(fake.client, "fleet", "2026-10-05", fake.accepted, binary=fake.binary)
    data = fake.accepted.read_bytes()
    assert result == {"schema": "hot-l1-prefix-proof-v1", "complete": True, "prefix_closed": True, "target": "fleet", "date": "2026-10-05",
                      "snapshot_db": "snapshot_20261005", "nodes_read": 9, "buckets": [{"pre": 1, "post": 4, "path": "a"}, {"pre": 5, "post": 8, "path": "b"}],
                      "accepted_artifact": {"path": str(fake.accepted), "sha256": sha256(data).hexdigest(), "bytes": len(data)},
                      "source_contract": module.SOURCE_CONTRACT, "source_query_id": TAG, "native": NATIVE, "audit_s": 0.0}
    assert fake.control == b"HL1PRE01" + pack("<QB", 9, 2) + pack("<QQQQ", 1, 4, 5, 8) + b"nodechunk"
    assert [event for event in fake.events if event[0] == "stream"] == [("stream", """SELECT assumeNotNull(toUInt64(pre)),assumeNotNull(toUInt64(post)),assumeNotNull(toUInt8(depth))
            FROM snapshot_20261005.nodes ORDER BY pre""", "RowBinary")]
    assert [event for event in fake.events if event[0] == "spawn"] == [("spawn", [str(fake.binary.resolve()), "--prefix-audit"])]
    assert fake.killed is False
    assert fake.events[-1] == ("close",)


@pytest.mark.parametrize("change,message", [
    ("date", "prefix audit accepted artifact target/date differs from request"),
    ("db", "prefix audit snapshot differs from accepted artifact"),
    ("count", "prefix audit source count/scalars/root disagree with accepted snapshot"),
])
def test_identity_and_count_disagreement_refuse_before_spawn(fake: SimpleNamespace, change: str, message: str) -> None:
    date = "2026-10-04" if change == "date" else "2026-10-05"
    if change == "db":
        fake.artifact["snapshot_db"] = "other_snapshot"
        write(fake.accepted, fake.artifact)
    elif change == "count":
        fake.nodes = 8
    with pytest.raises(ValueError) as caught:
        module.audit(fake.client, "fleet", date, fake.accepted, binary=fake.binary)
    assert str(caught.value) == message
    assert [e for e in fake.events if e[0] == "spawn"] == []


@pytest.mark.parametrize("mode", ["source", "consumer", "incomplete", "early"])
def test_failures_cancel_only_owned_stream_and_never_write_proof(fake: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    if mode == "source":
        fake.error = OSError("source failed")
    elif mode == "consumer":
        fake.rc = 1
    elif mode == "early":
        fake.early = True
    else:
        fake.native["complete"] = False
    monkeypatch.setattr(module, "Ch", lambda *args, **kwargs: fake.client)
    out = tmp_path / "proof.json"
    with pytest.raises(OSError if mode == "source" else RuntimeError) as caught:
        module.bench("http://unused", "fleet", "2026-10-05", fake.accepted, out, binary=fake.binary)
    assert str(caught.value) == {"source": "source failed", "consumer": "native prefix audit failed: hot-l1-native: fixture refusal", "incomplete": "native prefix audit output disagrees with the complete source", "early": "native prefix audit refused its input: hot-l1-native: prefix node lacks its present immediate parent"}[mode]
    assert [e for e in fake.events if e[0] == "cancel"] == [("cancel", f"KILL QUERY WHERE query_id='{TAG}' SYNC", {"fmt": None})]
    assert out.exists() is False


def test_proof_written_only_after_success_and_existing_file_preserved(fake: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "Ch", lambda *args, **kwargs: fake.client)
    out = tmp_path / "proof.json"
    result = module.bench("http://unused", "fleet", "2026-10-05", fake.accepted, out, binary=fake.binary)
    assert out.read_text() == dumps(result) + "\n"
    with pytest.raises(ValueError) as caught:
        module.bench("http://unused", "fleet", "2026-10-05", fake.accepted, out, binary=fake.binary)
    assert str(caught.value) == "prefix proof output must be new"
    assert loads(out.read_text()) == result


def test_cli_exact_forwarding_and_output(monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud.cli import main

    calls = []
    monkeypatch.setattr(module, "bench", lambda *args, **kwargs: calls.append((args, kwargs)) or {"complete": True})
    result = CliRunner().invoke(main, ["ch-hot-l1-prefix-audit", "fleet", "-a", "accepted.json", "-b", "native", "-d", "2026-10-05", "-o", "proof.json"])
    assert (result.exit_code, result.stdout, result.stderr) == (0, '{"complete": true}\n', "")
    assert calls == [(("http://localhost:8123", "fleet", "2026-10-05", Path("accepted.json"), Path("proof.json")), {"binary": Path("native"), "seconds": 1800})]


def prefix_payload(rows: list[tuple[int, int, int]], *, count: int | None = None) -> bytes:
    return b"HL1PRE01" + pack("<QBQQ", len(rows) if count is None else count, 1, 1, 3) + b"".join(pack("<QQB", *row) for row in rows)


def test_missing_parent_can_evade_scalar_checks_and_miss_ancestor_name() -> None:
    nodes = [Node(0, 3, "", 9, 1), Node(1, 3, "bucket", 9, 1), Node(3, 3, "leaf", 9, 1)]
    result = build(nodes, [Bucket("bucket", 1, 3)], [Predicate(1, "hit")], expected_nodes=3)
    assert result["results"] == [{"predicate_id": 1, "pattern": "hit", "root": {"b": 0, "o": 0}, "buckets": [{"path": "bucket", "pre": 1, "post": 3, "b": 0, "o": 0}]}]
    own = {"bucket/hit/leaf": (9, 1)}
    assert [weights for path, weights in own.items() if "hit" in path] == [(9, 1)]


def test_native_prefix_success(binary: str) -> None:
    rows = [(0, 3, 0), (1, 3, 1), (2, 3, 2), (3, 3, 3)]
    result = run([binary, "--prefix-audit"], input=prefix_payload(rows), capture_output=True, timeout=10)
    assert (result.returncode, result.stderr, loads(result.stdout)) == (0, b"", {"schema": "hot-l1-native-prefix-v1", "complete": True, "prefix_closed": True, "nodes_read": 4, "peak_stack": 3})


@pytest.mark.parametrize("mode,message", [
    ("missing", "prefix node lacks its present immediate parent"),
    ("depth", "prefix stream requires the complete depth-zero global root"),
    ("truncated", "truncated input"),
    ("trailing", "trailing input after prefix declared node count"),
    ("magic", "invalid prefix protocol magic"),
    ("unknown", "unsupported native arguments"),
])
def test_native_prefix_refusals_without_partial_output(binary: str, mode: str, message: str) -> None:
    rows = [(0, 3, 0), (1, 3, 1), (2, 3, 2), (3, 3, 3)]
    if mode == "missing":
        del rows[2]
    elif mode == "depth":
        rows[0] = 0, 3, 1
    data = prefix_payload(rows)
    if mode == "truncated":
        data = data[:-1]
    elif mode == "trailing":
        data += b"x"
    elif mode == "magic":
        data = b"BADMAGIC" + data[8:]
    result = run([binary, "--unknown" if mode == "unknown" else "--prefix-audit"], input=data, capture_output=True, timeout=10)
    assert (result.returncode, result.stdout, result.stderr) == (1, b"", f"hot-l1-native: {message}\n".encode())


@pytest.fixture(scope="module")
def prefix_fleet(request: pytest.FixtureRequest):
    value = environ.get("HL1_NATIVE_BINARY")
    if not value:
        pytest.skip("CH prefix adapter requires explicit HL1_NATIVE_BINARY; skip before starting ClickHouse")
    executable = Path(value)
    assert executable.is_file() is True
    # Dynamic fixture access deliberately follows the binary gate, so a local
    # server is never started for an acceptance test that cannot run its engine.
    source = request.getfixturevalue("fleet")
    url, db = request.getfixturevalue("ch_url"), request.getfixturevalue("ch_db")
    target, after = db + "_prefix", db + "_prefix_after"
    ch = Ch(url)
    try:
        for output, original in ((target, db), (after, db + "_after")):
            ch.exec(f"CREATE DATABASE {output}")
            ch.exec(f"CREATE TABLE {output}.nodes ENGINE=MergeTree ORDER BY pre AS SELECT *, toUInt8(if(path='',0,length(path)-length(replaceAll(path,'/',''))+1)) AS depth FROM {original}.nodes")
        ch.exec(f"CREATE VIEW {target}.dictionary AS SELECT * FROM {db}.dictionary")
        ch.exec(f"CREATE TABLE {target}.history_manifest (doc String) ENGINE=Memory")
        ch.exec(f"INSERT INTO {target}.history_manifest VALUES (" + lit(dumps({"prefix": "", "dates": ["2026-10-04", "2026-10-05"], "dbs": [target, after]})) + ")")
        yield {"binary": executable, "url": url, "target": target, "after": after, "source": source}
    finally:
        for output in (after, target):
            ch.exec(f"DROP DATABASE IF EXISTS {output} SYNC")
        ch.close()


@pytest.mark.parametrize("date", ["2026-10-04", "2026-10-05"])
def test_real_ch_rowbinary_uint8_depth_adapter_acceptance(prefix_fleet: dict, tmp_path: Path, date: str) -> None:
    from dt_cloud.chstore.hot_l1_batch_sql import build as batch
    from dt_cloud.chstore.hot_l1_batch_catalog import VALIDATION

    target, db = prefix_fleet["target"], prefix_fleet["target"] if date == "2026-10-04" else prefix_fleet["after"]
    own = prefix_fleet["source"]["own" if date == "2026-10-04" else "after"]
    union, present = {""}, {""}
    for values, paths in ((prefix_fleet["source"]["own"], union), (own, present)):
        for path in values:
            parts = path.split("/")
            paths.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    ordered = sorted(union)
    bounds = [{"pre": i, "post": max(j for j, child in enumerate(ordered) if child == path or child.startswith(path + "/")), "path": path}
              for i, path in enumerate(ordered) if path and "/" not in path]
    ch = Ch(prefix_fleet["url"], db=target)
    try:
        body = batch(ch, target, date, (".json", "bucket"))
        body.update(queries={"path": "fixture.jsonl", "patterns": 2,
                             "header": {"schema": "hot-frequency-queries-v1", "target": target, "date": date, "threshold_paths": 1, "max_chars": 7}},
                    validation={"description": VALIDATION, "references": [], "independently_scanned_entire_catalog": False})
        assert body["source_validation"]["rows"] == len(present)
        accepted = write(tmp_path / f"accepted-{date}.json", body)
        proof = module.audit(ch, target, date, accepted, binary=prefix_fleet["binary"])
        data = accepted.read_bytes()
        assert {key: value for key, value in proof.items() if key not in ("source_query_id", "audit_s")} == {
            "schema": "hot-l1-prefix-proof-v1", "complete": True, "prefix_closed": True, "target": target, "date": date,
            "snapshot_db": db, "nodes_read": len(present), "buckets": bounds,
            "accepted_artifact": {"path": str(accepted), "sha256": sha256(data).hexdigest(), "bytes": len(data)},
            "source_contract": module.SOURCE_CONTRACT,
            "native": {"schema": "hot-l1-native-prefix-v1", "complete": True, "prefix_closed": True, "nodes_read": len(present),
                       "peak_stack": max(len(path.split("/")) for path in own)},
        }
        assert fullmatch(r"hot_l1_prefix_[0-9a-f]{32}", proof["source_query_id"]) is not None
        assert type(proof["audit_s"]) is float and proof["audit_s"] >= 0
        assert [{"path": row["path"], "b": row["b"], "o": row["o"]} for row in body["results"][1]["buckets"]] == [
            {"path": row["path"], "b": sum(value[0] for path, value in own.items() if path.split("/")[0] == row["path"]),
             "o": sum(value[1] for path, value in own.items() if path.split("/")[0] == row["path"])} for row in bounds
        ]
    finally:
        ch.close()
