"""Benchmark mutation is limited to explicit new synthetic scratch prefixes."""

import pytest

from dt_cloud.sweep_delete_benchmark import benchmark_deletes, scratch_target


@pytest.mark.parametrize("url", [
    "gs://marin-us-central2/sweep/smoke-tests/delete-test",
    "gs://data-bucket/production/",
    "gs://data-bucket/sweep/smoke-tests/delete-*",
    "gs://data-bucket/sweep/smoke-tests/delete-",
    "gs://data-bucket/sweep/smoke-tests/delete-a/../../production",
    "gs://data-bucket/sweep/smoke-tests/delete-a#1",
    "https://data-bucket/sweep/smoke-tests/delete-a",
])
def test_production_or_ambiguous_targets_refused_before_client_creation(url):
    with pytest.raises(ValueError):
        benchmark_deletes(url)


def test_scratch_target_is_exact_and_trailing_slash_is_normalized():
    assert scratch_target("gs://data-bucket/sweep/smoke-tests/delete-20261005/") == ("data-bucket", "sweep/smoke-tests/delete-20261005")


@pytest.mark.parametrize("objects,workers", [(0, 1), (100001, 1), (1, 0), (1, 65)])
def test_benchmark_is_bounded_before_client_creation(objects, workers):
    with pytest.raises(ValueError, match="^benchmark objects must be 1..100000 and workers 1..64$"):
        benchmark_deletes("gs://data-bucket/sweep/smoke-tests/delete-test", objects, workers)
