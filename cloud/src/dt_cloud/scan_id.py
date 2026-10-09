"""Scan ids: a scan's identity is its id, never its date (specs/scan-ids-not-dates.md).

A scan id is ``YYYY-MM-DD`` (a daily job's scan) or ``YYYY-MM-DDTHHMM`` (a
sub-daily scan, UTC — cw's ``SNAP_ID``). A day may hold several scans, so a
date-ish argument is a *prefix*: it resolves to the latest scan whose id starts
with it (`latest_scan`), the same rule as the site's `site/src/scanSlug.ts`.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

# The one scan-id pattern (use `.fullmatch`); `static_names` and the rest import it from here.
SCAN_ID = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2}))?")


def is_scan_id(value: object) -> bool:
    """A full, calendar-valid scan id (``YYYY-MM-DD`` or ``YYYY-MM-DDTHHMM``)."""
    if not isinstance(value, str) or not (m := SCAN_ID.fullmatch(value)):
        return False
    y, mo, d, hh, mm = m.groups()
    try:
        dt.date(int(y), int(mo), int(d))
    except ValueError:
        return False
    return hh is None or (int(hh) <= 23 and int(mm) <= 59)


def check_scan_id(value: str) -> str:
    """`value` if it's a scan id, else a `ValueError` naming both accepted forms."""
    if not is_scan_id(value):
        raise ValueError(f"not a scan id: {value!r} (want YYYY-MM-DD or YYYY-MM-DDTHHMM)")
    return value


def scan_id_option(ctx, param, value):  # noqa: ANN001 — a click callback
    """Click callback: validate an optional scan-id option/argument."""
    from click import BadParameter

    if value is None:
        return None
    try:
        return check_scan_id(value)
    except ValueError as e:
        raise BadParameter(str(e)) from None


def latest_scan(prefix: str, scans: list[str]) -> str | None:
    """The latest scan (any order in) whose id starts with `prefix`; None if none."""
    matches = [s for s in scans if s.startswith(prefix)]
    return max(matches) if matches else None


def snapshot_scans(data_root: Path) -> list[str]:
    """The scan dirs under a site-data root (``<root>/<scan id>/meta.json``),
    newest first — `scans.json`'s contents. Sub-daily ids included."""
    return sorted(
        (p.name for p in data_root.iterdir() if p.is_dir() and is_scan_id(p.name) and (p / "meta.json").exists()),
        reverse=True,
    )
