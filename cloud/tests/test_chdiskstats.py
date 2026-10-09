"""Per-request device counters are whole-device deltas, never logical row reads."""

from dataclasses import replace
from pathlib import Path

import pytest

from dt_cloud.chstore import resources
from dt_cloud.chstore.resources import DiskSample, disk_delta, read_disk


def test_disk_sample_and_delta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "diskstats").write_text(
        "8 1 sda1 0 0 0 0 0 0 0 0 0 0 0\n"
        "8 0 sda 10 2 100 20 5 1 50 7 3 25 40 0 0 0 0 0 0\n"
    )
    monkeypatch.setattr(resources.time, "monotonic", lambda: 1.0)
    before = read_disk("sda", tmp_path)
    assert before == DiskSample("sda", 8, 0, 1.0, (10, 2, 100, 20, 5, 1, 50, 7, 3, 25, 40))
    after = DiskSample("sda", 8, 0, 3.0, (14, 3, 4196, 32, 7, 2, 58, 12, 0, 825, 1040))
    assert disk_delta(before, after) == {
        "device": "sda", "major": 8, "minor": 0, "scope": "whole-device", "elapsed_s": 2.0,
        "reads": 4, "read_merges": 1, "read_bytes": 2097152, "read_ms": 12,
        "writes": 2, "write_merges": 1, "write_bytes": 4096, "write_ms": 5,
        "busy_ms": 800, "weighted_io_ms": 1000, "in_flight_before": 3, "in_flight_after": 0,
        "read_mib_per_s": 1.0,
    }


@pytest.mark.parametrize("contents,error", [
    ("", "expected one complete diskstats entry: sda"),
    ("8 1 sda1 0 0 0 0 0 0 0 0 0 0 0\n", "expected one complete diskstats entry: sda"),
    ("8 0 sda 0 0\n", "expected one complete diskstats entry: sda"),
    ("8 0 sda 0 0 0 0 0 0 0 0 0 0 0\n" * 2, "expected one complete diskstats entry: sda"),
    ("8 0 sda -1 0 0 0 0 0 0 0 0 0 0\n", "negative diskstats counter: sda"),
])
def test_invalid_diskstats(tmp_path: Path, contents: str, error: str) -> None:
    (tmp_path / "diskstats").write_text(contents)
    with pytest.raises(ValueError) as caught:
        read_disk("sda", tmp_path)
    assert str(caught.value) == error


@pytest.mark.parametrize("change,error", [
    ({"device": "sdb"}, "monitored disk identity changed"),
    ({"minor": 1}, "monitored disk identity changed"),
    ({"at": 1.0}, "disk observation interval must be positive"),
    ({"at": 0.0}, "disk observation interval must be positive"),
    ({"counters": (0,) * 11}, "monitored disk counters reset or wrapped"),
])
def test_disk_delta_rejects_invalid_interval(change: dict, error: str) -> None:
    before = DiskSample("sda", 8, 0, 1.0, (1,) * 11)
    after = replace(before, **{"at": 2.0, **change})
    with pytest.raises(ValueError) as caught:
        disk_delta(before, after)
    assert str(caught.value) == error
