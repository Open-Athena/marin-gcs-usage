"""Summarize a saved, metadata-only `gcloud storage ls -l` raw-shard listing."""

import re
from typing import Iterable

ROW = re.compile(r"\s*(\d+)\s+\S+\s+gs://([^/]+)/listing/(\d{4}-\d{2}-\d{2})/(marin-[^/]+)/shard[^/]+\.parquet\s*")


def raw_shards(lines: Iterable[str]) -> dict:
    """Availability evidence, not a footer/content audit or proof of scan completeness."""
    dates = {}
    seen = set()
    stores = set()
    for number, line in enumerate(lines, 1):
        if not line.strip() or line.startswith("TOTAL:"):
            continue
        match = ROW.fullmatch(line)
        if match is None:
            raise ValueError(f"unrecognized raw shard metadata at line {number}: {line.rstrip()!r}")
        size, store, date, bucket = match.groups()
        stores.add(store)
        uri = line.split()[-1]
        if uri in seen:
            raise ValueError(f"duplicate raw shard URI at line {number}: {uri}")
        seen.add(uri)
        counts = dates.setdefault(date, {}).setdefault(bucket, {"shards": 0, "bytes": 0})
        counts["shards"] += 1
        counts["bytes"] += int(size)
    ordered = {date: dict(sorted(buckets.items())) for date, buckets in sorted(dates.items())}
    return {"data_buckets": sorted(stores), "dates": ordered, "n_dates": len(ordered), "shards": len(seen),
            "bytes": sum(row["bytes"] for buckets in ordered.values() for row in buckets.values()),
            "coverage": "object metadata only; not a completeness or content audit"}
