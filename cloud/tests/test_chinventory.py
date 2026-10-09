"""Raw listing availability must not be confused with full historical coverage."""

import pytest

from dt_cloud.chstore.inventory import raw_shards


def test_raw_shard_metadata_groups_exactly():
    assert raw_shards([
        "  12  2026-10-01T06:00:00Z  gs://store/listing/2026-10-01/marin-us-central2/shard-00.parquet\n",
        "  14  2026-09-30T06:00:00Z  gs://store/listing/2026-09-30/marin-eu-west4/shard-shallow.parquet\n",
        "  16  2026-10-01T06:00:00Z  gs://store/listing/2026-10-01/marin-us-central2/shard-01.parquet\n",
        "\n", "TOTAL: 3 objects, 42 bytes\n",
    ]) == {
        "data_buckets": ["store"], "dates": {"2026-09-30": {"marin-eu-west4": {"shards": 1, "bytes": 14}},
                  "2026-10-01": {"marin-us-central2": {"shards": 2, "bytes": 28}}},
        "n_dates": 2, "shards": 3, "bytes": 42,
        "coverage": "object metadata only; not a completeness or content audit",
    }


def test_raw_inventory_does_not_count_generated_caches_as_raw_shards():
    with pytest.raises(ValueError) as caught:
        raw_shards(["12 date gs://store/listing/2026-10-01/dir-cache/dir-stats.parquet\n"])
    assert str(caught.value) == "unrecognized raw shard metadata at line 1: '12 date gs://store/listing/2026-10-01/dir-cache/dir-stats.parquet'"


def test_duplicate_raw_shard_metadata_is_rejected():
    line = "12 date gs://store/listing/2026-10-01/marin-us-central2/shard-00.parquet\n"
    with pytest.raises(ValueError) as caught:
        raw_shards([line, line])
    assert str(caught.value) == "duplicate raw shard URI at line 2: gs://store/listing/2026-10-01/marin-us-central2/shard-00.parquet"
