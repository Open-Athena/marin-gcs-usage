"""Scan ids: a scan's identity is its id, never its date (specs/scan-ids-not-dates.md).

A scan id is date-only ``YYYY-MM-DD`` or timed ``YYYY-MM-DDTHHMM`` (UTC — a job's
``SNAP_ID``); any deployment may scan at any cadence. A day may hold several scans, so a
date-ish argument is a *prefix*: it resolves to the latest scan whose id starts
with it (`latest_scan`), the same rule as the site's `site/src/scanSlug.ts`.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

# The one scan-id pattern (use `.fullmatch`); `static_names` and the rest import it from here.
SCAN_ID = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2}))?")


# A published scan's `meta.json` path (`…/<scan id>/meta.json`); group 1 is the id.
META_PATH = re.compile(rf"/({SCAN_ID.pattern})/meta\.json$")


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


def scan_time(scan: str) -> dt.datetime:
    """A scan id's UTC instant; a date-only id reads as its midnight (`site/src/scanSlug.ts` `scanTime`)."""
    if not (m := SCAN_ID.fullmatch(scan)):
        raise ValueError(f"not a scan id: {scan!r}")
    y, mo, d, hh, mm = m.groups()
    return dt.datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), tzinfo=dt.timezone.utc)


def scan_slug(scan: str) -> str:
    """A scan id's canonical `?d=` slug (`scanSlug.ts` `encodeScan`): the year's
    leading `20` dropped, `-` before the time — `2026-09-15T0001` → `260915-0001`,
    `2026-09-15` → `260915`."""
    if not (m := SCAN_ID.fullmatch(scan)):
        raise ValueError(f"not a scan id: {scan!r}")
    y, mo, d, hh, mm = m.groups()
    return f"{y[2:]}{mo}{d}" + (f"-{hh}{mm}" if hh else "")


def latest_scan(prefix: str, scans: list[str]) -> str | None:
    """The latest scan (any order in) whose id starts with `prefix`; None if none."""
    matches = [s for s in scans if s.startswith(prefix)]
    return max(matches) if matches else None


def snapshot_scans(data_root: Path) -> list[str]:
    """The scan dirs under a site-data root (``<root>/<scan id>/meta.json``),
    newest first — `scans.json`'s contents. Timed ids included."""
    return sorted(
        (p.name for p in data_root.iterdir() if p.is_dir() and is_scan_id(p.name) and (p / "meta.json").exists()),
        reverse=True,
    )


def scan_epoch(scan: str) -> int:
    """A scan id's UTC instant as epoch seconds (`scan_time`): a version's stamp in the static name index."""
    return int(scan_time(check_scan_id(scan)).timestamp())


def scan_label(ts: int) -> str:
    """Epoch seconds as a scan-id-shaped label: the date at midnight, else the date and minute (reports only)."""
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d" if ts % 86400 == 0 else "%Y-%m-%dT%H%M")


def check_order(scans: list[str] | tuple[str, ...] | set[str] | dict) -> list[str]:
    """`scans` sorted, checked to be strictly increasing in time too: string order is time order except for a date and
    its `T0000` scan, one instant, which is refused (a stamp could not say which scan it was)."""
    out = sorted(scans)
    for a, b in zip(out, out[1:]):
        if scan_epoch(a) >= scan_epoch(b):
            raise ValueError(f"scans {a} and {b}: ids out of time order (stamps {scan_epoch(a)}, {scan_epoch(b)})")
    return out
