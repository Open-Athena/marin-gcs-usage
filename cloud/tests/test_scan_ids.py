"""`dt_cloud.scan_ids`: a scan's key is its id (date or date and minute), ordered by time, never by day."""
from __future__ import annotations

import pytest

from dt_cloud.scan_ids import check_order, is_scan_id, scan_epoch, scan_label


@pytest.mark.parametrize("v, ok", [
    ("2026-10-09", True), ("2026-10-09T1236", True), ("2026-10-09T0000", True),
    ("2026-02-30", False), ("2026-10-09T2460", False), ("2026-10-09T12", False), ("261009", False), (None, False),
])
def test_is_scan_id(v, ok):
    assert is_scan_id(v) == ok


def test_order_mixes_forms_by_time():
    """gcs's move to scan ids: a bare date sorts as its midnight, before that day's minute ids."""
    ids = ["2026-10-10", "2026-10-09T1236", "2026-10-09", "2026-10-10T0601"]
    assert check_order(ids) == ["2026-10-09", "2026-10-09T1236", "2026-10-10", "2026-10-10T0601"]
    assert [scan_label(scan_epoch(i)) for i in check_order(ids)] == check_order(ids)


def test_order_refuses_two_ids_at_one_instant():
    with pytest.raises(ValueError) as e:
        check_order(["2026-10-09T0000", "2026-10-09"])
    assert str(e.value) == "scans 2026-10-09 and 2026-10-09T0000: ids out of time order (stamps 1791504000, 1791504000)"
