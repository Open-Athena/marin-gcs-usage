"""``gcs-usage`` CLI.

``build`` derives the sparse dir -> user attribution table from a marin
``scan_gcs`` objects listing (parquet). The listing never loads fully into
pandas: DuckDB pre-filters it down to the two small row sets the signals
need (distinct ``users/<seg>/`` prefixes and record-file rows).
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from collections import Counter
from dataclasses import asdict
from functools import partial
from pathlib import Path

import duckdb
import pandas as pd
from click import Choice, argument, group, option

from .identity import DEFAULT_IDENTITIES, UNKNOWN_TEAM, load_identities
from .listing import prepare_listing
from .prefixes import load_prefix_map
from .records import mine_record_rows
from .signals import RECORD_BASENAME, manual_rows, record_file_paths, user_prefix_rows

err = partial(print, file=sys.stderr)


err = partial(print, file=sys.stderr)


def _connect() -> "duckdb.DuckDBPyConnection":
    """DuckDB with a hard memory cap — unbounded defaults (80% of RAM) have
    wedged the 61GB work node when combined with pandas-side structures."""
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{os.environ.get('DUCKDB_MEM', '24GB')}'")
    con.execute("SET threads=8")
    return con


@group()
def main() -> None:
    """Per-user attribution and reporting for Marin GCS storage."""


@main.command()
@option("-i", "--identities", "identities_path", type=Path, default=DEFAULT_IDENTITIES, help="identities.yaml path")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-o", "--out", required=True, type=Path, help="Output parquet path for the attribution table")
@option("-R", "--no-records", is_flag=True, help="Skip artifact-record mining (no GETs; path signals only)")
@option("-w", "--workers", default=16, help="Concurrent record reads")
def build(
    identities_path: Path,
    listings: tuple[str, ...],
    out: Path,
    no_records: bool,
    workers: int,
) -> None:
    """Build the attribution table from a listing parquet."""
    identities = load_identities(identities_path)
    asof = dt.date.today()
    con = _connect()
    src = prepare_listing(con, listings)

    users_df = con.execute(
        "SELECT DISTINCT bucket, regexp_extract(name, '^users/[^/]+/') AS name"
        f" FROM {src} WHERE name LIKE 'users/%'"
    ).df()
    records_df = con.execute(
        f"SELECT DISTINCT bucket, name FROM {src}"
        " WHERE regexp_extract(name, '[^/]+$') = ?",
        [RECORD_BASENAME],
    ).df()

    rows = user_prefix_rows(users_df, identities, asof) + manual_rows(identities, asof)
    if not no_records:
        paths = record_file_paths(records_df)
        err(f"record files to mine: {len(paths)}")
        record_rows, failed = mine_record_rows(paths, identities, asof, max_workers=workers)
        rows += record_rows
        if failed:
            err(f"unreadable record files ({len(failed)}):")
            for path in failed:
                err(f"  {path}")

    table = pd.DataFrame([asdict(row) for row in rows])
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(out, index=False)

    by_source = Counter(row.source for row in rows)
    err(f"wrote {len(rows)} attribution rows to {out}: {dict(by_source)}")
    unknown_users = sorted({row.user for row in rows if row.user is not None and row.team == UNKNOWN_TEAM})
    if unknown_users:
        err(f"users with no team (add to {identities_path}): {unknown_users}")


@main.command("executor-mine")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-o", "--out", "out_path", type=Path, default=Path("tmp/executor-infos.parquet"), help="Output parquet")
@option("-w", "--workers", default=64, help="Concurrent GETs")
def executor_mine(listings: tuple[str, ...], out_path: Path, workers: int) -> None:
    """Targeted-GET mine of legacy `.executor_info` sidecars (name/output_path/config gs paths)."""
    from .executor_info import mine_executor_infos

    con = _connect()
    src = prepare_listing(con, listings)
    paths = [
        f"gs://{b}/{n}"
        for b, n in con.execute(
            f"SELECT DISTINCT bucket, name FROM {src} WHERE name LIKE '%.executor_info'"
        ).fetchall()
    ]
    mine_executor_infos(paths, out_path, max_workers=workers)


@main.command("wandb-attr")
@option("-i", "--identities", "identities_path", type=Path, default=DEFAULT_IDENTITIES, help="identities.yaml path")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-o", "--out", required=True, type=Path, help="Output parquet path for wandb attribution rows")
@option("-r", "--runs", "runs_path", required=True, type=Path, help="wandb-mine output parquet")
@option("-x", "--executor-infos", "executor_path", type=Path, default=None, help="executor-mine output parquet (adds executor-wandb rows)")
def wandb_attr(
    identities_path: Path,
    listings: tuple[str, ...],
    out: Path,
    runs_path: Path,
    executor_path: Path | None,
) -> None:
    """Attribution rows from W&B runs: run-name ↔ checkpoints/grug dirs + writer-path configs."""
    from .wandb_signal import executor_rows, run_name_rows, writer_path_rows

    identities = load_identities(identities_path)
    asof = dt.date.today()
    runs = pd.read_parquet(runs_path)
    err(f"{len(runs)} mined runs")
    con = _connect()
    src = prepare_listing(con, listings)
    # Run-named dirs live at level 2 (checkpoints/<run>/) but also deeper under
    # namespace dirs — checkpoints/isoflop/<run>/, even
    # checkpoints/isoflop/isoflop/<run>/ — so emit levels 2-4 as (parent, leaf).
    run_dirs = con.execute(
        f"""
        WITH l AS (
          SELECT DISTINCT bucket,
            regexp_extract(name, '^([^/]+)/', 1) AS d1,
            regexp_extract(name, '^[^/]+/([^/]+)/', 1) AS d2,
            regexp_extract(name, '^[^/]+/[^/]+/([^/]+)/', 1) AS d3,
            regexp_extract(name, '^[^/]+/[^/]+/[^/]+/([^/]+)/', 1) AS d4
          FROM {src}
          WHERE (name LIKE 'checkpoints/%' OR name LIKE 'grug/%')
        )
        SELECT DISTINCT bucket, d1 AS parent, d2 AS leaf FROM l WHERE d2 IS NOT NULL
        UNION
        SELECT DISTINCT bucket, d1 || '/' || d2 AS parent, d3 AS leaf FROM l WHERE d3 IS NOT NULL
        UNION
        SELECT DISTINCT bucket, d1 || '/' || d2 || '/' || d3 AS parent, d4 AS leaf FROM l WHERE d4 IS NOT NULL
        """
    ).df()
    err(f"{len(run_dirs)} checkpoints/grug level-2/3/4 dirs")
    rows = run_name_rows(runs, run_dirs, identities, asof) + writer_path_rows(runs, identities, asof)
    if executor_path is not None:
        executor_df = pd.read_parquet(executor_path)
        err(f"{len(executor_df)} executor sidecars")
        rows += executor_rows(runs, executor_df, identities, asof)
    table = pd.DataFrame([asdict(row) for row in rows])
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(out, index=False)
    by_source = Counter(row.source for row in rows)
    err(f"wrote {len(rows)} attribution rows to {out}: {dict(by_source)}")


@main.command()
@option("-a", "--attribution", "attributions", required=True, multiple=True, help="Attribution parquet(s); repeatable, concatenated")
@option("-i", "--identities", "identities_path", type=Path, default=DEFAULT_IDENTITIES, help="identities.yaml path")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-n", "--top", default=30, help="Rows in the per-user table")
@option("-u", "--user", "claim_user", default=None, help="Print this user's claim list (their attributed prefixes by bytes)")
def report(
    attributions: tuple[str, ...],
    identities_path: Path,
    listings: tuple[str, ...],
    top: int,
    claim_user: str | None,
) -> None:
    """Join listing × attribution (deepest-prefix-wins) → per-user/team bytes + coverage.

    Users/teams are re-resolved against the *current* identities.yaml, so alias
    curation takes effect without rebuilding attribution parquets.
    """
    identities = load_identities(identities_path)
    con = _connect()
    src = prepare_listing(con, listings)
    by_prefix = load_prefix_map(con, attributions, identities, src)

    dirs = con.execute(
        "SELECT bucket || '/' || CASE WHEN name LIKE '%/%' THEN regexp_replace(name, '/[^/]*$', '') ELSE '' END AS dir,"
        " sum(size_bytes) AS bytes, count(*) AS objects"
        f" FROM {src} GROUP BY dir"
    ).df()
    err(f"{len(dirs)} distinct dirs")

    from collections import defaultdict

    per_user: dict[tuple, list] = defaultdict(lambda: [0, 0])
    per_source: dict[str, list] = defaultdict(lambda: [0, 0])
    claim: dict[str, list] = defaultdict(lambda: [0, 0])  # attributed-ancestor prefix -> [bytes, objects] for --user
    cache: dict[str, tuple | None] = {}
    prefix_of: dict[str, str] = {}  # dir_key -> matched attribution prefix (only tracked when --user)

    def deepest(dir_key: str) -> tuple | None:
        """Attribution of the deepest attributed ancestor of gs-less 'bucket/a/b'."""
        hit = cache.get(dir_key)
        if hit is not None or dir_key in cache:
            return hit
        probe = dir_key
        chopped = []
        result = None
        while True:
            row = by_prefix.get(f"gs://{probe}/")
            if row is not None:
                result = row
                break
            if "/" not in probe:
                break
            chopped.append(probe)
            probe = probe.rsplit("/", 1)[0]
        for key in chopped:
            cache[key] = result
            if result is not None:
                prefix_of[key] = f"gs://{probe}/"
        cache[dir_key] = result
        if result is not None:
            prefix_of[dir_key] = f"gs://{probe}/"
        return result

    total_bytes = int(dirs["bytes"].sum())
    for dir_key, nbytes, objects in zip(dirs["dir"], dirs["bytes"], dirs["objects"]):
        row = deepest(dir_key)
        user, team, source = row if row else (None, "unattributed", "none")
        per_user[(user, team)][0] += int(nbytes)
        per_user[(user, team)][1] += int(objects)
        per_source[source][0] += int(nbytes)
        per_source[source][1] += int(objects)
        if claim_user is not None and user == claim_user:
            c = claim[prefix_of[dir_key]]
            c[0] += int(nbytes)
            c[1] += int(objects)

    print("== coverage by source ==")
    for source, (nbytes, objects) in sorted(per_source.items(), key=lambda kv: -kv[1][0]):
        print(f"{source:>16}  {nbytes/1e12:10.2f} TB  {objects:>12,} objects  {100*nbytes/total_bytes:5.1f}%")

    print(f"\n== top {top} users/teams by bytes ==")
    rows = sorted(per_user.items(), key=lambda kv: -kv[1][0])[:top]
    for (user, team), (nbytes, objects) in rows:
        print(f"{user or '-':>24} {team:>14}  {nbytes/1e12:10.3f} TB  {objects:>12,} objects")

    if claim_user is not None:
        print(f"\n== claim list: {claim_user} ({len(claim)} prefixes) ==")
        for prefix, (nbytes, objects) in sorted(claim.items(), key=lambda kv: -kv[1][0]):
            print(f"{nbytes/1e9:12.2f} GB  {objects:>10,} objects  {prefix}")


@main.command()
@option("-a", "--attribution", "attributions", required=True, multiple=True, help="Attribution parquet(s); repeatable, concatenated")
@option("-d", "--depth", default=2, help="Prefix depth for the gap rollup (name components after bucket)")
@option("-i", "--identities", "identities_path", type=Path, default=DEFAULT_IDENTITIES, help="identities.yaml path")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-n", "--top", default=40, help="Rows in the gap table")
def gaps(
    attributions: tuple[str, ...],
    depth: int,
    identities_path: Path,
    listings: tuple[str, ...],
    top: int,
) -> None:
    """Largest *unattributed* prefixes at a given depth — the targeting list for
    new signals and `prefix_owners` curation."""
    identities = load_identities(identities_path)
    con = _connect()
    src = prepare_listing(con, listings)
    by_prefix = load_prefix_map(con, attributions, identities, src)

    dirs = con.execute(
        "SELECT bucket || '/' || CASE WHEN name LIKE '%/%' THEN regexp_replace(name, '/[^/]*$', '') ELSE '' END AS dir,"
        " sum(size_bytes) AS bytes, count(*) AS objects"
        f" FROM {src} GROUP BY dir"
    ).df()
    err(f"{len(dirs)} distinct dirs")

    from collections import defaultdict

    cache: dict[str, bool] = {}

    def attributed(dir_key: str) -> bool:
        hit = cache.get(dir_key)
        if hit is not None:
            return hit
        probe = dir_key
        chopped = []
        result = False
        while True:
            if f"gs://{probe}/" in by_prefix:
                result = True
                break
            if "/" not in probe:
                break
            chopped.append(probe)
            probe = probe.rsplit("/", 1)[0]
        for key in chopped:
            cache[key] = result
        cache[dir_key] = result
        return result

    gap: dict[str, list] = defaultdict(lambda: [0, 0])
    total_gap = 0
    for dir_key, nbytes, objects in zip(dirs["dir"], dirs["bytes"], dirs["objects"]):
        if attributed(dir_key):
            continue
        total_gap += int(nbytes)
        head = "/".join(dir_key.split("/")[: depth + 1])  # bucket + depth components
        g = gap[head]
        g[0] += int(nbytes)
        g[1] += int(objects)

    print(f"== top {top} unattributed prefixes at depth {depth} ({total_gap/1e12:.1f} TB total gap) ==")
    for head, (nbytes, objects) in sorted(gap.items(), key=lambda kv: -kv[1][0])[:top]:
        print(f"{nbytes/1e12:9.3f} TB  {objects:>12,} objects  gs://{head}/")


@main.command()
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-n", "--top", default=25, help="Rows per top-prefix table")
def census(listings: tuple[str, ...], top: int) -> None:
    """Listing-level coverage census: per-bucket totals, users/ bytes, record files, top prefixes."""
    con = _connect()
    src = prepare_listing(con, listings)
    con.execute(f"CREATE VIEW l AS SELECT * FROM {src}")

    print("== per-bucket totals ==")
    print(
        con.execute(
            "SELECT bucket, count(*) AS objects, round(sum(size_bytes)/1e12, 2) AS tb"
            " FROM l GROUP BY bucket ORDER BY tb DESC"
        ).df().to_string(index=False)
    )

    print("\n== users/<seg>/ coverage (signal 1) ==")
    print(
        con.execute(
            "SELECT bucket, regexp_extract(name, '^users/([^/]+)/', 1) AS segment,"
            " count(*) AS objects, round(sum(size_bytes)/1e9, 2) AS gb"
            " FROM l WHERE name LIKE 'users/%'"
            " GROUP BY bucket, segment ORDER BY gb DESC"
        ).df().to_string(index=False)
    )

    print("\n== record files (signal 2) ==")
    print(
        con.execute(
            "SELECT bucket, count(*) AS record_files FROM l"
            " WHERE regexp_extract(name, '[^/]+$') = ? GROUP BY bucket ORDER BY record_files DESC",
            [RECORD_BASENAME],
        ).df().to_string(index=False)
    )

    print(f"\n== top {top} (bucket, first-level dir) by bytes ==")
    print(
        con.execute(
            "SELECT bucket, regexp_extract(name, '^([^/]+)/', 1) AS dir1,"
            " count(*) AS objects, round(sum(size_bytes)/1e12, 3) AS tb"
            " FROM l GROUP BY bucket, dir1 ORDER BY tb DESC LIMIT ?",
            [top],
        ).df().to_string(index=False)
    )

    print(f"\n== top {top} (bucket, two-level dir) by bytes ==")
    print(
        con.execute(
            "SELECT bucket, regexp_extract(name, '^([^/]+/[^/]+)/', 1) AS dir2,"
            " count(*) AS objects, round(sum(size_bytes)/1e12, 3) AS tb"
            " FROM l GROUP BY bucket, dir2 ORDER BY tb DESC LIMIT ?",
            [top],
        ).df().to_string(index=False)
    )


@main.command("wandb-mine")
@option("-e", "--entity", default="marin-community", help="W&B entity to mine")
@option("-E", "--print-edges", is_flag=True, help="Print bisection-tree edges (valid --since/--until values for parallel workers) and exit")
@option("-M", "--no-merge", is_flag=True, help="Skip the final concat (parallel range-workers; run once without to merge)")
@option("-o", "--out", "out_path", type=Path, default=Path("tmp/wandb-runs.parquet"), help="Output parquet")
@option("-p", "--project-filter", default=None, help="Substring filter on project names")
@option("-s", "--since", default=None, help="Window start (bisection-tree edge; see -E)")
@option("-u", "--until", default=None, help="Window end (bisection-tree edge; see -E)")
def wandb_mine(
    entity: str,
    print_edges: bool,
    no_merge: bool,
    out_path: Path,
    project_filter: str | None,
    since: str | None,
    until: str | None,
) -> None:
    """Mine W&B run metadata (identity + config gs:// paths) for attribution."""
    from .wandb_mine import ROOT_SINCE, ROOT_UNTIL, mine_entity, window_edges

    if print_edges:
        for edge in window_edges():
            print(edge)
        return
    mine_entity(
        entity,
        out_path,
        project_filter,
        since=since or ROOT_SINCE,
        until=until or ROOT_UNTIL,
        merge=not no_merge,
    )


@main.command()
@option("-a", "--attribution", "attributions", multiple=True, help="Attribution parquet(s); adds per-node team/user overlays")
@option("-d", "--asof", required=True, help="Scan date the listing came from (YYYY-MM-DD)")
@option("-i", "--identities", "identities_path", type=Path, default=DEFAULT_IDENTITIES, help="identities.yaml path")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-o", "--out", "out_dir", type=Path, default=None, help="Output dir for JSON files [default: site/public/data/<asof>]")
def webdata(
    attributions: tuple[str, ...],
    asof: str,
    identities_path: Path,
    listings: tuple[str, ...],
    out_dir: Path | None,
) -> None:
    """Generate a dated site-data snapshot (tree/age/meta JSONs) from a listing.

    Snapshots live at site/public/data/<asof>/; the sibling scans.json index
    (dates, newest first — the site's scan dropdown) is refreshed afterwards.
    """
    import json
    import re

    from .viz import write_webdata

    if out_dir is None:
        out_dir = Path("site/public/data") / asof
    meta = write_webdata(listings, out_dir, asof, attributions, identities_path)
    err(f"wrote {out_dir}/: tree.json age.json meta.json ({meta['total_bytes']/1e12:.0f} TB, {meta['total_objects']:,} objects)")
    data_root = out_dir.parent
    dates = sorted(
        (
            p.name
            for p in data_root.iterdir()
            if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name) and (p / "meta.json").exists()
        ),
        reverse=True,
    )
    if dates:
        (data_root / "scans.json").write_text(json.dumps(dates) + "\n")
        err(f"scans.json: {dates}")


@main.command()
@option("-o", "--out", "out_root", type=Path, required=True, help="Local root; objects land at <out>/<bucket>/<object name>")
@option("-w", "--workers", default=16, help="Concurrent downloads")
@argument("globs", nargs=-1, required=True)
def stage(out_root: Path, workers: int, globs: tuple[str, ...]) -> None:
    """Stage /gcs/<bucket>/<pattern> globs onto local disk (parallel download).

    gcsfuse reads are slow (~20-50 MB/s) and webdata makes several passes over
    its inputs; staging to local NVMe first makes those passes local-speed.
    Already-staged files (same size) are skipped, so re-runs are idempotent.
    """
    from .stage import stage_globs

    stage_globs(globs, out_root, workers)


@main.command("list-bucket")
@option("-o", "--out", "out_dir", required=True, help="Output dir (local or gs://) for canonical listing parquet shards")
@option("-p", "--prefix", default=None, help="List only under this prefix (smoke tests / partial runs)")
@option("-P", "--procs", default=6, help="Worker processes (page parsing is GIL-bound; processes scale it)")
@option("-w", "--workers", "threads", default=8, help="Concurrent prefix streams per process")
@option("-W", "--weights-from", default=None, help="Prior canonical listing glob; balances worker chunks by object counts")
@option("-x", "--exists", type=Choice(["error", "reuse", "clear"]), default="error", help="On existing output: refuse, reuse-if-complete (clear partials), or clear")
@argument("bucket")
def list_bucket(out_dir: str, prefix: str | None, procs: int, threads: int, weights_from: str | None, exists: str, bucket: str) -> None:
    """Directly list a GCS bucket to canonical listing parquet shards.

    Patch for buckets where Storage Insights inventory reports are
    unavailable (currently `marin-us-central2`).
    """
    from .bucket_list import list_bucket_to_parquet

    list_bucket_to_parquet(bucket, out_dir, procs=procs, threads=threads, prefix=prefix, exists=exists, weights_from=weights_from)


@main.command()
@option("-i", "--identities", "identities_path", type=Path, default=DEFAULT_IDENTITIES, help="identities.yaml path")
@option("-o", "--out", type=Path, default=None, help="Write rules JSON (users/aliases/teams/prefix_owners + notes) for the site")
def rules(identities_path: Path, out: Path | None) -> None:
    """Validate identities.yaml; optionally export it as site JSON.

    Checks alias collisions/shadowing, unknown teams, and prefix_owners rows
    referencing unknown users or malformed/duplicate prefixes. Exits nonzero
    on findings (JSON is still written, so the site shows current state).
    """
    import json

    from .rules import export_rules

    payload, findings = export_rules(identities_path)
    for finding in findings:
        err(f"FINDING: {finding}")
    err(f"{len(payload['users'])} users, {len(payload['prefix_owners'])} prefix rules, {len(findings)} findings")
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=1) + "\n")
        err(f"wrote {out}")
    if findings:
        raise SystemExit(1)


@main.group()
def job() -> None:
    """Read-only ops for the daily snapshot Batch job (status/logs/watch/metrics)."""


def _resolve_job(name: str) -> dict:
    from .gcp import batch_job, batch_jobs

    if name in ("", "latest"):
        jobs = batch_jobs()
        if not jobs:
            raise SystemExit("no Batch jobs found")
        return jobs[0]
    return batch_job(name)


@job.command("status")
@option("-n", "--limit", default=8, help="Jobs to list")
@argument("name", required=False)
def job_status(limit: int, name: str | None) -> None:
    """List recent Batch jobs, or one job's state + status events."""
    import json

    from .gcp import batch_jobs

    if name is None:
        for j in batch_jobs()[:limit]:
            print(f"{j['name'].rsplit('/', 1)[-1]}  {j['status'].get('state', '?'):22} {j.get('createTime', '')}")
        return
    j = _resolve_job(name)
    print(f"{j['name'].rsplit('/', 1)[-1]}  {j['status'].get('state', '?')}  uid={j.get('uid')}")
    for e in j["status"].get("statusEvents", []):
        print(f"  {e.get('eventTime', '')[11:19]} {e.get('type', ''):16} {e.get('description', '')[:200]}")
    if rund := j["status"].get("runDuration"):
        print(f"  runDuration: {rund}")
    env = j["taskGroups"][0]["taskSpec"].get("environment", {}).get("variables", {})
    print(f"  env: {json.dumps(env)}")


@job.command("logs")
@option("-a", "--asc", is_flag=True, help="Oldest first (default: newest first)")
@option("-g", "--grep", default=None, help="Regex filter on textPayload (server-side)")
@option("-k", "--key-markers", is_flag=True, help="Only [rss]/stage/WARN/DONE/error marker lines")
@option("-n", "--limit", default=40, help="Max entries")
@argument("name", required=False)
def job_logs(asc: bool, grep: str | None, key_markers: bool, limit: int, name: str | None) -> None:
    """Container stdout for a Batch job (batch_task_logs; agent noise excluded)."""
    from .gcp import log_entries, task_log_filter

    j = _resolve_job(name or "latest")
    if key_markers:
        grep = r"\[rss\]|stage |WARN|SNAPSHOT-JOB-DONE|Deployment complete|reusing|objects listed|Error|Killed|Traceback"
    for e in log_entries(task_log_filter(j["uid"], grep), limit=limit, asc=asc):
        print(f"{e.get('timestamp', '')[:19]} {e.get('textPayload', '').rstrip()}")


@job.command("watch")
@option("-i", "--interval", default=90, help="Poll interval (seconds)")
@argument("name", required=False)
def job_watch(interval: int, name: str | None) -> None:
    """Poll a Batch job to terminal state, then print its key log markers."""
    import time
    from datetime import datetime, timezone

    from click import Context

    j = _resolve_job(name or "latest")
    short = j["name"].rsplit("/", 1)[-1]
    err(f"watching {short} (uid={j['uid']})")
    while True:
        state = _resolve_job(short)["status"].get("state", "?")
        err(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {state}")
        if state in ("SUCCEEDED", "FAILED", "DELETION_IN_PROGRESS"):
            break
        time.sleep(interval)
    ctx = Context(job_logs)
    ctx.invoke(job_logs, asc=True, grep=None, key_markers=True, limit=60, name=short)
    if state != "SUCCEEDED":
        raise SystemExit(1)


@job.command("submit-listing")
@option("-b", "--bucket", "buckets", multiple=True, help="Bucket(s) to list [default: whole fleet]")
@option("-d", "--date", "date", required=True, help="Listing date — output goes to listing/<date>/<bucket>/")
@option("-m", "--machine", default="n2-standard-32", help="Machine type per task")
@option("-P", "--procs", default=24, help="list-bucket worker processes per task")
@option("-w", "--workers", "threads", default=10, help="Concurrent prefix streams per process")
@option("-W", "--wait", "wait", is_flag=True, help="Block until the job reaches a terminal state")
def job_submit_listing(
    buckets: tuple[str, ...],
    date: str,
    machine: str,
    procs: int,
    threads: int,
    wait: bool,
) -> None:
    """Submit the DIY fleet-listing Batch job (one task per bucket).

    Tasks reuse completed listings (``-x reuse``), so re-submitting for the
    same date only re-lists buckets that haven't finished — safe to retry.
    """
    from .batch import BUCKET_JOB_REGIONS, FLEET_BUCKETS, REGION, listing_job_spec, submit_job, wait_jobs

    bkts = list(buckets) or FLEET_BUCKETS
    by_region: dict[str, list[str]] = {}
    for b in bkts:
        by_region.setdefault(BUCKET_JOB_REGIONS.get(b, REGION), []).append(b)
    jobs = []
    for region, rb in by_region.items():
        spec = listing_job_spec(date, rb, machine=machine, procs=procs, threads=threads, region=region)
        name = submit_job(spec, region=region)
        err(f"submitted {name} [{region}]: {len(rb)} bucket task(s) on {machine}")
        print(name)
        jobs.append((name, region))
    if wait:
        states = wait_jobs(jobs, log=err)
        if bad := {n: s for n, s in states.items() if s != "SUCCEEDED"}:
            raise SystemExit(f"listing job(s) failed: {bad}")


@job.command("metrics")
@option("-m", "--metric", type=Choice(["cpu", "net", "disk"]), default="cpu", help="Metric to show")
@option("-n", "--minutes", default=30, help="Lookback window")
@argument("name", required=False)
def job_metrics(metric: str, minutes: int, name: str | None) -> None:
    """VM utilization for a Batch job (finds the instance via agent logs)."""
    from .gcp import METRICS, job_instance_id, vm_metric

    j = _resolve_job(name or "latest")
    inst = job_instance_id(j["uid"])
    if not inst:
        raise SystemExit(f"no instance found in agent logs for {j['uid']} (job not started yet?)")
    unit = METRICS[metric][2]
    terminal = j["status"].get("state") in ("SUCCEEDED", "FAILED")
    span = dict(start=j.get("createTime"), end=j.get("updateTime")) if terminal else {}
    for t, v in vm_metric(inst, metric, minutes, **span):
        print(f"{t[:19]} {v:8.1f} {unit}")


@main.group()
def sii() -> None:
    """Read-only Storage Insights inventory-report ops."""


SII_BUCKETS = ["marin-us-east1", "marin-us-east5", "marin-us-central1", "marin-eu-west4", "marin-us-west4"]


@sii.command("status")
@option("-b", "--bucket", "buckets", multiple=True, help="Bucket(s) to check [default: all 5 SII buckets]")
def sii_status(buckets: tuple[str, ...]) -> None:
    """Per-bucket SII health: report config, latest generated report, and which
    days' shards have actually landed in gs://<bucket>/inventory-reports/."""
    import re as _re
    from collections import defaultdict

    from google.cloud import storage

    from .gcp import sii_report_configs, sii_report_details

    client = storage.Client()
    for b in buckets or SII_BUCKETS:
        location = b.removeprefix("marin-")
        print(f"== {b}")
        cfgs = [
            c
            for c in sii_report_configs(location)
            if c.get("objectMetadataReportOptions", {}).get("storageFilters", {}).get("bucket") == b
        ]
        if not cfgs:
            print("  NO report config")
            continue
        for c in cfgs:
            details = sii_report_details(c["name"])
            freq = c.get("frequencyOptions", {}).get("frequency", "?")
            print(f"  config {c['name'].rsplit('/', 1)[-1][:8]}… ({freq}); {len(details)} reports generated")
            for r in details[:2]:
                m = r.get("reportMetrics", {})
                print(
                    f"    {r.get('snapshotTime', '')[:16]} records={int(m.get('processedRecordsCount', 0)):,}"
                    f" shards={r.get('shardsCount', '?')}"
                )
        by_day: dict[str, list] = defaultdict(list)
        for blob in client.list_blobs(b, prefix="inventory-reports/"):
            if blob.name.endswith(".parquet") and (m := _re.search(r"_(\d{4}-\d{2}-\d{2})T", blob.name)):
                by_day[m.group(1)].append(blob)
        for day in sorted(by_day, reverse=True)[:3]:
            blobs = by_day[day]
            latest = max(x.time_created for x in blobs)
            print(f"    landed {day}: {len(blobs)} shards ({sum(x.size for x in blobs) / 1e9:.1f} GB, written {latest:%m-%d %H:%M}Z)")


@main.command()
@option("-d", "--depth", default=1, help="Path depth to compare at (1 = bucket level)")
@option("-n", "--top", default=30, help="Show top-N rows by absolute byte delta (depth >= 2)")
@argument("a")
@argument("b")
def compare(depth: int, top: int, a: str, b: str) -> None:
    """Compare two snapshot tree.jsons: objects/bytes per bucket (or deeper path).

    A/B are snapshot dates (resolved under site/public/data/) or dirs
    containing tree.json.
    """
    import json

    def load(spec: str) -> dict:
        p = Path(spec)
        if not p.exists():
            p = Path("site/public/data") / spec
        f = p / "tree.json" if p.is_dir() else p
        return json.loads(f.read_text())

    def walk(node: dict, prefix: str, d: int, out: dict) -> None:
        key = f"{prefix}/{node['n']}" if prefix else node["n"]
        if d == depth or not node.get("c"):
            o, byts = out.get(key, (0, 0))
            out[key] = (o + node["o"], byts + node["b"])
            return
        for c in node["c"]:
            walk(c, key, d + 1, out)

    ta, tb = load(a), load(b)
    ra: dict[str, tuple[int, int]] = {}
    rb: dict[str, tuple[int, int]] = {}
    for c in ta.get("c", []):
        walk(c, "", 1, ra)
    for c in tb.get("c", []):
        walk(c, "", 1, rb)
    all_keys = sorted(set(ra) | set(rb), key=lambda k: -abs(rb.get(k, (0, 0))[1] - ra.get(k, (0, 0))[1]))
    keys = all_keys[:top] if depth >= 2 else all_keys
    w = max(5, *(len(k) for k in keys)) if keys else 5
    print(f"{'path':{w}} {'a objs':>14} {'b objs':>14} {'Δobjs':>12} {'a TB':>9} {'b TB':>9} {'ΔTB':>8}")
    for k in keys:
        ao, ab_ = ra.get(k, (0, 0))
        bo, bb = rb.get(k, (0, 0))
        print(f"{k:{w}} {ao:>14,} {bo:>14,} {bo - ao:>+12,} {ab_ / 1e12:>9.1f} {bb / 1e12:>9.1f} {(bb - ab_) / 1e12:>+8.1f}")
    if len(keys) < len(all_keys):
        print(f"(… {len(all_keys) - len(keys)} more paths)")
    tao, tab_ = (sum(x) for x in zip(*ra.values())) if ra else (0, 0)
    tbo, tbb = (sum(x) for x in zip(*rb.values())) if rb else (0, 0)
    print(f"{'TOTAL':{w}} {tao:>14,} {tbo:>14,} {tbo - tao:>+12,} {tab_ / 1e12:>9.1f} {tbb / 1e12:>9.1f} {(tbb - tab_) / 1e12:>+8.1f}")


def _load_meta(root: str, date: str) -> dict:
    import json

    import fsspec

    with fsspec.open(f"{root.rstrip('/')}/{date}/meta.json", "rt") as f:
        return json.load(f)


def _snapshot_dates(root: str) -> list[str]:
    import re

    import fsspec

    fs, r = fsspec.core.url_to_fs(root)
    out = []
    for e in fs.ls(r, detail=False):
        name = e.rstrip("/").rsplit("/", 1)[-1]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", name):
            out.append(name)
    return sorted(out)


@main.command()
@option("-b", "--bot-token", help="Slack bot token (xoxb-…, or $SLACK_BOT_TOKEN); with --channel, posts via chat.postMessage so the per-message avatar applies")
@option("-c", "--ceiling-tb", type=float, help="absolute alert: flag when total TB exceeds this")
@option("-C", "--channel", help="Slack channel id for chat.postMessage (or $SLACK_CHANNEL)")
@option("-d", "--date", help="snapshot date (default: latest under --root)")
@option("-e", "--edit-ts", help="edit an existing message (chat.update at this ts) instead of posting a new one — back-applies a format change; avatar is unchanged")
@option("-n", "--dry-run", is_flag=True, help="print the message instead of posting to Slack")
@option("-p", "--prior", help="prior date to diff against (default: the snapshot before --date)")
@option("-r", "--root", help="snapshots root: gs://bucket/snapshots or a local dir (default $DATA_BUCKET)")
@option("-s", "--spike-pct", default=10.0, help="relative alert: flag when |Δ%%| exceeds this")
@option("-t", "--top", default=5, help="number of top movers to name")
@option("-w", "--webhook", help="Slack incoming webhook URL (or $SLACK_WEBHOOK); fallback with no per-message avatar")
def alert(
    bot_token: str | None,
    ceiling_tb: float | None,
    channel: str | None,
    date: str | None,
    edit_ts: str | None,
    dry_run: bool,
    prior: str | None,
    root: str | None,
    spike_pct: float,
    top: int,
    webhook: str | None,
) -> None:
    """Post a daily GCS-usage digest to Slack; flag threshold breaches (absolute
    ceiling and/or relative spike) and name the top movers by user.

    Per-message avatars (mark + 📊/🚨) require the Web API: pass a bot token
    (needs the chat:write.customize scope) + channel. Incoming webhooks ignore
    icon overrides, so the --webhook path posts with the app's static icon."""
    root = root or f"gs://{os.environ.get('DATA_BUCKET', 'oa-gcs-usage-dvx')}/snapshots"
    dates = _snapshot_dates(root)
    if not dates:
        raise SystemExit(f"no snapshots under {root}")
    date = date or dates[-1]
    if prior is None:
        earlier = [d for d in dates if d < date]
        prior = earlier[-1] if earlier else None

    cur = _load_meta(root, date)
    tb = cur["total_bytes"] / 1e12
    breach = []
    if ceiling_tb is not None and tb > ceiling_tb:
        breach.append(f"total {tb:,.0f} TB > ceiling {ceiling_tb:,.0f} TB")

    d_bytes = d_pct = 0.0
    movers: list[tuple[str, int, str, int]] = []  # (user, Δbytes, team, total bytes)
    if prior:
        pri = _load_meta(root, prior)
        d_bytes = cur["total_bytes"] - pri["total_bytes"]
        d_pct = 100 * d_bytes / pri["total_bytes"] if pri["total_bytes"] else 0.0
        if abs(d_pct) > spike_pct:
            breach.append(f"Δ {d_pct:+.1f}% vs {prior} exceeds ±{spike_pct:.0f}%")
        pri_u = {u["u"]: u["b"] for u in pri["users"]}
        movers = sorted(
            ((u["u"], u["b"] - pri_u.get(u["u"], 0), u["t"], u["b"]) for u in cur["users"]),
            key=lambda x: -x[1],
        )[:top]

    # Per-message avatar (mark + 📊/🚨 badge), served public so Slack can fetch it
    # (the app itself is Access-gated). See job/gen-slack-icons.py + the
    # gcs-usage-icons Pages project.
    ICON_BASE = "https://gcs-usage-icons.pages.dev"
    icon_url = f"{ICON_BASE}/gcs-{'breach' if breach else 'digest'}.png"
    lines = [
        f"*GCS usage — {date}*",  # no leading emoji — redundant with the bot avatar
        f"Total: *{tb:,.0f} TB* · {cur['total_objects']:,} objects",
    ]
    if prior:
        lines.append(f"Δ vs {prior}: *{d_bytes / 1e12:+,.1f} TB* ({d_pct:+.1f}%)")
    if breach:
        lines.append(":rotating_light: " + "; ".join(breach))
    # movers are sorted by Δ desc; drop the tail that rounds to ±0.0 TB, and
    # show each mover's current total attributed bytes alongside the delta.
    shown = [m for m in movers if abs(m[1]) / 1e12 >= 0.05]
    if shown:
        lines.append(f"Top movers (Δ vs {prior}):")
        lines += [f" • {u} ({t}): {db / 1e12:+.1f} TB ({b / 1e12:,.0f} TB total)" for u, db, t, b in shown]
    lines.append("<https://gcs.oa.dev|gcs.oa.dev>")
    text = "\n".join(lines)

    webhook = webhook or os.environ.get("SLACK_WEBHOOK")
    bot_token = bot_token or os.environ.get("SLACK_BOT_TOKEN")
    channel = channel or os.environ.get("SLACK_CHANNEL")
    use_api = bool(bot_token and channel)  # chat.postMessage → per-message avatar

    if edit_ts and not use_api:
        raise SystemExit("--edit-ts needs a bot token + channel (chat.update)")
    if dry_run or not (use_api or webhook):
        if not (use_api or webhook) and not dry_run:
            err("no bot-token+channel / --webhook set — printing (dry-run)")
        print(text)
        return

    import json
    import urllib.request

    if use_api:
        # chat.update to back-apply a format change (avatar set at post time is
        # untouched); else chat.postMessage with the per-message avatar.
        method = "chat.update" if edit_ts else "chat.postMessage"
        payload = {"channel": channel, "text": text, "unfurl_links": False, "unfurl_media": False}
        if edit_ts:
            payload["ts"] = edit_ts
        else:
            payload["icon_url"] = icon_url
        req = urllib.request.Request(
            f"https://slack.com/api/{method}",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        resp = json.loads(urllib.request.urlopen(req).read())
        if not resp.get("ok"):
            raise RuntimeError(f"Slack {method} failed: {resp.get('error')}")
        err(f"{'edited' if edit_ts else 'posted'} GCS-usage alert for {date}")
    else:
        req = urllib.request.Request(
            webhook,
            data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req).read()
        err(f"posted GCS-usage alert for {date} (webhook; no per-message avatar)")


if __name__ == "__main__":
    main()
