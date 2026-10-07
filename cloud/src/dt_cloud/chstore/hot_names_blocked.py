"""Bounded hot-name payload with block prefixes and shared source leaf weights.

Packed bytes exclude the common immutable source nodes, the zero-copy preorder
lookup and Python object overhead. There is no per-leaf weighted prefix array.
The caller must keep the source list and its immutable Node values unchanged
for the lifetime of the builder and every payload made from it.
"""

from array import array
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from .hot_names import Node, Payload, pack_roots


class Preorder:
    """Bisect-compatible view; retain the source once, not another pre array."""

    def __init__(self, nodes: list[Node]):
        self.nodes = nodes

    def __len__(self) -> int:
        return len(self.nodes)

    def __getitem__(self, index: int) -> int:
        return self.nodes[index].pre


@dataclass
class BlockedPayload:
    source: Preorder
    nonleaf: Payload
    leaf_positions: array
    bytes_blocks: array
    objects_blocks: array
    block_size: int

    def _leaf_sum(self, lo: int, hi: int) -> tuple[int, int]:
        b, o = 0, 0
        for index in range(lo, hi):
            node = self.source.nodes[self.leaf_positions[index]]
            b += node.b
            o += node.o
        return b, o

    def total(self, node: Node) -> tuple[int, int]:
        b, o = self.nonleaf.total(node)
        ordinal_lo = bisect_left(self.source, node.pre)
        ordinal_hi = bisect_right(self.source, node.post)
        lo = bisect_left(self.leaf_positions, ordinal_lo)
        hi = bisect_left(self.leaf_positions, ordinal_hi)
        first_block = (lo + self.block_size - 1) // self.block_size
        last_block = hi // self.block_size
        if first_block >= last_block:
            leaf_b, leaf_o = self._leaf_sum(lo, hi)
        else:
            left_b, left_o = self._leaf_sum(lo, first_block * self.block_size)
            right_b, right_o = self._leaf_sum(last_block * self.block_size, hi)
            leaf_b = self.bytes_blocks[last_block] - self.bytes_blocks[first_block] + left_b + right_b
            leaf_o = self.objects_blocks[last_block] - self.objects_blocks[first_block] + left_o + right_o
        return b + leaf_b, o + leaf_o

    @property
    def packed_bytes(self) -> int:
        return self.nonleaf.packed_bytes + sum(len(values) * values.itemsize for values in (
            self.leaf_positions, self.bytes_blocks, self.objects_blocks,
        ))


def _validate_source(nodes: list[Node]) -> Preorder:
    if not 1 <= len(nodes) <= 20_000:
        raise ValueError("source must contain 1..20K nodes")
    previous = -1
    for node in nodes:
        if not previous < node.pre <= node.post < 1 << 64 or not 0 <= min(node.b, node.o) or max(node.b, node.o) >= 1 << 64:
            raise ValueError("source preorder must be unique/increasing with valid UInt64 intervals and weights")
        previous = node.pre
    return Preorder(nodes)


class BlockedBuilder:
    """Validate one shared immutable source, then pack many checked root sets.

    Source validation is O(nodes) once. Every pack still verifies all roots
    against that source; no per-pattern source/preorder copy is retained.
    """

    def __init__(self, nodes: list[Node]):
        self.source = _validate_source(nodes)

    def pack(
        self,
        roots: Payload,
        block_size: int = 64,
    ) -> BlockedPayload:
        return _pack(self.source, roots, block_size)


def _pack(
    source: Preorder,
    roots: Payload,
    block_size: int,
) -> BlockedPayload:
    if not isinstance(block_size, int) or isinstance(block_size, bool) or not 1 <= block_size <= 4096:
        raise ValueError("block size must be an integer in 1..4096")
    nodes = source.nodes
    count = len(roots.starts)
    if len(roots.ends) != count or len(roots.bytes_prefix) != count + 1 or len(roots.objects_prefix) != count + 1:
        raise ValueError("root payload arrays have inconsistent lengths")
    if roots.bytes_prefix[0] != 0 or roots.objects_prefix[0] != 0:
        raise ValueError("root payload prefixes must start at zero")
    positions = array("I")
    if positions.itemsize != 4:
        raise ValueError("leaf positions require four-byte unsigned integers")
    nonleaf = []
    b_blocks, o_blocks = array("Q", [0]), array("Q", [0])
    b, o = 0, 0
    last_end = -1
    for index, pre in enumerate(roots.starts):
        ordinal = bisect_left(source, pre)
        if ordinal == len(nodes) or nodes[ordinal].pre != pre:
            raise ValueError("coverage root is absent from the source")
        node = nodes[ordinal]
        weight = (roots.bytes_prefix[index + 1] - roots.bytes_prefix[index],
                  roots.objects_prefix[index + 1] - roots.objects_prefix[index])
        if roots.ends[index] != node.post or weight != (node.b, node.o):
            raise ValueError("coverage root interval/weights differ from the source")
        if node.pre <= last_end:
            raise ValueError("coverage roots must be increasing and disjoint")
        last_end = node.post
        if node.pre != node.post:
            nonleaf.append(node)
            continue
        positions.append(ordinal)
        b += node.b
        o += node.o
        if max(b, o) >= 1 << 64:
            raise ValueError("leaf cumulative weights exceed UInt64")
        if len(positions) % block_size == 0:
            b_blocks.append(b)
            o_blocks.append(o)
    return BlockedPayload(source, pack_roots(nonleaf), positions, b_blocks, o_blocks, block_size)


def pack_blocked(
    nodes: list[Node],
    roots: Payload,
    block_size: int = 64,
) -> BlockedPayload:
    """Standalone convenience; reuse BlockedBuilder for multiple payloads."""
    return BlockedBuilder(nodes).pack(roots, block_size)
