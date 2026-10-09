"""Native protocol fixtures; executable tests require HL1_NATIVE_BINARY."""

from dataclasses import replace
from json import loads
from os import environ
from pathlib import Path
from struct import pack
from subprocess import run

import pytest

from dt_cloud.chstore.hot_l1_batch import Bucket, Node, Predicate, build
from test_chhot_l1_batch import OWN, PATTERNS, expected, fixture


def string(value: str) -> bytes:
    raw = value.encode("utf-8")
    length, prefix = len(raw), bytearray()
    while length >= 128:
        prefix.append((length & 127) | 128)
        length >>= 7
    prefix.append(length)
    return bytes(prefix) + raw


def payload(
    nodes: list[Node],
    buckets: list[Bucket],
    patterns: tuple[str, ...],
    *,
    count: int | None = None,
) -> bytes:
    return (b"HL1DFS01" + pack("<QIB", len(nodes) if count is None else count, len(patterns), len(buckets)) +
            b"".join(pack("<QQ", bucket.pre, bucket.post) for bucket in buckets) + b"".join(map(string, patterns)) +
            b"".join(pack("<QQQQ", node.pre, node.post, node.b, node.o) + string(node.name) for node in nodes))


@pytest.fixture
def binary() -> str:
    value = environ.get("HL1_NATIVE_BINARY")
    if not value:
        pytest.skip("native fixture requires explicit HL1_NATIVE_BINARY; never compiles locally")
    assert Path(value).is_file() is True
    return value


def invoke(binary: str, data: bytes):
    return run([binary], input=data, capture_output=True, timeout=10, check=False)


def test_protocol_is_fixed_little_endian_with_rowbinary_utf8_strings() -> None:
    assert payload([Node(0, 1, "", 7, 2)], [Bucket("a", 1, 1)], ("å",)) == (
        b"HL1DFS01" + b"\x01\0\0\0\0\0\0\0" + b"\x01\0\0\0\x01" +
        b"\x01\0\0\0\0\0\0\0\x01\0\0\0\0\0\0\0" + b"\x02\xc3\xa5" +
        b"\0\0\0\0\0\0\0\0\x01\0\0\0\0\0\0\0\x07\0\0\0\0\0\0\0\x02\0\0\0\0\0\0\0\0"
    )
    assert string("x" * 128) == b"\x80\x01" + b"x" * 128


@pytest.mark.parametrize("own", [OWN, {path: value for path, value in OWN.items() if path.startswith("b/")}, {}])
def test_complete_native_matrix_matches_independent_own_object_oracle(binary: str, own: dict) -> None:
    nodes, buckets = fixture(own)
    result = invoke(binary, payload(nodes, buckets, tuple(pattern.text for pattern in PATTERNS)))
    assert (result.returncode, result.stderr) == (0, b"")
    body = loads(result.stdout)
    assert body["matrix"] == [{"predicate_id": q, "buckets": [[str(row["b"]), str(row["o"])] for row in item["buckets"]]}
                              for q, item in enumerate(expected(own, buckets), 1)]
    reference = build(nodes, buckets, [Predicate(q, p.text) for q, p in enumerate(PATTERNS, 1)], expected_nodes=len(nodes))
    rss = body.pop("native_peak_rss_bytes")
    assert type(rss) is int and rss > 0
    assert body == {"schema": "hot-l1-native-stream-v1", "exact": True, "incremental": False, "levels": 1,
                    "nodes_read": len(nodes), "registered_predicates": len(PATTERNS), "peak_stack": reference["peak_stack_depth"],
                    "peak_active": reference["peak_active_predicates"], "matrix": body["matrix"]}


def test_native_unicode_literals_repeats_empty_names_and_max_uint64(binary: str) -> None:
    maximum = (1 << 64) - 1
    nodes = [Node(0, 3, "", maximum, maximum), Node(1, 3, "bucket", maximum, maximum),
             Node(2, 2, "åro🙂\n[+]\\aaaa", maximum, maximum), Node(3, 3, "", 0, 0)]
    patterns = ("å", "åro", "🙂", "\n", "[+]", "\\", "a", "aa", "aaa", "aaaa", "absent")
    result = invoke(binary, payload(nodes, [Bucket("bucket", 1, 3)], patterns))
    assert (result.returncode, result.stderr) == (0, b"")
    assert loads(result.stdout)["matrix"] == [{"predicate_id": q, "buckets": [[str(maximum if q <= 10 else 0), str(maximum if q <= 10 else 0)]]}
                                             for q in range(1, 12)]


@pytest.mark.parametrize("change,message", [
    ("truncate", "truncated input"),
    ("count-over", "truncated input"),
    ("count-under", "trailing input after declared node count"),
    ("trailing", "trailing input after declared node count"),
    ("magic", "invalid protocol magic"),
    ("duplicate", "query literals must be unique"),
    ("query-slash", "queries must be nonempty valid UTF-8 NUL/slash-free literals"),
    ("query-nul", "queries must be nonempty valid UTF-8 NUL/slash-free literals"),
    ("utf8", "node names must be valid UTF-8 slash-free strings"),
    ("slash", "node names must be valid UTF-8 slash-free strings"),
    ("order", "node order or interval bounds are invalid"),
    ("cross", "node intervals cross rather than nest"),
    ("root", "stream must start with the empty-name complete global root"),
    ("missing-bucket", "descendant has no present bucket root"),
    ("rollup", "child rollups exceed their parent"),
    ("overflow-rollup", "child rollups exceed their parent"),
    ("root-sum", "bucket rollups disagree with the global root"),
    ("buckets", "bucket intervals must partition the ordered global domain"),
])
def test_invalid_streams_refuse_without_partial_stdout(binary: str, change: str, message: str) -> None:
    nodes = [Node(0, 5, "", 10, 2), Node(1, 5, "bucket", 10, 2), Node(2, 3, "nested", 3, 1),
             Node(3, 3, "hit", 3, 1), Node(4, 5, "another", 7, 1), Node(5, 5, "hit", 7, 1)]
    buckets, patterns = [Bucket("bucket", 1, 5)], ("hit",)
    if change == "duplicate":
        patterns = ("hit", "hit")
    elif change == "query-slash":
        patterns = ("a/b",)
    elif change == "query-nul":
        patterns = ("a\0b",)
    elif change == "slash":
        nodes[3] = replace(nodes[3], name="a/b")
    elif change == "order":
        nodes[3] = replace(nodes[3], pre=2)
    elif change == "cross":
        nodes[3] = replace(nodes[3], post=4)
    elif change == "root":
        nodes[0] = replace(nodes[0], name="root")
    elif change == "missing-bucket":
        del nodes[1]
    elif change == "rollup":
        nodes[3] = replace(nodes[3], b=4)
    elif change == "overflow-rollup":
        maximum = (1 << 64) - 1
        nodes = [Node(0, 3, "", maximum, 2), Node(1, 3, "bucket", maximum, 2),
                 Node(2, 2, "hit", maximum, 1), Node(3, 3, "hit", maximum, 1)]
        buckets = [Bucket("bucket", 1, 3)]
    elif change == "root-sum":
        nodes[0] = replace(nodes[0], b=11)
    elif change == "buckets":
        buckets = [Bucket("bucket", 2, 5)]
    count = len(nodes) + 1 if change == "count-over" else len(nodes) - 1 if change == "count-under" else len(nodes)
    data = payload(nodes, buckets, patterns, count=count)
    if change == "truncate":
        data = data[:-1]
    elif change == "trailing":
        data += b"x"
    elif change == "magic":
        data = b"BADMAGIC" + data[8:]
    elif change == "utf8":
        data = data[:-3] + b"\xffit"
    result = invoke(binary, data)
    assert (result.returncode, result.stdout, result.stderr) == (1, b"", f"hot-l1-native: {message}\n".encode())
