"""Paired native fixtures require HL2_NATIVE_BINARY; never compile locally.

Own-object full-path predicates are the independent oracle. The wire validates
declared bucket/frame parents; deeper prefix closure is externally proved.
"""

from dataclasses import replace
from json import loads
from os import environ
from pathlib import Path
from struct import pack
from subprocess import run
from tempfile import TemporaryFile

import pytest

from dt_cloud.chstore.hot_l1_batch import Bucket, Node
from test_chhot_l1_native_stream import string


BEFORE = {"a": (3, 1), "a/hit": (0, 1), "a/hit/hit.json": (40, 1), "a/hit/plain": (10, 1),
          "a/aaaa": (2, 1), "a/ÅRO/Blå.json": (7, 1), "b": (5, 1),
          "b/nest/hit.json": (0, 2), "b/nest/%_": (3, 1), "b/other": (25, 1), "d": (9, 1)}
AFTER = {"a": (4, 1), "a/hit": (0, 1), "a/hit/plain": (1, 1), "a/aaaa": (3, 1),
         "a/ÅRO/Blå.json": (8, 1), "b": (0, 3), "b/nest/hit.json": (0, 5),
         "b/nest/%_": (0, 2), "b/other": (2, 1), "c/addedhit": (20, 1), "d": (0, 4)}
PATTERNS = ("hit", ".json", "a", "aa", "aaa", "å", "%_", "absent", "b", "d")


def fixture(before: dict, after: dict) -> tuple[list[Node], list[Node], list[Bucket], list[tuple[int, int, int]], list[str]]:
    paths = {""}
    for path in before.keys() | after.keys():
        parts = path.split("/")
        paths.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    paths = sorted(paths)
    bounds = {path: max(i for i, child in enumerate(paths) if not path or child == path or child.startswith(path + "/")) for path in paths}
    buckets = [Bucket(path, i, bounds[path]) for i, path in enumerate(paths) if path and "/" not in path]
    frames = [(i, bounds[path], next(j for j, bucket in enumerate(buckets) if path.startswith(bucket.path + "/")))
              for i, path in enumerate(paths) if path.count("/") == 1]

    def nodes(own: dict) -> list[Node]:
        result = []
        for pre, path in enumerate(paths):
            values = [value for child, value in own.items() if not path or child == path or child.startswith(path + "/")]
            if path and not values:
                continue
            result.append(Node(pre, bounds[path], path.rsplit("/", 1)[-1].lower(), sum(v[0] for v in values), sum(v[1] for v in values)))
        return result

    return nodes(before), nodes(after), buckets, frames, paths


def own_totals(own: dict, path: str, pattern: str) -> tuple[int, int]:
    values = [value for child, value in own.items()
              if (child == path or child.startswith(path + "/")) and pattern in child.lower()]
    return sum(v[0] for v in values), sum(v[1] for v in values)


def cutoffs(before: dict, after: dict, buckets: list[Bucket], patterns: tuple[str, ...], k: int = 4) -> list[list[int]]:
    return [[max(1, (max(own_totals(before, bucket.path, pattern)[0], own_totals(after, bucket.path, pattern)[0]) + k - 1) // k)
             for bucket in buckets] for pattern in patterns]


def expected(
    before: dict,
    after: dict,
    buckets: list[Bucket],
    frames: list[tuple[int, int, int]],
    paths: list[str],
    patterns: tuple[str, ...],
    taus: list[list[int]],
) -> dict:
    roots = [{"predicate_id": q + 1, "buckets": [[str(value) for value in (*own_totals(before, bucket.path, pattern), *own_totals(after, bucket.path, pattern))]
                                                 for bucket in buckets]} for q, pattern in enumerate(patterns)]
    cells = []
    for f, (pre, _, bucket) in enumerate(frames):
        for q, pattern in enumerate(patterns):
            b0, o0 = own_totals(before, paths[pre], pattern)
            b1, o1 = own_totals(after, paths[pre], pattern)
            if max(b0, b1) >= taus[q][bucket]:
                cells.append({"predicate_id": q + 1, "frame_id": f + 1, "b": [str(b0), str(b1)], "o": [str(o0), str(o1)]})
    return {"roots": roots, "cells": cells}


def control(
    left: list[Node],
    right: list[Node],
    buckets: list[Bucket],
    frames: list[tuple[int, int, int]],
    patterns: tuple[str, ...],
    taus: list[list[int]],
    *,
    counts: tuple[int, int] | None = None,
) -> bytes:
    n0, n1 = (len(left), len(right)) if counts is None else counts
    return (b"HL2PAIR1" + pack("<QQIIB", n0, n1, len(patterns), len(frames), len(buckets)) +
            b"".join(pack("<QQ", bucket.pre, bucket.post) for bucket in buckets) +
            b"".join(pack("<QQI", *frame) for frame in frames) +
            b"".join(string(pattern) + b"".join(pack("<Q", tau) for tau in row) for pattern, row in zip(patterns, taus, strict=True)))


def records(nodes: list[Node]) -> bytes:
    return b"".join(pack("<QQQQ", node.pre, node.post, node.b, node.o) + string(node.name) for node in nodes)


def matcher_counts(left: list[Node], right: list[Node]) -> dict:
    names = [name for pre, _, name in sorted((node.pre, side, node.name) for side, rows in enumerate((left, right)) for node in rows) if pre != 0]
    scans = sum(i == 0 or name != names[i - 1] for i, name in enumerate(names))
    return {"matcher_scans": scans, "cache_hits": len(names) - scans}


@pytest.fixture
def binary() -> str:
    value = environ.get("HL2_NATIVE_BINARY")
    if not value:
        pytest.skip("paired native fixture requires explicit HL2_NATIVE_BINARY; never compiles locally")
    assert Path(value).is_file() is True
    return value


def invoke(binary: str, header: bytes, left: bytes, right: bytes, tmp_path: Path, *, args: tuple[str, ...] = ()):
    with TemporaryFile(dir=tmp_path) as a, TemporaryFile(dir=tmp_path) as b:
        a.write(left)
        b.write(right)
        a.seek(0)
        b.seek(0)
        return run([binary, "--left-fd", str(a.fileno()), "--right-fd", str(b.fileno()), *args], input=header,
                   pass_fds=(a.fileno(), b.fileno()), capture_output=True, timeout=10, check=False)


def test_control_fixed_little_endian_and_per_bucket_taus() -> None:
    left, right = [Node(0, 2, "", 1, 1)], [Node(0, 2, "", 0, 0)]
    assert control(left, right, [Bucket("a", 1, 1), Bucket("b", 2, 2)], [], ("å",), [[7, 9]]) == (
        b"HL2PAIR1" + pack("<QQIIB", 1, 1, 1, 0, 2) + pack("<QQQQ", 1, 1, 2, 2) + b"\x02\xc3\xa5" + pack("<QQ", 7, 9)
    )
    assert records(left) == pack("<QQQQ", 0, 2, 1, 1) + b"\0"


@pytest.mark.parametrize("before,after", [(BEFORE, AFTER), ({}, AFTER), (BEFORE, {}), ({"d": (0, 3)}, {"d": (0, 7)})])
def test_complete_roots_and_sparse_paired_cells_match_independent_own_object_oracle(
    binary: str,
    tmp_path: Path,
    before: dict,
    after: dict,
) -> None:
    left, right, buckets, frames, paths = fixture(before, after)
    taus = cutoffs(before, after, buckets, PATTERNS)
    result = invoke(binary, control(left, right, buckets, frames, PATTERNS, taus), records(left), records(right), tmp_path)
    assert (result.returncode, result.stderr) == (0, b"")
    body = loads(result.stdout)
    stats = {key: body.pop(key) for key in ("peak_stack", "peak_active", "native_peak_rss_bytes")}
    assert [type(stats["native_peak_rss_bytes"]) is int, stats["native_peak_rss_bytes"] > 0,
            *[type(value) is int and 1 <= value <= len(nodes) for value, nodes in zip(stats["peak_stack"], (left, right), strict=True)],
            *[type(value) is int and 0 <= value <= len(PATTERNS) for value in stats["peak_active"]]] == [True] * 6
    oracle = expected(before, after, buckets, frames, paths, PATTERNS, taus)
    assert body == {"schema": "hot-l2-native-pair-v2", "exact": True, "incremental": False, "levels": 2,
                    "rows_read": [len(left), len(right)], "registered_predicates": len(PATTERNS), "registered_frames": len(frames),
                    "max_cells": 10_000_000, "emitted_cells": len(oracle["cells"]), **matcher_counts(left, right), **oracle}


def test_independent_fixture_retains_small_counterpart_bucket_self_and_zero_byte_remainder() -> None:
    left, right, buckets, frames, paths = fixture(BEFORE, AFTER)
    patterns = ("hit", "b", "d")
    oracle = expected(BEFORE, AFTER, buckets, frames, paths, patterns, cutoffs(BEFORE, AFTER, buckets, patterns))
    hit = next(row for row in oracle["cells"] if paths[frames[row["frame_id"] - 1][0]] == "a/hit" and row["predicate_id"] == 1)
    assert hit == {"predicate_id": 1, "frame_id": 2, "b": ["50", "1"], "o": ["3", "2"]}
    assert oracle["roots"][2] == {"predicate_id": 3, "buckets": [["0", "0", "0", "0"], ["0", "0", "0", "0"],
                                                               ["0", "0", "20", "1"], ["9", "1", "0", "4"]]}
    b_bucket = next(j for j, bucket in enumerate(buckets) if bucket.path == "b")
    b_cells = [row for row in oracle["cells"] if row["predicate_id"] == 2 and frames[row["frame_id"] - 1][2] == b_bucket]
    assert b_cells == [{"predicate_id": 2, "frame_id": 5, "b": ["25", "2"], "o": ["1", "1"]}]
    b_root = [int(value) for value in oracle["roots"][1]["buckets"][b_bucket]]
    assert [b_root[0] - 25, b_root[1] - 1, b_root[2] - 2, b_root[3] - 1] == [8, 4, 0, 10]
    assert (len(left), len(right)) == (14, 15)


def test_unicode_literal_punctuation_repeats_and_uint64_limit(binary: str, tmp_path: Path) -> None:
    maximum = (1 << 64) - 1
    own = {"bucket/åro🙂\n[+]\\aaaa": (maximum, maximum)}
    left, right, buckets, frames, paths = fixture(own, own)
    patterns = ("å", "åro", "🙂", "\n", "[+]", "\\", "a", "aa", "aaa", "aaaa", "absent")
    taus = [[1] for _ in patterns]
    result = invoke(binary, control(left, right, buckets, frames, patterns, taus), records(left), records(right), tmp_path)
    assert (result.returncode, result.stderr) == (0, b"")
    body = loads(result.stdout)
    assert {key: body[key] for key in ("roots", "cells")} == expected(own, own, buckets, frames, paths, patterns, taus)
    assert {key: body[key] for key in ("matcher_scans", "cache_hits")} == matcher_counts(left, right)


def test_cached_raw_hits_reapply_fresh_ancestor_context_and_dated_weights(binary: str, tmp_path: Path) -> None:
    before = {"bucket/hit": (0, 1), "bucket/hit/hit": (5, 1), "bucket/plain/hit": (7, 1)}
    after = {"bucket/hit/hit": (0, 4), "bucket/plain/hit": (2, 1)}
    left, right, buckets, frames, paths = fixture(before, after)
    patterns = ("hit", "it", "h", "plain", "absent")
    taus = cutoffs(before, after, buckets, patterns, k=2)
    result = invoke(binary, control(left, right, buckets, frames, patterns, taus), records(left), records(right), tmp_path)
    assert (result.returncode, result.stderr) == (0, b"")
    body = loads(result.stdout)
    assert {key: body[key] for key in ("roots", "cells")} == expected(before, after, buckets, frames, paths, patterns, taus)
    assert {key: body[key] for key in ("matcher_scans", "cache_hits")} == {"matcher_scans": 4, "cache_hits": 6}


def test_first_empty_nonroot_name_is_a_scan_not_an_uninitialized_cache_hit(binary: str, tmp_path: Path) -> None:
    left = [Node(0, 3, "", 5, 2), Node(1, 3, "", 5, 2), Node(2, 3, "", 5, 2), Node(3, 3, "hit", 5, 2)]
    right = [replace(node, b=7, o=3) for node in left]
    buckets, frames = [Bucket("bucket", 1, 3)], [(2, 3, 0)]
    result = invoke(binary, control(left, right, buckets, frames, ("hit",), [[1]]), records(left), records(right), tmp_path)
    assert (result.returncode, result.stderr) == (0, b"")
    body = loads(result.stdout)
    assert {key: body[key] for key in ("roots", "cells", "matcher_scans", "cache_hits")} == {
        "roots": [{"predicate_id": 1, "buckets": [["5", "2", "7", "3"]]}],
        "cells": [{"predicate_id": 1, "frame_id": 1, "b": ["5", "7"], "o": ["2", "3"]}],
        "matcher_scans": 2, "cache_hits": 4,
    }


def test_root_only_absent_pair_has_zero_matcher_counters(binary: str, tmp_path: Path) -> None:
    nodes, buckets = [Node(0, 1, "", 0, 0)], [Bucket("bucket", 1, 1)]
    result = invoke(binary, control(nodes, nodes, buckets, [], ("hit",), [[1]]), records(nodes), records(nodes), tmp_path)
    assert (result.returncode, result.stderr) == (0, b"")
    body = loads(result.stdout)
    assert {key: body[key] for key in ("roots", "cells", "matcher_scans", "cache_hits")} == {
        "roots": [{"predicate_id": 1, "buckets": [["0", "0", "0", "0"]]}], "cells": [], "matcher_scans": 0, "cache_hits": 0,
    }


@pytest.mark.parametrize("change,message", [
    ("magic", "invalid paired protocol magic"),
    ("control-trailing", "trailing control input"),
    ("control-truncate", "truncated input"),
    ("zero-threshold", "query threshold must be positive"),
    ("duplicate-query", "query literals must be unique"),
    ("query-slash", "queries must be bounded nonempty valid UTF-8 NUL/slash-free literals"),
    ("query-nul", "queries must be bounded nonempty valid UTF-8 NUL/slash-free literals"),
    ("frame-gap", "frames must partition complete ordered bucket descendants"),
    ("frame-overlap", "frames must partition complete ordered bucket descendants"),
    ("frame-bucket", "frames must partition complete ordered bucket descendants"),
    ("missing-bucket", "descendant has no present declared bucket root"),
    ("missing-frame", "descendant has no present declared frame root"),
    ("pair-name", "paired node declarations disagree for the same frozen position"),
    ("pair-post", "paired node declarations disagree for the same frozen position"),
    ("order", "dated node order or interval bounds are invalid"),
    ("rollup", "child rollups exceed their parent"),
    ("root-sum", "bucket rollups disagree with the global root"),
    ("truncate", "truncated input"),
    ("count-over", "truncated input"),
    ("count-under", "trailing dated input after declared node count"),
    ("trailing", "trailing dated input after declared node count"),
    ("utf8", "node names must be valid UTF-8 slash-free strings"),
    ("overflow-rollup", "child rollups exceed their parent"),
    ("cell-cap", "paired heavy-cell output exceeds its cap"),
    ("bad-cap", "heavy-cell cap must be from 1 to 10000000"),
    ("zero-count", "paired node/query/frame counts exceed protocol bounds"),
    ("query-cap", "paired node/query/frame counts exceed protocol bounds"),
    ("frame-cap", "paired node/query/frame counts exceed protocol bounds"),
])
def test_malformed_control_or_dated_sources_refuse_without_partial_stdout(binary: str, tmp_path: Path, change: str, message: str) -> None:
    left, right, buckets, frames, _ = fixture(BEFORE, AFTER)
    patterns, taus, counts, args = ("hit", "a"), [[1] * len(buckets)] * 2, None, ()
    if change == "zero-threshold":
        taus = [[0] * len(buckets)] * 2
    elif change == "duplicate-query":
        patterns = ("hit", "hit")
    elif change == "query-slash":
        patterns = ("a/b", "hit")
    elif change == "query-nul":
        patterns = ("a\0b", "hit")
    elif change == "frame-gap":
        frames[0] = (frames[0][0] + 1, frames[0][1], frames[0][2])
    elif change == "frame-overlap":
        frames[1] = (frames[0][0], frames[1][1], frames[1][2])
    elif change == "frame-bucket":
        frames[0] = (*frames[0][:2], len(buckets))
    elif change == "missing-bucket":
        del left[1]
    elif change == "missing-frame":
        pre = frames[1][0]
        left = [node for node in left if node.pre != pre]
    elif change == "pair-name":
        left[2] = replace(left[2], name="different")
    elif change == "pair-post":
        left[1] = replace(left[1], post=left[1].post - 1)
    elif change == "order":
        left[3] = replace(left[3], pre=0)
    elif change == "rollup":
        left[-1] = replace(left[-1], b=1000)
    elif change == "root-sum":
        left[0] = replace(left[0], b=left[0].b + 1)
    elif change == "count-over":
        counts = (len(left) + 1, len(right))
    elif change == "count-under":
        counts = (len(left) - 1, len(right))
    elif change == "zero-count":
        counts = (0, len(right))
    elif change == "overflow-rollup":
        maximum = (1 << 64) - 1
        left = [Node(0, 3, "", maximum, 2), Node(1, 3, "bucket", maximum, 2),
                Node(2, 2, "hit", maximum, 1), Node(3, 3, "hit", maximum, 1)]
        right = [Node(0, 3, "", 0, 0)]
        buckets, frames, taus = [Bucket("bucket", 1, 3)], [(2, 2, 0), (3, 3, 0)], [[1], [1]]
    elif change == "cell-cap":
        args = ("--max-cells", "1")
    elif change == "bad-cap":
        args = ("--max-cells", "10000001")
    header = control(left, right, buckets, frames, patterns, taus, counts=counts)
    a, b = records(left), records(right)
    if change == "magic":
        header = b"BADMAGIC" + header[8:]
    elif change == "query-cap":
        header = header[:24] + pack("<I", 500001) + header[28:]
    elif change == "frame-cap":
        header = header[:28] + pack("<I", 500001) + header[32:]
    elif change == "control-trailing":
        header += b"x"
    elif change == "control-truncate":
        header = header[:-1]
    elif change == "truncate":
        a = a[:-1]
    elif change == "trailing":
        a += b"x"
    elif change == "utf8":
        a = a[:-1] + b"\xff"
        right = [node for node in right if node.pre != left[-1].pre]
        header = control(left, right, buckets, frames, patterns, taus)
        b = records(right)
    result = invoke(binary, header, a, b, tmp_path, args=args)
    assert (result.returncode, result.stdout, result.stderr) == (1, b"", f"hot-l2-native: {message}\n".encode())
