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
    """A scan id's canonical `?d=` slug (`scanSlug.ts` `encodeScan`): dashless
    `YYMMDD[HHMM]`, the year's leading `20` dropped — `2026-09-15T0001` →
    `2609150001`, `2026-09-15` → `260915`."""
    if not (m := SCAN_ID.fullmatch(scan)):
        raise ValueError(f"not a scan id: {scan!r}")
    y, mo, d, hh, mm = m.groups()
    return f"{y[2:]}{mo}{d}{hh or ''}{mm or ''}"


# A slug: dashless compact `YYMMDD[HH[MM]]` (canonical; 8 digits are always
# YYMMDDHH), the legacy `YYMMDD-HH[MM]` / `YYMMDDTHH[MM]`, or ISO
# `YYYY-MM-DD[THH[MM]]`.
_COMPACT = re.compile(r"(\d{2})(\d{2})(\d{2})(?:[T-]?(\d{2})(\d{2})?)?")
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2})?)?")


def slug_prefix(slug: str) -> str | None:
    """A slug → the scan-id prefix it names (`26100912` → `2026-10-09T12`), or
    None if it isn't one (`scanSlug.ts` `decodeScan`, minus its year-less forms)."""
    if m := _COMPACT.fullmatch(slug):
        y, mo, d, hh, mm = m.groups()
        y = f"20{y}"
    elif m := _ISO.fullmatch(slug):
        y, mo, d, hh, mm = m.groups()
    else:
        return None
    try:
        dt.date(int(y), int(mo), int(d))
    except ValueError:
        return None
    if (hh and int(hh) > 23) or (mm and int(mm) > 59):
        return None
    return f"{y}-{mo}-{d}" + (f"T{hh}{mm or ''}" if hh else "")


def latest_scan(prefix: str, scans: list[str]) -> str | None:
    """The latest scan (any order in) whose id starts with `prefix`; None if none."""
    matches = [s for s in scans if s.startswith(prefix)]
    return max(matches) if matches else None


def resolve_slug(slug: str, scans: list[str]) -> str | None:
    """The resolver: the latest scan a slug names (`261009` a day, `26100912` an
    hour, `2610091236` a minute; legacy and ISO spellings too), or None — a miss,
    never the nearest or latest scan instead."""
    prefix = slug_prefix(slug)
    return latest_scan(prefix, scans) if prefix else None


def snapshot_scans(data_root: Path) -> list[str]:
    """The scan dirs under a site-data root (``<root>/<scan id>/meta.json``),
    newest first — `scans.json`'s contents. Timed ids included."""
    return sorted(
        (p.name for p in data_root.iterdir() if p.is_dir() and is_scan_id(p.name) and (p / "meta.json").exists()),
        reverse=True,
    )
