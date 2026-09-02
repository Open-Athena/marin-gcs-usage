"""Sweep executor — dry-run by default (specs/sweep-executor.md phase 3).

Consumes a `sweep manifest` plan dir. Per eligible directory: fresh re-list
(captures generations — the pinned listing has none), intersect with the
manifest, verify `timeCreated` matches (an overwrite since the scan keeps the
object), detect drift (new keys under a swept dir → skip the dir by default),
and — only with ``--for-real`` — issue generation-matched batch deletes.

Every decision lands in a per-bucket log parquet under the plan dir
(``would-delete/`` or ``deleted/``): name, size, generation, decision.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import partial

err = partial(print, file=sys.stderr)

#: Per-key decisions (the log's `decision` column).
DECISIONS = (
    "delete",              # in manifest ∩ live, created matches → deleted (or would be)
    "skipped_gone",        # in manifest, no longer live — graceful no-op
    "skipped_overwritten", # live but created moved — rewritten since the scan; keep
)

BATCH = 100  # GCS JSON batch limit per request


def execute_plan(
    plan_dir: str,
    for_real: bool = False,
    only_buckets: tuple[str, ...] = (),
    drift: str = "skip",  # skip | proceed — dirs that gained NEW keys since the scan
    workers: int = 8,
    min_soft_delete_days: int = 7,
    client=None,
    reclassify=None,  # (bucket, dir) -> category at the CURRENT ledger head; non-eligible dirs are skipped (ledger drift)
) -> dict:
    import fsspec
    import pyarrow as pa
    import pyarrow.parquet as pq
    from google.cloud import storage

    fs, ppath = fsspec.core.url_to_fs(plan_dir)
    with fs.open(f"{ppath}/plan-summary.json") as fh:
        plan = json.load(fh)
    client = client or storage.Client()

    mode = "deleted" if for_real else "would-delete"
    summary: dict = {"plan": plan_dir, "for_real": for_real, "drift": drift, "buckets": {}}
    log_schema = pa.schema([
        ("name", pa.string()), ("size_bytes", pa.int64()), ("generation", pa.int64()),
        ("decision", pa.string()), ("dir", pa.string()),
    ])

    for bucket, binfo in plan["buckets"].items():
        if only_buckets and bucket not in only_buckets:
            continue
        if "eligible" not in binfo:
            continue
        mpath = f"{ppath}/manifest/{bucket}.parquet"
        if not fs.exists(mpath):
            raise SystemExit(f"plan says {bucket} has eligible keys but {mpath} is missing")
        mf = pq.read_table(mpath, filesystem=fs).to_pandas()
        if for_real:
            _require_soft_delete(client, bucket, min_soft_delete_days)
        by_dir = {dn: g for dn, g in mf.groupby("dir")}
        ledger_drift: list[str] = []
        if reclassify is not None:
            still = {}
            for dn, g in by_dir.items():
                if reclassify(bucket, dn) == "eligible":
                    still[dn] = g
                else:
                    ledger_drift.append(dn)
            by_dir = still
        err(f"{bucket}: {len(mf):,} manifest keys in {len(by_dir):,} dirs ({mode})"
            + (f" — {len(ledger_drift):,} dirs dropped by newer marks" if ledger_drift else ""))
        bkt = client.bucket(bucket)
        counts: Counter = Counter()
        drift_dirs: list[dict] = []
        rows: list[dict] = []

        def do_dir(item):
            dn, g = item
            want = {r.name: r for r in g.itertuples()}
            live: dict = {}
            extra_b = extra_o = 0
            for blob in client.list_blobs(bucket, prefix=f"{dn}/" if dn else "", delimiter="/"):
                if blob.name in want:
                    live[blob.name] = blob
                else:
                    extra_b += blob.size or 0
                    extra_o += 1
            out: list[dict] = []
            gone = set(want) - set(live)
            for name in gone:
                out.append({"name": name, "size_bytes": int(want[name].size_bytes), "generation": 0, "decision": "skipped_gone", "dir": dn})
            todo = []
            for name, blob in live.items():
                created = blob.time_created.replace(tzinfo=dt.timezone.utc) if blob.time_created.tzinfo is None else blob.time_created
                if abs((created - want[name].created.to_pydatetime()).total_seconds()) > 1:
                    out.append({"name": name, "size_bytes": int(want[name].size_bytes), "generation": int(blob.generation), "decision": "skipped_overwritten", "dir": dn})
                else:
                    todo.append(blob)
            drifted = extra_o > 0
            if drifted and drift == "skip":
                return out, {"dir": dn, "new_objects": extra_o, "new_bytes": extra_b, "skipped_deletes": len(todo)}, 0
            if for_real:
                for i in range(0, len(todo), BATCH):
                    # raise on failure: a 412 (generation moved) or transient
                    # error aborts loudly; a re-run resumes via skipped_gone
                    with client.batch():
                        for blob in todo[i : i + BATCH]:
                            bkt.delete_blob(blob.name, if_generation_match=blob.generation)
            deleted_b = 0
            for blob in todo:
                out.append({"name": blob.name, "size_bytes": int(blob.size or 0), "generation": int(blob.generation), "decision": "delete", "dir": dn})
                deleted_b += blob.size or 0
            return out, ({"dir": dn, "new_objects": extra_o, "new_bytes": extra_b, "skipped_deletes": 0} if drifted else None), deleted_b

        total_deleted_b = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for out, drifted, dbytes in pool.map(do_dir, by_dir.items()):
                rows.extend(out)
                if drifted:
                    drift_dirs.append(drifted)
                total_deleted_b += dbytes
                for r in out:
                    counts[r["decision"]] += 1

        log_path = f"{plan_dir}/{mode}/{bucket}.parquet"
        lfs, lpath = fsspec.core.url_to_fs(log_path)
        lfs.makedirs(lpath.rsplit("/", 1)[0], exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rows, schema=log_schema), lpath, filesystem=lfs)
        summary["buckets"][bucket] = {
            "decisions": dict(counts),
            "delete_bytes": total_deleted_b,
            "drift_dirs": drift_dirs,
            "ledger_drift_dirs": ledger_drift,
        }
        err(
            f"  {bucket}: {counts['delete']:,} {mode} ({total_deleted_b / 1e12:.2f} TB), "
            f"{counts['skipped_gone']:,} gone, {counts['skipped_overwritten']:,} overwritten, "
            f"{len(drift_dirs):,} drifted dir(s){' (skipped)' if drift == 'skip' else ''}"
        )

    with fsspec.open(f"{plan_dir}/{mode}-summary.json", "w") as fh:
        json.dump(summary, fh, indent=2)
    return summary


def _require_soft_delete(client, bucket: str, min_days: int) -> None:
    b = client.get_bucket(bucket)
    pol = b.soft_delete_policy
    secs = (pol.retention_duration_millis or 0) / 1000 if pol else 0
    if secs < min_days * 86400:
        raise SystemExit(
            f"{bucket}: soft delete retention {secs / 86400:.0f}d < required {min_days}d — refusing --for-real"
        )
