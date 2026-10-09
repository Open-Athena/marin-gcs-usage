"""Scan ids: a scan's key (specs/scan-ids-not-dates.md), never its date — any deployment may scan several times a day.

A scan id is `YYYY-MM-DDTHHMM` (UTC; a scan job's `SNAP_ID`, `date -u +%Y-%m-%dT%H%M`) or a bare `YYYY-MM-DD` (a
deployment's ids from before it went sub-daily). Either form sorts in time order: a bare date is its day's midnight,
before that day's `T…` scans. Only `D` and `DT0000` share an instant, which `check_order` refuses.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime

SCAN_ID = re.compile(r"\d{4}-\d{2}-\d{2}(T\d{4})?")


def is_scan_id(v: object) -> bool:
    """A full, calendar-valid scan id."""
    if not isinstance(v, str) or not SCAN_ID.fullmatch(v):
        return False
    try:
        scan_epoch(v)
    except ValueError:
        return False
    return True


def scan_epoch(scan_id: str) -> int:
    """A scan id as epoch seconds (UTC)."""
    if not SCAN_ID.fullmatch(scan_id):
        raise ValueError(f"{scan_id!r} is not a scan id (YYYY-MM-DD or YYYY-MM-DDTHHMM)")
    fmt = "%Y-%m-%dT%H%M" if "T" in scan_id else "%Y-%m-%d"
    return int(datetime.strptime(scan_id, fmt).replace(tzinfo=UTC).timestamp())


def scan_label(ts: int) -> str:
    """Epoch seconds as a scan-id-shaped label: the date at midnight, else the date and minute (reports only)."""
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d" if ts % 86400 == 0 else "%Y-%m-%dT%H%M")


def check_order(ids: Iterable[str]) -> list[str]:
    """`ids` sorted, checked to be strictly increasing in time too (no two ids at one instant). Returns them."""
    out = sorted(ids)
    for a, b in zip(out, out[1:]):
        if scan_epoch(a) >= scan_epoch(b):
            raise ValueError(f"scans {a} and {b}: ids out of time order (stamps {scan_epoch(a)}, {scan_epoch(b)})")
    return out
