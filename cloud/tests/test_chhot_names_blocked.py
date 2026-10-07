from array import array
from dataclasses import replace

import pytest

from dt_cloud.chstore.hot_names import HotNames, Node, Payload, hash_fixture, pack_roots
from dt_cloud.chstore import hot_names_blocked
from dt_cloud.chstore.hot_names_blocked import BlockedBuilder, pack_blocked


NODES = [
    Node(10, 30, "r", 28, 9),
    Node(11, 16, "r/covered", 20, 5),
    Node(12, 12, "r/covered/a", 15, 2),
    Node(13, 15, "r/covered/nest", 3, 2),
    Node(14, 14, "r/covered/nest/b", 3, 1),
    Node(15, 15, "r/covered/nest/zero", 0, 1),
    Node(16, 16, "r/covered/empty", 0, 0),
    Node(20, 20, "r/c", 5, 2),
    Node(30, 30, "r/zero", 0, 1),
]


@pytest.mark.parametrize("block_size", [1, 2, 64])
def test_covered_nonleaf_and_leaf_payload_preserve_every_source_total(block_size: int) -> None:
    roots = pack_roots([NODES[1], NODES[7], NODES[8]])
    blocked = pack_blocked(NODES, roots, block_size)
    expected = [(25, 8), (20, 5), (15, 2), (3, 2), (3, 1), (0, 1), (0, 0), (5, 2), (0, 1)]
    assert [blocked.total(node) for node in NODES] == expected
    assert [roots.total(node) for node in NODES] == expected
    assert list(blocked.leaf_positions) == [7, 8]
    assert (list(blocked.nonleaf.starts), list(blocked.nonleaf.ends)) == ([11], [16])
    assert blocked.source.nodes is NODES


@pytest.mark.parametrize("selected,expected", [
    ([], [(0, 0)] * 9),
    ([5, 8], [(0, 2), (0, 1), (0, 0), (0, 1), (0, 0), (0, 1), (0, 0), (0, 0), (0, 1)]),
    ([0], [(28, 9), (20, 5), (15, 2), (3, 2), (3, 1), (0, 1), (0, 0), (5, 2), (0, 1)]),
])
def test_empty_zero_byte_and_entire_root_coverage(selected: list[int], expected: list[tuple[int, int]]) -> None:
    roots = pack_roots([NODES[index] for index in selected])
    blocked = pack_blocked(NODES, roots)
    assert [blocked.total(node) for node in NODES] == expected
    assert [roots.total(node) for node in NODES] == expected


def test_exact_block_prefixes_around_63_64_65_and_sparse_source_coordinates() -> None:
    leaves = [Node(101 + 3 * i, 101 + 3 * i, f"r/{i}", i % 5, 1 + i % 3) for i in range(130)]
    nodes = [Node(100, leaves[-1].post, "r", sum(n.b for n in leaves), sum(n.o for n in leaves)), *leaves]
    roots = pack_roots(leaves)
    blocked = pack_blocked(nodes, roots)
    assert (list(blocked.bytes_blocks), list(blocked.objects_blocks)) == ([0, 126, 253], [0, 127, 255])
    assert len(blocked.leaf_positions) == 130
    assert blocked.packed_bytes == 584
    assert roots.packed_bytes == 4176
    assert set(vars(blocked)) == {
        "source", "nonleaf", "leaf_positions", "bytes_blocks", "objects_blocks", "block_size",
    }
    assert vars(blocked.source) == {"nodes": nodes}
    assert (len(blocked.nonleaf.starts), len(blocked.nonleaf.ends), len(blocked.nonleaf.bytes_prefix), len(blocked.nonleaf.objects_prefix)) == (0, 0, 1, 1)
    ranges = [(0, 63), (0, 64), (0, 65), (1, 127), (63, 65), (64, 128), (65, 130), (127, 130)]
    queries = [Node(leaves[lo].pre, leaves[hi - 1].post, "range", 0, 0) for lo, hi in ranges]
    expected = [(123, 126), (126, 127), (130, 129), (251, 252), (7, 3), (127, 128), (130, 130), (9, 6)]
    assert [blocked.total(node) for node in queries] == expected
    assert [roots.total(node) for node in queries] == expected
    assert [blocked.total(node) for node in nodes] == [roots.total(node) for node in nodes]


def test_query_reads_only_boundary_leaf_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    leaves = [Node(i + 1, i + 1, f"r/{i}", 1, 1) for i in range(256)]
    nodes = [Node(0, 256, "r", 256, 256), *leaves]
    blocked = pack_blocked(nodes, pack_roots(leaves))
    reads = []
    original = blocked._leaf_sum

    def count(lo: int, hi: int) -> tuple[int, int]:
        reads.append((lo, hi))
        return original(lo, hi)

    monkeypatch.setattr(blocked, "_leaf_sum", count)
    assert blocked.total(Node(2, 255, "range", 0, 0)) == (254, 254)
    assert reads == [(1, 64), (192, 255)]
    assert sum(hi - lo for lo, hi in reads) == 126
    reads.clear()
    assert blocked.total(nodes[0]) == (256, 256)
    assert reads == [(0, 0), (256, 256)]


def test_complete_hash_fixture_matches_every_hot_payload_and_every_source_node() -> None:
    nodes = hash_fixture(130)
    index = HotNames(nodes, 5)
    for roots in index.shared.values():
        blocked = pack_blocked(nodes, roots)
        assert [blocked.total(node) for node in nodes] == [roots.total(node) for node in nodes]
    leaf_roots = index.payloads[".npy"]
    blocked = pack_blocked(nodes, leaf_roots)
    assert (leaf_roots.packed_bytes, blocked.packed_bytes) == (4176, 584)


def test_builder_validates_source_once_and_shares_one_preorder_across_packs(monkeypatch: pytest.MonkeyPatch) -> None:
    validated = []
    original = hot_names_blocked._validate_source

    def validate(nodes: list[Node]) -> hot_names_blocked.Preorder:
        validated.append(nodes)
        return original(nodes)

    monkeypatch.setattr(hot_names_blocked, "_validate_source", validate)
    builder = BlockedBuilder(NODES)
    root_sets = [pack_roots([NODES[1], NODES[7], NODES[8]]), pack_roots([NODES[5], NODES[8]]), pack_roots([])]
    payloads = [builder.pack(roots) for roots in root_sets]
    assert validated == [NODES]
    assert validated[0] is NODES
    assert [payload.source is builder.source for payload in payloads] == [True, True, True]
    assert vars(builder) == {"source": builder.source}
    assert [payload.packed_bytes for payload in payloads] == [72, 40, 32]
    assert [[payload.total(node) for node in NODES] for payload in payloads] == [
        [roots.total(node) for node in NODES] for roots in root_sets
    ]


@pytest.mark.parametrize("block_size", [0, -1, 4097, True, 1.5])
def test_invalid_block_size_refuses(block_size: int) -> None:
    with pytest.raises(ValueError) as caught:
        pack_blocked(NODES, pack_roots([]), block_size)
    assert str(caught.value) == "block size must be an integer in 1..4096"


@pytest.mark.parametrize("nodes", [[], [NODES[0]] * 20_001])
def test_source_size_is_bounded(nodes: list[Node]) -> None:
    with pytest.raises(ValueError) as caught:
        pack_blocked(nodes, pack_roots([]))
    assert str(caught.value) == "source must contain 1..20K nodes"


@pytest.mark.parametrize("nodes", [
    [NODES[0], NODES[0]],
    list(reversed(NODES)),
    [replace(NODES[0], pre=-1)],
    [replace(NODES[0], post=9)],
    [replace(NODES[0], b=-1)],
    [replace(NODES[0], o=1 << 64)],
])
def test_duplicate_or_invalid_source_preorder_and_weights_refuse(nodes: list[Node]) -> None:
    with pytest.raises(ValueError) as caught:
        pack_blocked(nodes, pack_roots([]))
    assert str(caught.value) == "source preorder must be unique/increasing with valid UInt64 intervals and weights"


@pytest.mark.parametrize("selected,error", [
    ([Node(25, 25, "absent", 0, 0)], "coverage root is absent from the source"),
    ([replace(NODES[1], post=15)], "coverage root interval/weights differ from the source"),
    ([replace(NODES[1], b=19)], "coverage root interval/weights differ from the source"),
    ([replace(NODES[1], o=4)], "coverage root interval/weights differ from the source"),
    ([NODES[1], NODES[2]], "coverage roots must be increasing and disjoint"),
    ([NODES[7], NODES[7]], "coverage roots must be increasing and disjoint"),
])
def test_root_source_correspondence_and_disjointness_refuse(selected: list[Node], error: str) -> None:
    roots = pack_roots(selected)
    for pack in (BlockedBuilder(NODES).pack, lambda value: pack_blocked(NODES, value)):
        with pytest.raises(ValueError) as caught:
            pack(roots)
        assert str(caught.value) == error


@pytest.mark.parametrize("roots,error", [
    (Payload(array("Q", [20]), array("Q"), array("Q", [0, 5]), array("Q", [0, 2])), "root payload arrays have inconsistent lengths"),
    (Payload(array("Q"), array("Q"), array("Q", [1]), array("Q", [0])), "root payload prefixes must start at zero"),
])
def test_malformed_root_payload_refuses(roots: Payload, error: str) -> None:
    for pack in (BlockedBuilder(NODES).pack, lambda value: pack_blocked(NODES, value)):
        with pytest.raises(ValueError) as caught:
            pack(roots)
        assert str(caught.value) == error
