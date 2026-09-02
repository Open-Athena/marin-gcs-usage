"""``gcs-usage`` CLI.

``build`` derives the sparse dir -> user attribution table from a marin
``scan_gcs`` objects listing (parquet). The listing never loads fully into
pandas: DuckDB pre-filters it down to the two small row sets the signals
need (distinct ``users/<seg>/`` prefixes and record-file rows).
"""

from __future__ import annotations

import datetime as dt
import json
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
from .mark import DEFAULT_URL as MARK_DEFAULT_URL
from .mark import KEEP_ACTIONS as MARK_KEEPS
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


@main.command("attr-report")
@option("-a", "--attribution", "attributions", required=True, multiple=True, help="Attribution parquet(s); repeatable, concatenated")
@option("-i", "--identities", "identities_path", type=Path, default=DEFAULT_IDENTITIES, help="identities.yaml path")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-n", "--top", default=30, help="Rows in the per-user table")
@option("-u", "--user", "claim_user", default=None, help="Print this user's claim list (their attributed prefixes by bytes)")
def attr_report(
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
@option("-j", "--jobs", default=1, help="Concurrent (project, window) mining tasks — network-bound threads; ~8 is safe per API key")
@option("-M", "--no-merge", is_flag=True, help="Skip the final concat (parallel range-workers; run once without to merge)")
@option("-o", "--out", "out_path", type=Path, default=Path("tmp/wandb-runs.parquet"), help="Output parquet")
@option("-p", "--project-filter", default=None, help="Substring filter on project names")
@option("-s", "--since", default=None, help="Window start (bisection-tree edge; see -E)")
@option("-u", "--until", default=None, help="Window end (bisection-tree edge; see -E)")
def wandb_mine(
    entity: str,
    print_edges: bool,
    jobs: int,
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
        jobs=jobs,
    )


@main.command()
@option("-a", "--attribution", "attributions", multiple=True, help="Attribution parquet(s); adds per-node team/user overlays")
@option("-c", "--dir-cache", "dir_cache", type=Path, default=None, help="Layer-2 cache dir (dir-stats/age-days parquet): attribution-independent rollups reused by re-attribution runs — see specs/dir-agg-cache.md")
@option("-d", "--asof", required=True, help="Scan date the listing came from (YYYY-MM-DD)")
@option("-i", "--identities", "identities_path", type=Path, default=DEFAULT_IDENTITIES, help="identities.yaml path")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-o", "--out", "out_dir", type=Path, default=None, help="Output dir for JSON files [default: site/public/data/<asof>]")
@option("-P", "--path-index", "path_index", type=Path, default=None, help="Write the complete floor-free path index parquet here (pixel-budget subtree API; specs/path-index-lazy-drill.md)")
@option("-x", "--access", "access", multiple=True, help="Access-log layer-2a agg parquet glob(s); adds per-node last-read ('a') for the read-recency lens")
def webdata(
    attributions: tuple[str, ...],
    dir_cache: Path | None,
    asof: str,
    identities_path: Path,
    listings: tuple[str, ...],
    out_dir: Path | None,
    path_index: Path | None,
    access: tuple[str, ...],
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
    meta = write_webdata(listings, out_dir, asof, attributions, identities_path, access=access, dir_cache=dir_cache, path_index=path_index)
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


@main.command()
@option("-f", "--file", "sources", multiple=True, type=Path, help="Read prefixes from FILE (one per line; '-' = stdin). Repeatable.")
@option("-k", "--keep", default="keep", type=Choice([*MARK_KEEPS, "none"]), help="Keep action to set ('none' leaves the keep axis untouched)")
@option("-m", "--memo", default=None, help="Note stored with every action in the ledger")
@option("-n", "--dry-run", is_flag=True, help="Print the actions that would be posted, and send nothing")
@option("-o", "--owner", default="@me", help="Owner to set ('@me' = you, resolved server-side); empty string leaves the owner axis untouched")
@option("-s", "--scan", default=None, help="Snapshot id these marks were derived from (provenance)")
@option("-t", "--token", default=None, help="Bearer token (default: $GCS_USAGE_TOKEN); copy yours from the dashboard")
@option("-u", "--url", default=None, help=f"Site base URL (default: $GCS_USAGE_URL or {MARK_DEFAULT_URL})")
@argument("prefixes", nargs=-1)
def mark(
    sources: tuple[Path, ...],
    keep: str,
    memo: str | None,
    dry_run: bool,
    owner: str,
    scan: str | None,
    token: str | None,
    url: str | None,
    prefixes: tuple[str, ...],
) -> None:
    """Bulk-mark GCS prefixes in the mark & sweep ledger.

    The agent-facing entry point: an agent that knows which prefixes it owns
    hands them over and claims them in one call.

        gcs-usage mark gs://marin-us-central1/checkpoints/my-run/

        find_my_dirs | gcs-usage mark --keep keep_last_ckpt

    PREFIXES come from arguments, --file (repeatable; '-' reads stdin), or —
    when neither is given — stdin. Each must be a directory prefix under a
    marin bucket, gs://marin-<bucket>/<path>/, trailing slash required.
    """
    from .mark import (
        MarkError,
        batches,
        build_actions,
        creds as mark_creds,
        gather_prefixes,
        post_actions,
    )

    line_sources = []
    opened = []
    try:
        for s in sources:
            if str(s) == "-":
                line_sources.append(sys.stdin)
            else:
                f = open(s)
                opened.append(f)
                line_sources.append(f)
        # No explicit prefixes anywhere → read stdin, the pipe-friendly default.
        if not prefixes and not sources:
            line_sources.append(sys.stdin)
        try:
            all_prefixes = gather_prefixes(list(prefixes), line_sources)
            actions = build_actions(
                all_prefixes,
                keep=None if keep == "none" else keep,
                owner=owner or None,
                memo=memo,
                scan=scan,
            )
        except MarkError as e:
            raise SystemExit(f"error: {e}")
    finally:
        for f in opened:
            f.close()

    chunks = batches(actions)
    if dry_run:
        print(json.dumps(actions, indent=2))
        err(f"dry-run: {len(actions)} action(s) in {len(chunks)} request(s); nothing sent")
        return

    base, tok = mark_creds(token, url)
    if not tok:
        raise SystemExit("error: no token — pass --token or set $GCS_USAGE_TOKEN (copy it from the dashboard)")

    total = 0
    for i, chunk in enumerate(chunks, 1):
        try:
            res = post_actions(base, tok, chunk)
        except MarkError as e:
            raise SystemExit(f"error: {e}")
        total += res.get("count", len(chunk))
        err(f"batch {i}/{len(chunks)}: {res.get('count', len(chunk))} action(s) accepted")
    err(f"marked {total} prefix(es) as {'owner=' + owner if owner else ''}{' ' if owner and keep != 'none' else ''}{'keep=' + keep if keep != 'none' else ''}")


@main.command()
@option("-j", "--json", "as_json", is_flag=True, help="Emit the raw /api/resolve JSON")
@option("-t", "--token", default=None, help="Bearer token (default: $GCS_USAGE_TOKEN)")
@option("-u", "--url", default=None, help=f"Site base URL (default: $GCS_USAGE_URL or {MARK_DEFAULT_URL})")
@argument("path")
def status(as_json: bool, token: str | None, url: str | None, path: str) -> None:
    """Show the effective keep + owner of PATH (a gs://marin-<bucket>/<dir>/ prefix).

    Resolves via /api/resolve — the same recency fold the dashboard uses, so an
    agent sees exactly what a person would. Works at any depth (resolution is
    prefix-matching over the ledger, independent of the tree's depth cap).
    """
    from .mark import MarkError, creds, resolve_path

    base, tok = creds(token, url)
    if not tok:
        raise SystemExit("error: no token — pass --token or set $GCS_USAGE_TOKEN")
    try:
        r = resolve_path(base, tok, path)
    except MarkError as e:
        raise SystemExit(f"error: {e}")
    if as_json:
        print(json.dumps(r, indent=2))
        return

    def _src(hit: dict) -> str:
        where = "here" if hit["own"] else f"inherited from {hit['prefix']}"
        day = dt.datetime.fromtimestamp(hit["ts"], dt.timezone.utc).strftime("%Y-%m-%d")
        memo = f', memo="{hit["memo"]}"' if hit.get("memo") else ""
        return f"{where}, by {hit['who']} on {day}{memo}"

    keep, owner = r.get("keep"), r.get("owner")
    print(f"path:  {r['path']}")
    print(f"keep:  {keep['action'] + '  (' + _src(keep) + ')' if keep else 'unmarked — sweeps at the deadline unless kept'}")
    print(f"owner: {owner['owner'] + '  (' + _src(owner) + ')' if owner else 'unattributed — in lost & found'}")


@main.command()
@option("-f", "--min-frac", default=None, type=float, help="Ignore prefixes below this fraction of total bytes")
@option("-j", "--json", "as_json", is_flag=True, help="Emit the raw /api/todo JSON")
@option("-n", "--limit", default=None, type=int, help="Max items to return")
@option("-p", "--prefixes", "prefixes_only", is_flag=True, help="Print bare prefixes (pipe into `gcs-usage mark`)")
@option("-t", "--token", default=None, help="Bearer token (default: $GCS_USAGE_TOKEN)")
@option("-u", "--url", default=None, help=f"Site base URL (default: $GCS_USAGE_URL or {MARK_DEFAULT_URL})")
def todo(
    min_frac: float | None,
    as_json: bool,
    limit: int | None,
    prefixes_only: bool,
    token: str | None,
    url: str | None,
) -> None:
    """List the largest prefixes still needing a keep/sweep decision.

    The review backlog: prefixes with no decision anywhere in their subtree or
    ancestry (unmarked defaults to sweep at the deadline). Marking a chunk
    keep/sweep drops it and surfaces its still-undecided siblings.

        gcs-usage todo -p | head        # feed prefixes to review

    Note the `-p` output is a review queue, not a mark command — you still
    decide keep vs sweep per prefix.
    """
    from .mark import MarkError, creds, todo_list

    base, tok = creds(token, url)
    if not tok:
        raise SystemExit("error: no token — pass --token or set $GCS_USAGE_TOKEN")
    try:
        r = todo_list(base, tok, limit=limit, min_frac=min_frac)
    except MarkError as e:
        raise SystemExit(f"error: {e}")
    if as_json:
        print(json.dumps(r, indent=2))
        return
    items = r.get("items", [])
    if prefixes_only:
        for it in items:
            print(it["prefix"])
        return
    err(f"scan {r.get('scan')}: {r.get('count')} undecided prefix(es) ≥ {r.get('min_bytes', 0) / 1e9:.1f} GB")
    for it in items:
        print(f"{it['bytes'] / 1e9:8.1f} GB  {it['objects']:>10,}  {it['prefix']}")


@main.command()
@option("-d", "--date", default=None, help="Scan date YYYY-MM-DD (default: latest from scans.json)")
@option("-f", "--max-age-days", default=2, type=int, help="Freshness: latest scan must be within this many days")
@option("-j", "--json", "as_json", is_flag=True, help="Emit machine-readable JSON to stdout")
@option("-m", "--max-ms", default=25000, type=int, help="marks/totals compute budget (ms) before it's flagged")
@option("-t", "--token", default=None, help="Bearer token (default: $GCS_USAGE_TOKEN)")
@option("-u", "--url", default=None, help=f"Site base URL (default: $GCS_USAGE_URL or {MARK_DEFAULT_URL})")
def healthcheck(date: str | None, max_age_days: int, as_json: bool, max_ms: int, token: str | None, url: str | None) -> None:
    """Live-site health: is the latest scan actually *servable* end-to-end?

    Catches failures where the data pipeline succeeds but the site can't serve
    the scan — e.g. a path-index footer that never synced to D1, so
    /api/marks/totals footer-parses and 1102s (the 2026-08-31 /users outage).
    Checks scan freshness, marks/totals (200 + D1-index path + non-empty users
    + compute budget), subtree, and the published data JSONs. Exits nonzero if
    any check fails — wire it into a cron / post-snapshot gate.
    """
    from .healthcheck import as_dict, run_checks
    from .mark import creds

    base, tok = creds(token, url)
    if not tok:
        raise SystemExit("error: no token — pass --token or set $GCS_USAGE_TOKEN")
    resolved, checks = run_checks(base, tok, date, max_age_days=max_age_days, max_ms=max_ms)
    err(f"healthcheck {base} @ {resolved or '?'}")
    for c in checks:
        err(f"  {'✓' if c.ok else '✗'} {c.name:<16} {c.detail}")
    n_ok = sum(c.ok for c in checks)
    ok = n_ok == len(checks)
    err(f"{'PASS' if ok else 'FAIL'} ({n_ok}/{len(checks)})")
    if as_json:
        print(json.dumps(as_dict(resolved, checks), indent=2))
    if not ok:
        raise SystemExit(1)


@main.command("diff")
@option("-b", "--budget", default=400, type=int, help="Max directory expansions for the best-first walk")
@option("-c", "--curr", default=None, help="Curr scan date YYYY-MM-DD (default: latest with a path-index)")
@option("-D", "--max-depth", default=8, type=int, help="Deepest level to descend to")
@option("-o", "--out", default=None, help="Output (default <root>/snapshots/<curr>/diff.json; '-' = stdout)")
@option("-p", "--prev", default=None, help="Prev scan date (default: nearest earlier date with a path-index)")
@option("-r", "--root", default="gs://oa-gcs-usage-dvx", help="Data bucket root (gs:// or a local mount)")
@option("-t", "--top", default=500, type=int, help="Max rows kept in the JSON (by |Δsize|)")
def diff_cmd(budget: int, curr: str | None, max_depth: int, out: str | None, prev: str | None, root: str, top: int) -> None:
    """Scan-over-scan diff → the site's `diff.json` ("Changes since previous
    scan" treemap). Runs disk-tree's best-first `recursive_diff` over two
    dates' `listing/<date>/path-index.parquet`, so a delta N levels deep
    surfaces as its own row instead of an undifferentiated blob at depth 1."""
    import fsspec

    from .diff import compute_diff, write_json

    fs, rootpath = fsspec.core.url_to_fs(root)
    idxs = sorted(fs.glob(f"{rootpath}/listing/*/path-index.parquet"))
    dates = [p.rsplit("/", 2)[-2] for p in idxs]
    if curr is None:
        curr = dates[-1] if dates else None
    if curr is None or curr not in dates:
        raise SystemExit(f"no path-index for curr={curr!r} under {root}/listing/ (have {dates[-3:]})")
    if prev is None:
        earlier = [d for d in dates if d < curr]
        if not earlier:
            raise SystemExit(f"no scan earlier than {curr} has a path-index — nothing to diff against")
        prev = earlier[-1]
    if prev not in dates:
        raise SystemExit(f"no path-index for prev={prev!r} under {root}/listing/")
    idx = lambda d: f"{rootpath}/listing/{d}/path-index.parquet"  # noqa: E731
    err(f"diff {prev} → {curr} (budget {budget}, max-depth {max_depth})")
    payload = compute_diff(idx(prev), idx(curr), prev_id=prev, curr_id=curr, budget=budget, max_depth=max_depth, top=top, fs=fs)
    if out == "-":
        print(json.dumps(payload))
    else:
        out = out or f"{root.rstrip('/')}/snapshots/{curr}/diff.json"
        write_json(payload, out)
        err(f"{len(payload['rows'])} delta rows, {payload['expansions']} expansions, "
            f"Δtotal {payload['total_b'] - payload['total_a']:+,} bytes -> {out}")


@main.group()
def sweep() -> None:
    """Sweep-executor phases — plan / review / execute (specs/sweep-executor.md)."""


@sweep.command("clobbers")
@option("-j", "--json", "as_json", is_flag=True, help="Machine-readable JSON to stdout")
@option("-t", "--token", default=None, help="Bearer token (default: $GCS_USAGE_TOKEN)")
@option("-u", "--url", default=None, help=f"Site base URL (default: $GCS_USAGE_URL or {MARK_DEFAULT_URL})")
def sweep_clobbers(as_json: bool, token: str | None, url: str | None) -> None:
    """Keeps whose effective fate is now sweep — a newer covering sweep
    repainted them (recency beats specificity). The review list for reverting
    accidental broad sweeps; the planner independently refuses to delete
    anything with keep history (`ever_kept_prefixes`)."""
    from .mark import creds, get_json
    from .sweep_plan import clobbered_keeps, load_keeps

    base, tok = creds(token, url)
    if not tok:
        raise SystemExit("no token — set $GCS_USAGE_TOKEN or pass -t")
    payload = get_json(base, tok, "/api/actions")
    rows = load_keeps(payload)
    clobbers = clobbered_keeps(rows)
    if as_json:
        print(json.dumps([asdict(c) for c in clobbers], indent=2))
        return
    if not clobbers:
        err("no clobbered keeps — every keep-marked prefix still resolves keep")
        return
    n_sweep = sum(1 for c in clobbers if c.to == "sweep")
    err(f"{len(clobbers)} keep-marked prefix(es) repainted away ({n_sweep} → sweep, {len(clobbers) - n_sweep} → unmarked):")
    fmt_ts = lambda ts: dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%m-%d %H:%M")  # noqa: E731
    for c in clobbers:
        print(f"{c.prefix}")
        print(f"    {c.keep} by {c.keeper} @ {fmt_ts(c.keep_ts)}  ⟵ now {c.to} via {c.by_prefix} ({c.by_who} @ {fmt_ts(c.by_ts)})")


@sweep.command("plan")
@option("-d", "--date", default=None, help="Scan date (default: latest from scans.json)")
@option("-j", "--json", "as_json", is_flag=True, help="Machine-readable JSON to stdout")
@option("-n", "--top", default=20, type=int, help="Top sweep-only bands to list")
@option("-t", "--token", default=None, help="Bearer token (default: $GCS_USAGE_TOKEN)")
@option("-u", "--url", default=None, help=f"Site base URL (default: $GCS_USAGE_URL or {MARK_DEFAULT_URL})")
def sweep_plan_cmd(date: str | None, as_json: bool, top: int, token: str | None, url: str | None) -> None:
    """Band-level sweep plan under the VOTE model (specs/vote-model.md):
    per-mark bands (the site's `?marks=1` manifest — exact D1-index bytes)
    re-aggregated by vote-state. `sweep` bands are deletable now; `conflict`
    goes to triage; `unmarked` waits for the deadline. Object-level manifest
    generation (the exact delete list) is the Batch-side follow-up."""
    from .mark import creds, get_json
    from .sweep_plan import VoteResolver, load_keeps

    base, tok = creds(token, url)
    if not tok:
        raise SystemExit("no token — set $GCS_USAGE_TOKEN or pass -t")
    actions = get_json(base, tok, "/api/actions")
    rows = load_keeps(actions)
    head = max((r.action_id for r in rows), default=0)
    vr = VoteResolver(rows)
    if not date:
        scans = get_json(base, tok, "/data/scans.json")
        date = scans[0] if isinstance(scans, list) and scans else None
        if not date:
            raise SystemExit("no --date and scans.json gave none")
    totals = get_json(base, tok, "/api/marks/totals", {"marks": "1", "date": date}, timeout=90)
    marks = totals.get("marks") or []

    states: dict[str, dict] = {}
    for m in marks:
        votes = vr.votes(m["prefix"])
        st = VoteResolver._agg(votes.values())
        if st == "keep" and votes and all(v == "keep_last_ckpt" for v in votes.values()):
            st = "keep_last_ckpt"
        b = states.setdefault(st, {"bands": 0, "net_bytes": 0, "net_objects": 0, "rows": []})
        b["bands"] += 1
        b["net_bytes"] += m.get("net_bytes") or 0
        b["net_objects"] += m.get("net_objects") or 0
        b["rows"].append({**m, "votes": votes})

    if as_json:
        print(json.dumps({
            "date": date, "head": head,
            "site_total": totals.get("total"),
            "states": {k: {kk: vv for kk, vv in v.items() if kk != "rows"} for k, v in states.items()},
            "sweep_bands": sorted(states.get("sweep", {}).get("rows", []), key=lambda r: -(r.get("net_bytes") or 0)),
            "conflict_bands": sorted(states.get("conflict", {}).get("rows", []), key=lambda r: -(r.get("net_bytes") or 0)),
        }, indent=2))
        return

    err(f"vote-model plan @ head {head} · scan {date or 'latest'} · {len(marks)} mark bands")
    for st in ("sweep", "conflict", "keep", "keep_last_ckpt", "unmarked"):
        v = states.get(st)
        if not v:
            continue
        err(f"  {st:15s} {v['bands']:6,} bands  {v['net_bytes'] / 1e12:10.2f} TB  {v['net_objects']:>13,} objects")
    err(f"\ntop {top} sweep-only (deletable) bands:")
    for r in sorted(states.get("sweep", {}).get("rows", []), key=lambda r: -(r.get("net_bytes") or 0))[:top]:
        who = ",".join(sorted(r["votes"]))
        print(f"{(r.get('net_bytes') or 0) / 1e12:9.2f} TB  {r['prefix']}  [{who}]")


@sweep.command("manifest")
@option("-A", "--approve", "approved", multiple=True, help="Approved band prefix (gs://…/); given ≥1, approval REPLACES the owner-claim check")
@option("-b", "--bucket", "only_buckets", multiple=True, help="Only these buckets (default: all six)")
@option("-d", "--date", required=True, help="Scan date whose listing to plan from (pinned)")
@option("-o", "--out", default=None, help="Output dir (default gs://oa-gcs-usage-dvx/sweep/<date>-h<head>)")
@option("-r", "--root", default="gs://oa-gcs-usage-dvx", help="Listing root (gs:// or local mount)")
@option("-t", "--token", default=None, help="Bearer token (default: $GCS_USAGE_TOKEN)")
@option("-u", "--url", default=None, help=f"Site base URL (default: $GCS_USAGE_URL or {MARK_DEFAULT_URL})")
def sweep_manifest(approved: tuple[str, ...], only_buckets: tuple[str, ...], date: str, out: str | None, root: str, token: str | None, url: str | None) -> None:
    """Object-level sweep manifest under the vote model + policy (b): stream
    the pinned listing, classify every directory (specs/sweep-executor.md,
    specs/vote-model.md), and write per-bucket parquets of the ELIGIBLE keys
    (sweep-only, sweeper-owned, no keep history) plus a category summary.
    Pure read + artifact write — deletes nothing."""
    import fsspec
    import pyarrow as pa
    import pyarrow.parquet as pq

    from .identity import load_identities
    from .mark import creds, get_json
    from .sweep_plan import (
        CATEGORIES, VoteResolver, classify_dir, ever_kept_prefixes, load_keeps, owners_resolver,
    )

    base, tok = creds(token, url)
    if not tok:
        raise SystemExit("no token — set $GCS_USAGE_TOKEN or pass -t")
    actions = get_json(base, tok, "/api/actions")
    rows = load_keeps(actions)
    head = max((r.action_id for r in rows), default=0)
    vr = VoteResolver(rows)
    own = owners_resolver(actions)
    idmap = load_identities()
    ever = ever_kept_prefixes(rows)
    out = out or f"gs://oa-gcs-usage-dvx/sweep/{date}-h{head}"
    err(f"sweep manifest: scan {date} @ head {head} → {out}"
        + (f" · {len(approved)} approved band(s)" if approved else ""))

    fs, rootpath = fsspec.core.url_to_fs(root)
    buckets = list(only_buckets) or [
        "marin-us-east1", "marin-us-east5", "marin-us-central1",
        "marin-us-central2", "marin-eu-west4", "marin-us-west4",
    ]
    summary: dict = {"date": date, "head": head, "policy": "b:approved-bands" if approved else "b:sweeper-owns", "approved": list(approved), "buckets": {}}
    schema = pa.schema([
        ("name", pa.string()), ("size_bytes", pa.int64()),
        ("storage_class_id", pa.int8()), ("created", pa.timestamp("us", tz="UTC")),
        ("dir", pa.string()), ("owner", pa.string()), ("sweepers", pa.string()),
    ])
    for bucket in buckets:
        shards = sorted(fs.glob(f"{rootpath}/listing/{date}/{bucket}/*.parquet"))
        if not shards:
            raise SystemExit(f"no listing shards for {bucket} under {root}/listing/{date}/")
        cache: dict[str, tuple[str, str | None, tuple[str, ...]]] = {}
        cats = {c: [0, 0] for c in CATEGORIES}  # bytes, objects
        writer = None
        out_path = f"{out}/manifest/{bucket}.parquet"
        ofs, opath = fsspec.core.url_to_fs(out_path)
        n = 0
        for shard in shards:
            pf = pq.ParquetFile(shard, filesystem=fs)
            for batch in pf.iter_batches(columns=["name", "size_bytes", "storage_class_id", "created"], batch_size=1 << 17):
                df = batch.to_pandas()
                dirs = df["name"].str.rpartition("/")[0]
                for dn in dirs.unique():
                    if dn not in cache:
                        cache[dn] = classify_dir(bucket, dn, vr, own, idmap, ever, approved)
                cat = dirs.map(lambda dn: cache[dn][0])
                sizes = df["size_bytes"]
                for c, g in sizes.groupby(cat):
                    cats[c][0] += int(g.sum())
                    cats[c][1] += len(g)
                elig = cat == "eligible"
                if elig.any():
                    sel = df[elig].copy()
                    sel["dir"] = dirs[elig]
                    sel["owner"] = sel["dir"].map(lambda dn: cache[dn][1])
                    sel["sweepers"] = sel["dir"].map(lambda dn: ",".join(cache[dn][2]))
                    t = pa.Table.from_pandas(sel, preserve_index=False).select(schema.names).cast(schema)
                    if writer is None:
                        writer = pq.ParquetWriter(opath, schema, filesystem=ofs)
                    writer.write_table(t)
                n += len(df)
        if writer is not None:
            writer.close()
        summary["buckets"][bucket] = {"objects": n, "dirs": len(cache), **{c: {"bytes": b, "objects": o} for c, (b, o) in cats.items() if o}}
        eb, eo = cats["eligible"]
        err(f"  {bucket}: {n:,} keys, {len(cache):,} dirs — eligible {eb / 1e12:.2f} TB / {eo:,} objects")

    tot = {c: [0, 0] for c in CATEGORIES}
    for b in summary["buckets"].values():
        for c in CATEGORIES:
            if c in b:
                tot[c][0] += b[c]["bytes"]
                tot[c][1] += b[c]["objects"]
    summary["total"] = {c: {"bytes": v[0], "objects": v[1]} for c, v in tot.items() if v[1]}
    with fsspec.open(f"{out}/plan-summary.json", "w") as fh:
        json.dump(summary, fh, indent=2)
    err("\ntotals by category:")
    for c, (b, o) in tot.items():
        if o:
            err(f"  {c:16s} {b / 1e12:10.2f} TB  {o:>13,} objects")
    err(f"\nwrote {out}/plan-summary.json")


@sweep.command("execute")
@option("-b", "--bucket", "only_buckets", multiple=True, help="Only these buckets")
@option("-D", "--drift", type=Choice(["skip", "proceed"]), default="skip", help="Dirs that gained new keys since the scan: skip (default) or proceed (manifest keys only — new keys always survive)")
@option("-t", "--token", default=None, help="Bearer token (default: $GCS_USAGE_TOKEN)")
@option("-u", "--url", default=None, help=f"Site base URL (default: $GCS_USAGE_URL or {MARK_DEFAULT_URL})")
@option("-w", "--workers", default=8, type=int, help="Concurrent directory re-lists")
@option("--for-real", is_flag=True, help="Actually delete (default: dry-run writes would-delete/)")
@argument("plan_dir")
def sweep_execute(only_buckets: tuple[str, ...], drift: str, token: str | None, url: str | None, workers: int, for_real: bool, plan_dir: str) -> None:
    """Execute (default: DRY-RUN) a `sweep manifest` plan: fresh re-list per
    eligible dir, generation-matched deletes of manifest∩live keys whose
    timeCreated is unchanged. Every manifest dir is re-classified at the
    CURRENT ledger head first — newer keeps/unmarks drop dirs (ledger drift).
    `--for-real` additionally requires ≥7d soft delete on every bucket."""
    from .identity import load_identities
    from .mark import creds, get_json
    from .sweep_plan import (
        VoteResolver, classify_dir, ever_kept_prefixes, load_keeps, owners_resolver,
    )
    from .sweep_exec import execute_plan

    base, tok = creds(token, url)
    if not tok:
        raise SystemExit("no token — set $GCS_USAGE_TOKEN or pass -t")
    actions = get_json(base, tok, "/api/actions")
    rows = load_keeps(actions)
    vr = VoteResolver(rows)
    own = owners_resolver(actions)
    idmap = load_identities()
    ever = ever_kept_prefixes(rows)
    head = max((r.action_id for r in rows), default=0)
    err(f"execute {'FOR REAL' if for_real else '(dry-run)'} @ current head {head}")

    def reclassify(bucket: str, dn: str) -> str:
        return classify_dir(bucket, dn, vr, own, idmap, ever)[0]

    summary = execute_plan(
        plan_dir,
        for_real=for_real,
        only_buckets=only_buckets,
        drift=drift,
        workers=workers,
        reclassify=reclassify,
    )
    total = sum(b.get("delete_bytes", 0) for b in summary["buckets"].values())
    err(f"\n{'deleted' if for_real else 'would delete'}: {total / 1e12:.2f} TB total")


@main.group()
def access() -> None:
    """GCS usage-log (access-log) ingest — layer-1a/2a parquet + watermarks."""


@access.command("ingest")
@option("-b", "--bucket", "buckets", multiple=True, help="Source buckets (default: the marin fleet)")
@option("-c", "--max-chunk-gb", default=32.0, help="Max staged CSV bytes per processing chunk")
@option("-d", "--data-bucket", default="oa-gcs-usage-dvx", help="Output/state bucket")
@option("-l", "--log-bucket", default=None, help="Usage-log delivery bucket (default: marin-usage-logs)")
@option("-M", "--memory-limit", default=None, help="DuckDB memory limit (default: $DUCKDB_MEM or 8GB)")
@option("-n", "--max-chunks", default=None, type=int, help="Stop after N chunks per bucket (smoke runs)")
@option("-s", "--stage-dir", type=Path, default=None, help="Local staging dir (default: $STAGE_DIR or /tmp, + /access-stage)")
@option("-w", "--workers", default=16, help="Concurrent CSV downloads")
def access_ingest(
    buckets: tuple[str, ...],
    max_chunk_gb: float,
    data_bucket: str,
    log_bucket: str | None,
    memory_limit: str | None,
    max_chunks: int | None,
    stage_dir: Path | None,
    workers: int,
) -> None:
    """Incrementally ingest new usage CSVs → layer-1a/2a parquet in the data bucket."""
    from .access import FLEET, USAGE_LOG_BUCKET, ingest

    ingest(
        buckets=buckets or FLEET,
        log_bucket=log_bucket or USAGE_LOG_BUCKET,
        data_bucket=data_bucket,
        stage_dir=(stage_dir or Path(os.environ.get("STAGE_DIR") or "/tmp") / "access-stage"),
        memory_limit=memory_limit or os.environ.get("DUCKDB_MEM_ACCESS") or "8GB",
        max_chunk_gb=max_chunk_gb,
        workers=workers,
        max_chunks=max_chunks,
    )


@access.command("ingest-one")
@option("-d", "--data-bucket", default="oa-gcs-usage-dvx", help="Output bucket")
@option("-l", "--log-bucket", default=None, help="Usage-log delivery bucket (default: marin-usage-logs)")
@option("-M", "--memory-limit", default=None, help="DuckDB memory limit (default: $DUCKDB_MEM_ACCESS or 8GB)")
@option("-S", "--no-sweep", is_flag=True, help="Leave the CSV in usage/ instead of moving it to ingested/")
@option("-s", "--stage-dir", type=Path, default=None, help="Local staging dir (default: $STAGE_DIR or /tmp, + /access-stage)")
@argument("object_name")
def access_ingest_one(
    data_bucket: str,
    log_bucket: str | None,
    memory_limit: str | None,
    no_sweep: bool,
    stage_dir: Path | None,
    object_name: str,
) -> None:
    """Ingest a single usage CSV at OBJECT_NAME → L0 shards (the drain's unit).

    Output names derive from OBJECT_NAME, so re-running is idempotent. Same code
    path `access drain` runs per file; use it to exercise one CSV by hand
    (`specs/reactive-ingest.md`).
    """
    from google.cloud import storage

    from .access import USAGE_LOG_BUCKET
    from .reactive import Skip, classify, ingest_one

    what = classify(object_name)
    if isinstance(what, Skip):
        err(f"skipping {object_name}: {what.reason}")
        return
    ingest_one(
        storage.Client(),
        what,
        stage_dir=(stage_dir or Path(os.environ.get("STAGE_DIR") or "/tmp") / "access-stage"),
        data_bucket=data_bucket,
        log_bucket=log_bucket or USAGE_LOG_BUCKET,
        memory_limit=memory_limit or os.environ.get("DUCKDB_MEM_ACCESS") or "8GB",
        sweep=not no_sweep,
    )


@access.command("drain")
@option("-a", "--min-age-hours", default=0, help="Hold back CSVs whose log-hour is newer than this")
@option("-b", "--bucket", "buckets", multiple=True, help="Source buckets (default: the marin fleet)")
@option("-d", "--data-bucket", default="oa-gcs-usage-dvx", help="Output bucket")
@option("-l", "--log-bucket", default=None, help="Usage-log delivery bucket (default: marin-usage-logs)")
@option("-M", "--memory-limit", default=None, help="DuckDB memory limit, split across workers (default: $DUCKDB_MEM_ACCESS or 8GB)")
@option("-n", "--dry-run", is_flag=True, help="Print the work list without ingesting it")
@option("-S", "--no-sweep", is_flag=True, help="Leave CSVs in usage/ instead of moving them to ingested/")
@option("-s", "--stage-dir", type=Path, default=None, help="Local staging dir (default: $STAGE_DIR or /tmp, + /access-stage)")
@option("-w", "--workers", default=4, help="Concurrent CSV ingests")
def access_drain(
    min_age_hours: int,
    buckets: tuple[str, ...],
    data_bucket: str,
    log_bucket: str | None,
    memory_limit: str | None,
    dry_run: bool,
    no_sweep: bool,
    stage_dir: Path | None,
    workers: int,
) -> None:
    """Ingest every usage CSV that has no L0 shard yet. The primary ingest path.

    The work list is a set difference between two bucket listings — what's in
    `usage/` minus what's already in `access/raw/<bucket>/l0/` — so there is no
    watermark, lease or queue to lose, and an interrupted run is repaired by
    simply running again (`specs/reactive-ingest.md`).

    Do not run this while the polled `access ingest` is still live: CSVs it has
    ingested but not yet swept have no L0 shard, so they would be ingested a
    second time. See `access sweep --through-watermark` for the cutover.
    """
    import datetime as dt

    from google.cloud import storage

    from .access import FLEET, USAGE_LOG_BUCKET, _ts_minus_hours
    from .reactive import drain, pending

    floor = ""
    if min_age_hours:
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y_%m_%d_%H_%M_%S")
        floor = _ts_minus_hours(now, min_age_hours)
    client = storage.Client()
    log_bucket = log_bucket or USAGE_LOG_BUCKET
    if dry_run:
        total = 0
        for b in buckets or FLEET:
            for basename in pending(client, b, log_bucket=log_bucket, data_bucket=data_bucket, floor=floor):
                print(f"{b}\t{basename}")
                total += 1
        err(f"drain: {total} CSV(s) pending" + (f" (floor {floor})" if floor else ""))
        return
    stats = drain(
        client,
        buckets or FLEET,
        log_bucket=log_bucket,
        data_bucket=data_bucket,
        stage_dir=(stage_dir or Path(os.environ.get("STAGE_DIR") or "/tmp") / "access-stage"),
        floor=floor,
        memory_limit=memory_limit or os.environ.get("DUCKDB_MEM_ACCESS") or "8GB",
        workers=workers,
        sweep=not no_sweep,
    )
    if stats["failed"]:
        raise SystemExit(1)


@access.command("sweep")
@option("-b", "--bucket", "buckets", multiple=True, help="Source buckets (default: the marin fleet)")
@option("-d", "--data-bucket", default="oa-gcs-usage-dvx", help="State bucket holding the watermarks")
@option("-l", "--log-bucket", default=None, help="Usage-log delivery bucket (default: marin-usage-logs)")
@option("-T", "--through-watermark", is_flag=True, help="Sweep through the watermark itself, not watermark − lag")
@option("-w", "--workers", default=16, help="Concurrent copy+delete pairs")
def access_sweep(
    buckets: tuple[str, ...],
    data_bucket: str,
    log_bucket: str | None,
    through_watermark: bool,
    workers: int,
) -> None:
    """Move already-ingested CSVs out of `usage/` into `ingested/` (7d TTL).

    The polled `ingest` does this itself at the end of each run, but only up to
    watermark − 6h, leaving the lag window in place for late deliveries.

    `-T` sweeps the lag window too, which is the one-shot cutover step to the
    list-based drain: the drain treats anything left in `usage/` as un-ingested,
    so the polled path's residue has to be cleared first. Run it only after the
    polled ingest has been switched off for good.
    """
    from google.cloud import storage

    from .access import FLEET, LAG_HOURS, USAGE_LOG_BUCKET, load_state, sweep_ingested

    client = storage.Client()
    total = 0
    for b in buckets or FLEET:
        state = load_state(client, data_bucket, b)
        total += sweep_ingested(
            client, log_bucket or USAGE_LOG_BUCKET, b, state.get("watermark"),
            workers=workers, lag_hours=0 if through_watermark else LAG_HOURS,
        )
    err(f"sweep: {total} CSV(s) moved to ingested/")


@access.command("compact")
@option("-b", "--bucket", "buckets", multiple=True, help="Source buckets (default: the marin fleet)")
@option("-d", "--data-bucket", default="oa-gcs-usage-dvx", help="Output bucket")
@option("-K", "--keep-l0", is_flag=True, help="Leave L0 shards in place after promoting them")
@option("-L", "--lag-days", default=2, help="Treat days within this many days of now as still open")
@option("-M", "--memory-limit", default=None, help="DuckDB memory limit (default: $DUCKDB_MEM_ACCESS or 8GB)")
@option("-n", "--dry-run", is_flag=True, help="Report what would be compacted")
@option("-s", "--stage-dir", type=Path, default=None, help="Local staging dir (default: $STAGE_DIR or /tmp, + /access-stage)")
def access_compact(
    buckets: tuple[str, ...],
    data_bucket: str,
    keep_l0: bool,
    lag_days: int,
    memory_limit: str | None,
    dry_run: bool,
    stage_dir: Path | None,
) -> None:
    """Promote closed days' L0 shards into span-named layer-1a/2a/2b files.

    Reactive ingest writes one L0 shard per CSV (~2,700/day fleet-wide); left
    alone that's ~1M/year, which dominates any scan over the archive. This is
    the L1 half of the split (`specs/reactive-ingest.md` § 4).
    """
    import datetime as dt

    from google.cloud import storage

    from .access import FLEET
    from .reactive import closed_days, compact_day, group_by_day

    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lag_days)).strftime("%Y_%m_%d")
    client = storage.Client()
    for b in buckets or FLEET:
        l0_prefix = f"access/raw/{b}/l0/"
        names = [
            blob.name[len(l0_prefix):].removesuffix(".parquet")
            for blob in client.list_blobs(data_bucket, prefix=l0_prefix)
        ]
        by_day = group_by_day(names)
        for day in closed_days(by_day, cutoff):
            if dry_run:
                print(f"{b}\t{day}\t{len(by_day[day])} shards")
                continue
            compact_day(
                client, b, day, by_day[day],
                stage_dir=(stage_dir or Path(os.environ.get("STAGE_DIR") or "/tmp") / "access-stage"),
                data_bucket=data_bucket,
                memory_limit=memory_limit or os.environ.get("DUCKDB_MEM_ACCESS") or "8GB",
                delete_l0=not keep_l0,
            )
    err(f"compact: done (cutoff {cutoff}, lag {lag_days}d)")


@access.command("status")
@option("-b", "--bucket", "buckets", multiple=True, help="Source buckets (default: the marin fleet)")
@option("-d", "--data-bucket", default="oa-gcs-usage-dvx", help="Output/state bucket")
@option("-l", "--log-bucket", default=None, help="Usage-log delivery bucket (default: marin-usage-logs)")
def access_status(buckets: tuple[str, ...], data_bucket: str, log_bucket: str | None) -> None:
    """Per-bucket watermark vs delivered backlog (files/bytes awaiting ingest)."""
    from google.cloud import storage

    from .access import FLEET, USAGE_LOG_BUCKET, list_new, load_state

    client = storage.Client()
    for b in buckets or FLEET:
        state = load_state(client, data_bucket, b)
        todo = list_new(client, log_bucket or USAGE_LOG_BUCKET, b, state)
        n_bytes = sum(s for _, s in todo)
        print(
            f"{b:22s}  watermark={state.get('watermark') or '(none)'}  "
            f"backlog={len(todo)} files / {n_bytes / 1e9:.1f} GB"
        )


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
@option("-P", "--procs", default=24, help="bulk-list worker processes per task")
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

    # An http(s) root (e.g. the dev proxy) can't be `ls`ed — the scan list is
    # synthesized. Fall back to its scans.json listing.
    if root.startswith(("http://", "https://")):
        import json

        with fsspec.open(f"{root.rstrip('/')}/scans.json", "rt") as f:
            return sorted(d for d in json.load(f) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d))
    fs, r = fsspec.core.url_to_fs(root)
    out = []
    for e in fs.ls(r, detail=False):
        name = e.rstrip("/").rsplit("/", 1)[-1]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", name):
            out.append(name)
    return sorted(out)


def _load_tree(root: str, date: str) -> dict:
    import json

    import fsspec

    with fsspec.open(f"{root.rstrip('/')}/{date}/tree.json", "rt") as f:
        return json.load(f)



def _fate_totals(tree: dict, keep_rows: list[dict], cutoff_ts: int) -> dict[str, int]:
    """Replay the actions ledger as of ``cutoff_ts`` against one archived tree.

    Mirrors the site's fate resolution (``site/src/sweep.ts``): most recent
    live mark on an ancestor-or-equal prefix wins; ``keep_last_ckpt``
    decomposes into real keep/sweep via the newest step-child of every
    checkpoint run under the mark (unresolvable KLC counts as keep, matching
    the UI fold). Returns bytes per fate: keep / sweep / undecided.
    """
    import re as _re

    live: dict[str, dict] = {}
    for r in keep_rows:
        if r["ts"] > cutoff_ts:
            continue
        cur = live.get(r["prefix"])
        if cur is None or (r["ts"], r["action_id"]) > (cur["ts"], cur["action_id"]):
            live[r["prefix"]] = r

    def norm(u: str) -> str:
        return u if u.endswith("/") else u + "/"

    anc: set[str] = set()
    for r in live.values():
        if r["keep"] is None:
            continue
        segs = r["prefix"].rstrip("/").split("/")
        for i in range(3, len(segs)):
            anc.add("/".join(segs[:i]) + "/")
    own = {norm(r["prefix"]): r for r in live.values()}

    ckpt_re = _re.compile(r"^(?:step|checkpoint|ckpt|iter|epoch|global_?step)[-_]?(\d+)", _re.I)

    def node_at(prefix: str) -> dict | None:
        node: dict | None = tree
        for s in prefix.split("://", 1)[-1].rstrip("/").split("/"):
            node = next((c for c in (node or {}).get("c", ()) if c["n"] == s), None)
            if node is None:
                return None
        return node

    splits: dict[str, tuple[list[tuple[str, int]], int, int]] = {}
    for r in live.values():
        if r["keep"] != "keep_last_ckpt":
            continue
        nd = node_at(r["prefix"])
        if not nd:
            continue
        kept: list[tuple[str, int]] = []

        def kw(n: dict, u: str) -> None:
            steps = [(c, ckpt_re.match(c["n"])) for c in n.get("c", ())]
            steps = [(c, m) for c, m in steps if m]
            if steps:
                best = max(steps, key=lambda x: int(x[1].group(1)))
                kept.append((u + best[0]["n"] + "/", best[0]["b"]))
                return
            for c in n.get("c", ()):
                if not c["n"].startswith("("):
                    kw(c, u + c["n"] + "/")

        kw(nd, norm(r["prefix"]))
        if kept:
            splits[norm(r["prefix"])] = (kept, sum(b for _, b in kept), nd["b"])

    tot = {"keep": 0, "sweep": 0, "undecided": 0}

    def newer(a: dict, b: dict) -> bool:
        return (a["ts"], a["action_id"]) > (b["ts"], b["action_id"])

    def win_of(uri: str, inh: dict | None) -> dict | None:
        o = own.get(norm(uri))
        return o if o and (inh is None or newer(o, inh)) else inh

    def settle(uri: str, b: int, win: dict | None) -> None:
        if b <= 0:
            return
        f = win["keep"] if win else None
        if f is None:
            tot["undecided"] += b
            return
        if f == "keep_last_ckpt":
            sp = splits.get(norm(win["prefix"]))
            if sp:
                kept, _kept_b, _total_b = sp
                u = norm(uri)
                if any(u.startswith(k) for k, _ in kept):
                    tot["keep"] += b
                    return
                inside = sum(kb for k, kb in kept if k.startswith(u))
                if inside:
                    kb = min(b, inside)
                    tot["keep"] += kb
                    tot["sweep"] += b - kb
                    return
                tot["sweep"] += b
                return
            tot["keep"] += b
            return
        tot["keep" if f == "keep" else "sweep"] += b

    def fwalk(n: dict, uri: str, inh: dict | None) -> None:
        win = win_of(uri, inh)
        if norm(uri) not in anc:
            settle(uri, n["b"], win)
            return
        rest = n["b"]
        for c in n.get("c", ()):
            if c["n"].startswith("("):
                continue
            rest -= c["b"]
            fwalk(c, f"{uri}/{c['n']}", win)
        settle(uri, rest, win)

    for bkt in tree.get("c", ()):
        fwalk(bkt, f"gs://{bkt['n']}", None)
    return tot


@main.command()
@option("-a", "--actions", "actions_path", help="Actions-ledger export (the /api/actions JSON; path or URL). When given, replays marks as of each scan date and emits per-date keep/sweep/undecided totals")
@option("-D", "--max-depth", default=4, help="Deepest prefix level to index (path segments below the bucket root)")
@option("-f", "--min-frac", default=0.002, help="Chart a prefix if its bytes reach this fraction of the fleet total in any scan")
@option("-F", "--full-depth", default=2, help="Always index prefixes this many segments deep (bucket + one dir), whatever their size, so common drill targets are covered")
@option("-o", "--out", type=Path, default=None, help="Write the series JSON here (default: stdout)")
@option("-r", "--root", help="snapshots root: gs://bucket/snapshots, an http base (the dev proxy), or a local dir (default $DATA_BUCKET)")
def series(actions_path: str | None, max_depth: int, min_frac: float, full_depth: int, out: Path | None, root: str | None) -> None:
    """Cross-scan size index for the site's per-subpath "size over time" chart.

    Folds every archived ``tree.json`` into one compact file: for each prefix
    whose bytes clear a fraction-of-fleet floor in any scan (mirrors the treemap
    fold), its stored bytes at every scan date. Scans are immutable, so this is
    effectively append-only — re-run after each new snapshot. See
    ``specs/size-over-time.md`` (case 1); below-floor / deeper prefixes fall back
    to the fleet total in the UI.
    """
    import json

    root = root or f"gs://{os.environ.get('DATA_BUCKET', 'oa-gcs-usage-dvx')}/snapshots"
    dates = _snapshot_dates(root)
    if not dates:
        raise SystemExit(f"no snapshots under {root}")

    def walk(node: dict, segs: tuple[str, ...], depth: int, flat: dict[str, int]) -> None:
        for c in node.get("c", ()):  # children
            n = c["n"]
            if n.startswith("("):  # synthetic "(other …)" fold, not a real prefix
                continue
            key = "/".join((*segs, n))
            flat[key] = c["b"]
            if depth + 1 < max_depth:
                walk(c, (*segs, n), depth + 1, flat)

    keep_rows: list[dict] | None = None
    if actions_path:
        import fsspec

        with fsspec.open(actions_path, "rt") as f:
            keep_rows = json.load(f)["keeps"]
        err(f"ledger: {len(keep_rows):,} keep rows from {actions_path}")

    import time as _time
    from datetime import datetime, timezone

    now_ts = int(_time.time())
    per_date: dict[str, dict[str, int]] = {}
    fate_by_date: dict[str, dict[str, int]] = {}
    peak = 0
    for d in dates:
        tree = _load_tree(root, d)
        peak = max(peak, tree.get("b", 0))
        flat: dict[str, int] = {}
        walk(tree, (), 0, flat)
        per_date[d] = flat
        note = ""
        if keep_rows is not None:
            # Marks made *during* scan day D count as of D (end-of-day UTC,
            # capped at now for the latest scan).
            y, mo, dd = map(int, d.split("-"))
            cutoff = min(now_ts, int(datetime(y, mo, dd, tzinfo=timezone.utc).timestamp()) + 86400)
            fate_by_date[d] = _fate_totals(tree, keep_rows, cutoff)
            f = fate_by_date[d]
            note = f" · keep {f['keep'] / 1e12:.0f} / sweep {f['sweep'] / 1e12:.0f} / undecided {f['undecided'] / 1e12:.0f} TB"
        err(f"  {d}: {len(flat):>6,} prefixes, {tree.get('b', 0) / 1e12:>6.0f} TB{note}")

    floor = peak * min_frac
    keep = sorted({
        p for flat in per_date.values() for p, b in flat.items()
        if b >= floor or p.count("/") < full_depth
    })
    payload: dict = {
        "dates": dates,
        "prefixes": keep,
        "bytes": {p: [per_date[d].get(p) for d in dates] for p in keep},
    }
    if fate_by_date:
        payload["fate"] = {
            k: [fate_by_date[d][k] for d in dates] for k in ("keep", "sweep", "undecided")
        }
    text = json.dumps(payload, separators=(",", ":")) + "\n"
    err(f"{len(keep)} prefixes ≥ {min_frac:.2%} of {peak / 1e12:.0f} TB across {len(dates)} scans ({len(text):,} bytes)")
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        err(f"wrote {out}")
    else:
        print(text, end="")



@main.command()
@option("-a", "--actions", "actions_path", required=True, help="Actions-ledger export (the /api/actions JSON; path or URL)")
@option("-o", "--out", type=Path, default=None, help="Write CSV here (default: stdout)")
@option("-r", "--root", help="snapshots root (default $DATA_BUCKET)")
@option("-s", "--sort", "sort", type=Choice(["undecided", "attributed", "user"]), default="undecided", help="row order: undecided desc (nag order, default), attributed desc (size, stable within a day), or user A-Z (stable identity)")
@option("-u", "--site-url", default="https://gcs.oa.dev", help="Site base for per-user page links")
def report(actions_path: str, out: Path | None, root: str | None, sort: str, site_url: str) -> None:
    """Per-user mark-status CSV — the "who still needs to mark & sweep" list.

    Mirrors the site's /users page: scan attribution + live claims applied as
    a WAL, keep_last_ckpt decomposed, one row per user, with the ownerless pools
    at the bottom. Default order is undecided-bytes desc (nag order); `-s` picks
    a more diff-stable order for a synced mirror (a username tiebreaker keeps
    equal-value rows from swapping seats regardless).
    """
    import csv
    import io
    import json
    import sys

    import fsspec

    from .identity import load_identities

    root = root or f"gs://{os.environ.get('DATA_BUCKET', 'oa-gcs-usage-dvx')}/snapshots"
    dates = _snapshot_dates(root)
    if not dates:
        raise SystemExit(f"no snapshots under {root}")
    date = dates[-1]
    tree = _load_tree(root, date)
    meta = _load_meta(root, date)
    with fsspec.open(actions_path, "rt") as f:
        ledger = json.load(f)
    idmap = load_identities()

    def canon(who: str) -> str:
        return idmap.resolve(who)

    def norm(u: str) -> str:
        return u if u.endswith("/") else u + "/"

    def latest(rows: list[dict]) -> dict[str, dict]:
        live: dict[str, dict] = {}
        for r in rows:
            cur = live.get(r["prefix"])
            if cur is None or (r["ts"], r["action_id"]) > (cur["ts"], cur["action_id"]):
                live[r["prefix"]] = r
        return live

    keeps = latest(ledger["keeps"])
    owners = latest(ledger["owners"])

    anc: set[str] = set()
    for r in keeps.values():
        if r["keep"] is not None:
            segs = r["prefix"].rstrip("/").split("/")
            for i in range(3, len(segs)):
                anc.add("/".join(segs[:i]) + "/")
    for r in owners.values():
        if r["owner"] is not None:
            segs = r["prefix"].rstrip("/").split("/")
            for i in range(3, len(segs)):
                anc.add("/".join(segs[:i]) + "/")
    own_keep = {norm(r["prefix"]): r for r in keeps.values()}
    own_owner = {norm(r["prefix"]): r for r in owners.values()}

    def newer(a: dict, b: dict) -> bool:
        return (a["ts"], a["action_id"]) > (b["ts"], b["action_id"])

    # user -> fate -> bytes; claims override scan `us` shares wholesale.
    per_user: dict[str, dict[str, float]] = {}

    def add(u: str, f: str, b: float) -> None:
        per_user.setdefault(u, {"keep": 0.0, "sweep": 0.0, "undecided": 0.0})[f] += b

    def fate_of(win: dict | None) -> str:
        k = win["keep"] if win else None
        if k is None:
            return "undecided"
        # keep_last_ckpt folds to keep here (its sweep share is small and the
        # site strip does the exact split; a nag list doesn't need it).
        return "sweep" if k == "sweep" else "keep"

    def walk(n: dict, uri: str, inh_k: dict | None, inh_o: dict | None) -> None:
        u = norm(uri)
        ok = own_keep.get(u)
        win_k = ok if ok and (inh_k is None or newer(ok, inh_k)) else inh_k
        oo = own_owner.get(u)
        win_o = oo if oo and (inh_o is None or newer(oo, inh_o)) else inh_o
        claimant = canon(win_o["owner"]) if win_o and win_o["owner"] is not None else None
        if u not in anc:
            f = fate_of(win_k)
            if claimant:
                add(claimant, f, n["b"])
            else:
                for usr, b in n.get("us") or ():
                    if b > 0:
                        add(canon(usr), f, b)
            return
        rest_b = n["b"]
        rest = {usr: b for usr, b in n.get("us") or ()}
        for c in n.get("c", ()):
            if c["n"].startswith("("):
                continue
            rest_b -= c["b"]
            walk(c, f"{uri}/{c['n']}", win_k, win_o)
            for usr, b in c.get("us") or ():
                rest[usr] = rest.get(usr, 0) - b
        f = fate_of(win_k)
        if claimant:
            if rest_b > 0:
                add(claimant, f, rest_b)
        else:
            for usr, b in rest.items():
                if b > 0:
                    add(canon(usr), f, b)

    for bkt in tree.get("c", ()):
        walk(bkt, f"gs://{bkt['n']}", None, None)

    authored: dict[str, int] = {}
    for r in keeps.values():
        if r["keep"] is not None:
            authored[canon(r["who"])] = authored.get(canon(r["who"]), 0) + 1

    user_meta = {u["u"]: u for u in meta.get("users", ())}
    mixes = meta.get("user_class_bytes", {})
    price = {"1": 0.02, "2": 0.01, "3": 0.004, "4": 0.0012}

    def usd_mo(uid: str, b: float) -> float:
        mix = mixes.get(uid)
        if not mix:
            return b / 1024**3 * 0.02
        tot = sum(mix.values()) or 1
        rate = sum(price.get(c, 0.02) * v for c, v in mix.items()) / tot
        return b / 1024**3 * rate

    tib = 1024**4
    rows_out = []
    for uid, f in per_user.items():
        total = f["keep"] + f["sweep"] + f["undecided"]
        if total < 1e9:
            continue
        team = idmap.team_of(uid) or user_meta.get(uid, {}).get("t", "unknown")
        rows_out.append({
            "user": uid,
            "group": team,
            "attributed_TiB": round(total / tib, 1),
            "est_usd_mo": round(usd_mo(uid, total)),
            "keep_TiB": round(f["keep"] / tib, 1),
            "sweep_TiB": round(f["sweep"] / tib, 1),
            "undecided_TiB": round(f["undecided"] / tib, 1),
            "undecided_pct": round(100 * f["undecided"] / total) if total else 0,
            "marks_made": authored.get(uid, 0),
            "page": f"{site_url}/user/{uid}",
        })
    sort_key = {
        "undecided": lambda r: (-r["undecided_TiB"], r["user"]),
        "attributed": lambda r: (-r["attributed_TiB"], r["user"]),
        "user": lambda r: r["user"],
    }[sort]
    rows_out.sort(key=sort_key)

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows_out[0].keys()))
    w.writeheader()
    w.writerows(rows_out)
    text = buf.getvalue()
    err(f"{len(rows_out)} users · scan {date} · {sum(r['undecided_TiB'] for r in rows_out):,.0f} TiB undecided across users")
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        err(f"wrote {out}")
    else:
        sys.stdout.write(text)


@main.command("sheet-push")
@option("-D", "--disclaimer", help="static footer text 2 rows below the table; a '; last change <ts>' stamp is appended that only advances when data changes")
@option("-I", "--impersonate", help="service-account email to impersonate for Sheets auth (needs Token Creator); default is ambient ADC")
@option("-n", "--dry-run", is_flag=True, help="parse + summarize, don't touch the sheet")
@option("-w", "--worksheet", default="", help="tab to replace, by title (default: the first tab)")
@argument("sheet_id")
@argument("csv_path", default="-")
def sheet_push(disclaimer: str | None, impersonate: str | None, dry_run: bool, worksheet: str, sheet_id: str, csv_path: str) -> None:
    """Push a mark-status CSV (from `report`) to a Google Sheet.

    Syncs ONE named tab in place (the site's `/users` mirror). Target it by
    `-w <title>` — the sheet may hold other, human-authored tabs (derived
    views), so never blindly overwrite the first. Writes only the cells whose
    value actually changed (diffing the tab's current contents), so Google's
    version history highlights just the real deltas instead of the whole range
    — and formatting / frozen rows survive untouched. `-D` writes an
    "auto-synced" footer two rows below the table (with a "last change"
    stamp that only advances when data actually moves, so no-op runs write
    nothing). Idempotent.

    Auth is Application Default Credentials: the job's GCP service account in
    Cloud Run / Batch, or your `gcloud auth application-default` locally. The
    sheet must be shared (Editor) with that identity, and the Sheets API enabled
    in the project. See specs/gsheet-mark-status-sync.md.

    Pipe straight from `report`:  gcs-usage report -a … | gcs-usage sheet-push <id>
    """
    import csv
    import io as _io

    text = sys.stdin.read() if csv_path == "-" else Path(csv_path).read_text()
    rows = [r for r in csv.reader(_io.StringIO(text)) if r]
    if len(rows) < 2:
        raise SystemExit(f"expected a header + ≥1 data row, got {len(rows)}")
    err(f"{len(rows) - 1} rows → sheet {sheet_id} tab '{worksheet or '(first)'}'")
    if dry_run:
        err("dry-run — not writing")
        return

    import google.auth  # noqa: PLC0415 — optional (sheets extra), imported on use
    import gspread  # noqa: PLC0415

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if impersonate:
        from google.auth import impersonated_credentials  # noqa: PLC0415
        source, _ = google.auth.default()
        creds = impersonated_credentials.Credentials(
            source_credentials=source, target_principal=impersonate, target_scopes=scopes,
        )
    else:
        creds, _ = google.auth.default(scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(worksheet) if worksheet else sh.get_worksheet(0)

    # Cell-level diff against what's already there, so version history shows the
    # real deltas (not a full-range rewrite) and we never clear()/re-write
    # unchanged cells. Compare numerically where possible: RAW-writing "0.0"
    # makes Sheets store 0 (displayed "0"), so a string compare would flag every
    # "0.0" cell as changed on every run.
    existing = ws.get_all_values()

    def at(grid: list[list[str]], r: int, c: int) -> str:
        return grid[r][c] if r < len(grid) and c < len(grid[r]) else ""

    def norm(v: str) -> tuple[str, object]:
        v = (v or "").strip()
        try:
            return ("n", float(v))
        except ValueError:
            return ("s", v)

    # Did any DATA cell (the header+data block) change? Compared in isolation so
    # the footer's own timestamp never counts as a data change.
    data_cols = max((len(r) for r in rows), default=0)
    data_changed = any(
        norm(at(rows, r, c)) != norm(at(existing, r, c))
        for r in range(len(rows))
        for c in range(data_cols)
    )

    # Target grid: header + data at A1, then a blank separator row and the
    # optional footer at row N+2 (col A). The footer's "last change" stamp only
    # advances when data actually moved (parsed back from the prior footer
    # otherwise) — so a no-op hourly run rewrites nothing: no cell churn, no new
    # version. First run seeds it (old footer has no parseable stamp).
    target: list[list[str]] = [list(r) for r in rows]
    if disclaimer:
        import datetime  # noqa: PLC0415
        import re  # noqa: PLC0415
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        pat = re.compile(r"last change (\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC)")
        prior = next((m.group(1) for row in existing for cell in row if (m := pat.search(cell or ""))), None)
        stamp = now if (data_changed or prior is None) else prior
        target.append([])
        target.append([f"{disclaimer}; last change {stamp}"])

    n_rows = max(len(target), len(existing))
    n_cols = max((len(r) for r in (*target, *existing)), default=0)
    changed = [
        gspread.Cell(r + 1, c + 1, at(target, r, c))
        for r in range(n_rows)
        for c in range(n_cols)
        if norm(at(target, r, c)) != norm(at(existing, r, c))
    ]
    if changed:
        ws.update_cells(changed, value_input_option="RAW")
    verb = "changed" if data_changed else "unchanged"
    err(f"synced '{ws.title}': {len(changed)} cell(s) written ({len(rows) - 1} data rows, data {verb})")


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
    webhook: str | None,
) -> None:
    """Post a daily GCS-usage digest to Slack: a one-line headline (date · total
    · Δ, in the per-message sender name) + the $/mo run-rate linked to the site
    (with Δ$); flag threshold breaches (absolute ceiling and/or relative spike).

    Per-message avatars (mark + 📊/🚨) require the Web API: pass a bot token
    (needs the chat:write.customize scope) + channel. Incoming webhooks ignore
    icon/name overrides, so the --webhook path folds the headline into the body
    and posts with the app's static icon."""
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

    # est. $/mo from the class-byte mix (US list prices; mirror site CLASS_PRICE_US).
    CLASS_PRICE = {"1": 0.02, "2": 0.01, "3": 0.004, "4": 0.0012}  # $/GiB·mo
    cost = lambda cb: sum((cb.get(c, 0) / 1024**3) * p for c, p in CLASS_PRICE.items())
    cur_cost = cost(cur["class_bytes"])

    d_bytes = d_pct = d_cost = 0.0
    if prior:
        pri = _load_meta(root, prior)
        d_bytes = cur["total_bytes"] - pri["total_bytes"]
        d_pct = 100 * d_bytes / pri["total_bytes"] if pri["total_bytes"] else 0.0
        d_cost = cur_cost - cost(pri["class_bytes"])
        if abs(d_pct) > spike_pct:
            breach.append(f"Δ {d_pct:+.1f}% vs {prior} exceeds ±{spike_pct:.0f}%")

    # Resolve the Slack transport. A chat.postMessage carries the headline
    # (date · total · Δ) in its per-message username — the bold name Slack
    # renders beside the avatar — leaving a one-line body: the $/mo run-rate
    # linked to the site, plus the Δ$. Webhooks can't set a username, so that
    # path folds the headline into the body as a bold first line instead.
    webhook = webhook or os.environ.get("SLACK_WEBHOOK")
    bot_token = bot_token or os.environ.get("SLACK_BOT_TOKEN")
    channel = channel or os.environ.get("SLACK_CHANNEL")
    use_api = bool(bot_token and channel)  # chat.postMessage → per-message avatar + username

    # Per-message avatar (mark + 📊/🚨 badge), served public so Slack can fetch
    # it (the app itself is Access-gated). See job/gen-slack-icons.py + the
    # gcs-usage-icons Pages project.
    icon_url = f"https://gcs-usage-icons.pages.dev/gcs-{'breach' if breach else 'digest'}.png"
    md = lambda s: f"{int(s[5:7])}/{int(s[8:10])}"  # 2026-08-06 → 8/6

    def pct(p: float) -> str:
        s = f"{abs(p):.1f}"  # 0.6 → ".6", 12.3 → "12.3" (sign carried by the ΔTB)
        return s[1:] if s.startswith("0") else s

    headline = f"{md(date)} — {tb:,.0f} TB"
    yymmdd = date[2:].replace("-", "")  # 2026-08-09 → 260809 (site's ?d= deep-link)
    cost_line = f"<https://gcs.oa.dev/?d={yymmdd}|${cur_cost:,.0f}/mo>"
    if prior:
        headline += f" ({d_bytes / 1e12:+.1f}, {pct(d_pct)}%)"
        d_cost_s = f"{'-' if d_cost < 0 else '+'}${abs(d_cost):,.0f}"  # sign before $
        cost_line += f" ({d_cost_s}/mo)"
    username = headline
    lines = [] if use_api else [f"*{headline}*"]  # webhook has no username → headline in body
    lines.append(cost_line)
    if breach:
        lines.append(":rotating_light: " + "; ".join(breach))
    text = "\n".join(lines)

    if edit_ts and not use_api:
        raise SystemExit("--edit-ts needs a bot token + channel (chat.update)")
    if dry_run or not (use_api or webhook):
        if not (use_api or webhook) and not dry_run:
            err("no bot-token+channel / --webhook set — printing (dry-run)")
        if use_api and not edit_ts:  # headline rides in the sender name, not the body
            err(f"[sender: {username}]")
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
            payload["username"] = username
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


@main.command("index-sync")
@option("-b", "--bucket", default="oa-gcs-usage-dvx", help="Data bucket holding listing/<date>/path-index*.parquet")
@option("-d", "--dir", "listing_dir", default=None, help="Override the listing dir holding the parquets (default: <bucket>/listing/<date>)")
@option("-L", "--local", is_flag=True, help="Write to the local wrangler D1 instead of --remote")
@option("-v", "--variant", "variants", multiple=True, type=Choice(["path", "user", "team"]), help="Only sync these variants (default: all three)")
@argument("date")
def index_sync(bucket: str, listing_dir: str | None, local: bool, variants: tuple[str, ...], date: str) -> None:
    """Sync a scan's path-index parquet footers into D1 (index_schema/index_groups)
    so the site's reader skips the cold-isolate footer parse. Syncs all three
    sort variants (path / user / team) — the drill index plus the by-user/by-team
    lens indexes (specs/path-agnostic-serving.md §2.1/§2.3). Needs
    CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID in the env."""
    from .index_footer import sync_d1

    base = listing_dir or f"{bucket}/listing/{date}"
    files = {"path": f"{base}/path-index.parquet", "user": f"{base}/path-index-by-user.parquet", "team": f"{base}/path-index-by-team.parquet"}
    for variant in (variants or ("path", "user", "team")):
        n = sync_d1(date, files[variant], variant=variant, remote=not local)
        err(f"index-sync: {date} [{variant}] — schema + {n} row groups ({'local' if local else 'remote'})")


@main.command()
@option("-c", "--channel", help="Slack channel id (default $SLACK_CHANNEL)")
@option("-D", "--reply-delay", "reply_delay", default=0.0, type=float, help="Seconds to sleep between replies (e.g. 305 for a spaced backfill so Slack keeps per-reply sender chrome)")
@option("-m", "--month", help="Month YYYY-MM (default: current UTC month)")
@option("-n", "--dry-run", is_flag=True, help="Render the plot + print OP/replies; post & host nothing")
@option("-r", "--root", help="Snapshots root (default gs://$DATA_BUCKET/snapshots)")
@option("-t", "--token", help="Slack bot token (default $SLACK_BOT_TOKEN)")
@option("-u", "--url", "site_url", default=None, help="Site base for links (default gcs.oa.dev)")
def digest(channel: str | None, reply_delay: float, month: str | None, dry_run: bool, root: str | None, token: str | None, site_url: str | None) -> None:
    """Converge the Shape-C monthly digest thread in Slack: an OP (month-to-date
    headline, per-week bullets, mosaic plot) edited in place + one reply per scan
    (headline sender, class breakdown body, colour-coded arrow avatar). State
    lives in gs://<bucket>/digest/<YYYY-MM>.json. See specs/done/slack-digest-shape-c.md."""
    from pathlib import Path

    from . import digest as dg

    site_url = site_url or dg.DEFAULT_URL
    m = (
        dt.datetime.strptime(month, "%Y-%m").date()
        if month
        else dt.datetime.now(dt.timezone.utc).date().replace(day=1)
    )
    root = root or f"gs://{os.environ.get('DATA_BUCKET', 'oa-gcs-usage-dvx')}/snapshots"

    if dry_run:
        rows = dg.load_month(root, m)
        if not rows:
            raise SystemExit(f"digest: no scans for {m:%Y-%m}")
        import tempfile

        out = Path(tempfile.gettempdir()) / f"digest-{m:%Y%m}.png"
        dg.render_plot(rows, m, out)
        err(f"rendered plot → {out}")
        print(dg.op_body(rows, m, "<plot-url>", site_url))
        print("\n--- replies (sender | body | avatar) ---")
        for r in rows:
            s, b, a = dg.reply(r, site_url)
            print(f"{s} | {b} | {a.split('/')[-1]}")
        return

    channel = channel or os.environ.get("SLACK_CHANNEL")
    token = token or os.environ.get("SLACK_BOT_TOKEN")
    if not (channel and token):
        raise SystemExit("digest: need SLACK_BOT_TOKEN + SLACK_CHANNEL (or -t/-c)")
    # Resolve job/icons in both layouts: pip-installed in the image (cwd=/app,
    # icons at /app/job/icons) or the repo checkout (…/parents[3]/job/icons).
    icons = next(
        (c for c in (Path.cwd() / "job" / "icons", Path(__file__).resolve().parents[3] / "job" / "icons") if c.exists()),
        Path(__file__).resolve().parents[3] / "job" / "icons",
    )

    def deploy(local: Path, name: str) -> str | None:
        # publish the icons dir (incl. the freshly-rendered plot) to the Pages
        # project; needs CLOUDFLARE_* + node/wrangler. Return the deployment-
        # specific URL (served instantly), which the OP image uses to avoid
        # racing root-alias CDN propagation (→ Slack `invalid_blocks`).
        import re
        import subprocess

        r = subprocess.run(
            ["npx", "wrangler", "pages", "deploy", str(icons), "--project-name", "gcs-usage-icons", "--branch", "main", "--commit-dirty=true"],
            check=True, capture_output=True, text=True,
        )
        err(r.stdout)
        m = re.search(r"https://[a-z0-9]+\.gcs-usage-icons\.pages\.dev", r.stdout + r.stderr)
        return m.group(0) if m else None

    dg.post_digest(root, m, token, channel, site_url=site_url, icons_dir=icons, deploy_plot=deploy, reply_delay=reply_delay)
    err(f"digest: converged {m:%Y-%m}")


if __name__ == "__main__":
    main()
