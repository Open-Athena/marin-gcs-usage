"""CoreWeave S3 mark & sweep — plan-driven deletion (specs/cw-sweep.md).

The flow is mark -> plan -> dispatch -> run. A **plan** is a curated list of
prefixes (an admin's explicit choice, assembled from `sweep`-marked dirs); it
replaces gcs's owner==marker attribution slice wholesale — the curated list *is*
the eligibility decision, so none of gcs's classification / vote / owner logic
ports over.

This module (Slice 1) carries the plan model, the CAIOS boto3 client + the
versioning-enabled guard, and the **manifest builder**: it expands a plan's
prefixes into an object-level manifest from the pinned layer-2 parquet (the
canonical per-object scan output at `cw-l2/<date>/<bucket>.parquet`), so a run
only ever deletes what was reviewed. The executor (the boto3 delete loop,
sweep_exec-derived) lands in a following slice and consumes this manifest.

Eligibility is deepest-mark-wins: a key is swept iff its longest matching plan
prefix is a `sweep` prefix (a deeper `keep` prefix carves it back out). The
layer-2 parquet has no ETag, so the run's overwrite guard keys off (size, mtime)
captured here, not a version id.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

# CAIOS S3-compatible endpoint (see job/cw-batch-submit.sh). CAIOS rejects
# path-style requests and ignores the region, but boto3 requires both set.
CW_ENDPOINT = os.environ.get("CW_ENDPOINT", "https://cwobject.com")
CW_BUCKET = os.environ.get("CW_BUCKET", "marin-us-east-02a")
DATA_BUCKET = os.environ.get("DATA_BUCKET", "oa-gcs-usage-dvx")

# A plan prefix, once normalized to a relative key prefix: non-empty, no scheme,
# no leading slash, trailing slash, no `.`/`..` segments or backslashes.
PREFIX_RE = re.compile(r"^(?!/)(?![.]{1,2}/)[^\\]+/$")


class SweepError(Exception):
    """A caller-fixable sweep error (bad plan, versioning off, missing input)."""


@dataclass
class Plan:
    """A curated deletion plan: prefixes to sweep, minus deeper keep carve-outs.

    Prefixes are relative key prefixes (e.g. `marin/checkpoints/old/`), already
    stripped of any `s3://<bucket>/` scheme and normalized to a trailing slash.
    """

    name: str
    bucket: str
    sweep: list[str]
    keep: list[str] = field(default_factory=list)
    plan_id: int | None = None

    def validate(self) -> None:
        if not self.sweep:
            raise SweepError(f"plan {self.name!r} has no sweep prefixes")
        for p in (*self.sweep, *self.keep):
            if not PREFIX_RE.match(p):
                raise SweepError(f"bad prefix {p!r} (want a relative key prefix ending in '/')")


def normalize_prefix(raw: str, bucket: str) -> str:
    """`s3://bucket/a/b/` or `/a/b` or `a/b` -> `a/b/` (relative, trailing slash)."""
    s = raw.strip()
    s = re.sub(r"^s3://", "", s)
    if s.startswith(f"{bucket}/"):
        s = s[len(bucket) + 1 :]
    s = s.lstrip("/")
    if not s.endswith("/"):
        s += "/"
    return s


def load_plan(path: str | Path) -> Plan:
    """Read a plan.json (as written by /api/sweep/dispatch), normalizing prefixes."""
    d = json.loads(Path(path).read_text())
    bucket = d.get("bucket", CW_BUCKET)
    plan = Plan(
        name=d["name"],
        bucket=bucket,
        sweep=[normalize_prefix(p, bucket) for p in d.get("sweep", [])],
        keep=[normalize_prefix(p, bucket) for p in d.get("keep", [])],
        plan_id=d.get("plan_id"),
    )
    plan.validate()
    return plan


def s3_client(endpoint: str = CW_ENDPOINT) -> "S3Client":
    """boto3 S3 client for CAIOS. Creds come from the env (Secret Manager on
    Batch: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "virtual"}, retries={"max_attempts": 10, "mode": "standard"}),
    )


def versioning_enabled(client: "S3Client", bucket: str) -> bool:
    """The delete-safety guard: a real sweep is refused unless the bucket has
    versioning `Status=Enabled`, so every delete writes a recoverable delete
    marker (CAIOS has no separate soft-delete; see specs/cw-sweep.md)."""
    resp = client.get_bucket_versioning(Bucket=bucket)
    return resp.get("Status") == "Enabled"


def _eligible_query() -> str:
    """DuckDB SELECT of the file rows eligible under a plan (deepest-mark-wins).

    Reads the layer-2 parquet from the `L2` DuckDB variable; `$sweep`/`$keep`
    bind lists of relative key prefixes. A row is kept iff its longest matching
    sweep prefix is longer than its longest matching keep prefix (no match ->
    length -1), i.e. the deepest mark wins and keep carves out. `$keep` is sent
    as `['']` when empty (length 0, beaten by any real sweep prefix) so the
    lambda has a typed, non-empty list to filter.
    """
    return """
        SELECT
          path AS name,
          size AS size_bytes,
          mtime,
          CASE WHEN path LIKE '%/%' THEN regexp_replace(path, '/[^/]*$', '/') ELSE '' END AS dir
        FROM read_parquet(getvariable('L2'))
        WHERE kind = 'file'
          AND coalesce(list_max(list_transform(
                list_filter($sweep, p -> starts_with(path, p)), p -> length(p))), -1)
            > coalesce(list_max(list_transform(
                list_filter($keep, p -> starts_with(path, p)), p -> length(p))), -1)
    """


def build_manifest(l2_path: str, plan: Plan, out_dir: str) -> dict:
    """Expand `plan` against the layer-2 parquet at `l2_path` into an object-level
    manifest under `out_dir` (`manifest/<bucket>.parquet` + `plan-summary.json`).

    Returns the summary dict. Deletes nothing; pure read + artifact write.
    """
    plan.validate()
    out = Path(out_dir)
    (out / "manifest").mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest" / f"{plan.bucket}.parquet"

    con = duckdb.connect()
    con.execute(f"SET memory_limit='{os.environ.get('DUCKDB_MEM', '8GB')}'")
    con.execute("SET VARIABLE L2 = ?", [l2_path])
    params = {"sweep": plan.sweep, "keep": plan.keep or [""]}
    con.execute(
        f"COPY ({_eligible_query()} ORDER BY name) TO '{manifest_path}' (FORMAT PARQUET)",
        params,
    )
    objects, byts = con.execute(
        f"SELECT count(*), coalesce(sum(size_bytes), 0) FROM read_parquet('{manifest_path}')"
    ).fetchone()

    summary = {
        "plan_id": plan.plan_id,
        "name": plan.name,
        "bucket": plan.bucket,
        "sweep": plan.sweep,
        "keep": plan.keep,
        "objects": int(objects),
        "bytes": int(byts),
        "manifest": str(manifest_path),
    }
    (out / "plan-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary
