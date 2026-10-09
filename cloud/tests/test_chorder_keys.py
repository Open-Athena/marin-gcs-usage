import pytest

from dt_cloud.chstore.client import Ch
from dt_cloud.chstore.order_keys import append_key, bounds

from chserver import ch_db, ch_url  # noqa: F401


def test_appended_descendants_need_no_old_key_changes() -> None:
    before = {40: (40,), 6: (40, 6), 90: (40, 6, 90), 2: (40, 2), 17: (40, 6, 90, 17)}
    after = {**before, 1000: append_key(before[6], 1000), 1001: append_key((*before[6], 1000), 1001)}
    assert [after[id] for id in before] == list(before.values())
    lo, hi = bounds(before[6])
    assert sorted(id for id, key in after.items() if lo <= key < hi) == [6, 17, 90, 1000, 1001]
    assert sorted(after, key=after.get) == [40, 2, 6, 90, 17, 1000, 1001]


def test_prefix_successor_handles_wide_ids_without_a_text_sentinel() -> None:
    maximum = (1 << 63) - 1
    assert bounds((40, maximum)) == ((40, maximum), (40, 1 << 63))
    lo, hi = bounds((40, maximum))
    assert [lo <= key < hi for key in [(40, maximum), (40, maximum, 1), (40, 7), (41,)]] == [True, True, False, False]


@pytest.mark.parametrize("parent,identity,error", [
    ((40, 6), 40, "lineage exceeds the depth budget or repeats an identity"),
    ((40, 40), 7, "lineage exceeds the depth budget or repeats an identity"),
    ((40,), -1, "lineage identities must fit the nonnegative signed parent-ID domain"),
    ((40,), 1 << 63, "lineage identities must fit the nonnegative signed parent-ID domain"),
    (tuple(range(256)), 256, "lineage exceeds the depth budget or repeats an identity"),
])
def test_invalid_lineage_refuses(
    parent: tuple[int, ...],
    identity: int,
    error: str,
) -> None:
    with pytest.raises(ValueError) as caught:
        append_key(parent, identity)
    assert str(caught.value) == error


def test_clickhouse_numeric_array_ranges_keep_subtrees_contiguous_after_insert(ch_db: str, ch_url: str) -> None:
    ch = Ch(ch_url, db=ch_db)
    try:
        ch.exec("CREATE TABLE ordered_nodes (id UInt64, key Array(UInt64), own_b UInt64) ENGINE = MergeTree ORDER BY key")
        ch.exec("INSERT INTO ordered_nodes VALUES (40,[40],2),(6,[40,6],0),(90,[40,6,90],0),(17,[40,6,90,17],70),(2,[40,2],30)")
        assert ch.json("SELECT id FROM ordered_nodes ORDER BY key") == [[40], [2], [6], [90], [17]]
        ch.exec("INSERT INTO ordered_nodes VALUES (1000,[40,6,1000],0),(1001,[40,6,1000,1001],5)")
        assert ch.json("SELECT id FROM ordered_nodes WHERE key >= [40,6] AND key < [40,7] ORDER BY key") == [[6], [90], [17], [1000], [1001]]
        assert ch.json("SELECT count(),sum(own_b) FROM ordered_nodes WHERE key >= [40,6] AND key < [40,7]") == [[5, 75]]
        assert ch.json("SELECT count(),sum(own_b) FROM ordered_nodes WHERE key >= [40] AND key < [41]") == [[7, 107]]
        assert ch.json("SELECT count(),sum(own_b) FROM ordered_nodes WHERE key >= [40,2] AND key < [40,3]") == [[1, 30]]
    finally:
        ch.close()


def test_small_lineage_benchmark_checks_dense_oracle_and_append(ch_url: str) -> None:
    from dt_cloud.chstore.order_keys import bench

    body = bench(ch_url, 3, 5, 12)
    assert (body["rows"], body["depth"], body["logical_key_value_bytes"], body["logical_pre_value_bytes"],
            body["appended_descendant_in_unchanged_range"]) == (15, 12, 1440, 120, True)
    assert [(v["group"], v["exact_scalar_equal"]) for v in body["views"]] == [(0, True), (1, True), (2, True)]


def test_additive_history_preserves_stable_ranges_across_changes(ch_url: str) -> None:
    from dt_cloud.chstore.order_keys import history_bench

    body = history_bench(ch_url, 4, 30)
    assert (body["union_leaves"], body["scans"], body["updates_deletions_resurrection_and_appends_verified"]) == (124, 3, True)
    assert [(v["group"], v["tick"], v["exact_scalar_equal"]) for v in body["views"]] == [
        (0, 0, True), (0, 1, True), (0, 2, True),
        (2, 0, True), (2, 1, True), (2, 2, True),
        (3, 0, True), (3, 1, True), (3, 2, True),
    ]


def test_signed_history_keeps_wide_weights_and_deleted_zero_byte_counts(ch_db: str, ch_url: str) -> None:
    from dt_cloud.chstore.key_history import build_deltas

    ch = Ch(ch_url, db=ch_db)
    try:
        ch.tmp("wide_keys", "SELECT toUInt64(7) id,[toUInt64(0),toUInt64(7)] key")
        ch.tmp("wide_states", f"SELECT toUInt64(7) id,toUInt32(0) tick,toUInt64({(1 << 64) - 1}) b,toUInt64(1) o UNION ALL SELECT toUInt64(7),toUInt32(2),toUInt64(0),toUInt64(1)")
        output = build_deltas(ch, "wide_keys", "wide_states", 3)
        assert [ch.json(f"SELECT sum(dn),sum(db),sum(do) FROM {output} WHERE tick <= {tick}")[0] for tick in range(3)] == [
            [1, (1 << 64) - 1, 1], [0, 0, 0], [1, 0, 1],
        ]
    finally:
        ch.close()


@pytest.mark.parametrize("keys,states,ticks,error", [
    ("SELECT 7 id,[0,7] key", "SELECT 7 id,0 tick,1 b,1 o", 0, "history staging exceeds its 1M-state grid budget"),
    ("SELECT 7 id,[0,7] key UNION ALL SELECT 7,[0,8]", "SELECT 7 id,0 tick,1 b,1 o", 2, "history identities and lineage keys must be unique"),
    ("SELECT 7 id,[0,7,7] key", "SELECT 7 id,0 tick,1 b,1 o", 2, "history lineage keys violate the identity/depth domain"),
    ("SELECT 7 id,[0,8] key", "SELECT 7 id,0 tick,1 b,1 o", 2, "history lineage keys violate the identity/depth domain"),
    ("SELECT 7 id,[0,7] key", "SELECT 7 id,0 tick,1 b,1 o UNION ALL SELECT 7,0,2,1", 2, "history snapshot states have duplicate keys or invalid identities/ticks/weights"),
    ("SELECT 7 id,[0,7] key", "SELECT 8 id,0 tick,1 b,1 o", 2, "history snapshot states have duplicate keys or invalid identities/ticks/weights"),
    ("SELECT 7 id,[0,7] key", "SELECT 7 id,-1 tick,1 b,1 o", 2, "history snapshot states have duplicate keys or invalid identities/ticks/weights"),
    ("SELECT 7 id,[0,7] key", "SELECT 7 id,0 tick,-1 b,1 o", 2, "history snapshot states have duplicate keys or invalid identities/ticks/weights"),
])
def test_invalid_history_inputs_refuse_before_grid_creation(
    ch_db: str,
    ch_url: str,
    keys: str,
    states: str,
    ticks: int,
    error: str,
) -> None:
    from dt_cloud.chstore.key_history import build_deltas

    ch = Ch(ch_url, db=ch_db)
    try:
        ch.tmp("bad_keys", keys)
        ch.tmp("bad_states", states)
        with pytest.raises(ValueError) as caught:
            build_deltas(ch, "bad_keys", "bad_states", ticks)
        assert str(caught.value) == error
        assert ch._tmp == ["bad_keys", "bad_states"]
    finally:
        ch.close()
