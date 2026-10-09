import pytest

from dt_cloud.chstore.client import Ch
from dt_cloud.chstore.range_bench import Prefix, aggregate, direct

from chserver import ch_db, ch_url  # noqa: F401


def test_sparse_prefix() -> None:
    prefix = Prefix([[1, 2, 15, 2], [4, 1, 8, 1]])
    assert [prefix.before(i) for i in range(7)] == [
        (0, 0, 0), (0, 0, 0), (2, 15, 2), (2, 15, 2),
        (2, 15, 2), (3, 23, 3), (3, 23, 3),
    ]
    assert Prefix([]).before(99) == (0, 0, 0)


@pytest.mark.parametrize("rows", [[[1, 1, 1, 1], [1, 1, 1, 1]], [[2, 1, 1, 1], [1, 1, 1, 1]]])
def test_invalid_prefix(rows: list[list[int]]) -> None:
    with pytest.raises(ValueError, match="^summary blocks must be strictly increasing$"):
        Prefix(rows)


def test_all_small_ranges(ch_db, ch_url) -> None:
    ch = Ch(ch_url, db=ch_db)
    source = "SELECT pre, b, o FROM range_fixture"
    try:
        postings = [(0, 0, 1), (3, 7, 1), (4, 0, 0), (6, 11, 2), (13, 2**54 + 1, 3)]
        ch.tmp("range_fixture", " UNION ALL ".join(
            f"SELECT toUInt32({pre}) AS pre, toUInt64({b}) AS b, toUInt64({o}) AS o"
            for pre, b, o in postings
        ))
        ranges = [(lo, hi) for lo in range(17) for hi in range(lo, 17)]
        expected = [
            (sum(lo <= pre < hi for pre, _, _ in postings),
             sum(b for pre, b, _ in postings if lo <= pre < hi),
             sum(o for pre, _, o in postings if lo <= pre < hi))
            for lo, hi in ranges
        ]
        prefix = Prefix(ch.json("SELECT intDiv(pre, 4), count(), sum(b), sum(o) FROM range_fixture GROUP BY intDiv(pre, 4) ORDER BY intDiv(pre, 4)"))
        assert aggregate(ch, source, ranges, prefix, 4) == expected
        assert direct(ch, source, ranges) == expected
        assert aggregate(ch, source, [], prefix, 4) == []
    finally:
        ch.close()
