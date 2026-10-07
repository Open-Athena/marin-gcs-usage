from uuid import uuid4

import pytest

from dt_cloud.chstore.client import Ch, lit
from dt_cloud.chstore.query_census import census

from chserver import ch_db, ch_url  # noqa: F401


def test_census_counts_each_name_once_per_pattern_with_unicode(ch_db: str, ch_url: str) -> None:
    ch = Ch(ch_url, db=ch_db)
    database = "census_" + uuid4().hex
    ch.exec(f"CREATE DATABASE {database}")
    try:
        ch.exec(f"CREATE TABLE {database}.names (nid UInt32,l String) ENGINE=Memory")
        ch.exec(f"INSERT INTO {database}.names VALUES (1,'aaaa'),(2,'aba'),(3,{lit('x🙂x')})")
        body = census(ch, database, 5, 1, 3)
        assert (body["names"], body["name_chars"], body["name_bytes"], body["sampled_names"]) == (3, 10, 13, 3)
        assert body["lengths"] == [
            {"chars": 1, "global_occurrence_windows": 10, "sample_distinct_name_pattern_pairs": 5, "sample_distinct_patterns": 4},
            {"chars": 2, "global_occurrence_windows": 7, "sample_distinct_name_pattern_pairs": 5, "sample_distinct_patterns": 5},
            {"chars": 3, "global_occurrence_windows": 4, "sample_distinct_name_pattern_pairs": 3, "sample_distinct_patterns": 3},
            {"chars": 4, "global_occurrence_windows": 1, "sample_distinct_name_pattern_pairs": 1, "sample_distinct_patterns": 1},
            {"chars": 5, "global_occurrence_windows": 0, "sample_distinct_name_pattern_pairs": 0, "sample_distinct_patterns": 0},
        ]
        assert (body["global_distinct_patterns_estimated"], body["full_path_boundaries_included"]) == (False, False)
        with pytest.raises(ValueError) as caught:
            census(ch, database, 5, 1, 2)
        assert str(caught.value) == "short-query sample exceeds its name budget; increase the sampling modulus"
        ch.exec(f"INSERT INTO {database}.names VALUES (4,{lit('a' * 2049)})")
        with pytest.raises(ValueError) as caught:
            census(ch, database, 5, 1, 4)
        assert str(caught.value) == "short-query sample contains a name over 2048 characters"
    finally:
        ch.close()
        ch.exec(f"DROP DATABASE {database}")


@pytest.mark.parametrize("max_chars,modulus,budget", [(0, 1, 1), (8, 1, 1), (3, 0, 1), (3, 1, 0)])
def test_invalid_census_limits_refuse_before_queries(max_chars: int, modulus: int, budget: int) -> None:
    with pytest.raises(ValueError) as caught:
        census(None, "safe", max_chars, modulus, budget)
    assert str(caught.value) == "short-query census requires lengths 1..7, positive modulus and a 1..100K-name budget"
